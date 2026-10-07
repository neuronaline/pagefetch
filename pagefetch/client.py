"""Main PageFetch client and fetch pipeline."""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from hashlib import md5
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .cache import SQLiteCache, build_cache_key
from .config import _UNSET, VALID_MODES, VALID_PROXIES, PageFetchConfig
from .constants import (
    _UA_POOL_BY_OS,
    BLOCKED_STATUS_CODES,
    BROWSER_HEADERS,
    RETRYABLE_STATUS_CODES,
    SAFE_RESPONSE_HEADERS,
)
from .exceptions import PageFetchError
from .fetching import BrowserFetcher, HTTPFetcher, HTTPResponse, TransportFailure
from .models import FetchErrorInfo, FetchResult
from .processing.detector import ConfidenceReport, analyze_html
from .processing.pipeline import (
    build_csv_result,
    build_document_result,
    build_docx_result,
    build_html_result,
    build_json_result,
    build_pdf_result,
    build_text_result,
    build_xml_result,
    decode_response_body,
    detect_document_kind,
    is_html_like,
    is_pdf_content,
    is_xml_content,
    looks_like_html,
    parse_content_type,
)
from .proxy import ProxyConfigurationError, ProxySettings, resolve_proxy
from .proxy.pool import _MAX_PROXY_HTTP_CLIENTS, _ProxyClientPool
from .proxy.providers import (
    inject_session_id_for,
    make_domain_session,
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


class _AsyncPacer:
    """Async token-bucket pacer for the ``request_pacing`` bot-evasion signal.

    The previous implementation interleaved ``asyncio.sleep`` with
    ``create_task`` in :meth:`PageFetch.fetch_many`, serialising task
    creation and starving the semaphore when many URLs were queued. This
    pacer lets every task be created immediately while still guaranteeing
    a uniformly distributed inter-request gap: each call to
    :meth:`acquire` reserves a slot at ``now + random.uniform(0, max_delay)``
    and sleeps until that slot. Slot reservation is lock-guarded but the
    actual sleep happens outside the lock so concurrent acquires do not
    block each other once they have their slot.
    """

    __slots__ = ("_max_delay", "_lock", "_next_release")

    def __init__(self, max_delay: float) -> None:
        if max_delay < 0:
            raise ValueError("max_delay must be non-negative")
        self._max_delay = float(max_delay)
        self._lock = asyncio.Lock()
        self._next_release = time.monotonic()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            base = max(now, self._next_release)
            target = base + random.uniform(0, self._max_delay)
            self._next_release = target
        delay = base - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


class PageFetch:
    """Asynchronous HTTP-first page fetcher with automatic Camoufox fallback.

    The client may be used as an async context manager. Calling :meth:`fetch`
    without an explicit context also starts resources lazily; call :meth:`close`
    when finished in that case.
    """

    def __init__(
        self,
        *,
        config: PageFetchConfig | None = None,
        mode: Literal["auto", "http", "browser"] = "auto",
        proxy: Literal["none", "custom", "decodo", "byteful"] = "none",
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
        block_images: Any = _UNSET,
        block_level: Literal["minimal", "balanced", "aggressive"] | None = None,
        accept_language: str = "en-US,en;q=0.5",
        humanize: bool | None = None,
        session_rotation: Literal["sticky", "rotate"] | None = None,
        session_duration: str | int | None = None,
        request_pacing: float | None = None,
        stealth_level: Literal["off", "balanced", "max"] = "off",
        raise_on_error: bool = False,
        screenshot_max_bytes: int = 50 * 1024 * 1024,
        browser_pre_check_byte_margin: float = 1.5,
    ) -> None:
        if config is not None:
            if not isinstance(config, PageFetchConfig):
                raise TypeError(f"config must be an instance of PageFetchConfig, got {type(config).__name__}")
            self.config = config
        else:
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
                session_duration=session_duration,
                request_pacing=request_pacing,
                stealth_level=stealth_level,
                raise_on_error=raise_on_error,
                screenshot_max_bytes=screenshot_max_bytes,
                browser_pre_check_byte_margin=browser_pre_check_byte_margin,
            )
        self._http_semaphore = asyncio.Semaphore(self.config.http_concurrency)
        self._browser_semaphore = asyncio.Semaphore(self.config.browser_concurrency)
        self._http_clients: dict[str, httpx.AsyncClient] = {}
        self._http_fetchers: dict[str, HTTPFetcher] = {}
        # Per-proxy-URL httpx clients (Phase 1 Item 3, hardened for sticky
        # residential proxies). Residential gateways in ``sticky`` mode
        # inject a per-domain session token into the proxy URL, so without
        # a bound every distinct domain would mint a new client — and
        # every client keeps its own connection pool and keep-alive
        # sockets alive. ``_ProxyClientPool`` enforces a hard LRU ceiling
        # and schedules ``aclose()`` on eviction so we never blow past
        # ``EMFILE``.
        self._proxy_http_clients: _ProxyClientPool = _ProxyClientPool()
        # Phase 3 Item 7: pool key is just the provider name ("none",
        # "custom", "decodo", "byteful") so a plain dict suffices — at most
        # four entries can ever exist.  Concurrency is bounded by
        # ``_browser_semaphore``; the previous LRU/``asyncio.Condition``
        # bookkeeping only added deadlock risk during ``close()`` without
        # providing extra throughput.  ``_browser_init_lock`` is the only
        # lock needed — it serializes the first ``new_browser_fetcher``
        # call when two coroutines race for the same provider key.
        self._browser_fetchers: dict[str, BrowserFetcher] = {}
        self._browser_init_lock = asyncio.Lock()
        self._http_init_lock = asyncio.Lock()
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

    @classmethod
    def from_config(cls, config: PageFetchConfig) -> PageFetch:
        """Create a PageFetch client with an existing PageFetchConfig instance."""
        return cls(config=config)

    @classmethod
    def from_yaml(cls, path: str | Path) -> PageFetch:
        """Create a PageFetch client from a YAML configuration file."""
        return cls(config=PageFetchConfig.from_yaml(path))

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
        if cfg.session_duration is not None:
            parts.append(f"session_ttl={cfg.session_duration}s")
        parts.append(f"lang={cfg.accept_language}")
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
                proxy_results = await self._proxy_http_clients.aclose_all()
                for failure in (*browser_results, *client_results, *proxy_results):
                    if isinstance(failure, Exception):
                        logger.warning("resource cleanup failed: %s", type(failure).__name__)
                if self._cache:
                    await self._close_resource(self._cache.close)
                self._browser_fetchers.clear()
                self._http_fetchers.clear()
                self._http_clients.clear()
                self._proxy_http_clients.clear()
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
        proxy: Literal["none", "custom", "decodo", "byteful"] | None = None,

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
        proxy: Literal["none", "custom", "decodo", "byteful"] | None = None,

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

        # Bound the fanout of ``fetch_many`` so a single batch with thousands
        # of URLs cannot create that many concurrent cache reads against the
        # shared SQLite connection (which aiosqlite serializes internally,
        # queueing them on its per-connection lock) nor thousands of
        # ``FetchResult`` instances alive at once. The gate is the looser of
        # the two transport semaphores plus a small buffer for cache hits
        # that never reach the network.
        fanout_limit = max(
            self.config.http_concurrency,
            self.config.browser_concurrency,
        ) * 2
        fanout_sem = asyncio.Semaphore(fanout_limit)

        pacer: _AsyncPacer | None = None
        if self.config.request_pacing > 0 and len(unique) > 1:
            # Build a single pacer that distributes the inter-request delay
            # across the whole batch instead of serialising task creation.
            pacer = _AsyncPacer(self.config.request_pacing)

        async def run_one(item: str) -> FetchResult:
            if pacer is not None:
                await pacer.acquire()
            async with fanout_sem:
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

        fetched = await asyncio.gather(*(run_one(item) for item in unique))
        by_url = dict(zip(unique, fetched, strict=True))
        # Duplicate URLs need independent copies: ``FetchResult`` holds
        # mutable containers (``links``, ``images``, ``metadata``,
        # ``warnings``) and callers must be able to mutate one entry without
        # aliasing the rest. ``FetchResult.clone()`` shares the immutable
        # payloads (HTML, screenshot bytes, structure) by reference and only
        # shallow-copies the mutable containers — far cheaper than
        # :func:`copy.deepcopy`, which used to walk the entire DOM tree and
        # block the event loop for hundreds of milliseconds.
        results: list[FetchResult] = []
        emitted: set[int] = set()
        for item in ordered:
            result = by_url[item]
            if id(result) in emitted:
                result = result.clone()
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
        proxy: Literal["none", "custom", "decodo", "byteful"] | None = None,

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
            "accept_language": self.config.accept_language,
            "block_images": self.config.block_images,
            "block_level": self.config.block_level,
            "cleaning_level": self.config.cleaning_level,
            "confidence_threshold": self.config.confidence_threshold,
            "humanize": self.config.humanize,
            "max_redirects": self.config.max_redirects,
            "session_rotation": self.config.session_rotation,
            # ``session_duration`` only affects how the residential proxy
            # URL is constructed; a cached response is technically the
            # same content either way.  We still include it in the cache
            # key so that callers who toggle TTL between fetches see
            # fresh upstream requests rather than stale hits, mirroring
            # the existing ``session_rotation`` treatment above.
            "session_duration": self.config.session_duration,
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
            else:
                # Stamp duration on the successful path. ``_finish_error``
                # already populates ``duration_ms`` on the error branches and
                # the cache-hit branch above sets it from ``started_at``;
                # leaving it unset here caused every fresh fetch to persist
                # ``duration_ms = None`` into the SQLite cache.
                if result.duration_ms is None:
                    result.duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
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

    async def _escalate_to_browser(
        self,
        url: str,
        proxy: str,
        status_code: int | None = None,
    ) -> FetchResult:
        # Auto-mode double-hit softening delay
        await asyncio.sleep(random.uniform(0.5, 3.0))
        return await self._fetch_browser(url, proxy, status_code=status_code)

    def _degraded_http_result(
        self,
        url: str,
        response: HTTPResponse,
        html: str,
        raw_soup: BeautifulSoup,
        report: ConfidenceReport,
        content_type: str,
        proxy: str,
        reason: str,
    ) -> FetchResult:
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
        available.warnings.extend([reason, "Content may be incomplete."])
        return available

    async def _fetch_http_or_auto(
        self,
        url: str,
        mode: Literal["auto", "http", "browser"],
        proxy: str,
    ) -> FetchResult:
        try:
            fetcher = await self._http_fetcher(proxy)
            per_request_proxy = self._resolve_proxy_url(proxy, url)
            retry_codes = (
                RETRYABLE_STATUS_CODES - {429}
                if mode == "auto"
                else RETRYABLE_STATUS_CODES
            )
            response = await fetcher.fetch(
                url,
                proxy_url=per_request_proxy,
                headers=self._headers_for_url(url),
                retryable_status_codes=retry_codes,
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
            escalate_to_browser = mode == "auto" and response.status_code in BLOCKED_STATUS_CODES
            # Cloudflare "Under Attack Mode" returns HTTP 503 with a JavaScript
            # challenge body — that 503 is *not* a transient outage, it is an
            # anti-bot gate.  Detect it by sniffing the body for WAF markers and
            # escalate only when those markers are present, so genuine 503
            # outages still fail fast at the HTTP layer.
            if (
                not escalate_to_browser
                and mode == "auto"
                and response.status_code == 503
                and self._body_looks_like_waf_challenge(response.content)
            ):
                escalate_to_browser = True
            if escalate_to_browser:
                logger.info("HTTP %s; using browser for %s", response.status_code, url)
                return await self._escalate_to_browser(
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
        kind = detect_document_kind(content_type, response.content, url=url)
        if kind == "pdf":
            return self._result_from_pdf(url, response, proxy)
        if kind == "docx":
            return self._result_from_docx(url, response, proxy)
        if kind in ("csv", "tsv"):
            return self._result_from_csv(
                url, response, proxy, delimiter="\t" if kind == "tsv" else None
            )
        if kind == "json":
            return self._result_from_json(url, response, proxy)
        if kind == "xml":
            return self._result_from_xml(url, response, proxy)
        if kind == "text":
            return self._result_from_text(url, response, proxy)
        if kind != "html":
            if mode == "auto":
                logger.info(
                    "HTTP returned %s; using browser for %s", content_type, url
                )
                return await self._escalate_to_browser(
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
            try:
                rendered = await self._escalate_to_browser(
                    url,
                    proxy,
                    status_code=response.status_code,
                )
            except TransportFailure:
                return self._degraded_http_result(
                    url,
                    response,
                    html,
                    raw_soup,
                    report,
                    content_type,
                    proxy,
                    "Browser rendering failed; showing basic HTTP version instead.",
                )
            if not rendered.success:
                return self._degraded_http_result(
                    url,
                    response,
                    html,
                    raw_soup,
                    report,
                    content_type,
                    proxy,
                    "Browser rendered content was not usable; showing HTTP version instead.",
                )
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
        cache_key, proxy_settings, fetcher = await self._acquire_browser_fetcher(proxy, url)
        try:
            response = await fetcher.fetch(
                url,
                proxy=proxy_settings,
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
                # ``follow_redirects=False`` lets ``HTTPFetcher._request``
                # intercept every redirect response so each ``Location``
                # header is validated by the SSRF guard before the next
                # network hop (Phase 1 Item 2).
                client = httpx.AsyncClient(
                    headers=BROWSER_HEADERS,
                    timeout=httpx.Timeout(self.config.http_timeout),
                    follow_redirects=False,
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
                    max_redirects=self.config.max_redirects,
                    proxy_client_provider=self._proxy_client_provider,
                )
        return self._http_fetchers[cache_key]

    async def _proxy_client_provider(self, proxy_url: str) -> httpx.AsyncClient:
        """Return a pooled ``httpx.AsyncClient`` keyed by *proxy_url*.

        Phase 1 Item 3: the previous implementation created and tore down a
        fresh client per proxied request, paying a TCP/TLS handshake on every
        URL and exhausting ``TIME_WAIT`` sockets.  Pooling by proxy URL keeps
        the keep-alive socket warm and lets HTTP/2 multiplexing work across
        requests to the same exit.  Residential proxies in ``sticky`` mode
        inject a per-domain session token, so the pool is bounded by an
        LRU ceiling (``_ProxyClientPool``) to avoid file-descriptor leaks.
        Lifecycle is owned by ``PageFetch.close``; ``_teardown`` awaits
        ``aclose_all`` on every client this provider hands out plus any
        eviction tasks scheduled by the LRU.
        """
        cached = self._proxy_http_clients.get(proxy_url)
        if cached is not None:
            return cached
        async with self._http_init_lock:
            cached = self._proxy_http_clients.get(proxy_url)
            if cached is not None:
                return cached
            client = httpx.AsyncClient(
                headers=BROWSER_HEADERS,
                timeout=httpx.Timeout(self.config.http_timeout),
                follow_redirects=False,
                max_redirects=self.config.max_redirects,
                http2=True,
                proxy=proxy_url,
                limits=httpx.Limits(
                    max_connections=self.config.http_concurrency * 2,
                    max_keepalive_connections=self.config.http_concurrency,
                ),
            )
            self._proxy_http_clients.put(proxy_url, client)
            return client

    def _headers_for_url(self, url: str) -> dict[str, str]:
        headers = dict(BROWSER_HEADERS)
        domain = urlsplit(url).hostname or url
        # Pick the User-Agent from the pool matching the host OS so outgoing
        # HTTP requests declare an OS consistent with the runtime; the browser
        # fallback sets its own UA independently of these headers.
        pool = _UA_POOL_BY_OS[_HOST_OS]
        pool_idx = int(md5(domain.encode()).hexdigest()[:8], 16) % len(pool)
        headers["User-Agent"] = pool[pool_idx]
        headers["Accept-Language"] = self.config.accept_language
        return headers

    def _resolve_proxy_url(self, provider: str, url: str) -> str | None:
        """Return the per-request proxy URL for *provider* under the current rotation policy.

        - ``none`` returns ``None`` (no proxy).
        - ``custom`` and residential providers in ``rotate`` mode return the
          configured URL verbatim — self-hosted proxies have no concept of
          session affinity, and residential gateways rotate the egress IP on
          every request when no session token is appended.
        - Residential providers in ``sticky`` mode get a domain-stable
          session ID embedded in the username so the same exit is reused
          across requests for the same site.  When the config carries a
          ``session_duration``, the matching documented TTL token is also
          appended (``-sessionduration-<minutes>`` for Decodo,
          ``_ttl_<n><unit>`` for Byteful — DECODO_DOCS §4, BYTEFUL_DOCS §4).

        Used by both the HTTP and browser transports so the two paths cannot
        drift on rotation semantics.
        """
        if provider == "none":
            return None
        settings = resolve_proxy(provider)
        if not settings.url:
            return None
        if provider == "custom" or self.config.session_rotation == "rotate":
            return settings.url
        domain = registrable_host(url) or url
        return inject_session_id_for(
            provider,
            settings.url,
            make_domain_session(domain),
            session_duration_seconds=self.config.session_duration,
        )

    def _browser_pool_target(self, provider: str, url: str) -> tuple[str, ProxySettings]:
        proxy_url = self._resolve_proxy_url(provider, url)
        return provider, ProxySettings(provider=provider, url=proxy_url)

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
        )

    async def _acquire_browser_fetcher(
        self,
        provider: str,
        url: str,
    ) -> tuple[str, ProxySettings, BrowserFetcher]:
        """Return a long-lived :class:`BrowserFetcher` for *provider*.

        Phase 3 Item 7: the pool is keyed only by ``provider`` so a plain
        ``dict`` lookup is enough. Concurrency is bounded by
        ``self._browser_semaphore``; this method only guarantees that two
        coroutines racing for the same key do not double-spawn a browser.
        """
        cache_key, proxy = self._browser_pool_target(provider, url)
        fetcher = self._browser_fetchers.get(cache_key)
        if fetcher is not None:
            return cache_key, proxy, fetcher
        async with self._browser_init_lock:
            fetcher = self._browser_fetchers.get(cache_key)
            if fetcher is None:
                fetcher = self._new_browser_fetcher(proxy)
                self._browser_fetchers[cache_key] = fetcher
        return cache_key, proxy, fetcher

    async def _release_browser_fetcher(self, cache_key: str) -> None:
        # ``_browser_semaphore`` already enforces concurrency; the pool no
        # longer tracks per-key user counts or evicts idle fetchers, so the
        # release is a no-op kept as a single funnel for future hooks.
        return None

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
        return build_html_result(
            original_url=original_url,
            final_url=final_url,
            status_code=status_code,
            html=html,
            content_type=content_type,
            encoding=encoding,
            proxy=proxy,
            method=method,
            cleaning_level=self.config.cleaning_level,
            response_headers=response_headers,
            soup=soup,
            confidence=confidence,
            include_structure=include_structure,
            compact_structure=compact_structure,
        )

    def _result_from_pdf(self, url: str, response: HTTPResponse, proxy: str) -> FetchResult:
        return build_pdf_result(url, response, proxy)

    def _result_from_docx(self, url: str, response: HTTPResponse, proxy: str) -> FetchResult:
        return build_docx_result(url, response, proxy)

    def _result_from_csv(
        self,
        url: str,
        response: HTTPResponse,
        proxy: str,
        delimiter: str | None = None,
    ) -> FetchResult:
        return build_csv_result(url, response, proxy, delimiter=delimiter)

    def _result_from_json(self, url: str, response: HTTPResponse, proxy: str) -> FetchResult:
        return build_json_result(url, response, proxy)

    def _result_from_xml(self, url: str, response: HTTPResponse, proxy: str) -> FetchResult:
        return build_xml_result(url, response, proxy)

    def _result_from_text(self, url: str, response: HTTPResponse, proxy: str) -> FetchResult:
        return build_text_result(url, response, proxy)

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
        return build_document_result(
            url, response, proxy, doc, method, content_type, raw_source=raw_source
        )

    @staticmethod
    def _decode(response: HTTPResponse) -> str:
        return decode_response_body(response)

    @staticmethod
    def _content_type(header: str | None) -> str:
        return parse_content_type(header)

    @staticmethod
    def _is_pdf(content_type: str, content: bytes) -> bool:
        return is_pdf_content(content_type, content)

    @staticmethod
    def _is_xml(content_type: str) -> bool:
        return is_xml_content(content_type)

    @staticmethod
    def _is_html_like(content_type: str) -> bool:
        return is_html_like(content_type)

    @staticmethod
    def _looks_like_html(content: bytes) -> bool:
        return looks_like_html(content)


    # Sniff markers for "Under Attack Mode" / interstitial WAF challenges
    # that arrive with a 503 (or occasionally a 403) status.  Cheap substring
    # checks against the first ~4 KiB — we never re-parse this body.
    _WAF_CHALLENGE_MARKERS = (
        b"cf-chl-bypass",
        b"cf_chl_opt",
        b"__cf_chl_jschl_tk__",
        b"challenge-running",
        b"checking your browser",
        b"verify you are human",
        b"just a moment",
        b"attention required",
        b"access denied",
        b"akamai bot manager",
        b"perimeterx",
        b"datadome",
        b"kasada",
        b"px-captcha",
    )

    @classmethod
    def _body_looks_like_waf_challenge(cls, content: bytes) -> bool:
        """Return True when *content* carries WAF challenge markers.

        Only the first 4 KiB are scanned — challenge pages embed the marker
        near the top, and the head bytes are already in memory after the
        content-type / status decision.
        """
        if not content:
            return False
        head = content[:4096].lower()
        return any(marker in head for marker in cls._WAF_CHALLENGE_MARKERS)

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
