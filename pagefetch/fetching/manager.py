"""Transport lifecycle, proxy routing, and fetcher pooling."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from hashlib import md5
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import PageFetchConfig, merge_headers
from ..constants import _UA_POOL_BY_OS, BROWSER_HEADERS
from ..cookies import CookieSession
from ..models import FetchResult
from ..processing.pipeline import build_non_html_result, is_pdf_content
from ..proxy import ProxySettings, resolve_proxy
from ..proxy.pool import ProxyTransportPool
from ..proxy.providers import (
    RESIDENTIAL_PROVIDERS,
    accept_language_for_country,
    infer_target_country,
    inject_session_id_for,
    is_government_or_restricted_target,
    make_domain_session,
    make_random_session,
)
from ..utils.urls import registrable_host
from .browser import BrowserFetcher, BrowserResponse
from .http import HTTPFetcher

logger = logging.getLogger("pagefetch")

_RESOURCE_CLOSE_TIMEOUT = 0.5

if sys.platform == "win32":
    _HOST_OS = "windows"
elif sys.platform == "darwin":
    _HOST_OS = "macos"
else:
    _HOST_OS = "linux"


class TransportManager:
    """Owns shared HTTP transport pooling, BrowserFetcher instances, and drain teardown."""

    def __init__(self, config: PageFetchConfig) -> None:
        self.config = config
        self.http_semaphore = asyncio.Semaphore(config.http_concurrency)
        self.browser_semaphore = asyncio.Semaphore(config.browser_concurrency)
        self._direct_transport: httpx.AsyncHTTPTransport | None = None
        self._http_fetcher: HTTPFetcher | None = None
        self._proxy_transports = ProxyTransportPool()
        self._browser_fetchers: dict[str, BrowserFetcher] = {}
        self._browser_init_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._closing = False
        self._active_fetches = 0
        self._active_fetches_lock = asyncio.Lock()
        self._drain_event = asyncio.Event()
        self._drain_event.set()

    @property
    def lifecycle_lock(self) -> asyncio.Lock:
        return self._lifecycle_lock

    @property
    def is_closed(self) -> bool:
        return self._closed or self._closing

    async def begin_operation(self) -> bool:
        """Register an active operation unless teardown has already begun."""
        async with self._lifecycle_lock:
            if self._closed or self._closing:
                return False
            async with self._active_fetches_lock:
                self._active_fetches += 1
                self._drain_event.clear()
        return True

    async def finish_operation(self) -> None:
        """Unregister an active operation and signal drain waiters when zero."""
        async with self._active_fetches_lock:
            self._active_fetches -= 1
            if self._active_fetches <= 0:
                self._active_fetches = 0
                self._drain_event.set()

    async def close(self, *, cache: Any = None) -> None:
        """Drain in-flight fetches and release all transport and cache resources."""
        async with self._lifecycle_lock:
            if self._closed:
                return
            if self._close_task is None:
                self._closing = True
                self._close_task = asyncio.create_task(self._teardown(cache=cache))
            close_task = self._close_task

        await asyncio.shield(close_task)

    async def _teardown(self, *, cache: Any = None) -> None:
        """Run the client-owned resource teardown to completion."""
        try:
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
                await asyncio.gather(
                    *(
                        self._close_resource(browser.close)
                        for browser in self._browser_fetchers.values()
                    ),
                    return_exceptions=True,
                )
                if self._direct_transport is not None:
                    await self._close_resource(self._direct_transport.aclose)
                proxy_results = await self._proxy_transports.aclose_all()
                for failure in proxy_results:
                    if isinstance(failure, Exception):
                        logger.warning("resource cleanup failed: %s", type(failure).__name__)
                if cache is not None:
                    await self._close_resource(cache.close)
                self._browser_fetchers.clear()
                self._http_fetcher = None
                self._direct_transport = None
                self._proxy_transports.clear()
                self._closed = True
                self._closing = False
        except asyncio.CancelledError:
            async with self._lifecycle_lock:
                self._closing = False
                self._close_task = None
            raise

    @staticmethod
    async def _close_resource(close_fn: Callable[[], Awaitable[Any]]) -> None:
        """Close one transport without allowing a hung child to stall teardown."""
        try:
            await asyncio.wait_for(close_fn(), timeout=_RESOURCE_CLOSE_TIMEOUT)
        except TimeoutError:
            logger.warning("resource cleanup timed out after %.1f seconds", _RESOURCE_CLOSE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.warning("resource cleanup failed: %s", type(exc).__name__)

    def _pool_limits(self) -> httpx.Limits:
        return httpx.Limits(
            max_connections=self.config.http_concurrency * 2,
            max_keepalive_connections=self.config.http_concurrency,
        )

    def _acquire_proxy_transport(self, proxy_url: str) -> httpx.AsyncHTTPTransport:
        cached = self._proxy_transports.get(proxy_url)
        if cached is None:
            cached = httpx.AsyncHTTPTransport(
                http2=True,
                proxy=proxy_url,
                limits=self._pool_limits(),
            )
            self._proxy_transports.put(proxy_url, cached)
        self._proxy_transports.retain(cached)
        return cached

    async def get_http_fetcher(self, provider: str) -> HTTPFetcher:
        """Validate *provider* credentials and return the shared :class:`HTTPFetcher`."""
        resolve_proxy(provider)
        if self._http_fetcher is None:
            self._direct_transport = httpx.AsyncHTTPTransport(
                http2=True,
                limits=self._pool_limits(),
            )
            self._http_fetcher = HTTPFetcher(
                self._direct_transport,
                self.http_semaphore,
                retries=self.config.retries_http,
                max_content_size=self.config.max_content_size,
                max_redirects=self.config.max_redirects,
                timeout=self.config.http_timeout,
                default_headers=BROWSER_HEADERS,
                proxy_transport_provider=self._acquire_proxy_transport,
                proxy_transport_releaser=self._proxy_transports.release,
            )
        return self._http_fetcher

    def headers_for_url(
        self,
        url: str,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build domain-consistent OS headers merged with config and call overrides."""
        domain = urlsplit(url).hostname or url
        pool = _UA_POOL_BY_OS[_HOST_OS]
        pool_idx = int(md5(domain.encode()).hexdigest()[:8], 16) % len(pool)
        target_country = infer_target_country(url)
        base = {
            **BROWSER_HEADERS,
            "User-Agent": pool[pool_idx],
            "Accept-Language": accept_language_for_country(
                target_country, self.config.accept_language
            ),
        }
        return merge_headers(base, self.config.headers, extra_headers)

    def resolve_proxy_url(self, provider: str, url: str, salt: str = "") -> str | None:
        """Return the per-request proxy URL for *provider* under current rotation policy."""
        if provider == "none":
            return None
        settings = resolve_proxy(provider)
        if not settings.url:
            return None
        if provider == "custom":
            return settings.url
        if provider in RESIDENTIAL_PROVIDERS and is_government_or_restricted_target(url):
            logger.debug("Bypassing residential proxy for government/restricted target: %s", url)
            return None
        domain = registrable_host(url) or url
        country = infer_target_country(url)
        session_id = (
            make_random_session()
            if self.config.session_rotation == "rotate"
            else make_domain_session(domain, salt=salt)
        )
        return inject_session_id_for(
            provider,
            settings.url,
            session_id,
            session_duration_seconds=self.config.session_duration,
            country=country,
        )

    def _new_browser_fetcher(self, proxy: ProxySettings) -> BrowserFetcher:
        return BrowserFetcher(
            self.browser_semaphore,
            timeout=self.config.browser_timeout,
            retries=self.config.retries_browser,
            proxy=proxy,
            max_content_size=self.config.max_content_size,
            browser_pre_check_byte_margin=self.config.browser_pre_check_byte_margin,
            confidence_threshold=self.config.confidence_threshold,
            block_images=self.config.block_images,
            block_level=self.config.block_level,
            humanize=self.config.humanize,
            accept_language=self.config.accept_language,
            max_browser_requests=self.config.max_browser_requests,
            idle_ttl=self.config.browser_idle_ttl,
            max_scroll_steps=self.config.max_scroll_steps,
            max_scroll_height=self.config.max_scroll_height,
        )

    async def get_browser_fetcher(
        self,
        provider: str,
        url: str,
        salt: str = "",
    ) -> tuple[ProxySettings, BrowserFetcher]:
        """Return ``(ProxySettings, BrowserFetcher)`` for *provider*, initializing lazily."""
        proxy_url = self.resolve_proxy_url(provider, url, salt=salt)
        # Bypassed targets (.gov/.mil) must use the direct browser process rather
        # than inheriting a proxied BrowserFetcher's global browser proxy config.
        fetcher_key = provider if proxy_url else "none"
        proxy = ProxySettings(provider=fetcher_key, url=proxy_url)
        fetcher = self._browser_fetchers.get(fetcher_key)
        if fetcher is not None:
            return proxy, fetcher
        async with self._browser_init_lock:
            fetcher = self._browser_fetchers.get(fetcher_key)
            if fetcher is None:
                fetcher = self._new_browser_fetcher(proxy)
                self._browser_fetchers[fetcher_key] = fetcher
        return proxy, fetcher

    async def recover_browser_pdf(
        self,
        *,
        url: str,
        response: BrowserResponse,
        proxy: str,
        headers: dict[str, str] | None,
        cookie_session: CookieSession,
    ) -> FetchResult | None:
        """Recover structured PDF text via HTTP when a browser page renders a PDF.js viewer."""
        is_pdf_target = is_pdf_content("", b"", url=response.url) or is_pdf_content("", b"", url=url)
        is_pdf_viewer_dom = bool(
            "sidebarContainer" in response.html
            and "viewerContainer" in response.html
            and ("findbar" in response.html or "secondaryToolbar" in response.html)
        )
        if not (is_pdf_target or is_pdf_viewer_dom):
            return None
        try:
            target_url = (
                response.url
                if response.url and not response.url.startswith(("chrome:", "resource:", "about:"))
                else url
            )
            http_fetcher = await self.get_http_fetcher(proxy)
            per_request_proxy = self.resolve_proxy_url(proxy, target_url)
            http_resp = await http_fetcher.fetch(
                target_url,
                proxy_url=per_request_proxy,
                headers=self.headers_for_url(target_url, extra_headers=headers),
                cookie_profile=cookie_session.current,
                initial_site=registrable_host(target_url) or None,
            )
            if is_pdf_content(http_resp.headers.get("Content-Type", ""), http_resp.content, url=target_url):
                cookie_session.finalize(success=True)
                pdf_result = build_non_html_result("pdf", url, http_resp, proxy)
                assert pdf_result is not None
                if response.screenshot:
                    pdf_result.screenshot = response.screenshot
                    pdf_result.screenshot_format = response.screenshot_format
                pdf_result.actions_executed = response.actions_executed
                pdf_result.xhr_responses = response.xhr_responses
                if response.warnings:
                    pdf_result.warnings.extend(response.warnings)
                return pdf_result
        except Exception as exc:
            logger.warning("Failed to recover PDF via HTTP in browser fetch: %s", exc)
        return None
