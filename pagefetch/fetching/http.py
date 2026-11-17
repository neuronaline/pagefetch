"""Shared asynchronous HTTP transport with bounded retries and response size."""

from __future__ import annotations

import asyncio
import random
import socket
from collections.abc import Awaitable, Callable, Mapping
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
from ..cookies import CookieProfile
from ..models import FetchErrorInfo
from ..utils.urls import registrable_host, validate_url


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


class _BorrowedTransport(httpx.AsyncBaseTransport):
    """Non-closing wrapper letting a per-operation AsyncClient borrow a shared transport pool."""

    __slots__ = ("transport",)

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self.transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.transport.handle_async_request(request)

    async def aclose(self) -> None:
        pass


class HTTPFetcher:
    """Fetch through a pooled direct transport and optional proxy transport pool."""

    def __init__(
        self,
        direct_transport: httpx.AsyncBaseTransport,
        semaphore: asyncio.Semaphore,
        *,
        retries: int,
        max_content_size: int,
        max_redirects: int = 10,
        timeout: httpx.Timeout | float = 20.0,
        default_headers: Mapping[str, str] | None = None,
        proxy_transport_provider: Callable[[str], httpx.AsyncBaseTransport] | None = None,
        proxy_transport_releaser: Callable[[httpx.AsyncBaseTransport], None] | None = None,
    ) -> None:
        self.direct_transport = direct_transport
        self.semaphore = semaphore
        self.retries = retries
        self.max_content_size = max_content_size
        self.max_redirects = max_redirects
        self.timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        self.default_headers: dict[str, str] = dict(default_headers or {})
        self.proxy_transport_provider = proxy_transport_provider
        self.proxy_transport_releaser = proxy_transport_releaser

    def _acquire_transport(self, proxy_url: str | None) -> tuple[httpx.AsyncBaseTransport, bool]:
        """Return ``(transport, is_ephemeral)`` for direct or proxied requests."""
        if proxy_url is None:
            return self.direct_transport, False
        if self.proxy_transport_provider is not None:
            return self.proxy_transport_provider(proxy_url), False
        ephemeral = httpx.AsyncHTTPTransport(
            http2=True,
            proxy=proxy_url,
            limits=httpx.Limits(
                max_connections=self.retries + 1,
                max_keepalive_connections=1,
            ),
        )
        return ephemeral, True

    async def _release_transport(
        self, transport: httpx.AsyncBaseTransport | None, is_ephemeral: bool
    ) -> None:
        if transport is None or transport is self.direct_transport:
            return
        if is_ephemeral:
            await transport.aclose()
        elif self.proxy_transport_releaser is not None:
            self.proxy_transport_releaser(transport)

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        proxy_url: str | None = None,
        proxy_resolver: Callable[[int], str | None] | None = None,
        headers: dict[str, str] | None = None,
        cookie_profile: CookieProfile | None = None,
        cookie_rotator: Callable[[int], CookieProfile | None] | None = None,
        initial_site: str | None = None,
        content: bytes | str | None = None,
        data: dict[str, Any] | None = None,
        json_data: Any = None,
        retryable_status_codes: frozenset[int] | None = None,
    ) -> HTTPResponse:
        active_transport, is_ephemeral = self._acquire_transport(proxy_url)
        borrowed = _BorrowedTransport(active_transport)
        async with self.semaphore:
            try:
                async def _rotate_proxy_on_retry(next_attempt: int) -> None:
                    nonlocal proxy_url, active_transport, is_ephemeral
                    if proxy_resolver is None or proxy_url is None:
                        return
                    new_proxy_url = proxy_resolver(next_attempt)
                    if not new_proxy_url or new_proxy_url == proxy_url:
                        return
                    await self._release_transport(active_transport, is_ephemeral)
                    proxy_url = new_proxy_url
                    active_transport, is_ephemeral = self._acquire_transport(proxy_url)
                    borrowed.transport = active_transport

                # Dedicated lightweight client per operation isolates Set-Cookie redirect jars
                # without leaking across concurrent fetches or mutating default client headers.
                op_client = httpx.AsyncClient(
                    transport=borrowed,
                    timeout=self.timeout,
                    follow_redirects=False,
                    max_redirects=self.max_redirects,
                )
                try:
                    return await self._fetch_with_retries(
                        url,
                        method=method,
                        client=op_client,
                        headers=headers,
                        cookie_profile=cookie_profile,
                        cookie_rotator=cookie_rotator,
                        initial_site=initial_site,
                        content=content,
                        data=data,
                        json_data=json_data,
                        retryable_status_codes=retryable_status_codes,
                        has_proxy=bool(proxy_url),
                        on_retry=_rotate_proxy_on_retry if proxy_resolver is not None else None,
                    )
                finally:
                    await op_client.aclose()
            finally:
                await self._release_transport(active_transport, is_ephemeral)

    async def _fetch_with_retries(
        self,
        url: str,
        *,
        method: str = "GET",
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
        cookie_profile: CookieProfile | None = None,
        cookie_rotator: Callable[[int], CookieProfile | None] | None = None,
        initial_site: str | None = None,
        content: bytes | str | None = None,
        data: dict[str, Any] | None = None,
        json_data: Any = None,
        retryable_status_codes: frozenset[int] | None = None,
        has_proxy: bool = False,
        on_retry: Callable[[int], Awaitable[None]] | None = None,
    ) -> HTTPResponse:
        retry_codes = retryable_status_codes if retryable_status_codes is not None else RETRYABLE_STATUS_CODES
        active_profile = cookie_profile
        for attempt in range(self.retries + 1):
            try:
                response = await self._request(
                    url,
                    method=method,
                    client=client,
                    headers=headers,
                    cookie_profile=active_profile,
                    initial_site=initial_site,
                    content=content,
                    data=data,
                    json_data=json_data,
                    has_proxy=has_proxy,
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
                    if client is not None:
                        client.cookies.clear()
                    if on_retry is not None:
                        await on_retry(attempt + 1)
                    if cookie_rotator is not None and response.status_code in {403, 429, 503}:
                        rotated = cookie_rotator(attempt + 1)
                        if rotated is not None:
                            active_profile = rotated
                    continue
                return response
            except TransportFailure as exc:
                is_idempotent = method.upper() in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
                if not exc.error.retryable or attempt >= self.retries or not is_idempotent:
                    raise
                await self._backoff(attempt)
                if client is not None:
                    client.cookies.clear()
                if on_retry is not None:
                    await on_retry(attempt + 1)
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
        cookie_profile: CookieProfile | None = None,
        initial_site: str | None = None,
        content: bytes | str | None = None,
        data: dict[str, Any] | None = None,
        json_data: Any = None,
        has_proxy: bool = False,
    ) -> HTTPResponse:
        assert client is not None
        current_url = url
        current_method = method.upper()
        current_content = content
        current_data = data
        current_json = json_data
        effective_site = initial_site or registrable_host(url) or (urlsplit(url).hostname or "").lower()
        current_headers: dict[str, str] = dict(
            headers if headers is not None else self.default_headers
        )
        redirect_count = 0
        server_set_names: set[str] = set()
        while True:
            hop_cookies: dict[str, str] = {}
            if cookie_profile is not None:
                for k, v in cookie_profile.to_http_cookies(
                    current_url, initial_site=effective_site
                ).items():
                    if k not in server_set_names:
                        hop_cookies[k] = v
            try:
                async with client.stream(
                    current_method,
                    current_url,
                    headers=current_headers,
                    cookies=hop_cookies if hop_cookies else None,
                    content=current_content,
                    data=current_data,
                    json=current_json,
                ) as response:
                    # Pre-flight redirect handling. ``follow_redirects`` is
                    # forced off on every per-operation httpx client so the SSRF
                    # guard has the final say before a single byte is sent to the
                    # next hop. Without this loop a 302 to ``http://127.0.0.1`` or
                    # ``http://169.254.169.254`` would race the connection
                    # open and render any post-response host check useless.
                    if response.status_code in REDIRECT_STATUS_CODES:
                        for raw_sc in response.headers.get_list("set-cookie"):
                            head = raw_sc.split(";", 1)[0]
                            if "=" in head:
                                sc_name = head.split("=", 1)[0].strip()
                                if sc_name:
                                    server_set_names.add(sc_name)
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
                            # Skip local DNS check when proxy is active to prevent DNS leaks, but still block private literal IPs
                            validate_url(next_url, check_ssrf=True, resolve_dns=not has_proxy)
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
                        current_url = next_url
                        redirect_count += 1
                        continue
                    declared = response.headers.get("Content-Length")
                    if response.status_code < 400 and declared and declared.isdigit() and int(declared) > self.max_content_size:
                        raise TransportFailure(
                            FetchErrorInfo("content_too_large", "response exceeds maximum content size", False),
                            status_code=response.status_code,
                        )
                    ct_header = response.headers.get("Content-Type", "")
                    if response.status_code < 400:
                        from ..processing.pipeline import (
                            is_unsupported_binary_response,
                            parse_content_type,
                        )

                        if is_unsupported_binary_response(
                            ct_header,
                            str(response.url),
                            response.headers.get("Content-Disposition", ""),
                        ):
                            normalized_ct = parse_content_type(ct_header)
                            raise TransportFailure(
                                FetchErrorInfo(
                                    "unsupported_content_type",
                                    f"Content type {normalized_ct!r} is an unsupported binary format",
                                    False,
                                ),
                                status_code=response.status_code,
                            )
                    chunks: list[bytes] = []
                    size = 0
                    max_read_size = 4096 if response.status_code >= 400 else self.max_content_size
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_read_size:
                            if response.status_code >= 400:
                                remaining = max_read_size - (size - len(chunk))
                                if remaining > 0:
                                    chunks.append(chunk[:remaining])
                                break
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
            except httpx.ProxyError as exc:
                message = str(exc)
                msg_lower = message.lower()
                is_forbidden = any(token in msg_lower for token in ("403", "forbidden", "restricted", "denied"))
                is_auth_fail = "407" in msg_lower or "authentication" in msg_lower
                if is_forbidden:
                    code = "proxy_forbidden"
                    err_msg = f"Proxy rejected connection to target: {message}"
                    retryable = False
                elif is_auth_fail:
                    code = "proxy_error"
                    err_msg = f"Proxy authentication failed: {message}"
                    retryable = False
                else:
                    code = "proxy_error"
                    err_msg = f"Proxy transport failed: {message}"
                    retryable = True
                raise TransportFailure(
                    FetchErrorInfo(code, err_msg, retryable, type(exc).__name__)
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
