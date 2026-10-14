"""Shared asynchronous HTTP transport with bounded retries and response size."""

from __future__ import annotations

import asyncio
import random
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx

from ..constants import (
    REDIRECT_STATUS_CODES,
    RETRYABLE_STATUS_CODES,
    looks_like_waf_challenge,
)
from ..models import FetchErrorInfo
from ..utils.urls import validate_url


# Header names that may carry credentials or other material that must never
# leak across origins. Lookups are case-insensitive (``Authorization`` /
# ``authorization`` are equivalent) so we normalize once at comparison time.
_CREDENTIAL_HEADERS: frozenset[str] = frozenset(
    {"authorization", "cookie", "proxy-authorization"}
)


def _strip_credentials(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of *headers* with credential-bearing entries removed."""
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _CREDENTIAL_HEADERS
    }


def _is_cross_origin(current_url: str, next_url: str) -> bool:
    """Return True if *current_url* and *next_url* differ in scheme, host, or port."""
    cur = urlsplit(current_url)
    nxt = urlsplit(next_url)
    if cur.scheme.lower() != nxt.scheme.lower():
        return True
    cur_host = (cur.hostname or "").lower().rstrip(".")
    nxt_host = (nxt.hostname or "").lower().rstrip(".")
    if not cur_host or not nxt_host:
        # Missing host means a malformed redirect; treat as cross-origin so
        # we drop credentials rather than risk leaking them.
        return True
    if cur_host != nxt_host:
        return True
    cur_port = cur.port or (443 if cur.scheme.lower() == "https" else 80)
    nxt_port = nxt.port or (443 if nxt.scheme.lower() == "https" else 80)
    return cur_port != nxt_port


@dataclass(slots=True)
class HTTPResponse:
    url: str
    status_code: int
    headers: httpx.Headers
    content: bytes
    encoding: str | None


class TransportFailure(Exception):
    def __init__(self, error: FetchErrorInfo, *, status_code: int | None = None) -> None:
        self.error = error
        self.status_code = status_code
        super().__init__(error.message)


class _SharedClientTransport(httpx.AsyncBaseTransport):
    """Transport adapter delegating to a client's pooled transport while isolating cookies.

    httpx.AsyncClient accumulates cookies in `client.cookies` across all requests.
    Wrapping the client's underlying transport in an ephemeral per-fetch `AsyncClient`
    gives each fetch operation an isolated cookie jar (so Set-Cookie headers survive
    redirect hops within the operation) without leaking cookies across distinct fetches
    or concurrent requests, and without closing the shared connection pool on exit.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._client._transport_for_url(request.url)
        return await transport.handle_async_request(request)

    async def aclose(self) -> None:
        pass


class HTTPFetcher:
    """Fetch through one or more shared ``httpx.AsyncClient`` instances."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        *,
        retries: int,
        max_content_size: int,
        max_redirects: int = 10,
        proxy_client_provider: Callable[[str], Awaitable[httpx.AsyncClient]] | None = None,
        proxy_client_releaser: Callable[[httpx.AsyncClient], None] | None = None,
    ) -> None:
        self.client = client
        self.semaphore = semaphore
        self.retries = retries
        self.max_content_size = max_content_size
        self.max_redirects = max_redirects
        self.proxy_client_provider = proxy_client_provider
        self.proxy_client_releaser = proxy_client_releaser

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        proxy_url: str | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        content: bytes | str | None = None,
        data: dict[str, Any] | None = None,
        json_data: Any = None,
        retryable_status_codes: frozenset[int] | None = None,
    ) -> HTTPResponse:
        fallback_client: httpx.AsyncClient | None = None
        base_client: httpx.AsyncClient | None = None
        try:
            if proxy_url is not None:
                if self.proxy_client_provider is not None:
                    base_client = await self.proxy_client_provider(proxy_url)
                else:
                    req_headers = headers if headers is not None else self.client.headers
                    fallback_client = httpx.AsyncClient(
                        headers=req_headers,
                        timeout=self.client.timeout,
                        follow_redirects=False,
                        max_redirects=self.max_redirects,
                        http2=True,
                        proxy=proxy_url,
                        limits=httpx.Limits(
                            max_connections=self.retries + 1,
                            max_keepalive_connections=1,
                        ),
                    )
                    base_client = fallback_client
            else:
                base_client = self.client

            # Do not inject client-level headers into op_client so that _strip_credentials
            # during cross-origin redirects is not subverted by httpx client-level header merging.
            op_client = httpx.AsyncClient(
                transport=_SharedClientTransport(base_client),
                timeout=base_client.timeout,
                follow_redirects=False,
                max_redirects=self.max_redirects,
            )
            try:
                return await self._fetch_with_retries(
                    url,
                    method=method,
                    client=op_client,
                    headers=headers,
                    cookies=cookies,
                    content=content,
                    data=data,
                    json_data=json_data,
                    retryable_status_codes=retryable_status_codes,
                )
            finally:
                await op_client.aclose()
        finally:
            if proxy_url is not None and base_client is not None:
                if fallback_client is not None:
                    await fallback_client.aclose()
                elif self.proxy_client_releaser is not None:
                    self.proxy_client_releaser(base_client)

    async def _fetch_with_retries(
        self,
        url: str,
        *,
        method: str = "GET",
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        content: bytes | str | None = None,
        data: dict[str, Any] | None = None,
        json_data: Any = None,
        retryable_status_codes: frozenset[int] | None = None,
    ) -> HTTPResponse:
        retry_codes = retryable_status_codes if retryable_status_codes is not None else RETRYABLE_STATUS_CODES
        for attempt in range(self.retries + 1):
            try:
                async with self.semaphore:
                    response = await self._request(
                        url,
                        method=method,
                        client=client,
                        headers=headers,
                        cookies=cookies,
                        content=content,
                        data=data,
                        json_data=json_data,
                    )
                is_idempotent = method.upper() in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
                if response.status_code in retry_codes and attempt < self.retries:
                    # Cloudflare Under Attack / WAF interstitial: backoff will not
                    # resolve a JS bot gate, fast-track to allow caller escalation.
                    if response.status_code == 503 and looks_like_waf_challenge(response.content):
                        return response
                    if not is_idempotent and response.status_code != 429:
                        return response
                    await self._backoff(attempt, response.headers.get("Retry-After"))
                    continue
                return response
            except TransportFailure as exc:
                is_idempotent = method.upper() in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
                if not exc.error.retryable or attempt >= self.retries or not is_idempotent:
                    raise
                await self._backoff(attempt)
        # Unreachable: every loop iteration either ``continue``-s, ``return``-s,
        # or ``raise``-s. The exhaustive-iteration reasoning guarantees we never
        # fall through here; linters may flag this and that is fine.
        raise RuntimeError("unreachable: HTTP retry loop exited without terminating")

    async def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        content: bytes | str | None = None,
        data: dict[str, Any] | None = None,
        json_data: Any = None,
    ) -> HTTPResponse:
        client = client or self.client
        current_url = url
        current_method = method.upper()
        current_content = content
        current_data = data
        current_json = json_data
        current_cookies: dict[str, str] | None = dict(cookies) if cookies else None
        # Operate on a working copy so the caller's dict is never mutated.
        # Initialize from headers or self.client.headers so all required request headers
        # are present while allowing _strip_credentials to cleanly remove credentials
        # on cross-origin redirects without interference from client-level headers.
        current_headers: dict[str, str] = dict(
            headers if headers is not None else self.client.headers
        )
        redirect_count = 0
        while True:
            try:
                async with client.stream(
                    current_method,
                    current_url,
                    headers=current_headers,
                    cookies=current_cookies,
                    content=current_content,
                    data=current_data,
                    json=current_json,
                ) as response:
                    # Pre-flight redirect handling. ``follow_redirects`` is
                    # forced off on every httpx client (see ``HTTPFetcher.fetch``
                    # and ``PageFetch._http_fetcher``) so the SSRF guard has
                    # the final say before a single byte is sent to the next
                    # hop.  Without this loop a 302 to ``http://127.0.0.1`` or
                    # ``http://169.254.169.254`` would race the connection
                    # open and render any post-response host check useless.
                    if response.status_code in REDIRECT_STATUS_CODES:
                        if redirect_count >= self.max_redirects:
                            raise TransportFailure(
                                FetchErrorInfo(
                                    "too_many_redirects",
                                    "too many HTTP redirects",
                                    False,
                                ),
                                status_code=response.status_code,
                            )
                        location = response.headers.get("Location")
                        if not location:
                            raise TransportFailure(
                                FetchErrorInfo(
                                    "too_many_redirects",
                                    "redirect response missing Location header",
                                    False,
                                ),
                                status_code=response.status_code,
                            )
                        # Release the response before issuing the next hop so
                        # the underlying connection returns to the keep-alive
                        # pool instead of being held open across redirects.
                        await response.aclose()
                        next_url = urljoin(current_url, location)
                        try:
                            validate_url(next_url)
                        except ValueError as exc:
                            raise TransportFailure(
                                FetchErrorInfo(
                                    "ssrf_blocked",
                                    f"redirect points at a restricted network address: {exc}",
                                    False,
                                ),
                                status_code=response.status_code,
                            ) from exc
                        # RFC 9110 § 15.4: 303 always converts to GET.
                        # 301/302 from POST also conventionally convert to GET.
                        if response.status_code == 303 or (
                            response.status_code in {301, 302} and current_method == "POST"
                        ):
                            current_method = "GET"
                            current_content = None
                            current_data = None
                            current_json = None
                            if current_headers is not None:
                                current_headers = {
                                    k: v
                                    for k, v in current_headers.items()
                                    if k.lower() not in {"content-type", "content-length"}
                                }
                        # Strip credential-bearing headers and cookies when the redirect
                        # crosses an origin boundary — otherwise an attacker
                        # who controls the redirect target can harvest the
                        # caller's ``Authorization``/``Cookie``/proxy auth
                        # in plaintext. Same-origin redirects keep the full
                        # header set so internal app routing still works.
                        if _is_cross_origin(current_url, next_url):
                            if current_headers is not None:
                                current_headers = _strip_credentials(current_headers)
                            current_cookies = None
                        current_url = next_url
                        redirect_count += 1
                        continue
                    declared = response.headers.get("Content-Length")
                    if declared and declared.isdigit() and int(declared) > self.max_content_size:
                        raise TransportFailure(
                            FetchErrorInfo("content_too_large", "response exceeds maximum content size", False),
                            status_code=response.status_code,
                        )
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_content_size:
                            raise TransportFailure(
                                FetchErrorInfo("content_too_large", "response exceeds maximum content size", False),
                                status_code=response.status_code,
                            )
                        chunks.append(chunk)
                    return HTTPResponse(
                        url=str(response.url),
                        status_code=response.status_code,
                        headers=response.headers,
                        content=b"".join(chunks),
                        encoding=response.encoding,
                    )
            except TransportFailure:
                raise
            except httpx.TimeoutException as exc:
                raise TransportFailure(
                    FetchErrorInfo("http_timeout", "HTTP request timed out", True, type(exc).__name__)
                ) from exc
            except httpx.TooManyRedirects as exc:
                raise TransportFailure(
                    FetchErrorInfo("too_many_redirects", "too many HTTP redirects", False, type(exc).__name__)
                ) from exc
            except httpx.ConnectError as exc:
                message = str(exc).lower()
                cause: BaseException | None = exc
                dns_cause = False
                while cause is not None:
                    if isinstance(cause, socket.gaierror):
                        dns_cause = True
                        break
                    cause = cause.__cause__ or cause.__context__
                dns_words = ("dns", "getaddrinfo", "name resolution", "nodename nor servname")
                code = "dns_error" if dns_cause or any(word in message for word in dns_words) else "connection_error"
                raise TransportFailure(
                    FetchErrorInfo(code, "could not connect to the remote host", True, type(exc).__name__)
                ) from exc
            except httpx.HTTPError as exc:
                raise TransportFailure(
                    FetchErrorInfo("connection_error", "HTTP transport failed", True, type(exc).__name__)
                ) from exc

    @staticmethod
    async def _backoff(attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            seconds = HTTPFetcher._parse_retry_after(retry_after)
            if seconds is not None:
                delay = min(seconds, 10.0)
            else:
                delay = min(0.30 * (2**attempt) + random.uniform(0, 0.40), 5.0)
        else:
            delay = min(0.30 * (2**attempt) + random.uniform(0, 0.40), 5.0)
        await asyncio.sleep(delay)

    @staticmethod
    def _parse_retry_after(value: str) -> float | None:
        """Parse a Retry-After header as delta-seconds or HTTP-date."""
        if value.isdigit():
            return float(value)
        # Try HTTP-date (RFC 7231), e.g. "Wed, 21 Oct 2015 07:28:00 GMT"
        try:
            retry_dt = parsedate_to_datetime(value)
            now = datetime.now(UTC)
            delta = (retry_dt - now).total_seconds()
            return max(0.0, delta)
        except (ValueError, TypeError, OverflowError):
            return None
