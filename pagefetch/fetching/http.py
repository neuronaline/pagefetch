"""Shared asynchronous HTTP transport with bounded retries and response size."""

from __future__ import annotations

import asyncio
import random
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import httpx

from ..constants import REDIRECT_STATUS_CODES, RETRYABLE_STATUS_CODES
from ..models import FetchErrorInfo
from ..utils.urls import validate_url


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
    ) -> None:
        self.client = client
        self.semaphore = semaphore
        self.retries = retries
        self.max_content_size = max_content_size
        self.max_redirects = max_redirects
        self.proxy_client_provider = proxy_client_provider

    async def fetch(
        self,
        url: str,
        *,
        proxy_url: str | None = None,
        headers: dict[str, str] | None = None,
        retryable_status_codes: frozenset[int] | None = None,
    ) -> HTTPResponse:
        # Reuse a single pooled ``httpx.AsyncClient`` per proxy URL when a
        # provider callback is supplied. The caller owns the lifecycle
        # (``PageFetch._teardown`` closes every client handed out by the
        # provider). If no provider is available (e.g. standalone fetcher),
        # fall back to a dedicated per-request client so ``proxy_url`` is
        # never silently ignored.
        if proxy_url is not None:
            if self.proxy_client_provider is not None:
                client = await self.proxy_client_provider(proxy_url)
                return await self._fetch_with_retries(
                    url,
                    client=client,
                    headers=headers,
                    retryable_status_codes=retryable_status_codes,
                )
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
            try:
                return await self._fetch_with_retries(
                    url,
                    client=fallback_client,
                    headers=headers,
                    retryable_status_codes=retryable_status_codes,
                )
            finally:
                await fallback_client.aclose()
        return await self._fetch_with_retries(
            url, client=None, headers=headers, retryable_status_codes=retryable_status_codes
        )

    async def _fetch_with_retries(
        self,
        url: str,
        *,
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
        retryable_status_codes: frozenset[int] | None = None,
    ) -> HTTPResponse:
        retry_codes = retryable_status_codes if retryable_status_codes is not None else RETRYABLE_STATUS_CODES
        for attempt in range(self.retries + 1):
            try:
                async with self.semaphore:
                    response = await self._request(url, client=client, headers=headers)
                if response.status_code in retry_codes and attempt < self.retries:
                    await self._backoff(attempt, response.headers.get("Retry-After"))
                    continue
                return response
            except TransportFailure as exc:
                if not exc.error.retryable or attempt >= self.retries:
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
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
    ) -> HTTPResponse:
        client = client or self.client
        current_url = url
        redirect_count = 0
        while True:
            try:
                async with client.stream("GET", current_url, headers=headers) as response:
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
