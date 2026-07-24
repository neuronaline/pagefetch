"""Shared asynchronous HTTP transport with bounded retries and response size."""

from __future__ import annotations

import asyncio
import random
import socket
from dataclasses import dataclass

import httpx

from ..constants import RETRYABLE_STATUS_CODES
from ..models import FetchErrorInfo


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
    """Fetch through one shared ``httpx.AsyncClient``."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        *,
        retries: int,
        max_content_size: int,
    ) -> None:
        self.client = client
        self.semaphore = semaphore
        self.retries = retries
        self.max_content_size = max_content_size

    async def fetch(self, url: str) -> HTTPResponse:
        async with self.semaphore:
            return await self._fetch_with_retries(url)

    async def _fetch_with_retries(self, url: str) -> HTTPResponse:
        last_failure: TransportFailure | None = None
        for attempt in range(self.retries + 1):
            try:
                response = await self._request(url)
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.retries:
                    await self._backoff(attempt, response.headers.get("Retry-After"))
                    continue
                return response
            except TransportFailure as exc:
                last_failure = exc
                if not exc.error.retryable or attempt >= self.retries:
                    raise
                await self._backoff(attempt)
        assert last_failure is not None
        raise last_failure

    async def _request(self, url: str) -> HTTPResponse:
        try:
            async with self.client.stream("GET", url) as response:
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
                delay = min(0.25 * (2**attempt) + random.uniform(0, 0.15), 3.0)
        else:
            delay = min(0.25 * (2**attempt) + random.uniform(0, 0.15), 3.0)
        await asyncio.sleep(delay)

    @staticmethod
    def _parse_retry_after(value: str) -> float | None:
        """Parse a Retry-After header as delta-seconds or HTTP-date."""
        if value.isdigit():
            return float(value)
        # Try HTTP-date (RFC 7231), e.g. "Wed, 21 Oct 2015 07:28:00 GMT"
        try:
            from email.utils import parsedate_to_datetime
            from datetime import UTC, datetime
            retry_dt = parsedate_to_datetime(value)
            now = datetime.now(UTC)
            delta = (retry_dt - now).total_seconds()
            return max(0.0, delta)
        except (ValueError, TypeError, OverflowError):
            return None
