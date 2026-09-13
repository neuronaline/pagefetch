"""Main PageFetch client and fetch pipeline."""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from hashlib import md5
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .cache import SQLiteCache, build_cache_key
from .config import VALID_MODES, VALID_PROXIES, PageFetchConfig
from .constants import (
    _UA_POOL_BY_OS,
    BLOCKED_STATUS_CODES,
    BROWSER_HEADERS,
    GEO_MAP,
    RETRYABLE_STATUS_CODES,
    SAFE_RESPONSE_HEADERS,
)
from .exceptions import PageFetchError
from .fetching import BrowserFetcher, HTTPFetcher, HTTPResponse, TransportFailure
from .models import FetchErrorInfo, FetchResult
from .processing.detector import ConfidenceReport, analyze_html
from .processing.html import process_html
from .processing.non_html import (
    MissingOptionalDependency,
    process_pdf,
    process_text,
    process_xml,
)
from .processing.structure import StructureLimits, extract_structure
from .proxy import ProxyConfigurationError, ProxySettings, resolve_proxy
from .proxy.providers import (
    _inject_session_id,
    make_domain_session,
    make_random_session,
)
from .utils.durations import parse_duration
from .utils.urls import normalize_url, registrable_host, validate_url

logger = logging.getLogger("pagefetch")

# Per-resource close timeout. Hung teardowns (a browser process wedged on a
# subprocess, an HTTP connection that refuses to drain) must not stall
# ``close()`` indefinitely; the timeout is intentionally short so the
# outer drain event keeps moving and the next caller can still abort.
_RESOURCE_CLOSE_TIMEOUT = 0.5

# Detect the host platform once at import time so HTTP requests and the
# browser fallback share the same OS signature (mixed-OS fingerprints
# from the same IP are a known Cloudflare/Akamai bot-detection trigger).
if sys.platform == "win32":
    _HOST_OS = "windows"
elif sys.platform == "darwin":
    _HOST_OS = "macos"
else:
    _HOST_OS = "linux"


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
        cleaning_level: Literal["minimal", "standard", "maximum"] = "standard",
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
        block_images: bool = True,
        block_level: Literal["minimal", "balanced", "aggressive"] | None = None,
        accept_language: str = "en-US,en;q=0.5",
        humanize: bool | None = None,
        session_rotation: Literal["sticky", "rotate"] | None = None,
        request_pacing: float | None = None,
        stealth_level: Literal["off", "balanced", "max"] = "off",
        proxy_geo: str | None = None,
        raise_on_error: bool = False,
        screenshot_max_bytes: int = 50 * 1024 * 1024,
        browser_pre_check_byte_margin: float = 1.5,
    ) -> None:
        self.config = PageFetchConfig.build(
            mode=mode,
            proxy=proxy,
            cleaning_level=cleaning_level,
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
            block_images=block_images,
            block_level=block_level,
            accept_language=accept_language,
            humanize=humanize,
            session_rotation=session_rotation,
            request_pacing=request_pacing,
            stealth_level=stealth_level,
            proxy_geo=proxy_geo,
            raise_on_error=raise_on_error,
            screenshot_max_bytes=screenshot_max_bytes,
            browser_pre_check_byte_margin=browser_pre_check_byte_margin,
        )
        self._http_semaphore = asyncio.Semaphore(self.config.http_concurrency)
        self._browser_semaphore = asyncio.Semaphore(self.config.browser_concurrency)
        self._http_clients: dict[str, httpx.AsyncClient] = {}
        self._http_fetchers: dict[str, HTTPFetcher] = {}
        self._browser_fetchers: OrderedDict[str, BrowserFetcher] = OrderedDict()
        self._browser_fetcher_users: dict[str, int] = {}
        self._browser_pool_limit = max(
            self.config.browser_concurrency,
            min(32, self.config.browser_concurrency * 2),
        )
        self._http_init_lock = asyncio.Lock()
        self._browser_pool_condition = asyncio.Condition()
        self._cache = SQLiteCache(self.config.cache_path) if self.config.cache_enabled else None
        self._started = False
        self._closed = False
        self._closing = False
        self._lifecycle_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._startup_warnings: list[str] = []
        self._active_fetches = 0
        self._active_fetches_lock = asyncio.Lock()
        # Set whenever ``_active_fetches`` is zero. ``close()`` waits on this
        # event (with a hard 5 s deadline) instead of busy-polling the
        # counter. Initialising it ``set`` lets the first ``close()`` skip the
        # wait when no fetches are in flight.
        self._drain_event = asyncio.Event()
        self._drain_event.set()

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
            if self._closed or self._closing:
                raise RuntimeError("PageFetch has already been closed")
            if self._cache:
                try:
                    await self._cache.start()
                except Exception as exc:
                    self._cache = None
                    self._startup_warnings.append("Cache initialization failed; caching is disabled.")
                    logger.warning("cache initialization failed: %s", type(exc).__name__)
            self._started = True
            self._log_fingerprint_summary()
        return self

    def _log_fingerprint_summary(self) -> None:
        """Emit a one-line diagnostic summarising the anti-detection posture."""
        cfg = self.config
        parts: list[str] = []
        parts.append(f"stealth={cfg.stealth_level}")
        parts.append(f"humanize={'on' if cfg.humanize else 'off'}")
        parts.append(f"block={cfg.block_level}")
        if cfg.request_pacing > 0:
            parts.append(f"pacing={cfg.request_pacing:.1f}s")
        parts.append(f"session={cfg.session_rotation}")
        parts.append(f"lang={cfg.accept_language}")
        if cfg.proxy_geo:
            parts.append(f"geo={cfg.proxy_geo}")
        if cfg.mode == "auto":
            parts.append("mode=auto (HTTP→browser)")
        logger.info("fingerprint profile: %s", ", ".join(parts))

    async def close(self) -> None:
        """Release all HTTP clients, browser processes, and the cache database.

        Safe to call repeatedly and while another caller is closing the client.

        The teardown belongs to a task owned by the client rather than to its
        first caller. This prevents cancellation of a caller (for example by a
        request timeout) from leaving the client half-closed.
        """
        async with self._lifecycle_lock:
            if self._closed:
                return
            if self._close_task is None:
                self._closing = True
                self._close_task = asyncio.create_task(self._teardown())
            close_task = self._close_task

        await asyncio.shield(close_task)

    async def _teardown(self) -> None:
        """Run the client-owned resource teardown to completion."""
        try:
            # Do not hold the lifecycle lock while active operations finish.
            # A single ``asyncio.wait_for`` replaces a 50-step busy-wait: the
            # event is cleared in ``_begin_operation`` and set in
            # ``_finish_operation`` once the counter drops back to zero.
            try:
                await asyncio.wait_for(self._drain_event.wait(), timeout=5.0)
            except TimeoutError:
                async with self._active_fetches_lock:
                    remaining = self._active_fetches
                if remaining > 0:
                    logger.warning(
                        "close timed out waiting for %d in-flight fetch(es); tearing down resources anyway",
                        remaining,
                    )
            async with self._lifecycle_lock:
                browser_results = await asyncio.gather(
                    *(
                        self._close_resource(browser.close)
                        for browser in self._browser_fetchers.values()
                    ),
                    return_exceptions=True,
                )
                client_results = await asyncio.gather(
                    *(self._close_resource(client.aclose) for client in self._http_clients.values()),
                    return_exceptions=True,
                )
                for failure in (*browser_results, *client_results):
                    if isinstance(failure, Exception):
                        logger.warning("resource cleanup failed: %s", type(failure).__name__)
                if self._cache:
                    await self._close_resource(self._cache.close)
                self._browser_fetchers.clear()
                self._browser_fetcher_users.clear()
                self._http_fetchers.clear()
                self._http_clients.clear()
                self._closed = True
                self._closing = False
        except asyncio.CancelledError:
            # A direct cancellation of the owned task is exceptional, but it
            # must leave the client retryable. Normal caller cancellation is
            # shielded in ``close()`` and never reaches this branch.
            async with self._lifecycle_lock:
                self._closing = False
                self._close_task = None
            raise

    @staticmethod
    async def _close_resource(close: Callable[[], Awaitable[Any]]) -> None:
        """Close one transport without allowing a hung child to stall teardown."""
        try:
            await asyncio.wait_for(close(), timeout=_RESOURCE_CLOSE_TIMEOUT)
        except TimeoutError:
            logger.warning("resource cleanup timed out after %.1f seconds", _RESOURCE_CLOSE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.warning("resource cleanup failed: %s", type(exc).__name__)

    async def _begin_operation(self) -> bool:
        """Start resources and register work before touching shared state."""
        try:
            await self.start()
        except RuntimeError:
            return False
        async with self._lifecycle_lock:
            if self._closed or self._closing:
                return False
            async with self._active_fetches_lock:
                self._active_fetches += 1
                self._drain_event.clear()
        return True

    async def _finish_operation(self) -> None:
        async with self._active_fetches_lock:
            self._active_fetches -= 1
            if self._active_fetches <= 0:
                self._active_fetches = 0
                self._drain_event.set()

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

        For structural / RAW HTML / screenshot capture use :meth:`extract` instead.

        Returns
        -------
        FetchResult
            Structured result with success status, content, and metadata.
        """
        selected_mode = mode or self.config.mode
        selected_proxy = proxy or self.config.proxy
        should_raise = self.config.raise_on_error if raise_on_error is None else raise_on_error
        async def transport(normalized_url: str) -> FetchResult:
            if selected_mode == "browser":
                return await self._fetch_browser(normalized_url, selected_proxy, status_code=None)
            return await self._fetch_http_or_auto(normalized_url, selected_mode, selected_proxy)

        return await self._run_operation(
            url=url,
            mode=selected_mode,
            proxy=selected_proxy,
            use_cache=use_cache,
            cache_ttl=cache_ttl,
            should_raise=should_raise,
            cache_settings=self._cache_settings(),
            error_context="fetching the page",
            transport=transport,
        )

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

        Each per-URL call returns a structured ``FetchResult`` even when
        ``raise_on_error=True`` (``fetch()`` raises :class:`PageFetchError`,
        not ``ValueError``); only :class:`PageFetchError` is captured here
        because every other failure mode is already converted to an
        ``error`` field before the coroutine returns.
        """
        ordered = list(urls)
        unique = list(dict.fromkeys(ordered))

        async def one(item: str) -> FetchResult:
            try:
                return await self.fetch(
                    item,
                    mode=mode,
                    proxy=proxy,
                    use_cache=use_cache,
                    cache_ttl=cache_ttl,
                    raise_on_error=raise_on_error,
                )
            except PageFetchError as exc:
                # Use the raw item string — normalize_url may itself raise for
                # the same invalid URL that produced the error; ``str(item)``
                # preserves the original input verbatim.
                return FetchResult(
                    url=str(item),
                    success=False,
                    proxy_provider=proxy or self.config.proxy,
                    error=exc.error,
                    fetched_at=datetime.now(UTC),
                )

        if self.config.request_pacing > 0 and len(unique) > 1:
            # Stagger task creation to avoid a synchronized burst of
            # requests arriving at the same instant — a strong bot signal.
            tasks: list[asyncio.Task[FetchResult]] = []
            for i, item in enumerate(unique):
                if i > 0:
                    await asyncio.sleep(random.uniform(0, self.config.request_pacing))
                tasks.append(asyncio.create_task(one(item)))
            fetched = await asyncio.gather(*tasks)
        else:
            fetched = await asyncio.gather(*(one(item) for item in unique))
        by_url = dict(zip(unique, fetched, strict=True))
        # For duplicate URLs, return independent copies so callers
        # do not accidentally alias mutable fields across entries.
        from copy import deepcopy
        results: list[FetchResult] = []
        emitted: set[int] = set()
        for item in ordered:
            result = by_url[item]
            if id(result) in emitted:
                result = deepcopy(result)
            emitted.add(id(result))
            results.append(result)
        return results

    async def extract(
        self,
        url: str,
        *,
        structure: bool = True,
        compact_structure: bool = False,
        screenshot: Literal["none", "viewport", "full"] = "none",
        screenshot_format: Literal["png", "jpeg"] = "png",
        proxy: Literal["none", "decodo", "dataimpulse"] | None = None,
        use_cache: bool = True,
        cache_ttl: str | int | None = None,
        raise_on_error: bool | None = None,
    ) -> FetchResult:
        """Fetch a page and return its RAW HTML, structural summary, and/or
        screenshot.

        Always uses ``mode='browser'`` — there is no HTTP→browser pipeline
        because the goal here is a complete, rendered page shell, not an
        article-shaped extraction. The :meth:`fetch` coroutine remains the
        right tool for the information layer (markdown, metadata, links).
        """
        selected_proxy = proxy or self.config.proxy
        should_raise = self.config.raise_on_error if raise_on_error is None else raise_on_error
        async def transport(normalized_url: str) -> FetchResult:
            return await self._fetch_browser(
                normalized_url,
                selected_proxy,
                status_code=None,
                structure=structure,
                compact_structure=compact_structure,
                screenshot=screenshot,
                screenshot_format=screenshot_format,
            )

        return await self._run_operation(
            url=url,
            mode="browser",
            proxy=selected_proxy,
            use_cache=use_cache,
            cache_ttl=cache_ttl,
            should_raise=should_raise,
            cache_settings=self._cache_settings(
                compact_structure=compact_structure,
                screenshot=screenshot,
                screenshot_format=screenshot_format,
                structure=structure,
            ),
            requested_screenshot=screenshot != "none",
            error_context="extracting the page",
            transport=transport,
        )

    def _cache_settings(self, **operation_settings: Any) -> dict[str, Any]:
        """Return cache-key settings shared by fetch and extraction operations."""
        settings = {
            "accept_language": (
                GEO_MAP[self.config.proxy_geo]["accept_language"]
                if self.config.proxy_geo
                else self.config.accept_language
            ),
            "block_images": self.config.block_images,
            "block_level": self.config.block_level,
            "cleaning_level": self.config.cleaning_level,
            "confidence_threshold": self.config.confidence_threshold,
            "humanize": self.config.humanize,
            "max_redirects": self.config.max_redirects,
            "proxy_geo": self.config.proxy_geo,
            "session_rotation": self.config.session_rotation,
        }
        settings.update(operation_settings)
        return settings

    async def _run_operation(
        self,
        *,
        url: str,
        mode: str,
        proxy: str,
        use_cache: bool,
        cache_ttl: str | int | None,
        should_raise: bool,
        cache_settings: dict[str, Any],
        error_context: str,
        transport: Callable[[str], Awaitable[FetchResult]],
        requested_screenshot: bool = False,
    ) -> FetchResult:
        """Run the validation, lifecycle, cache, and result pipeline for one operation."""
        started_at = time.perf_counter()
        try:
            self._validate_fetch_options(mode, proxy)
            validate_url(url)
            normalized_url = normalize_url(url)
        except (TypeError, ValueError) as exc:
            code = "unsupported_scheme" if "scheme" in str(exc) else "invalid_url"
            return self._finish_error(
                url=str(url), proxy=proxy,
                error=FetchErrorInfo(code, str(exc), False, type(exc).__name__),
                started_at=started_at, should_raise=should_raise,
            )
        if not await self._begin_operation():
            result = self._finish_error(
                url=normalized_url, proxy=proxy,
                error=FetchErrorInfo("client_closed", "PageFetch client has been closed", False),
                started_at=started_at, should_raise=should_raise,
            )
            result.warnings[:0] = self._startup_warnings
            return result
        try:
            try:
                ttl = self.config.cache_ttl if cache_ttl is None else parse_duration(cache_ttl)
            except (TypeError, ValueError) as exc:
                return self._finish_error(
                    url=normalized_url, proxy=proxy,
                    error=FetchErrorInfo("invalid_cache_ttl", f"Invalid cache_ttl: {exc}", False, type(exc).__name__),
                    started_at=started_at, should_raise=should_raise,
                )
            cache_key = build_cache_key(normalized_url, mode=mode, proxy=proxy, settings=cache_settings)
            warnings = list(self._startup_warnings)
            if self._cache and use_cache:
                try:
                    cached = await self._cache.get(cache_key, requested_screenshot=requested_screenshot)
                    if cached:
                        cached.duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                        logger.debug("cache hit for %s", normalized_url)
                        return cached
                except Exception as exc:
                    warnings.append("Cache read failed; content was fetched normally.")
                    logger.warning("cache read failed: %s", type(exc).__name__)
            try:
                result = await transport(normalized_url)
            except (TransportFailure, ProxyConfigurationError) as exc:
                error = exc.error if isinstance(exc, TransportFailure) else FetchErrorInfo(
                    "connection_error", str(exc), False, type(exc).__name__
                )
                result = self._finish_error(
                    url=normalized_url, proxy=proxy, error=error,
                    status_code=getattr(exc, "status_code", None),
                    started_at=started_at, should_raise=should_raise,
                )
            except Exception as exc:
                result = self._finish_error(
                    url=normalized_url, proxy=proxy,
                    error=FetchErrorInfo("unknown_error", f"An unexpected error occurred while {error_context}.", False, type(exc).__name__),
                    started_at=started_at, should_raise=should_raise,
                )
            result.warnings[:0] = warnings
            if not result.success and should_raise and result.error:
                raise PageFetchError(result.error, url=result.url)
            if self._cache and use_cache and result.success and not self._uncacheable(result):
                try:
                    await self._cache.set(cache_key, result, ttl)
                except Exception as exc:
                    result.warnings.append("Result could not be written to cache.")
                    logger.warning("cache write failed: %s", type(exc).__name__)
            return result
        finally:
            await self._finish_operation()

    async def _fetch_http_or_auto(
        self,
        url: str,
        mode: Literal["auto", "http", "browser"],
        proxy: str,
    ) -> FetchResult:
        try:
            fetcher = await self._http_fetcher(proxy)
            per_request_proxy: str | None = None
            if proxy != "none":
                settings = resolve_proxy(proxy)
                if settings.url:
                    if self.config.session_rotation == "rotate":
                        session_id = make_random_session()
                    else:
                        domain = registrable_host(url) or url
                        session_id = make_domain_session(domain)
                    per_request_proxy = _inject_session_id(settings.url, session_id)
            response = await fetcher.fetch(
                url,
                proxy_url=per_request_proxy,
                headers=self._headers_for_url(url),
            )
        except TransportFailure:
            # Timeouts, DNS failures, disconnects, and other transport errors are not
            # fixed by rendering in a browser — surface them immediately instead of
            # waiting on an expensive Camoufox navigation that will fail the same way.
            raise
        if response.status_code >= 400:
            retryable = response.status_code in RETRYABLE_STATUS_CODES
            # Only anti-bot / rate-limit responses benefit from a stealth browser.
            # 4xx like 404 and 5xx server errors should fail fast at the HTTP layer.
            if mode == "auto" and response.status_code in BLOCKED_STATUS_CODES:
                logger.info("HTTP %s; using browser for %s", response.status_code, url)
                # ── auto-mode double-hit softening ──
                # Insert a short random delay before the browser fallback so
                # the same proxy/source IP does not emit two different TLS
                # stacks (httpx → Camoufox) back-to-back — a strong bot signal.
                await asyncio.sleep(random.uniform(0.5, 3.0))
                return await self._fetch_browser(
                    url,
                    proxy,
                    status_code=response.status_code,
                )
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
        if not self._is_html_like(content_type) and not self._looks_like_html(response.content):
            # Auto mode escalates non-HTML responses (JSON, binary blobs, etc.)
            # to the browser so SPA fallbacks and JS-rendered JSON pages still
            # get a chance to surface real content. The HTTP path stays
            # fail-fast because the caller explicitly opted out of a browser.
            if mode == "auto":
                logger.info(
                    "HTTP returned %s; using browser for %s", content_type, url
                )
                await asyncio.sleep(random.uniform(0.5, 3.0))
                return await self._fetch_browser(
                    url,
                    proxy,
                    status_code=response.status_code,
                )
            raise TransportFailure(
                FetchErrorInfo(
                    "unsupported_content_type",
                    f"Content type {content_type!r} is not HTML; cannot process with HTTP mode",
                    False,
                ),
                status_code=response.status_code,
            )
        html = self._decode(response)
        raw_soup = BeautifulSoup(html, "lxml")
        report = analyze_html(html, soup=raw_soup)
        if mode == "auto" and report.score < self.config.confidence_threshold:
            logger.info("HTTP confidence %.3f; using browser for %s", report.score, url)
            # ── auto-mode double-hit softening ──
            await asyncio.sleep(random.uniform(0.5, 3.0))
            try:
                rendered = await self._fetch_browser(
                    url,
                    proxy,
                    status_code=response.status_code,
                )
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
                    confidence=report,
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
                    confidence=report,
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
            confidence=report,
        )
        if mode == "http" and report.score < self.config.confidence_threshold:
            result.warnings.append("HTTP content may be incomplete; browser fallback is disabled.")
        return result

    async def _fetch_browser(
        self,
        url: str,
        proxy: str,
        status_code: int | None,
        *,
        structure: bool = False,
        compact_structure: bool = False,
        screenshot: str = "none",
        screenshot_format: str = "png",
    ) -> FetchResult:
        """Unified browser-backed fetch path.

        Used by ``auto``-mode HTTP→browser fallback and by :meth:`extract`. The
        optional kwargs select the extraction flow:

        * ``structure`` / ``compact_structure`` populate the ``structure`` field.
        * ``screenshot`` / ``screenshot_format`` capture and attach a screenshot.

        Centralising the two legacy methods (``_fetch_browser`` and
        ``_fetch_browser_extract``) keeps the proxy rotation, fetcher
        acquisition/release, and error-handling branches in a single place so
        they cannot drift apart.
        """
        if self.config.session_rotation == "rotate" and proxy != "none":
            settings = resolve_proxy(proxy)
            proxy_url = (
                _inject_session_id(settings.url, make_random_session())
                if settings.url
                else None
            )
            fetcher = self._new_browser_fetcher(
                ProxySettings(provider=settings.provider, url=proxy_url)
            )
            try:
                response = await fetcher.fetch(
                    url,
                    screenshot=screenshot,
                    screenshot_format=screenshot_format,
                    screenshot_max_bytes=self.config.screenshot_max_bytes,
                )
            finally:
                await self._close_browser_quietly(fetcher)
        else:
            cache_key, fetcher = await self._acquire_browser_fetcher(proxy, url)
            try:
                response = await fetcher.fetch(
                    url,
                    screenshot=screenshot,
                    screenshot_format=screenshot_format,
                    screenshot_max_bytes=self.config.screenshot_max_bytes,
                )
            finally:
                await self._release_browser_fetcher(cache_key)
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
            confidence=response.confidence,
            include_structure=structure,
            compact_structure=compact_structure,
        )
        result.screenshot = response.screenshot
        result.screenshot_format = response.screenshot_format
        result.warnings.extend(response.warnings)
        report = response.confidence
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
        cache_key = provider
        if cache_key not in self._http_fetchers:
            async with self._http_init_lock:
                if cache_key in self._http_fetchers:
                    return self._http_fetchers[cache_key]
                resolve_proxy(provider)
                client = httpx.AsyncClient(
                    headers=BROWSER_HEADERS,
                    timeout=httpx.Timeout(self.config.http_timeout),
                    follow_redirects=True,
                    max_redirects=self.config.max_redirects,
                    http2=True,
                    limits=httpx.Limits(
                        max_connections=self.config.http_concurrency * 2,
                        max_keepalive_connections=self.config.http_concurrency,
                    ),
                )
                self._http_clients[cache_key] = client
                self._http_fetchers[cache_key] = HTTPFetcher(
                    client,
                    self._http_semaphore,
                    retries=self.config.retries_http,
                    max_content_size=self.config.max_content_size,
                )
        return self._http_fetchers[cache_key]

    def _headers_for_url(self, url: str) -> dict[str, str]:
        headers = dict(BROWSER_HEADERS)
        domain = urlsplit(url).hostname or url
        # Pick the User-Agent from the pool matching the host OS so outgoing
        # HTTP requests declare an OS consistent with the runtime; the browser
        # fallback sets its own UA independently of these headers.
        pool = _UA_POOL_BY_OS[_HOST_OS]
        pool_idx = int(md5(domain.encode()).hexdigest()[:8], 16) % len(pool)
        headers["User-Agent"] = pool[pool_idx]
        if self.config.proxy_geo:
            headers["Accept-Language"] = GEO_MAP[self.config.proxy_geo]["accept_language"]
        else:
            headers["Accept-Language"] = self.config.accept_language
        return headers

    def _browser_pool_target(self, provider: str, url: str) -> tuple[str, ProxySettings]:
        session_id = ""
        if self.config.session_rotation == "sticky" and provider != "none":
            domain = registrable_host(url) or url
            session_id = make_domain_session(domain)
        cache_key = f"{provider}_{session_id}" if session_id else provider
        settings = resolve_proxy(provider)
        if session_id and settings.url:
            proxy_url = _inject_session_id(settings.url, session_id)
        else:
            proxy_url = settings.url
        return cache_key, ProxySettings(provider=settings.provider, url=proxy_url)

    def _new_browser_fetcher(self, proxy: ProxySettings) -> BrowserFetcher:
        return BrowserFetcher(
            self._browser_semaphore,
            timeout=self.config.browser_timeout,
            retries=self.config.retries_browser,
            proxy=proxy,
            max_content_size=self.config.max_content_size,
            browser_pre_check_byte_margin=self.config.browser_pre_check_byte_margin,
            confidence_threshold=self.config.confidence_threshold,
            block_images=self.config.block_images,
            block_level=self.config.block_level,
            humanize=self.config.humanize,
            geo_locale=GEO_MAP[self.config.proxy_geo]["locale"] if self.config.proxy_geo else None,
            geo_timezone=GEO_MAP[self.config.proxy_geo]["timezone"] if self.config.proxy_geo else None,
        )

    async def _acquire_browser_fetcher(
        self,
        provider: str,
        url: str,
    ) -> tuple[str, BrowserFetcher]:
        cache_key, proxy = self._browser_pool_target(provider, url)
        evicted: BrowserFetcher | None = None
        async with self._browser_pool_condition:
            while cache_key not in self._browser_fetchers:
                if len(self._browser_fetchers) < self._browser_pool_limit:
                    break
                idle_key = next(
                    (
                        key
                        for key in self._browser_fetchers
                        if self._browser_fetcher_users.get(key, 0) == 0
                    ),
                    None,
                )
                if idle_key is not None:
                    evicted = self._browser_fetchers.pop(idle_key)
                    self._browser_fetcher_users.pop(idle_key, None)
                    break
                await self._browser_pool_condition.wait()
            if cache_key not in self._browser_fetchers:
                self._browser_fetchers[cache_key] = self._new_browser_fetcher(proxy)
                self._browser_fetcher_users[cache_key] = 0
            self._browser_fetchers.move_to_end(cache_key)
            self._browser_fetcher_users[cache_key] += 1
            fetcher = self._browser_fetchers[cache_key]
        if evicted is not None:
            await self._close_browser_quietly(evicted)
        return cache_key, fetcher

    async def _release_browser_fetcher(self, cache_key: str) -> None:
        async with self._browser_pool_condition:
            users = self._browser_fetcher_users.get(cache_key, 0)
            if users > 0:
                self._browser_fetcher_users[cache_key] = users - 1
            self._browser_pool_condition.notify_all()

    @staticmethod
    async def _close_browser_quietly(fetcher: BrowserFetcher) -> None:
        try:
            await fetcher.close()
        except Exception as exc:
            logger.warning("browser cleanup failed: %s", type(exc).__name__)

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
        confidence: ConfidenceReport | None = None,
        include_structure: bool = False,
        compact_structure: bool = False,
    ) -> FetchResult:
        # Build the structural summary from the unmodified DOM before the
        # processing pipeline mutates the soup. This avoids the cleaner
        # dropping the very nodes the structural extractor needs to report
        # without paying for an extra DOM copy.
        structure_source = soup if soup is not None else html
        limits = StructureLimits(compact=compact_structure) if compact_structure else None
        structure = (
            extract_structure(structure_source, final_url, limits=limits) if include_structure else None
        )
        try:
            processed = process_html(
                html,
                final_url,
                response_headers,
                soup=soup,
                confidence=confidence,
                cleaning_level=self.config.cleaning_level,
            )
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
            structure=structure,
            fetch_method=method,
            proxy_provider=proxy,
            content_confidence=processed.confidence.score,
            fetched_at=datetime.now(UTC),
            warnings=processed.warnings,
        )

    def _result_from_pdf(self, url: str, response: HTTPResponse, proxy: str) -> FetchResult:
        try:
            doc = process_pdf(response.content)
        except MissingOptionalDependency as exc:
            raise TransportFailure(
                FetchErrorInfo("missing_dependency", str(exc), False, type(exc).__name__)
            ) from exc
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
        if content_type in ("application/xml", "text/xml"):
            return True
        # Match XML-based subtypes like application/atom+xml, image/svg+xml
        # but NOT application/xhtml+xml (which is HTML5, not generic XML).
        return "+xml" in content_type and content_type != "application/xhtml+xml"

    @staticmethod
    def _is_html_like(content_type: str) -> bool:
        """Return True when *content_type* should be processed as HTML."""
        return content_type in ("text/html", "application/xhtml+xml")

    @staticmethod
    def _looks_like_html(content: bytes) -> bool:
        """Heuristic HTML sniff for responses with ambiguous content types."""
        stripped = content.lstrip()
        if not stripped:
            return False
        return stripped.startswith(b"<") and any(
            marker in stripped[:512].lower()
            for marker in (b"<!doctype html", b"<html", b"<head", b"<body", b"<title", b"<meta", b"<div", b"<p", b"<a ")
        )

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
            duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )
