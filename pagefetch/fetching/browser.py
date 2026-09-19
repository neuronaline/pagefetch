"""Lazy single-instance Camoufox browser transport."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..config import VALID_SCREENSHOT_FORMATS, VALID_SCREENSHOT_MODES
from ..models import FetchErrorInfo
from ..processing.detector import ConfidenceReport, analyze_html
from ..proxy.providers import ProxySettings
from ..utils.urls import is_safe_host, registrable_host
from .http import TransportFailure
from .readiness import controlled_scroll, in_page_metrics, wait_for_stability
from .virtual_display import XvfbDisplay, XvfbLaunchError, XvfbNotFound

logger = logging.getLogger("pagefetch.fetching.browser")

# ``DISPLAY`` is process-global, while fetchers may run from different event
# loops in different threads.  A thread lock is therefore required here;
# asyncio.Lock is loop-affine.  Acquisition is polled asynchronously below so
# a launch in another thread never blocks an event loop.
_DISPLAY_LAUNCH_LOCK = threading.Lock()


@asynccontextmanager
async def _display_launch_lock():
    """Hold the process-wide launch lock without blocking an event loop."""
    acquired = False
    try:
        while not _DISPLAY_LAUNCH_LOCK.acquire(blocking=False):  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        acquired = True
        yield
    finally:
        if acquired:
            _DISPLAY_LAUNCH_LOCK.release()

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

# Common challenge/WAF providers that run inside child frames or require
# WebSockets and dynamic scripts to verify human behavior.
_CHALLENGE_DOMAINS: frozenset[str] = frozenset(
    {
        "cloudflare.com",
        "challenges.cloudflare.com",
        "hcaptcha.com",
        "recaptcha.net",
        "gstatic.com",
        "arkoselabs.com",
        "datadome.co",
        "perimeterx.net",
        "kasada.io",
    }
)

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
    screenshot: bytes | None = None
    screenshot_format: str | None = None


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
        browser_pre_check_byte_margin: float = 1.5,
        confidence_threshold: float = 0.80,
        block_images: bool = True,
        block_level: str = "aggressive",
        humanize: bool = False,
    ) -> None:
        self.semaphore = semaphore
        self.timeout = timeout
        self.retries = retries
        self.proxy = proxy
        self.max_content_size = max_content_size
        self.browser_pre_check_byte_margin = browser_pre_check_byte_margin
        self.confidence_threshold = confidence_threshold
        self.block_images = block_images
        self.block_level = block_level
        self.humanize = humanize
        self._manager: Any = None
        self._browser: Any = None
        self._start_lock = asyncio.Lock()
        self._active_count = 0
        self._active_lock = asyncio.Lock()
        self._needs_reset = False
        # Linux runs the browser headed against an in-process Xvfb to avoid
        # the fingerprinting tells of Firefox's native headless mode. On
        # Windows/macOS we stay on native headless — no display server is
        # needed there.
        self._xvfb: XvfbDisplay | None = None

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
            from ..bootstrap import RuntimeBootstrapError, bootstrap_browser

            try:
                await bootstrap_browser()
            except RuntimeBootstrapError as exc:
                raise TransportFailure(
                    FetchErrorInfo("browser_bootstrap_error", str(exc), True, type(exc).__name__)
                ) from exc

            try:
                from camoufox.async_api import AsyncCamoufox

                host_os = self._detect_os()
                # Linux: headed against an in-process Xvfb. Other platforms:
                # Firefox's native headless (no display server available).
                use_xvfb = host_os == "linux"
                if use_xvfb:
                    if self._xvfb is None or not self._xvfb.is_running:
                        xvfb = XvfbDisplay()
                        start_task = asyncio.create_task(asyncio.to_thread(xvfb.start))
                        try:
                            await asyncio.shield(start_task)
                        except asyncio.CancelledError:
                            # The shielded task is still running; wait for
                            # it to settle so ``xvfb.stop`` sees a consistent
                            # process state. The cancellation supersedes any
                            # start failure, so we discard the inner result.
                            try:
                                await asyncio.shield(start_task)
                            except Exception:  # noqa: BLE001 — tear-down supersedes
                                pass
                            finally:
                                await asyncio.shield(asyncio.to_thread(xvfb.stop))
                            raise
                        except XvfbNotFound as exc:
                            raise TransportFailure(
                                FetchErrorInfo(
                                    "xvfb_missing",
                                    str(exc),
                                    False,
                                    "XvfbNotFound",
                                )
                            ) from exc
                        except XvfbLaunchError as exc:
                            raise TransportFailure(
                                FetchErrorInfo(
                                    "xvfb_launch_error",
                                    str(exc),
                                    True,
                                    "XvfbLaunchError",
                                )
                            ) from exc
                        self._xvfb = xvfb

                options: dict[str, Any] = {
                    "headless": not use_xvfb,
                    "humanize": self.humanize,
                    "enable_cache": True,
                    "block_webrtc": True,
                    "locale": "en-US",
                    "window": random.choice(_VIEWPORT_POOL),
                }
                if self.block_images:
                    options["block_images"] = True
                    # Suppress the Camoufox LeakWarning that fires when
                    # ``block_images`` is enabled.  The warning exists
                    # because image blocking creates CSS/Canvas/WebGL
                    # inconsistencies that bot-detection scripts look for;
                    # in ``off`` stealth mode this is acceptable, and the
                    # operator has explicitly opted in by setting
                    # ``block_images=True``.  The ``balanced``/``max``
                    # stealth presets default to ``False`` to keep
                    # browser fingerprints coherent — see
                    # ``config.build()``.
                    options["i_know_what_im_doing"] = True
                if host_os is not None:
                    options["os"] = host_os
                # Proxy is configured per-context in _fetch_page_once rather
                # than browser-wide, ensuring clean per-request session isolation.
                # Firefox user prefs for fetch-oriented performance.
                # Disable cosmetic animations to reduce GPU/CPU overhead;
                # keep disk and memory cache on for repeat visits.
                options["firefox_user_prefs"] = {
                    "browser.cache.disk.enable": True,
                    "browser.cache.memory.enable": True,
                    "toolkit.cosmeticAnimations.enabled": False,
                }
                browser_env = dict(os.environ)
                if use_xvfb:
                    browser_env["DISPLAY"] = self._xvfb.display
                    browser_env.pop("WAYLAND_DISPLAY", None)
                    browser_env.pop("X_PRIVILEGED_WAYLAND_SOCKET", None)
                    browser_env["GDK_BACKEND"] = "x11"
                    browser_env["MOZ_ENABLE_WAYLAND"] = "0"
                    options["virtual_display"] = self._xvfb.display
                options["env"] = browser_env

                # AsyncCamoufox starts Firefox during ``__aenter__()`` and
                # the child inherits os.environ.  Keep the temporary DISPLAY
                # and Wayland mutation confined to that spawn and always restore the
                # caller's process environment, including on cancellation.
                async with _display_launch_lock():
                    previous_display = os.environ.get("DISPLAY")
                    previous_wayland = os.environ.get("WAYLAND_DISPLAY")
                    previous_privileged_wayland = os.environ.get("X_PRIVILEGED_WAYLAND_SOCKET")
                    previous_gdk_backend = os.environ.get("GDK_BACKEND")
                    previous_moz_wayland = os.environ.get("MOZ_ENABLE_WAYLAND")
                    try:
                        if use_xvfb:
                            os.environ["DISPLAY"] = self._xvfb.display
                            os.environ.pop("WAYLAND_DISPLAY", None)
                            os.environ.pop("X_PRIVILEGED_WAYLAND_SOCKET", None)
                            os.environ["GDK_BACKEND"] = "x11"
                            os.environ["MOZ_ENABLE_WAYLAND"] = "0"
                        self._manager = AsyncCamoufox(**options)
                        self._browser = await self._manager.__aenter__()
                    finally:
                        def _restore(key: str, val: str | None) -> None:
                            if val is None:
                                os.environ.pop(key, None)
                            else:
                                os.environ[key] = val

                        _restore("DISPLAY", previous_display)
                        _restore("WAYLAND_DISPLAY", previous_wayland)
                        _restore("X_PRIVILEGED_WAYLAND_SOCKET", previous_privileged_wayland)
                        _restore("GDK_BACKEND", previous_gdk_backend)
                        _restore("MOZ_ENABLE_WAYLAND", previous_moz_wayland)
            except TransportFailure:
                raise
            except Exception as exc:
                if self._manager is not None:
                    try:
                        await self._manager.__aexit__(None, None, None)
                    except Exception:
                        pass
                self._manager = None
                self._browser = None
                if self._xvfb is not None:
                    xvfb = self._xvfb
                    self._xvfb = None
                    try:
                        await asyncio.to_thread(xvfb.stop)
                    except Exception:
                        pass
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

    @staticmethod
    def _timeout_failure() -> TransportFailure:
        return TransportFailure(
            FetchErrorInfo("browser_timeout", "browser navigation timed out", False)
        )

    async def _backoff_before_deadline(self, attempt: int, deadline: float) -> bool:
        """Sleep for retry jitter without exceeding the operation deadline."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        delay = 0.5 * (2**attempt) + random.uniform(0, 0.50)
        await asyncio.sleep(min(delay, remaining))
        return time.monotonic() < deadline

    async def fetch(
        self,
        url: str,
        *,
        proxy: ProxySettings | None = None,
        screenshot: str = "none",
        screenshot_format: str = "png",
        screenshot_max_bytes: int = 50 * 1024 * 1024,
    ) -> BrowserResponse:
        """Fetch a URL through the browser with adaptive retries.

        The semaphore is acquired only during browser I/O, not during HTML
        analysis or inter-retry backoff, so other tasks can use the browser
        during those windows.

        When *proxy* is supplied, it is applied to the isolated browser
        context created for this request.

        ``screenshot`` selects capture mode: ``"none"`` (default) skips the
        capture, ``"viewport"`` captures the initial visible area,
        ``"full"`` captures the entire scrollable page. ``screenshot_format``
        is ``"png"`` (default) or ``"jpeg"``. Captures exceeding
        ``screenshot_max_bytes`` are discarded with a warning.
        """
        if screenshot not in VALID_SCREENSHOT_MODES:
            raise ValueError(f"screenshot must be one of {sorted(VALID_SCREENSHOT_MODES)}")
        if screenshot_format not in VALID_SCREENSHOT_FORMATS:
            raise ValueError(f"screenshot_format must be one of {sorted(VALID_SCREENSHOT_FORMATS)}")
        if not isinstance(screenshot_max_bytes, int) or isinstance(screenshot_max_bytes, bool) or screenshot_max_bytes <= 0:
            raise ValueError("screenshot_max_bytes must be a positive integer")

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
                    remaining = total_deadline - time.monotonic()
                    if remaining <= 0:
                        raise self._timeout_failure()
                    # ── browser I/O inside semaphore ──
                    async with self.semaphore:
                        await asyncio.wait_for(self.start(), timeout=remaining)
                        remaining = total_deadline - time.monotonic()
                        if remaining <= 0:
                            raise self._timeout_failure()
                        result = await self._fetch_page_once(
                            url,
                            proxy=proxy,
                            max_scrolls=max_scrolls,
                            scroll_sleep_early=scroll_sleep_early,
                            scroll_sleep_late=scroll_sleep_late,
                            page_timeout=remaining,
                            screenshot=screenshot,
                            screenshot_format=screenshot_format,
                            screenshot_max_bytes=screenshot_max_bytes,
                        )
                    # ── analysis outside semaphore ──
                    report = analyze_html(result.html)
                    result.confidence = report

                    # Retry only for genuine failures: empty DOM, an active anti-bot challenge,
                    # an unmounted JavaScript shell, or very low confidence without meaningful content.
                    empty = not result.html.strip()
                    very_low_threshold = min(0.40, self.confidence_threshold)
                    has_substantive_content = (
                        not report.challenge
                        and not report.javascript_shell
                        and "very little visible text" not in report.reasons
                    )
                    should_retry = empty or report.challenge or report.javascript_shell or (
                        report.score < very_low_threshold and not has_substantive_content
                    )

                    if should_retry and attempt < self.retries:
                        # Escalate: more scrolls and longer waits on next attempt.
                        max_scrolls = min(12, max_scrolls + 3)
                        scroll_sleep_early = min(0.20, scroll_sleep_early + 0.03)
                        scroll_sleep_late = min(0.28, scroll_sleep_late + 0.05)
                        if not await self._backoff_before_deadline(attempt, total_deadline):
                            raise self._timeout_failure()
                        continue

                    return result
                except TimeoutError as exc:
                    raise self._timeout_failure() from exc
                except TransportFailure as exc:
                    last_failure = exc
                    if not exc.error.retryable or attempt >= self.retries:
                        raise
                    if exc.error.code in {"browser_launch_error", "browser_navigation_error"}:
                        self._browser = None
                        self._needs_reset = True
                    if not await self._backoff_before_deadline(attempt, total_deadline):
                        raise self._timeout_failure() from exc
            # Unreachable: every iteration ``continue``-s, ``return``-s, or
            # ``raise``-s. ``last_failure`` is recorded for forensics but the
            # exhaustive loop terminates before this line runs.
            raise AssertionError(
                f"browser retry loop exited without terminating: last_failure={last_failure!r}"
            )
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
        proxy: ProxySettings | None = None,
        max_scrolls: int = 6,
        scroll_sleep_early: float = 0.10,
        scroll_sleep_late: float = 0.15,
        page_timeout: float,
        screenshot: str = "none",
        screenshot_format: str = "png",
        screenshot_max_bytes: int = 50 * 1024 * 1024,
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
                context_kwargs: dict[str, Any] = {}
                effective_proxy = proxy if proxy is not None else self.proxy
                browser_proxy = effective_proxy.browser_config() if effective_proxy else None
                if browser_proxy:
                    context_kwargs["proxy"] = browser_proxy
                context = await self._browser.new_context(**context_kwargs)
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
                    request_url = request.url
                    req_host = ""
                    if request_url.startswith(("http://", "https://", "ws://", "wss://")):
                        req_host = (urlsplit(request_url).hostname or "").lower()
                        if not is_safe_host(req_host):
                            await route.abort()
                            return

                    is_challenge_host = any(
                        req_host == d or req_host.endswith("." + d)
                        for d in _CHALLENGE_DOMAINS
                    )

                    external_frame = False
                    if request.resource_type == "document" and request.frame != page.main_frame:
                        if request_url.startswith(("http://", "https://")) and not is_challenge_host:
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

                    # Never block WebSockets or scripts essential for challenge verification
                    if is_challenge_host and request.resource_type in {"websocket", "script", "xhr", "fetch"}:
                        is_blocked_type = False
                    else:
                        is_blocked_type = request.resource_type in blocked

                    # Also block cross-site scripts, XHR, and fetch requests
                    # initiated inside child frames (unless it's an anti-bot challenge host).
                    external_script = False
                    _main_frame = getattr(page, "main_frame", None)
                    if request.resource_type in {"script", "xhr", "fetch"} and _main_frame is not None:
                        if getattr(request, "frame", None) != _main_frame and not is_challenge_host:
                            if request_url.startswith(("http://", "https://")):
                                external_script = registrable_host(request_url) != _main_site

                    if is_blocked_type or external_frame or external_script:
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
                # Convert char length to approximate byte length using the
                # configured margin over max_content_size. The default
                # browser_pre_check_byte_margin is 1.5 (multi-byte UTF-8
                # safety margin); lower values reject pages earlier and
                # higher values are more permissive. Retrying this immutable
                # fetcher cannot change its size limit, so this failure is
                # terminal for the current operation.
                margin = self.browser_pre_check_byte_margin
                if pre_size > self.max_content_size * margin:
                    raise TransportFailure(
                        FetchErrorInfo(
                            "content_too_large",
                            "rendered content exceeds maximum size",
                            False,
                        )
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
                        FetchErrorInfo(
                            "content_too_large",
                            "rendered content exceeds maximum size",
                            False,
                        )
                    )

                # ── optional screenshot capture ──
                screenshot_bytes: bytes | None = None
                screenshot_ext: str | None = None
                if screenshot != "none":
                    try:
                        full_page = screenshot == "full"
                        # Playwright's type argument is "png" / "jpeg".
                        screenshot_bytes = await page.screenshot(
                            full_page=full_page,
                            type=screenshot_format,
                        )
                        if len(screenshot_bytes) > screenshot_max_bytes:
                            warnings.append("Screenshot exceeded max size; discarded.")
                            screenshot_bytes = None
                            screenshot_ext = None
                        else:
                            screenshot_ext = screenshot_format
                    except Exception as exc:
                        logger.warning(
                            "screenshot capture failed: %s", exc, exc_info=True
                        )
                        warnings.append("Screenshot capture failed; continuing without it.")
                        screenshot_bytes = None
                        screenshot_ext = None

                return BrowserResponse(
                    url=page.url,
                    status_code=response.status if response else None,
                    html=html,
                    warnings=warnings,
                    confidence=ConfidenceReport(1.0, ()),
                    screenshot=screenshot_bytes,
                    screenshot_format=screenshot_ext,
                )
        except TransportFailure:
            raise
        except TimeoutError as exc:
            raise TransportFailure(
                FetchErrorInfo(
                    "browser_timeout",
                    "browser navigation timed out",
                    True,
                    type(exc).__name__,
                )
            ) from exc
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
        xvfb = self._xvfb
        self._xvfb = None
        if xvfb is not None:
            await asyncio.to_thread(xvfb.stop)
