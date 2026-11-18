"""Browser transport lifecycle, proxy routing, and non-HTML document extraction."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import PageFetchConfig
from ..cookies import CookieSession
from ..exceptions import TransportFailure
from ..models import FetchErrorInfo, FetchResult
from ..processing.pipeline import (
    DocumentPayload,
    build_non_html_result,
    decode_response_body,
    detect_document_kind,
    parse_content_type,
)
from ..proxy import ProxySettings, resolve_proxy
from ..proxy.providers import (
    RESIDENTIAL_PROVIDERS,
    infer_target_country,
    inject_session_id_for,
    is_government_or_restricted_target,
    make_domain_session,
    make_random_session,
)
from ..utils.urls import registrable_host
from .browser import BrowserFetcher, BrowserResponse

logger = logging.getLogger("pagefetch")

_BROWSER_CLOSE_TIMEOUT = 5.0
_CACHE_CLOSE_TIMEOUT = 1.0


class TransportManager:
    """Owns BrowserFetcher pooling, proxy routing, and drain teardown."""

    def __init__(self, config: PageFetchConfig) -> None:
        self.config = config
        self.browser_semaphore = asyncio.Semaphore(config.concurrency)
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
        """Drain in-flight fetches and release all browser and cache resources."""
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
                        self._close_resource(browser.close, timeout=_BROWSER_CLOSE_TIMEOUT)
                        for browser in self._browser_fetchers.values()
                    ),
                    return_exceptions=True,
                )
                if cache is not None:
                    await self._close_resource(cache.close, timeout=_CACHE_CLOSE_TIMEOUT)
                self._browser_fetchers.clear()
                self._closed = True
                self._closing = False
        except asyncio.CancelledError:
            async with self._lifecycle_lock:
                self._closing = False
                self._close_task = None
            raise

    @staticmethod
    async def _close_resource(
        close_fn: Callable[[], Awaitable[Any]],
        *,
        timeout: float = _BROWSER_CLOSE_TIMEOUT,
    ) -> None:
        """Close one resource without allowing a hung child to stall teardown."""
        try:
            await asyncio.wait_for(close_fn(), timeout=timeout)
        except TimeoutError:
            logger.warning("resource cleanup timed out after %.1f seconds", timeout)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.warning("resource cleanup failed: %s", type(exc).__name__)

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
            timeout=self.config.timeout,
            retries=self.config.retries,
            proxy=proxy,
            max_content_size=self.config.max_content_size,
            pre_check_byte_margin=self.config.pre_check_byte_margin,
            confidence_threshold=self.config.confidence_threshold,
            block_images=self.config.block_images,
            block_level=self.config.block_level,
            humanize=self.config.humanize,
            accept_language=self.config.accept_language,
            max_requests=self.config.max_requests,
            idle_ttl=self.config.idle_ttl,
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

    @staticmethod
    def _extract_charset(content_type_header: str | None) -> str | None:
        if not content_type_header or "charset=" not in content_type_header.lower():
            return None
        for part in content_type_header.split(";")[1:]:
            key, sep, val = part.partition("=")
            if sep and key.strip().lower() == "charset":
                cleaned = val.strip().strip('"').strip("'")
                if cleaned:
                    return cleaned
        return None

    @staticmethod
    def _header_value(headers: dict[str, str], name: str, default: str = "") -> str:
        target = name.lower()
        for key, value in headers.items():
            if str(key).lower() == target:
                return str(value)
        return default

    def extract_browser_document(
        self,
        *,
        url: str,
        response: BrowserResponse,
        proxy: str,
        cookie_session: CookieSession,
    ) -> FetchResult | None:
        """Convert captured non-HTML browser bytes (inline or downloaded) into a FetchResult."""
        if response.raw_content is None:
            return None
        target_url = (
            response.url
            if response.url and not response.url.startswith(("chrome:", "resource:", "about:"))
            else url
        )
        raw_headers = dict(response.headers or {})
        if response.content_type and not self._header_value(raw_headers, "content-type"):
            raw_headers["Content-Type"] = response.content_type
        raw_ct = self._header_value(raw_headers, "content-type", response.content_type or "")
        ct = parse_content_type(raw_ct)
        kind = detect_document_kind(
            ct,
            response.raw_content,
            url=target_url,
            filename=response.suggested_filename,
        )
        payload = DocumentPayload(
            url=target_url,
            status_code=response.status_code or 200,
            headers=raw_headers,
            content=response.raw_content,
            encoding=self._extract_charset(raw_ct),
        )
        if kind == "html":
            response.html = decode_response_body(payload)
            response.url = target_url
            return None

        doc_result = build_non_html_result(kind, url, payload, proxy)
        if doc_result is not None:
            cookie_session.finalize(success=True)
            if response.screenshot:
                doc_result.screenshot = response.screenshot
                doc_result.screenshot_format = response.screenshot_format
            doc_result.actions_executed = response.actions_executed
            doc_result.xhr_responses = response.xhr_responses
            if response.warnings:
                doc_result.warnings.extend(response.warnings)
            return doc_result

        raise TransportFailure(
            FetchErrorInfo(
                "unsupported_content_type",
                f"Content type {ct!r} is an unsupported binary or document format",
                False,
            ),
            status_code=response.status_code,
        )
