"""Lazy single-instance Camoufox browser transport."""

from __future__ import annotations

import asyncio
import random
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup

from ..models import FetchErrorInfo
from ..processing.detector import ConfidenceReport, analyze_html
from ..proxy.providers import ProxySettings
from ..utils.urls import registrable_host
from .http import TransportFailure
from .readiness import controlled_scroll, in_page_metrics, wait_for_stability

# Resource-type blocking sets per stealth level.
#   minimal:   only pure overhead (media, beacon) + websocket
#   balanced:  also strip images and pings
#   aggressive: block everything non-essential including fonts
#   'websocket' is always included — never needed for content extraction.
BLOCK_LEVEL_SETS: dict[str, set[str]] = {
    "minimal": {"media", "beacon", "websocket"},
    "balanced": {"media", "beacon", "ping", "image", "websocket"},
    "aggressive": {"media", "beacon", "ping", "image", "font", "websocket"},
}

# Common desktop viewport pool — avoids a single fixed fingerprint
# while staying within realistic bounds for content extraction.
_VIEWPORT_POOL: list[tuple[int, int]] = [
    (1280, 720),
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1600, 900),
    (1920, 1080),
]


@dataclass(slots=True)
class BrowserResponse:
    url: str
    status_code: int | None
    html: str
    warnings: list[str]
    confidence: ConfidenceReport


class BrowserFetcher:
    """Manage one Camoufox process and create an isolated page per request."""

    def __init__(
        self,
        semaphore: asyncio.Semaphore,
        *,
        timeout: float,
        retries: int,
        proxy: ProxySettings,
        max_content_size: int,
        confidence_threshold: float = 0.80,
        block_images: bool = True,
        block_level: str = "aggressive",
        humanize: bool = False,
        geo_locale: str | None = None,
        geo_timezone: str | None = None,
    ) -> None:
        self.semaphore = semaphore
        self.timeout = timeout
        self.retries = retries
        self.proxy = proxy
        self.max_content_size = max_content_size
        self.confidence_threshold = confidence_threshold
        self.block_images = block_images
        self.block_level = block_level
        self.humanize = humanize
        self.geo_locale = geo_locale
        self.geo_timezone = geo_timezone
        self._manager: Any = None
        self._browser: Any = None
        self._start_lock = asyncio.Lock()
        self._active_count = 0
        self._active_lock = asyncio.Lock()
        self._needs_reset = False

    def _detect_os(self) -> str | None:
        """Return a Camoufox-compatible OS string matching the host."""
        if sys.platform == "win32":
            return "windows"
        if sys.platform == "darwin":
            return "macos"
        if sys.platform.startswith("linux"):
            return "linux"
        return None

    async def start(self) -> None:
        if self._browser is not None:
            return
        async with self._start_lock:
            if self._browser is not None:
                return
            # Clean up a stale manager left behind by an earlier reset
            if self._manager is not None:
                try:
                    await self._manager.__aexit__(None, None, None)
                except Exception:
                    pass
                self._manager = None

            # Auto-install camoufox + browser binary on first use so the
            # import and launch below do not fail with a "not installed"
            # error.  No-op when everything is already in place.
            from ..bootstrap import auto_bootstrap_browser

            auto_bootstrap_browser()

            try:
                from camoufox.async_api import AsyncCamoufox

                options: dict[str, Any] = {
                    "headless": True,
                    "humanize": self.humanize,
                    "enable_cache": True,
                    "block_webrtc": True,
                    "locale": self.geo_locale or "en-US",
                    "window": random.choice(_VIEWPORT_POOL),
                }
                if self.block_images:
                    options["block_images"] = True
                if self.geo_timezone:
                    options["timezone_id"] = self.geo_timezone
                host_os = self._detect_os()
                if host_os is not None:
                    options["os"] = host_os
                browser_proxy = self.proxy.browser_config()
                if browser_proxy:
                    options["proxy"] = browser_proxy
                # Firefox user prefs for fetch-oriented performance.
                # Disable cosmetic animations to reduce GPU/CPU overhead;
                # keep disk and memory cache on for repeat visits.
                options["firefox_user_prefs"] = {
                    "browser.cache.disk.enable": True,
                    "browser.cache.memory.enable": True,
                    "toolkit.cosmeticAnimations.enabled": False,
                }
                self._manager = AsyncCamoufox(**options)
                self._browser = await self._manager.__aenter__()
            except Exception as exc:
                if self._manager is not None:
                    try:
                        await self._manager.__aexit__(None, None, None)
                    except Exception:
                        pass
                self._manager = None
                self._browser = None
                raise TransportFailure(
                    FetchErrorInfo(
                        "browser_launch_error",
                        "Camoufox could not be launched; install 'pagefetch[browser]' "
                        "and run 'python -m camoufox fetch'",
                        True,
                        type(exc).__name__,
                    )
                ) from exc
            self._needs_reset = False

    async def fetch(self, url: str, *, proxy: ProxySettings | None = None) -> BrowserResponse:
        """Fetch a URL through the browser with adaptive retries.

        The semaphore is acquired only during browser I/O, not during HTML
        analysis or inter-retry backoff, so other tasks can use the browser
        during those windows.

        A browser process has one immutable proxy configuration. Callers that
        need a different proxy must create a separate fetcher; changing it
        while pages are active would close contexts belonging to other tasks.
        """
        if proxy is not None and proxy != self.proxy:
            raise TransportFailure(
                FetchErrorInfo(
                    "browser_proxy_mismatch",
                    "browser proxy cannot be changed after fetcher creation",
                    False,
                )
            )

        last_failure: TransportFailure | None = None
        total_deadline = time.monotonic() + self.timeout
        # Start with a balanced scroll profile; escalate on retry.
        max_scrolls = 6
        scroll_sleep_early = 0.10
        scroll_sleep_late = 0.15

        async with self._active_lock:
            self._active_count += 1
        try:
            for attempt in range(self.retries + 1):
                try:
                    # ── browser I/O inside semaphore ──
                    async with self.semaphore:
                        await self.start()
                        remaining = max(5.0, total_deadline - time.monotonic())
                        result = await self._fetch_page_once(
                            url,
                            max_scrolls=max_scrolls,
                            scroll_sleep_early=scroll_sleep_early,
                            scroll_sleep_late=scroll_sleep_late,
                            page_timeout=remaining,
                        )
                    # ── analysis outside semaphore ──
                    report = analyze_html(result.html)
                    result.confidence = report

                    # Retry only for genuine failures: empty, challenge, or
                    # *very* low confidence. Slightly-below-threshold pages
                    # are returned with a warning instead of burning a retry.
                    empty = not result.html.strip()
                    very_low_threshold = min(0.40, self.confidence_threshold)
                    should_retry = empty or report.challenge or report.score < very_low_threshold

                    if should_retry and attempt < self.retries:
                        # Escalate: more scrolls and longer waits on next attempt.
                        max_scrolls = min(12, max_scrolls + 3)
                        scroll_sleep_early = min(0.20, scroll_sleep_early + 0.03)
                        scroll_sleep_late = min(0.28, scroll_sleep_late + 0.05)
                        await asyncio.sleep(0.5 * (2**attempt) + random.uniform(0, 0.50))
                        continue

                    return result
                except TransportFailure as exc:
                    last_failure = exc
                    if not exc.error.retryable or attempt >= self.retries:
                        raise
                    if exc.error.code in {"browser_launch_error", "browser_navigation_error"}:
                        self._browser = None
                        self._needs_reset = True
                    await asyncio.sleep(0.5 * (2**attempt) + random.uniform(0, 0.50))
            assert last_failure is not None
            raise last_failure
        finally:
            async with self._active_lock:
                self._active_count -= 1
                if self._active_count == 0 and self._needs_reset:
                    self._needs_reset = False
                    if self._browser is None and self._manager is not None:
                        try:
                            await self._manager.__aexit__(None, None, None)
                        except Exception:
                            pass
                        self._manager = None

    async def _fetch_page_once(
        self,
        url: str,
        *,
        max_scrolls: int = 6,
        scroll_sleep_early: float = 0.10,
        scroll_sleep_late: float = 0.15,
        page_timeout: float,
    ) -> BrowserResponse:
        """Navigate, wait for stability, optionally scroll, and return raw HTML.

        Uses an adaptive early-exit probe: if the page is already complete
        after the first stability wait, scrolling and the second wait are
        skipped entirely.

        Each call creates a new browser *context* (isolated cookie jar,
        localStorage, cache) and tears it down at the end so consecutive
        fetches are not linkable through shared storage.
        """
        page: Any = None
        context: Any = None
        warnings: list[str] = []
        try:
            async with asyncio.timeout(page_timeout):
                context = await self._browser.new_context()
                page = await context.new_page()
                network = {"active": 0, "last_activity": time.monotonic()}

                def request_started(_request: Any) -> None:
                    network["active"] += 1
                    network["last_activity"] = time.monotonic()

                def request_finished(_request: Any) -> None:
                    network["active"] = max(0, network["active"] - 1)
                    network["last_activity"] = time.monotonic()

                page.on("request", request_started)
                page.on("requestfinished", request_finished)
                page.on("requestfailed", request_finished)

                # Pre-compute the registrable host once so route_handler avoids
                # the expensive tldextract call on every document-frame request.
                _main_site = registrable_host(url)

                async def route_handler(route: Any) -> None:
                    request = route.request
                    external_frame = False
                    if request.resource_type == "document" and request.frame != page.main_frame:
                        request_url = request.url
                        if request_url.startswith(("http://", "https://")):
                            external_frame = registrable_host(request_url) != _main_site
                    # Block non-essential resource types according to the
                    # configured block_level.  Image blocking via the route
                    # handler is defense-in-depth when Camoufox `block_images`
                    # is set; `ping`/`beacon` are pure overhead for content
                    # extraction.
                    blocked = BLOCK_LEVEL_SETS.get(
                        self.block_level, BLOCK_LEVEL_SETS["aggressive"]
                    )
                    if not self.block_images:
                        blocked = blocked - {"image"}
                    # Also block cross-site scripts, XHR, and fetch requests
                    # initiated inside child frames. Main-frame dependencies
                    # are allowed because they may be required to render content.
                    external_script = False
                    _main_frame = getattr(page, "main_frame", None)
                    if request.resource_type in {"script", "xhr", "fetch"} and _main_frame is not None:
                        if getattr(request, "frame", None) != _main_frame:
                            request_url = request.url
                            if request_url.startswith(("http://", "https://")):
                                external_script = registrable_host(request_url) != _main_site
                    if request.resource_type in blocked or external_frame or external_script:
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", route_handler)
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(self.timeout * 1000),
                )

                # ── first stability wait (shorter cap) ──
                first_stability_timeout = min(2.5, self.timeout / 4)
                await wait_for_stability(
                    page,
                    timeout=first_stability_timeout,
                    network_activity=lambda: (network["active"], network["last_activity"]),
                )

                # ── early-exit probe ──
                probe = await in_page_metrics(page)
                skip_scroll = (
                    not probe["challenge"]
                    and (
                        probe["text"] >= 800
                        or probe["main_text"] >= 300
                    )
                )

                if skip_scroll:
                    # Page already has good content — skip scrolling.
                    await page.evaluate("() => window.scrollTo(0, 0)")
                else:
                    limit_reached = await controlled_scroll(
                        page,
                        max_scrolls=max_scrolls,
                        sleep_early=scroll_sleep_early,
                        sleep_late=scroll_sleep_late,
                    )
                    await wait_for_stability(
                        page,
                        timeout=min(3.0, self.timeout / 4),
                        stable_rounds=2,
                        network_activity=lambda: (network["active"], network["last_activity"]),
                    )
                    if limit_reached:
                        warnings.append("Maximum controlled-scroll limit was reached.")

                # Fast size pre-check — character length is enough as a soft
                # guard; avoids the cost of a TextEncoder byte-length encode.
                pre_size = await page.evaluate(
                    "() => (document.documentElement?.outerHTML.length || 0)"
                )
                # Convert char length to approximate byte length (1.5× for
                # multi-byte UTF-8 safety margin).
                if pre_size > self.max_content_size * 1.5:
                    raise TransportFailure(
                        FetchErrorInfo("content_too_large", "rendered content exceeds maximum size", False)
                    )
                html = await page.content()

                # ── gated iframe merge ──
                if not skip_scroll:
                    # Only merge iframes when the main document is weak.
                    html = await self._include_same_site_frames(page, html, page.url or url, warnings)
                else:
                    warnings.append("Same-domain iframe content was skipped because the main document is already content-rich.")

                if len(html.encode("utf-8")) > self.max_content_size:
                    raise TransportFailure(
                        FetchErrorInfo("content_too_large", "rendered content exceeds maximum size", False)
                    )
                return BrowserResponse(
                    url=page.url,
                    status_code=response.status if response else None,
                    html=html,
                    warnings=warnings,
                    confidence=ConfidenceReport(1.0, ()),
                )
        except TransportFailure:
            raise
        except Exception as exc:
            message = str(exc).lower()
            timeout = "timeout" in message
            code = "browser_timeout" if timeout else "browser_navigation_error"
            public_message = "browser navigation timed out" if timeout else "browser navigation failed"
            raise TransportFailure(
                FetchErrorInfo(code, public_message, True, type(exc).__name__)
            ) from exc
        finally:
            if page is not None:
                try:
                    # Stop route interception before closing so Playwright can
                    # cleanly tear down its internal async handler tasks.  This
                    # prevents "TargetClosedError — Future exception was never
                    # retrieved" warnings when the page closes while routes are
                    # still being serviced.
                    await page.unroute("**/*")
                except Exception:
                    pass
                # Give the event loop a chance to flush any in-flight route
                # handler callbacks before we close the page.
                await asyncio.sleep(0)
                try:
                    await page.close()
                except Exception:
                    pass
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass

    async def _include_same_site_frames(
        self, page: Any, html: str, main_url: str, warnings: list[str]
    ) -> str:
        soup = BeautifulSoup(html, "lxml")
        main_site = registrable_host(main_url)
        matched_iframes: set[int] = set()
        frame_count = 0
        max_frames = 3
        for frame in page.frames[1:]:
            frame_url = frame.url
            depth = 0
            parent = frame.parent_frame
            while parent is not None:
                depth += 1
                parent = parent.parent_frame
            if depth > 2:
                warnings.append("Same-domain iframe depth limit was reached.")
                continue

            is_srcdoc = frame_url in {"about:blank", "about:srcdoc"}
            if not frame_url or (not is_srcdoc and registrable_host(frame_url) != main_site):
                continue
            try:
                content = await frame.content()
                if not content:
                    continue
                section = soup.new_tag("section")
                section["data-pagefetch-iframe"] = frame_url
                frame_soup = BeautifulSoup(content, "lxml")
                frame_root = frame_soup.body or frame_soup
                for child in list(frame_root.contents):
                    section.append(child.extract())

                target = None
                for iframe in soup.find_all("iframe"):
                    if id(iframe) in matched_iframes:
                        continue
                    source = iframe.get("src")
                    source_matches = source and urldefrag(urljoin(main_url, str(source)))[0] == urldefrag(frame_url)[0]
                    if source_matches or (is_srcdoc and iframe.has_attr("srcdoc")):
                        target = iframe
                        matched_iframes.add(id(iframe))
                        break
                if target is not None:
                    target.insert_after(section)
                else:
                    (soup.body or soup).append(section)
                frame_count += 1
                if frame_count >= max_frames:
                    warnings.append("Same-domain iframe limit was reached.")
                    break
            except Exception:
                warnings.append("Same-domain iframe could not be accessed.")
        return str(soup)

    async def close(self) -> None:
        if self._manager is not None:
            try:
                await self._manager.__aexit__(None, None, None)
            finally:
                self._manager = None
                self._browser = None
