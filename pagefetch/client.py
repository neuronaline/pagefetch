"""Main PageFetch client and fetch pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from bs4 import BeautifulSoup

from .bootstrap import ensure_runtime_requirements
from .cache import SQLiteCache, build_cache_key
from .config import VALID_MODES, VALID_PROXIES, PageFetchConfig
from .constants import (
    BLOCKED_STATUS_CODES,
    BROWSER_HEADERS,
    RETRYABLE_STATUS_CODES,
    SAFE_RESPONSE_HEADERS,
    XML_TYPES,
)
from .exceptions import PageFetchError
from .fetching import BrowserFetcher, HTTPFetcher, HTTPResponse, TransportFailure
from .models import FetchErrorInfo, FetchResult
from .processing.detector import analyze_html
from .processing.html import process_html
from .processing.non_html import process_pdf, process_text, process_xml
from .proxy import ProxyConfigurationError, resolve_proxy
from .utils.durations import parse_duration
from .utils.urls import normalize_url, validate_url

logger = logging.getLogger("pagefetch")


class PageFetch:
    """Asynchronous HTTP-first page fetcher with automatic Camoufox fallback.

    The client may be used as an async context manager. Calling :meth:`fetch`
    without an explicit context also starts resources lazily; call :meth:`close`
    when finished in that case.
    """

    def __init__(
        self,
        *,
        mode: Literal["auto", "http", "browser"] = "auto",
        proxy: Literal["none", "decodo", "dataimpulse"] = "none",
        http_concurrency: int = 10,
        browser_concurrency: int = 4,
        cache_enabled: bool = True,
        cache_ttl: str | int = "24h",
        cache_path: str | Path | None = None,
        http_timeout: float = 20.0,
        browser_timeout: float = 45.0,
        retries_http: int = 3,
        retries_browser: int = 2,
        max_redirects: int = 10,
        max_content_size: int = 25 * 1024 * 1024,
        confidence_threshold: float = 0.80,
        raise_on_error: bool = False,
    ) -> None:
        self.config = PageFetchConfig.build(
            mode=mode,
            proxy=proxy,
            http_concurrency=http_concurrency,
            browser_concurrency=browser_concurrency,
            cache_enabled=cache_enabled,
            cache_ttl=cache_ttl,
            cache_path=cache_path,
            http_timeout=http_timeout,
            browser_timeout=browser_timeout,
            retries_http=retries_http,
            retries_browser=retries_browser,
            max_redirects=max_redirects,
            max_content_size=max_content_size,
            confidence_threshold=confidence_threshold,
            raise_on_error=raise_on_error,
        )
        self._http_semaphore = asyncio.Semaphore(self.config.http_concurrency)
        self._browser_semaphore = asyncio.Semaphore(self.config.browser_concurrency)
        self._http_clients: dict[str, httpx.AsyncClient] = {}
        self._http_fetchers: dict[str, HTTPFetcher] = {}
        self._browser_fetchers: dict[str, BrowserFetcher] = {}
        self._http_init_lock = asyncio.Lock()
        self._browser_init_lock = asyncio.Lock()
        self._cache = SQLiteCache(self.config.cache_path) if self.config.cache_enabled else None
        self._started = False
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()
        self._startup_warnings: list[str] = []

    async def __aenter__(self) -> PageFetch:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.close()

    async def start(self) -> PageFetch:
        """Initialize the cache; network and browser transports remain lazy."""
        async with self._lifecycle_lock:
            if self._started and not self._closed:
                return self
            if self._closed:
                raise RuntimeError("PageFetch has already been closed")
            ensure_runtime_requirements()
            if self._cache:
                try:
                    await self._cache.start()
                except Exception as exc:
                    self._cache = None
                    self._startup_warnings.append("Cache initialization failed; caching is disabled.")
                    logger.warning("cache initialization failed: %s", type(exc).__name__)
            self._started = True
        return self

    async def close(self) -> None:
        """Release all HTTP clients, browser processes, and the cache database.

        Safe to call repeatedly — subsequent calls are no-ops.
        """
        async with self._lifecycle_lock:
            if self._closed:
                return
            browser_results = await asyncio.gather(
                *(browser.close() for browser in self._browser_fetchers.values()),
                return_exceptions=True,
            )
            client_results = await asyncio.gather(
                *(client.aclose() for client in self._http_clients.values()),
                return_exceptions=True,
            )
            for failure in (*browser_results, *client_results):
                if isinstance(failure, Exception):
                    logger.warning("resource cleanup failed: %s", type(failure).__name__)
            if self._cache:
                try:
                    await self._cache.close()
                except Exception as exc:
                    logger.warning("cache cleanup failed: %s", type(exc).__name__)
            self._browser_fetchers.clear()
            self._http_fetchers.clear()
            self._http_clients.clear()
            self._closed = True

    async def fetch(
        self,
        url: str,
        *,
        mode: Literal["auto", "http", "browser"] | None = None,
        proxy: Literal["none", "decodo", "dataimpulse"] | None = None,
        use_cache: bool = True,
        cache_ttl: str | int | None = None,
        raise_on_error: bool | None = None,
    ) -> FetchResult:
        """Fetch one URL and return a structured result.

        Parameters
        ----------
        url : str
            The target URL to fetch.
        mode : str | None
            Override the default fetch mode (``'auto'``, ``'http'``, or ``'browser'``).
        proxy : str | None
            Override the default proxy provider.
        use_cache : bool
            Whether to attempt reading from and writing to the cache (default ``True``).
        cache_ttl : str | int | None
            Override the default cache TTL.
        raise_on_error : bool | None
            Override the default ``raise_on_error`` flag.

        Returns
        -------
        FetchResult
            Structured result with success status, content, and metadata.
        """
        started_at = time.perf_counter()
        selected_mode = mode or self.config.mode
        selected_proxy = proxy or self.config.proxy
        should_raise = self.config.raise_on_error if raise_on_error is None else raise_on_error
        try:
            self._validate_fetch_options(selected_mode, selected_proxy)
            validate_url(url)
            normalized_url = normalize_url(url)
        except (TypeError, ValueError) as exc:
            code = "unsupported_scheme" if "scheme" in str(exc) else "invalid_url"
            result = self._finish_error(
                url=str(url),
                proxy=selected_proxy,
                error=FetchErrorInfo(code, str(exc), False, type(exc).__name__),
                started_at=started_at,
                should_raise=should_raise,
            )
            result.duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            return result

        await self.start()
        ttl = self.config.cache_ttl if cache_ttl is None else parse_duration(cache_ttl)
        cache_key = build_cache_key(
            normalized_url,
            mode=selected_mode,
            proxy=selected_proxy,
            settings={
                "browser_timeout": self.config.browser_timeout,
                "confidence_threshold": self.config.confidence_threshold,
                "headers": BROWSER_HEADERS,
                "max_content_size": self.config.max_content_size,
                "retries_browser": self.config.retries_browser,
            },
        )
        fetch_warnings = list(self._startup_warnings)
        if self._cache and use_cache:
            try:
                cached = await self._cache.get(cache_key)
                if cached:
                    cached.duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                    logger.debug("cache hit for %s", normalized_url)
                    return cached
            except Exception as exc:
                fetch_warnings.append("Cache read failed; content was fetched normally.")
                logger.warning("cache read failed: %s", type(exc).__name__)

        try:
            if selected_mode == "browser":
                result = await self._fetch_browser(normalized_url, selected_proxy, status_code=None)
            else:
                result = await self._fetch_http_or_auto(normalized_url, selected_mode, selected_proxy)
        except (TransportFailure, ProxyConfigurationError) as exc:
            error = exc.error if isinstance(exc, TransportFailure) else FetchErrorInfo(
                "connection_error", str(exc), False, type(exc).__name__
            )
            result = self._finish_error(
                url=normalized_url,
                proxy=selected_proxy,
                error=error,
                status_code=getattr(exc, "status_code", None),
                started_at=started_at,
                should_raise=should_raise,
            )
        except Exception as exc:
            result = self._finish_error(
                url=normalized_url,
                proxy=selected_proxy,
                error=FetchErrorInfo("unknown_error", "An unexpected error occurred while fetching the page.", False, type(exc).__name__),
                started_at=started_at,
                should_raise=should_raise,
            )

        result.duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        result.warnings[:0] = fetch_warnings
        if not result.success and should_raise and result.error:
            raise PageFetchError(result.error, url=result.url)
        if self._cache and use_cache and result.success and not self._uncacheable(result):
            try:
                await self._cache.set(cache_key, result, ttl)
            except Exception as exc:
                result.warnings.append("Result could not be written to cache.")
                logger.warning("cache write failed: %s", type(exc).__name__)
        return result

    async def fetch_many(
        self,
        urls: Iterable[str],
        *,
        mode: Literal["auto", "http", "browser"] | None = None,
        proxy: Literal["none", "decodo", "dataimpulse"] | None = None,
        use_cache: bool = True,
        cache_ttl: str | int | None = None,
        raise_on_error: bool | None = None,
    ) -> list[FetchResult]:
        """Fetch unique URLs concurrently while preserving input order.

        Parameters
        ----------
        urls : Iterable[str]
            URLs to fetch (duplicates are deduplicated before fetching).
        mode : str | None
            Override the default fetch mode.
        proxy : str | None
            Override the default proxy provider.
        use_cache : bool
            Whether to use the cache (default ``True``).
        cache_ttl : str | int | None
            Override the default cache TTL.
        raise_on_error : bool | None
            Override the default ``raise_on_error`` flag.

        Returns
        -------
        list[FetchResult]
            One result per input URL in the original order, preserving
            duplicates and failures.
        """
        ordered = list(urls)
        unique = list(dict.fromkeys(ordered))

        async def one(
            item: str,
            _mode: str | None = mode,
            _proxy: str | None = proxy,
            _use_cache: bool = use_cache,
            _cache_ttl: str | int | None = cache_ttl,
            _raise_on_error: bool | None = raise_on_error,
        ) -> FetchResult:
            item_start = time.perf_counter()
            try:
                return await self.fetch(
                    item,
                    mode=_mode,
                    proxy=_proxy,
                    use_cache=_use_cache,
                    cache_ttl=_cache_ttl,
                    raise_on_error=_raise_on_error,
                )
            except PageFetchError as exc:
                return FetchResult(
                    url=normalize_url(item),
                    success=False,
                    proxy_provider=proxy or self.config.proxy,
                    error=exc.error,
                    duration_ms=round((time.perf_counter() - item_start) * 1000, 2),
                    fetched_at=datetime.now(UTC),
                )

        fetched = await asyncio.gather(*(one(item) for item in unique))
        by_url = dict(zip(unique, fetched, strict=True))
        return [by_url[item] for item in ordered]

    async def _fetch_http_or_auto(self, url: str, mode: Literal["auto", "http", "browser"], proxy: str) -> FetchResult:
        try:
            response = await (await self._http_fetcher(proxy)).fetch(url)
        except TransportFailure as exc:
            if mode == "auto" and exc.error.retryable:
                logger.info("HTTP transport failed; using browser for %s", url)
                return await self._fetch_browser(url, proxy, status_code=None)
            raise
        if response.status_code >= 400:
            retryable = response.status_code in RETRYABLE_STATUS_CODES
            if mode == "auto" and (response.status_code in BLOCKED_STATUS_CODES or retryable):
                logger.info("HTTP %s; using browser for %s", response.status_code, url)
                return await self._fetch_browser(url, proxy, status_code=response.status_code)
            code = "blocked" if response.status_code in BLOCKED_STATUS_CODES else "http_error"
            raise TransportFailure(
                FetchErrorInfo(code, f"HTTP request returned status {response.status_code}", retryable),
                status_code=response.status_code,
            )

        content_type = self._content_type(response.headers.get("Content-Type"))
        if self._is_pdf(content_type, response.content):
            return self._result_from_pdf(url, response, proxy)
        if self._is_xml(content_type):
            return self._result_from_xml(url, response, proxy)
        if content_type.startswith("text/plain"):
            return self._result_from_text(url, response, proxy)
        html = self._decode(response)
        raw_soup = BeautifulSoup(html, "lxml")
        report = analyze_html(html)
        if mode == "auto" and report.score < self.config.confidence_threshold:
            logger.info("HTTP confidence %.3f; using browser for %s", report.score, url)
            try:
                rendered = await self._fetch_browser(url, proxy, status_code=response.status_code)
            except TransportFailure:
                available = self._result_from_html(
                    original_url=url,
                    final_url=response.url,
                    status_code=response.status_code,
                    html=html,
                    content_type=content_type,
                    encoding=response.encoding,
                    proxy=proxy,
                    method="http",
                    response_headers=response.headers,
                    soup=raw_soup,
                )
                available.warnings.extend(
                    [
                        "Browser rendering failed; showing basic HTTP version instead.",
                        "Content may be incomplete.",
                    ]
                )
                return available
            if not rendered.success:
                available = self._result_from_html(
                    original_url=url,
                    final_url=response.url,
                    status_code=response.status_code,
                    html=html,
                    content_type=content_type,
                    encoding=response.encoding,
                    proxy=proxy,
                    method="http",
                    response_headers=response.headers,
                    soup=raw_soup,
                )
                available.warnings.extend(
                    [
                        "Browser rendered content was not usable; showing HTTP version instead.",
                        "Content may be incomplete.",
                    ]
                )
                return available
            rendered.warnings.insert(0, "HTTP content confidence was low; browser fallback was used.")
            return rendered
        result = self._result_from_html(
            original_url=url,
            final_url=response.url,
            status_code=response.status_code,
            html=html,
            content_type=content_type,
            encoding=response.encoding,
            proxy=proxy,
            method="http",
            response_headers=response.headers,
            soup=raw_soup,
        )
        if mode == "http" and report.score < self.config.confidence_threshold:
            result.warnings.append("HTTP content may be incomplete; browser fallback is disabled.")
        return result

    async def _fetch_browser(self, url: str, proxy: str, status_code: int | None) -> FetchResult:
        response = await (await self._browser_fetcher(proxy)).fetch(url)
        raw_soup = BeautifulSoup(response.html, "lxml")
        result = self._result_from_html(
            original_url=url,
            final_url=response.url,
            status_code=response.status_code if response.status_code is not None else status_code,
            html=response.html,
            content_type="text/html",
            encoding="utf-8",
            proxy=proxy,
            method="browser",
            soup=raw_soup,
        )
        result.warnings.extend(response.warnings)
        report = analyze_html(response.html)
        if response.status_code is not None and response.status_code >= 400:
            result.success = False
            code = "blocked" if response.status_code in BLOCKED_STATUS_CODES else "http_error"
            result.error = FetchErrorInfo(
                code,
                f"browser navigation returned status {response.status_code}",
                response.status_code in RETRYABLE_STATUS_CODES,
            )
        elif report.challenge:
            result.success = False
            result.error = FetchErrorInfo("captcha_detected", "challenge page remained after browser retries", False)
        elif report.score < self.config.confidence_threshold:
            result.warnings.append("Rendered content may still be incomplete.")
        return result

    async def _http_fetcher(self, provider: str) -> HTTPFetcher:
        if provider not in self._http_fetchers:
            async with self._http_init_lock:
                if provider in self._http_fetchers:
                    return self._http_fetchers[provider]
                settings = resolve_proxy(provider)
                client = httpx.AsyncClient(
                    headers=BROWSER_HEADERS,
                    timeout=httpx.Timeout(self.config.http_timeout),
                    follow_redirects=True,
                    max_redirects=self.config.max_redirects,
                    http2=True,
                    proxy=settings.url,
                    limits=httpx.Limits(
                        max_connections=self.config.http_concurrency * 2,
                        max_keepalive_connections=self.config.http_concurrency,
                    ),
                )
                self._http_clients[provider] = client
                self._http_fetchers[provider] = HTTPFetcher(
                    client,
                    self._http_semaphore,
                    retries=self.config.retries_http,
                    max_content_size=self.config.max_content_size,
                )
        return self._http_fetchers[provider]

    async def _browser_fetcher(self, provider: str) -> BrowserFetcher:
        if provider not in self._browser_fetchers:
            async with self._browser_init_lock:
                if provider in self._browser_fetchers:
                    return self._browser_fetchers[provider]
                self._browser_fetchers[provider] = BrowserFetcher(
                    self._browser_semaphore,
                    timeout=self.config.browser_timeout,
                    retries=self.config.retries_browser,
                    proxy=resolve_proxy(provider),
                    max_content_size=self.config.max_content_size,
                    confidence_threshold=self.config.confidence_threshold,
                )
        return self._browser_fetchers[provider]

    def _result_from_html(
        self,
        *,
        original_url: str,
        final_url: str,
        status_code: int | None,
        html: str,
        content_type: str,
        encoding: str | None,
        proxy: str,
        method: str,
        response_headers: httpx.Headers | None = None,
        soup: BeautifulSoup | None = None,
    ) -> FetchResult:
        try:
            processed = process_html(html, final_url, response_headers, soup=soup)
        except Exception as exc:
            raise TransportFailure(
                FetchErrorInfo("parse_error", "HTML content could not be processed", False, type(exc).__name__)
            ) from exc
        return FetchResult(
            url=original_url,
            final_url=final_url,
            status_code=status_code,
            success=True,
            content_type=content_type,
            encoding=encoding,
            title=processed.title,
            markdown=processed.markdown,
            html=html,
            text=processed.text,
            metadata=processed.metadata,
            links=processed.links,
            images=processed.images,
            fetch_method=method,
            proxy_provider=proxy,
            content_confidence=processed.confidence.score,
            fetched_at=datetime.now(UTC),
            warnings=processed.warnings,
        )

    def _result_from_pdf(self, url: str, response: HTTPResponse, proxy: str) -> FetchResult:
        try:
            doc = process_pdf(response.content)
        except Exception as exc:
            raise TransportFailure(FetchErrorInfo("pdf_parse_error", "PDF could not be parsed", False, type(exc).__name__)) from exc
        return self._document_result(url, response, proxy, doc, "pdf", "application/pdf")

    def _result_from_xml(self, url: str, response: HTTPResponse, proxy: str) -> FetchResult:
        try:
            doc = process_xml(response.content, response.encoding)
        except Exception as exc:
            raise TransportFailure(FetchErrorInfo("xml_parse_error", "XML could not be parsed", False, type(exc).__name__)) from exc
        return self._document_result(
            url,
            response,
            proxy,
            doc,
            "xml",
            self._content_type(response.headers.get("Content-Type")),
            raw_source=self._decode(response),
        )

    def _result_from_text(self, url: str, response: HTTPResponse, proxy: str) -> FetchResult:
        doc = process_text(response.content, response.encoding)
        return self._document_result(url, response, proxy, doc, "text", self._content_type(response.headers.get("Content-Type")))

    @staticmethod
    def _document_result(
        url: str,
        response: HTTPResponse,
        proxy: str,
        doc: Any,
        method: str,
        content_type: str,
        *,
        raw_source: str | None = None,
    ) -> FetchResult:
        doc.metadata["headers"] = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower() in SAFE_RESPONSE_HEADERS
        }
        return FetchResult(
            url=url,
            final_url=response.url,
            status_code=response.status_code,
            success=True,
            content_type=content_type,
            encoding=response.encoding,
            title=doc.title,
            markdown=doc.markdown,
            html=raw_source,
            text=doc.text,
            metadata=doc.metadata,
            fetch_method=method,
            proxy_provider=proxy,
            content_confidence=1.0,
            fetched_at=datetime.now(UTC),
            warnings=doc.warnings,
        )

    @staticmethod
    def _decode(response: HTTPResponse) -> str:
        encoding = response.encoding or "utf-8"
        try:
            return response.content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            return response.content.decode("utf-8", errors="replace")

    @staticmethod
    def _content_type(header: str | None) -> str:
        return (header or "application/octet-stream").split(";", 1)[0].strip().lower()

    @staticmethod
    def _is_pdf(content_type: str, content: bytes) -> bool:
        return content_type == "application/pdf" or content.startswith(b"%PDF-")

    @staticmethod
    def _is_xml(content_type: str) -> bool:
        return any(value in content_type for value in XML_TYPES)

    @staticmethod
    def _validate_fetch_options(mode: str, proxy: str) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        if proxy not in VALID_PROXIES:
            raise ValueError(f"proxy must be one of {sorted(VALID_PROXIES)}")

    @staticmethod
    def _uncacheable(result: FetchResult) -> bool:
        return bool(result.error) or result.status_code in BLOCKED_STATUS_CODES or (
            result.status_code is not None and result.status_code >= 500
        )

    @staticmethod
    def _finish_error(
        *,
        url: str,
        proxy: str,
        error: FetchErrorInfo,
        started_at: float,
        should_raise: bool,
        status_code: int | None = None,
    ) -> FetchResult:
        if should_raise:
            raise PageFetchError(error, url=url)
        return FetchResult(
            url=url,
            status_code=status_code,
            success=False,
            proxy_provider=proxy,
            fetched_at=datetime.now(UTC),
            error=error,
        )
