"""Lazy single-instance Camoufox browser transport with ephemeral page contexts."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import logging
import os
import random
import sys
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..config import VALID_SCREENSHOT_FORMATS, VALID_SCREENSHOT_MODES, merge_headers
from ..cookies import CookieProfile
from ..exceptions import TransportFailure
from ..models import ActionResult, CapturedXHR, FetchErrorInfo, PageAction
from ..processing.challenges import has_waf_challenge_header
from ..processing.detector import ConfidenceReport, analyze_html
from ..processing.pipeline import (
    detect_document_kind,
    is_explicit_non_html_mime,
    is_unsupported_binary_response,
    parse_content_type,
)
from ..processing.sections import prepare_expanded_dom
from ..proxy.providers import ProxySettings
from ..utils.urls import registrable_host
from .expander import (
    expand_disclosures_only,
    iterate_dynamic_tabs,
    neutralize_overlays_and_unlock_scroll,
)
from .interception import PageNetworkTracker
from .readiness import controlled_scroll, in_page_metrics, wait_for_stability
from .virtual_display import XvfbDisplay, XvfbLaunchError, XvfbNotFound

logger = logging.getLogger("pagefetch.fetching.browser")


@dataclass(slots=True)
class BrowserResponse:
    url: str
    status_code: int | None
    html: str
    warnings: list[str]
    confidence: ConfidenceReport
    soup: BeautifulSoup | None = None
    screenshot: bytes | None = None
    screenshot_format: str | None = None
    actions_executed: list[ActionResult] | None = None
    xhr_responses: list[CapturedXHR] | None = None
    headers: dict[str, str] | None = None
    content_type: str | None = None
    raw_content: bytes | None = None
    suggested_filename: str | None = None


_CREDENTIAL_HEADERS: frozenset[str] = frozenset(
    {"authorization", "cookie", "proxy-authorization"}
)

# Pre-connection Gecko PAC policy blocking loopback, RFC1918, link-local/metadata,
# CGNAT, multicast/reserved literals, and DNS-rebinding targets on all requests
# and internal 3xx redirect hops when running without an external proxy.
_SSRF_PAC_JS = """function FindProxyForURL(url, host) {
    var h = (host || "").toLowerCase().replace(/\\.$/, "");
    if (!h) return "DIRECT";
    if (h === "localhost" || h === "localhost.localdomain" || /\\.local$/.test(h)) {
        return "PROXY 0.0.0.0:1";
    }
    function badIp(ip) {
        var s = (ip || "").toLowerCase().replace(/^\\[|\\]$/g, "");
        if (!s) return false;
        if (s.indexOf("::ffff:") === 0) {
            var tail = s.substring(7);
            var hm = /^([0-9a-f]{1,4}):([0-9a-f]{1,4})$/.exec(tail);
            if (hm) {
                var hi = parseInt(hm[1], 16), lo = parseInt(hm[2], 16);
                s = ((hi >> 8) & 255) + "." + (hi & 255) + "." + ((lo >> 8) & 255) + "." + (lo & 255);
            } else {
                s = tail;
            }
        }
        if (s.indexOf(":") !== -1) {
            return (
                s === "::" || s === "::1" ||
                /^f[cd][0-9a-f]{2}:/.test(s) ||
                /^fe[89ab][0-9a-f]:/.test(s) ||
                /^ff[0-9a-f]{2}:/.test(s)
            );
        }
        var m = /^(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})$/.exec(s);
        if (!m) return false;
        var a = +m[1], b = +m[2], c = +m[3];
        return (
            a === 0 || a === 10 || a === 127 ||
            (a === 169 && b === 254) ||
            (a === 172 && b >= 16 && b <= 31) ||
            (a === 192 && b === 168) ||
            (a === 192 && b === 0 && (c === 0 || c === 2)) ||
            (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) ||
            (a === 203 && b === 0 && c === 113) ||
            a >= 224
        );
    }
    if (badIp(h)) return "PROXY 0.0.0.0:1";
    try {
        var raw = (typeof dnsResolveEx === "function") ? dnsResolveEx(h) : dnsResolve(h);
        if (raw) {
            var parts = String(raw).split(";");
            for (var i = 0; i < parts.length; i++) {
                var cand = parts[i].replace(/^\\s+|\\s+$/g, "");
                if (cand && badIp(cand)) return "PROXY 0.0.0.0:1";
            }
        }
    } catch (_e) {}
    return "DIRECT";
}"""
_SSRF_PAC_B64 = base64.b64encode(_SSRF_PAC_JS.encode("ascii")).decode("ascii")


def _ssrf_hooks_unpatched() -> bool:
    """Return True unless test hooks overrode default SSRF/DNS host validation."""
    from ..utils import urls as urls_mod
    from . import interception as inter_mod

    return (
        urls_mod.resolve_host_ips is urls_mod._DEFAULT_RESOLVE_HOOK
        and inter_mod.async_is_safe_host is urls_mod.async_is_safe_host
        and not urls_mod.is_safe_host("127.0.0.1", resolve_dns=False)
    )


def _is_http_url(value: str | None) -> bool:
    return bool(value and value.startswith(("http://", "https://")))


def _header_value(headers: Mapping[str, str] | None, name: str) -> str:
    """Return a header value case-insensitively or an empty string."""
    if not headers:
        return ""
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return ""


class BrowserFetcher:
    """Manage one Camoufox process and open an isolated context per attempt."""

    def __init__(
        self,
        semaphore: asyncio.Semaphore,
        *,
        timeout: float,
        retries: int,
        proxy: ProxySettings,
        max_content_size: int,
        pre_check_byte_margin: float = 1.5,
        confidence_threshold: float = 0.80,
        block_images: bool = False,
        block_level: str = "balanced",
        humanize: bool = True,
        accept_language: str = "en-US,en;q=0.5",
        max_requests: int = 50,
        idle_ttl: float | None = 180.0,
        max_scroll_steps: int = 25,
        max_scroll_height: int = 150_000,
    ) -> None:
        self.semaphore = semaphore
        self.timeout = timeout
        self.retries = retries
        self.proxy = proxy
        self.max_content_size = max_content_size
        self.pre_check_byte_margin = pre_check_byte_margin
        self.confidence_threshold = confidence_threshold
        self.block_images = block_images
        self.block_level = block_level
        self.humanize = humanize
        self.accept_language = accept_language
        self.max_requests = max_requests
        self.idle_ttl = float(idle_ttl) if idle_ttl is not None and idle_ttl > 0 else None
        self.max_scroll_steps = max_scroll_steps
        self.max_scroll_height = max_scroll_height
        self._request_count = 0
        self._manager: Any = None
        self._browser: Any = None
        self._start_lock = asyncio.Lock()
        self._active_count = 0
        self._active_lock = asyncio.Lock()
        self._needs_reset = False
        self._last_used = time.monotonic()
        self._idle_task: asyncio.Task[None] | None = None
        # Serializes initial page.goto() under authenticated proxy to avoid Firefox 407 races.
        self._proxy_nav_lock = asyncio.Lock()
        self._xvfb: XvfbDisplay | None = None

    def _cancel_idle_task(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task is not None and not task.done():
            task.cancel()

    def _schedule_idle_close(self) -> None:
        self._cancel_idle_task()
        if self.idle_ttl is None or self.idle_ttl <= 0 or self._browser is None:
            return
        ttl = self.idle_ttl

        async def _reap_when_idle() -> None:
            try:
                await asyncio.sleep(ttl)
                async with self._active_lock:
                    if self._active_count > 0 or (time.monotonic() - self._last_used) < ttl * 0.95:
                        return
                logger.info("Closing idle Camoufox browser after %.1fs of inactivity", ttl)
                await self._close_if_unused()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug("Idle browser reaper error: %s", exc)

        try:
            self._idle_task = asyncio.get_running_loop().create_task(_reap_when_idle())
        except RuntimeError:
            pass

    @staticmethod
    def _detect_os() -> str | None:
        if sys.platform == "win32":
            return "windows"
        if sys.platform == "darwin":
            return "macos"
        if sys.platform.startswith("linux"):
            return "linux"
        return None

    def _build_camoufox_options(
        self,
        host_os: str | None,
        use_xvfb: bool,
        proxy: ProxySettings | None = None,
    ) -> dict[str, Any]:
        effective_proxy = proxy if proxy is not None else self.proxy
        has_proxy = bool(effective_proxy and effective_proxy.url)
        custom_lang = self.accept_language != "en-US,en;q=0.5"
        primary_locale = self.accept_language.split(",")[0].split(";")[0].strip() or "en-US"
        firefox_prefs: dict[str, Any] = {
            "network.proxy.allow_hijacking_localhost": True,
            "network.proxy.failover_direct": False,
        }
        options: dict[str, Any] = {
            "headless": not use_xvfb,
            "humanize": self.humanize,
            "enable_cache": True,
            "block_webrtc": not has_proxy,
            "locale": primary_locale,
        }
        has_geoip = bool(importlib.util.find_spec("maxminddb"))
        if has_proxy and effective_proxy is not None:
            options["geoip"] = has_geoip and not custom_lang
            browser_proxy = effective_proxy.browser_config()
            if browser_proxy:
                options["proxy"] = browser_proxy
        else:
            options["geoip"] = False
            if _ssrf_hooks_unpatched():
                firefox_prefs["network.proxy.type"] = 2
                firefox_prefs["network.proxy.autoconfig_url"] = (
                    f"data:text/javascript;base64,{_SSRF_PAC_B64}"
                )
        options["firefox_user_prefs"] = firefox_prefs
        if self.block_images:
            options["block_images"] = True
            options["i_know_what_im_doing"] = True
        if host_os is not None:
            options["os"] = host_os
        browser_env = dict(os.environ)
        if use_xvfb and self._xvfb is not None:
            browser_env["DISPLAY"] = self._xvfb.display
            browser_env.pop("WAYLAND_DISPLAY", None)
            browser_env.pop("X_PRIVILEGED_WAYLAND_SOCKET", None)
            browser_env["GDK_BACKEND"] = "x11"
            browser_env["MOZ_ENABLE_WAYLAND"] = "0"
            options["virtual_display"] = self._xvfb.display
        options["env"] = browser_env
        return options

    async def start(self, proxy: ProxySettings | None = None) -> None:
        async with self._start_lock:
            if self._browser is not None and getattr(self._browser, "is_connected", lambda: True)():
                return
            if proxy is not None and self.proxy is None:
                self.proxy = proxy
            if self._manager is not None:
                try:
                    await self._manager.__aexit__(None, None, None)
                except Exception:
                    pass
                self._manager = None
            self._browser = None

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
                use_xvfb = host_os == "linux"
                if use_xvfb and (self._xvfb is None or not self._xvfb.is_running):
                    xvfb = XvfbDisplay()
                    start_task = asyncio.create_task(asyncio.to_thread(xvfb.start))
                    try:
                        await asyncio.shield(start_task)
                    except asyncio.CancelledError:
                        try:
                            await asyncio.shield(start_task)
                        except Exception:  # noqa: BLE001 — teardown supersedes
                            pass
                        finally:
                            await asyncio.shield(asyncio.to_thread(xvfb.stop))
                        raise
                    except XvfbNotFound as exc:
                        raise TransportFailure(
                            FetchErrorInfo("xvfb_missing", str(exc), False, "XvfbNotFound")
                        ) from exc
                    except XvfbLaunchError as exc:
                        raise TransportFailure(
                            FetchErrorInfo("xvfb_launch_error", str(exc), True, "XvfbLaunchError")
                        ) from exc
                    self._xvfb = xvfb

                options = self._build_camoufox_options(host_os, use_xvfb, proxy=proxy)
                self._manager = AsyncCamoufox(**options)
                self._browser = await self._manager.__aenter__()
            except TransportFailure:
                raise
            except BaseException as exc:
                if self._manager is not None:
                    manager = self._manager
                    self._manager = None
                    try:
                        await asyncio.shield(manager.__aexit__(None, None, None))
                    except BaseException:
                        pass
                self._browser = None
                if self._xvfb is not None:
                    xvfb = self._xvfb
                    self._xvfb = None
                    try:
                        await asyncio.shield(asyncio.to_thread(xvfb.stop))
                    except BaseException:
                        pass
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise TransportFailure(
                    FetchErrorInfo(
                        "browser_launch_error",
                        "Camoufox could not be launched; ensure 'camoufox[geoip]' is installed "
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
        proxy_resolver: Any = None,
        actions: Sequence[PageAction] | None = None,
        capture_xhr: bool | str | Sequence[str] = False,
        headers: dict[str, str] | None = None,
        cookie_profile: CookieProfile | None = None,
        cookie_rotator: Any = None,
        expand: bool = False,
        screenshot: str = "none",
        screenshot_format: str = "png",
        screenshot_max_bytes: int = 50 * 1024 * 1024,
    ) -> BrowserResponse:
        """Fetch a URL through the browser with adaptive retries and clean per-attempt contexts."""
        if screenshot not in VALID_SCREENSHOT_MODES:
            raise ValueError(f"screenshot must be one of {sorted(VALID_SCREENSHOT_MODES)}")
        if screenshot_format not in VALID_SCREENSHOT_FORMATS:
            raise ValueError(f"screenshot_format must be one of {sorted(VALID_SCREENSHOT_FORMATS)}")
        if not isinstance(screenshot_max_bytes, int) or isinstance(screenshot_max_bytes, bool) or screenshot_max_bytes <= 0:
            raise ValueError("screenshot_max_bytes must be a positive integer")

        total_deadline = time.monotonic() + self.timeout
        active_cookie_profile = cookie_profile

        async with self._active_lock:
            self._cancel_idle_task()
            self._active_count += 1
            self._request_count += 1
            if self.max_requests > 0 and self._request_count >= self.max_requests:
                self._needs_reset = True
        try:
            last_failure: TransportFailure | None = None
            for attempt in range(self.retries + 1):
                try:
                    remaining = total_deadline - time.monotonic()
                    if remaining <= 0:
                        raise self._timeout_failure()
                    attempt_proxy = proxy
                    async with self.semaphore:
                        if attempt_proxy is not None and self.proxy is None:
                            self.proxy = attempt_proxy
                        await asyncio.wait_for(self.start(proxy=attempt_proxy), timeout=remaining)
                        remaining = total_deadline - time.monotonic()
                        if remaining <= 0:
                            raise self._timeout_failure()
                        if attempt < self.retries:
                            attempt_timeout = min(
                                remaining,
                                max(15.0, (self.timeout / (self.retries + 1)) * 1.6),
                            )
                        else:
                            attempt_timeout = remaining
                        result = await self._fetch_page_once(
                            url,
                            proxy=attempt_proxy,
                            actions=actions,
                            capture_xhr=capture_xhr,
                            headers=headers,
                            cookie_profile=active_cookie_profile,
                            expand=expand,
                            page_timeout=attempt_timeout,
                            screenshot=screenshot,
                            screenshot_format=screenshot_format,
                            screenshot_max_bytes=screenshot_max_bytes,
                        )
                    if result.raw_content is not None:
                        return result
                    report = analyze_html(result.html, soup=result.soup)
                    if (
                        not report.challenge
                        and report.score < self.confidence_threshold
                        and has_waf_challenge_header(result.headers, status_code=result.status_code)
                    ):
                        report = ConfidenceReport(
                            score=min(report.score, 0.08),
                            reasons=tuple(
                                dict.fromkeys((*report.reasons, "challenge or anti-bot page detected"))
                            ),
                            challenge=True,
                            javascript_shell=report.javascript_shell,
                        )
                    result.confidence = report

                    empty = not result.html.strip()
                    very_low_threshold = min(0.40, self.confidence_threshold)
                    has_substantive_content = (
                        not report.challenge
                        and not report.javascript_shell
                        and "very little visible text" not in report.reasons
                    )
                    is_site_block = result.status_code in {403, 429, 503} and not (
                        has_substantive_content and result.status_code == 503
                    )
                    transient_status = (
                        result.status_code in {407, 502, 504}
                        and report.score < self.confidence_threshold
                    )
                    needs_retry = (
                        empty
                        or report.challenge
                        or report.javascript_shell
                        or is_site_block
                        or transient_status
                        or (report.score < very_low_threshold and not has_substantive_content)
                    )
                    if needs_retry and attempt < self.retries:
                        if cookie_rotator is not None and (is_site_block or report.challenge):
                            rotated = cookie_rotator(attempt + 1)
                            if rotated is not None:
                                active_cookie_profile = rotated
                        should_rotate_proxy = (
                            is_site_block
                            or report.challenge
                            or result.status_code in {502, 504}
                        )
                        if should_rotate_proxy and callable(proxy_resolver):
                            new_proxy = proxy_resolver(attempt + 1)
                            if new_proxy is not None:
                                proxy = new_proxy
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
                    if callable(proxy_resolver):
                        new_proxy = proxy_resolver(attempt + 1)
                        if new_proxy is not None:
                            proxy = new_proxy
                    cause_text = str(exc.__cause__ or exc).lower()
                    browser_dead = (
                        exc.error.code == "browser_launch_error"
                        or self._browser is None
                        or not getattr(self._browser, "is_connected", lambda: True)()
                        or any(
                            tok in cause_text
                            for tok in (
                                "browser has been closed",
                                "target page, context or browser has been closed",
                                "connection closed",
                            )
                        )
                    )
                    if browser_dead:
                        if not await self._close_if_unused(max_active=1):
                            self._needs_reset = True
                    if not await self._backoff_before_deadline(attempt, total_deadline):
                        raise self._timeout_failure() from exc
            raise AssertionError(
                f"browser retry loop exited without terminating: last_failure={last_failure!r}"
            )
        finally:
            should_reset = False
            schedule_idle = False
            async with self._active_lock:
                self._active_count -= 1
                self._last_used = time.monotonic()
                if self._active_count == 0:
                    if self._needs_reset:
                        self._needs_reset = False
                        self._request_count = 0
                        should_reset = True
                    else:
                        schedule_idle = True
            if should_reset:
                await self._close_if_unused()
            elif schedule_idle:
                self._schedule_idle_close()

    @asynccontextmanager
    async def _open_isolated_page(
        self,
        url: str,
        *,
        effective_proxy: ProxySettings | None,
        headers: dict[str, str] | None,
        cookie_profile: CookieProfile | None,
        capture_xhr: bool | str | Sequence[str],
        screenshot: bool,
    ) -> AsyncIterator[tuple[Any, PageNetworkTracker, list[Any], asyncio.Event, bool]]:
        """Create and deterministically tear down an isolated context + tracked page."""
        has_proxy = bool(effective_proxy and effective_proxy.url)
        # Honor explicit accept_language overrides; leave default unset so GeoIP/locale governs.
        base_lang = (
            {"Accept-Language": self.accept_language}
            if self.accept_language != "en-US,en;q=0.5"
            else None
        )
        context_headers = merge_headers(base_lang, headers)
        has_credential_headers = any(k.lower() in _CREDENTIAL_HEADERS for k in context_headers)

        context_kwargs: dict[str, Any] = {
            "bypass_csp": False,
            "ignore_https_errors": True,
            "java_script_enabled": True,
            "accept_downloads": True,
        }
        browser_proxy = effective_proxy.browser_config() if effective_proxy else None
        if browser_proxy:
            context_kwargs["proxy"] = browser_proxy

        context = None
        page = None
        downloads: list[Any] = []
        download_event = asyncio.Event()
        tracker = PageNetworkTracker(
            target_url=url,
            block_level=self.block_level,
            block_images=self.block_images,
            has_proxy=has_proxy,
            capture_xhr=capture_xhr,
            screenshot=screenshot,
            strip_cross_origin_credentials=has_credential_headers,
            max_content_size=self.max_content_size,
        )
        try:
            context = await self._browser.new_context(**context_kwargs)
            if cookie_profile is not None:
                cookie_list = cookie_profile.to_playwright_cookies(url)
                if cookie_list:
                    try:
                        await context.add_cookies(cookie_list)
                    except Exception:
                        target_host = urlsplit(url).hostname or "localhost"
                        fallback_list = [
                            {"name": c["name"], "value": c["value"], "domain": target_host, "path": "/"}
                            for c in cookie_list
                        ]
                        try:
                            await context.add_cookies(fallback_list)
                        except Exception as exc:
                            logger.debug("Failed to set cookies on browser context: %s", exc)

            page = await context.new_page()

            def _on_download(dl: Any) -> None:
                downloads.append(dl)
                download_event.set()

            page.on("download", _on_download)
            if context_headers:
                try:
                    await page.set_extra_http_headers(context_headers)
                except Exception:
                    pass
            await tracker.attach(page)
            has_auth_proxy = bool(browser_proxy and browser_proxy.get("username"))
            yield page, tracker, downloads, download_event, has_auth_proxy
        finally:
            for dl in downloads:
                await self._cleanup_download(dl, cancel=True)
            await tracker.detach(page)
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass

    async def _try_resolve_interstitial(self, page: Any) -> bool:
        """Attempt a single click on a Turnstile widget or benign continue button."""
        try:
            box = await page.evaluate(
                """() => {
                    const el = document.querySelector(
                        '.cf-turnstile, [id^="cf-chl-widget"], #challenge-stage iframe, #challenge-stage input[type="checkbox"]'
                    );
                    if (el) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 20 && r.height > 20) {
                            return {x: r.x + Math.min(28, r.width / 2), y: r.y + r.height / 2};
                        }
                    }
                    const hasTextInput = !!document.querySelector('input[type="text"], input[name*="field-keywords"]');
                    if (!hasTextInput) {
                        const btn = document.querySelector(
                            'form[action*="validateCaptcha" i] button[type="submit"], ' +
                            'form[action*="validateCaptcha" i] input[type="submit"]'
                        );
                        if (btn) {
                            const r = btn.getBoundingClientRect();
                            if (r.width > 10 && r.height > 10) {
                                return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                            }
                        }
                    }
                    return null;
                }"""
            )
            if isinstance(box, dict) and "x" in box and "y" in box:
                await page.mouse.click(float(box["x"]), float(box["y"]))
                return True
        except Exception:
            pass
        return False

    async def _wait_initial_readiness(
        self,
        page: Any,
        tracker: PageNetworkTracker,
    ) -> tuple[bool, bool]:
        """Wait for initial DOM stability and passive challenge resolution."""
        await wait_for_stability(
            page,
            timeout=min(2.5, self.timeout / 4),
            network_activity=tracker.activity,
        )

        probe = await in_page_metrics(page)
        if probe["challenge"]:
            challenge_timeout = min(15.0, self.timeout / 2)
            challenge_started = time.monotonic()
            challenge_deadline = challenge_started + challenge_timeout
            clicked_interstitial = False
            empty_non_challenge_polls = 0
            while time.monotonic() < challenge_deadline:
                await asyncio.sleep(0.35)
                if not clicked_interstitial and (time.monotonic() - challenge_started) >= 2.0:
                    clicked_interstitial = await self._try_resolve_interstitial(page)
                probe = await in_page_metrics(page)
                if not probe["challenge"]:
                    if probe["text"] > 0 or probe["main_text"] > 0:
                        break
                    empty_non_challenge_polls += 1
                    if empty_non_challenge_polls >= 3 and tracker.active == 0:
                        break
                else:
                    empty_non_challenge_polls = 0
            await wait_for_stability(
                page,
                timeout=min(3.0, self.timeout / 4),
                stable_rounds=2,
                network_activity=tracker.activity,
            )
            probe = await in_page_metrics(page)
            if probe["challenge"]:
                return True, True

        weak_main = probe["text"] < 800 and probe["main_text"] < 300
        return False, weak_main

    async def _run_goto(
        self,
        page: Any,
        url: str,
        *,
        has_auth_proxy: bool,
        goto_timeout_ms: int,
    ) -> Any:
        if has_auth_proxy:
            async with self._proxy_nav_lock:
                return await page.goto(
                    url, wait_until="domcontentloaded", timeout=goto_timeout_ms
                )
        return await page.goto(
            url, wait_until="domcontentloaded", timeout=goto_timeout_ms
        )

    @staticmethod
    def _check_tracker_abort(tracker: PageNetworkTracker) -> None:
        if tracker.ssrf_blocked_url:
            raise BrowserFetcher._ssrf_failure(tracker.ssrf_blocked_url)
        if tracker.unsupported_content_type is not None:
            raise TransportFailure(
                FetchErrorInfo(
                    "unsupported_content_type",
                    f"Content type {tracker.unsupported_content_type!r} is an unsupported binary format",
                    False,
                ),
                status_code=tracker.last_document_status,
            )
        if tracker.oversized_response:
            raise TransportFailure(
                FetchErrorInfo(
                    "content_too_large",
                    "response exceeds maximum content size",
                    False,
                ),
                status_code=tracker.last_document_status,
            )

    async def _navigate_and_await_readiness(
        self,
        page: Any,
        tracker: PageNetworkTracker,
        url: str,
        *,
        has_auth_proxy: bool,
        page_deadline: float,
    ) -> tuple[Any, bool, bool, bool]:
        rem_ms = max(5000.0, (page_deadline - time.monotonic()) * 1000)
        goto_timeout_ms = int(min(self.timeout * 1000, rem_ms))
        goto_task = asyncio.create_task(
            self._run_goto(
                page,
                url,
                has_auth_proxy=has_auth_proxy,
                goto_timeout_ms=goto_timeout_ms,
            )
        )
        abort_task = asyncio.create_task(tracker.abort_event.wait())
        try:
            done, pending = await asyncio.wait(
                {goto_task, abort_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p_task in pending:
                p_task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if abort_task in done:
                if goto_task in done and not goto_task.cancelled():
                    _ = goto_task.exception()
                self._check_tracker_abort(tracker)
            response = goto_task.result()
        except TransportFailure:
            raise
        except Exception as exc:
            self._check_tracker_abort(tracker)
            if "download is starting" in str(exc).lower():
                return None, False, False, True
            raise

        doc_headers = tracker.last_document_headers or (
            dict(response.headers)
            if response and isinstance(getattr(response, "headers", None), dict)
            else None
        )
        doc_status = (
            tracker.last_document_status
            if tracker.last_document_status is not None
            else (response.status if response else None)
        )
        ct_raw = _header_value(doc_headers, "content-type")
        cd_raw = _header_value(doc_headers, "content-disposition")
        page_url = page.url if _is_http_url(getattr(page, "url", None)) else None
        target_url = (
            tracker.last_document_url
            if _is_http_url(tracker.last_document_url)
            else (page_url or url)
        )
        if doc_status is None or doc_status < 400:
            if is_unsupported_binary_response(ct_raw, target_url, cd_raw):
                normalized_ct = parse_content_type(ct_raw)
                raise TransportFailure(
                    FetchErrorInfo(
                        "unsupported_content_type",
                        f"Content type {normalized_ct!r} is an unsupported binary format",
                        False,
                    ),
                    status_code=doc_status,
                )
            if is_explicit_non_html_mime(ct_raw):
                return response, False, False, False

        unresolved_challenge, weak_main = await self._wait_initial_readiness(page, tracker)
        return response, unresolved_challenge, weak_main, False

    async def _capture_screenshot(
        self,
        page: Any,
        screenshot: str,
        screenshot_format: str,
        screenshot_max_bytes: int,
        warnings: list[str],
    ) -> tuple[bytes | None, str | None]:
        if screenshot == "none":
            return None, None
        try:
            screenshot_bytes = await page.screenshot(
                full_page=(screenshot == "full"),
                type=screenshot_format,
            )
            if len(screenshot_bytes) > screenshot_max_bytes:
                warnings.append("Screenshot exceeded max size; discarded.")
                return None, None
            return screenshot_bytes, screenshot_format
        except Exception as exc:
            logger.warning("screenshot capture failed: %s", exc, exc_info=True)
            warnings.append("Screenshot capture failed; continuing without it.")
            return None, None

    async def _run_page_interactions(
        self,
        page: Any,
        tracker: PageNetworkTracker,
        url: str,
        *,
        actions: Sequence[PageAction] | None,
        expand: bool,
        screenshot: str,
        screenshot_format: str,
        screenshot_max_bytes: int,
        page_deadline: float,
        unresolved_challenge: bool,
        weak_main: bool,
        warnings: list[str],
    ) -> tuple[
        str,
        BeautifulSoup | None,
        bytes | None,
        str | None,
        list[ActionResult] | None,
    ]:
        actions_executed = None
        response_soup: BeautifulSoup | None = None

        if unresolved_challenge:
            html = await page.content()
            screenshot_bytes, screenshot_ext = await self._capture_screenshot(
                page, screenshot, screenshot_format, screenshot_max_bytes, warnings
            )
            return html, None, screenshot_bytes, screenshot_ext, None

        await neutralize_overlays_and_unlock_scroll(page)
        if actions:
            from .actions import execute_page_actions, normalize_actions

            actions_executed = await execute_page_actions(
                page,
                normalize_actions(actions),
                humanize=self.humanize,
                default_timeout=min(15.0, self.timeout),
                warnings=warnings,
                network_activity=tracker.activity,
            )
            await neutralize_overlays_and_unlock_scroll(page)

        scroll_budget_end = page_deadline - (3.5 if expand else 1.5)
        limit_reached = await controlled_scroll(
            page,
            max_scrolls=self.max_scroll_steps,
            max_height=self.max_scroll_height,
            sleep_early=0.12,
            sleep_late=0.18,
            deadline=max(time.monotonic() + 1.0, scroll_budget_end),
            network_activity=tracker.activity,
        )
        await wait_for_stability(
            page,
            timeout=min(2.5, self.timeout / 4),
            stable_rounds=2,
            network_activity=tracker.activity,
        )
        if limit_reached:
            warnings.append(
                "Full-page scroll reached configured step or height limit; returning all content loaded so far."
            )

        if expand:
            expand_deadline = max(time.monotonic() + 0.5, page_deadline - 1.0)
            await expand_disclosures_only(
                page, deadline=expand_deadline, network_activity=tracker.activity
            )
            screenshot_bytes, screenshot_ext = await self._capture_screenshot(
                page, screenshot, screenshot_format, screenshot_max_bytes, warnings
            )
            baseline_html, raw_sections = await iterate_dynamic_tabs(
                page, deadline=expand_deadline, network_activity=tracker.activity
            )
            html, response_soup = prepare_expanded_dom(baseline_html, raw_sections)
        else:
            html = await page.content()
            screenshot_bytes, screenshot_ext = await self._capture_screenshot(
                page, screenshot, screenshot_format, screenshot_max_bytes, warnings
            )

        if len(html) > self.max_content_size * self.pre_check_byte_margin:
            raise TransportFailure(
                FetchErrorInfo("content_too_large", "rendered content exceeds maximum size", True)
            )
        if weak_main and not expand:
            html = await self._include_same_site_frames(page, html, page.url or url, warnings)

        return html, response_soup, screenshot_bytes, screenshot_ext, actions_executed

    @staticmethod
    def _classify_browser_error(exc: Exception) -> TransportFailure:
        message = str(exc).lower()
        proxy_tokens = (
            "err_tunnel_connection_failed",
            "ns_error_proxy_forbidden",
            "err_proxy_connection_failed",
            "proxy connection refused",
            "proxy error",
            "proxy 403",
        )
        if "timeout" in message:
            code, public_message, retryable = "browser_timeout", "browser navigation timed out", True
        elif any(token in message for token in proxy_tokens):
            code = "proxy_forbidden" if ("forbidden" in message or "403" in message) else "proxy_error"
            public_message, retryable = f"browser proxy connection failed: {exc}", False
        else:
            code, public_message, retryable = "browser_navigation_error", "browser navigation failed", True
        return TransportFailure(FetchErrorInfo(code, public_message, retryable, type(exc).__name__))

    @staticmethod
    async def _cleanup_download(dl: Any, *, cancel: bool = False) -> None:
        if dl is None:
            return
        if cancel:
            try:
                await dl.cancel()
            except Exception:
                pass
        try:
            await dl.delete()
        except Exception:
            pass

    async def _read_download_bytes(self, dl: Any, page_deadline: float) -> bytes:
        rem = max(1.0, page_deadline - time.monotonic())
        try:
            failure_reason = await asyncio.wait_for(dl.failure(), timeout=rem)
            if failure_reason:
                await self._cleanup_download(dl, cancel=True)
                raise TransportFailure(
                    FetchErrorInfo(
                        "browser_navigation_error",
                        f"browser download failed: {failure_reason}",
                        True,
                    )
                )
            rem = max(0.5, page_deadline - time.monotonic())
            dl_path_str = await asyncio.wait_for(dl.path(), timeout=rem)
        except TimeoutError as exc:
            await self._cleanup_download(dl, cancel=True)
            raise TransportFailure(
                FetchErrorInfo("browser_timeout", "browser download timed out", True, "TimeoutError")
            ) from exc
        if not dl_path_str:
            await self._cleanup_download(dl, cancel=True)
            raise TransportFailure(
                FetchErrorInfo("browser_navigation_error", "browser download failed to produce a file", True)
            )
        dl_path = Path(dl_path_str)
        try:
            size = await asyncio.to_thread(lambda: dl_path.stat().st_size)
            if size > self.max_content_size:
                raise TransportFailure(
                    FetchErrorInfo("content_too_large", "response exceeds maximum content size", False)
                )
            return await asyncio.to_thread(dl_path.read_bytes)
        finally:
            await self._cleanup_download(dl, cancel=False)

    async def _build_non_html_response(
        self,
        page: Any,
        tracker: PageNetworkTracker,
        response: Any,
        downloads: list[Any],
        download_event: asyncio.Event,
        url: str,
        *,
        download_started: bool,
        page_deadline: float,
        screenshot: str,
        screenshot_format: str,
        screenshot_max_bytes: int,
        warnings: list[str],
    ) -> BrowserResponse | None:
        doc_headers = tracker.last_document_headers or (
            dict(response.headers)
            if response and isinstance(getattr(response, "headers", None), dict)
            else None
        )
        doc_status = (
            tracker.last_document_status
            if tracker.last_document_status is not None
            else (response.status if response else None)
        )
        page_url = page.url if _is_http_url(getattr(page, "url", None)) else None
        target_url = (
            tracker.last_document_url
            if _is_http_url(tracker.last_document_url)
            else (page_url or url)
        )
        ct_raw = _header_value(doc_headers, "content-type")
        cd_raw = _header_value(doc_headers, "content-disposition")

        if download_started or downloads:
            if download_started and not downloads:
                wait_sec = min(2.0, max(0.1, page_deadline - time.monotonic()))
                try:
                    await asyncio.wait_for(download_event.wait(), timeout=wait_sec)
                except TimeoutError:
                    pass
            dl = downloads[-1] if downloads else None
            dl_url = getattr(dl, "url", None) if dl is not None else None
            dl_target_url = dl_url if _is_http_url(dl_url) else target_url
            if (
                _is_http_url(dl_url)
                and _is_http_url(tracker.last_document_url)
                and urldefrag(str(dl_url))[0] != urldefrag(str(tracker.last_document_url))[0]
            ):
                # Download originated from a prior HTML page rather than the tracked document URL.
                dl_headers: dict[str, str] | None = None
                dl_status: int | None = None
            else:
                dl_headers = doc_headers
                dl_status = doc_status
            dl_ct = _header_value(dl_headers, "content-type")
            dl_cd = _header_value(dl_headers, "content-disposition")
            suggested_name = (getattr(dl, "suggested_filename", None) or "").strip() if dl else ""
            effective_cd = dl_cd or (f'attachment; filename="{suggested_name}"' if suggested_name else "")
            if is_unsupported_binary_response(dl_ct, dl_target_url, effective_cd):
                await self._cleanup_download(dl, cancel=True)
                normalized_ct = parse_content_type(dl_ct)
                raise TransportFailure(
                    FetchErrorInfo(
                        "unsupported_content_type",
                        f"Content type {normalized_ct!r} is an unsupported binary format",
                        False,
                    ),
                    status_code=dl_status,
                )
            declared_len = _header_value(dl_headers, "content-length").strip()
            if declared_len.isdigit() and int(declared_len) > self.max_content_size:
                await self._cleanup_download(dl, cancel=True)
                raise TransportFailure(
                    FetchErrorInfo("content_too_large", "response exceeds maximum content size", False),
                    status_code=dl_status,
                )
            if dl_status is not None and dl_status >= 400:
                await self._cleanup_download(dl, cancel=True)
                return BrowserResponse(
                    url=dl_target_url,
                    status_code=dl_status,
                    html="",
                    warnings=warnings,
                    confidence=ConfidenceReport(1.0, ()),
                    headers=dl_headers,
                    content_type=dl_ct or None,
                )
            if dl is None:
                raise TransportFailure(
                    FetchErrorInfo(
                        "browser_navigation_error",
                        "browser download started without a capture handle",
                        True,
                    ),
                    status_code=dl_status,
                )
            raw_content = await self._read_download_bytes(dl, page_deadline)
            if screenshot != "none":
                warnings.append("Screenshot unavailable for downloaded attachment.")
            xhr_responses = await tracker.finalize_xhr()
            return BrowserResponse(
                url=dl_target_url,
                status_code=dl_status or 200,
                html="",
                warnings=warnings,
                confidence=ConfidenceReport(1.0, ()),
                xhr_responses=xhr_responses,
                headers=dl_headers,
                content_type=dl_ct or None,
                raw_content=raw_content,
                suggested_filename=suggested_name or None,
            )

        if doc_status is None or doc_status < 400:
            if is_unsupported_binary_response(ct_raw, target_url, cd_raw):
                normalized_ct = parse_content_type(ct_raw)
                raise TransportFailure(
                    FetchErrorInfo(
                        "unsupported_content_type",
                        f"Content type {normalized_ct!r} is an unsupported binary format",
                        False,
                    ),
                    status_code=doc_status,
                )
            ct_norm = parse_content_type(ct_raw)
            is_html_ct = ct_norm in ("text/html", "application/xhtml+xml")
            should_probe_body = not is_html_ct
            if is_html_ct:
                try:
                    should_probe_body = bool(
                        await page.evaluate(
                            "() => { const t = (document.body?.textContent || '').slice(0, 8); "
                            "return t.startsWith('%PDF-') || t.startsWith('PK\\x03\\x04'); }"
                        )
                    )
                except Exception:
                    should_probe_body = False
            if should_probe_body:
                doc_resp = tracker.last_document_response or response
                raw_content: bytes | None = None
                if doc_resp is not None and hasattr(doc_resp, "body"):
                    try:
                        raw_content = await doc_resp.body()
                    except Exception:
                        raw_content = None
                if raw_content is not None and len(raw_content) > self.max_content_size:
                    raise TransportFailure(
                        FetchErrorInfo("content_too_large", "response exceeds maximum content size", False),
                        status_code=doc_status,
                    )
                if raw_content is None:
                    return None
                kind = detect_document_kind(ct_raw, raw_content, url=target_url)
                if kind == "html":
                    return None
                if kind == "unknown" and not ct_raw:
                    return None
                if screenshot != "none":
                    await wait_for_stability(
                        page,
                        timeout=min(1.5, self.timeout / 6),
                        stable_rounds=2,
                        network_activity=tracker.activity,
                    )
                screenshot_bytes, screenshot_ext = await self._capture_screenshot(
                    page, screenshot, screenshot_format, screenshot_max_bytes, warnings
                )
                xhr_responses = await tracker.finalize_xhr()
                return BrowserResponse(
                    url=target_url,
                    status_code=doc_status,
                    html="",
                    warnings=warnings,
                    confidence=ConfidenceReport(1.0, ()),
                    screenshot=screenshot_bytes,
                    screenshot_format=screenshot_ext,
                    xhr_responses=xhr_responses,
                    headers=doc_headers,
                    content_type=ct_raw or None,
                    raw_content=raw_content,
                )
        return None

    @staticmethod
    def _ssrf_failure(_blocked_url: str = "") -> TransportFailure:
        return TransportFailure(
            FetchErrorInfo(
                "ssrf_blocked",
                "URL or redirect points to a restricted local or private network address (SSRF protection)",
                False,
            )
        )

    async def _fetch_page_once(
        self,
        url: str,
        *,
        proxy: ProxySettings | None = None,
        actions: Sequence[PageAction] | None = None,
        capture_xhr: bool | str | Sequence[str] = False,
        headers: dict[str, str] | None = None,
        cookie_profile: CookieProfile | None = None,
        expand: bool = False,
        page_timeout: float,
        screenshot: str = "none",
        screenshot_format: str = "png",
        screenshot_max_bytes: int = 50 * 1024 * 1024,
    ) -> BrowserResponse:
        """Open an isolated context, navigate, interact/expand, and capture rendered HTML."""
        warnings: list[str] = []
        effective_proxy = proxy if proxy is not None else self.proxy
        page_deadline = time.monotonic() + page_timeout
        tracker_ref: PageNetworkTracker | None = None
        try:
            async with asyncio.timeout(page_timeout):
                async with self._open_isolated_page(
                    url,
                    effective_proxy=effective_proxy,
                    headers=headers,
                    cookie_profile=cookie_profile,
                    capture_xhr=capture_xhr,
                    screenshot=(screenshot != "none"),
                ) as (page, tracker, downloads, download_event, has_auth_proxy):
                    tracker_ref = tracker
                    response, unresolved_challenge, weak_main, download_started = (
                        await self._navigate_and_await_readiness(
                            page,
                            tracker,
                            url,
                            has_auth_proxy=has_auth_proxy,
                            page_deadline=page_deadline,
                        )
                    )
                    blocked_url = await tracker.check_ssrf(getattr(page, "url", None))
                    if blocked_url:
                        raise self._ssrf_failure(blocked_url)
                    non_html_resp = await self._build_non_html_response(
                        page,
                        tracker,
                        response,
                        downloads,
                        download_event,
                        url,
                        download_started=download_started,
                        page_deadline=page_deadline,
                        screenshot=screenshot,
                        screenshot_format=screenshot_format,
                        screenshot_max_bytes=screenshot_max_bytes,
                        warnings=warnings,
                    )
                    if non_html_resp is not None:
                        return non_html_resp
                    (
                        html,
                        response_soup,
                        screenshot_bytes,
                        screenshot_ext,
                        actions_executed,
                    ) = await self._run_page_interactions(
                        page,
                        tracker,
                        url,
                        actions=actions,
                        expand=expand,
                        screenshot=screenshot,
                        screenshot_format=screenshot_format,
                        screenshot_max_bytes=screenshot_max_bytes,
                        page_deadline=page_deadline,
                        unresolved_challenge=unresolved_challenge,
                        weak_main=weak_main,
                        warnings=warnings,
                    )
                    blocked_url = await tracker.check_ssrf(getattr(page, "url", None))
                    if blocked_url:
                        raise self._ssrf_failure(blocked_url)
                    if downloads:
                        dl_resp = await self._build_non_html_response(
                            page,
                            tracker,
                            response,
                            downloads,
                            download_event,
                            url,
                            download_started=True,
                            page_deadline=page_deadline,
                            screenshot="none",
                            screenshot_format=screenshot_format,
                            screenshot_max_bytes=screenshot_max_bytes,
                            warnings=warnings,
                        )
                        if dl_resp is not None:
                            dl_resp.actions_executed = actions_executed
                            if screenshot_bytes is not None:
                                dl_resp.screenshot = screenshot_bytes
                                dl_resp.screenshot_format = screenshot_ext
                            return dl_resp
                    if len(html.encode("utf-8")) > self.max_content_size:
                        raise TransportFailure(
                            FetchErrorInfo("content_too_large", "rendered content exceeds maximum size", True)
                        )
                    xhr_responses = await tracker.finalize_xhr()
                    final_status = (
                        tracker.last_document_status
                        if tracker.last_document_status is not None
                        else (response.status if response else None)
                    )
                    final_headers = (
                        tracker.last_document_headers
                        if tracker.last_document_headers is not None
                        else (
                            dict(response.headers)
                            if response and isinstance(getattr(response, "headers", None), dict)
                            else None
                        )
                    )
                    return BrowserResponse(
                        url=page.url,
                        status_code=final_status,
                        html=html,
                        warnings=warnings,
                        confidence=ConfidenceReport(1.0, ()),
                        soup=response_soup,
                        screenshot=screenshot_bytes,
                        screenshot_format=screenshot_ext,
                        actions_executed=actions_executed,
                        xhr_responses=xhr_responses,
                        headers=final_headers,
                    )
        except TransportFailure:
            raise
        except TimeoutError as exc:
            if tracker_ref is not None and tracker_ref.ssrf_blocked_url:
                raise self._ssrf_failure(tracker_ref.ssrf_blocked_url) from exc
            raise TransportFailure(
                FetchErrorInfo("browser_timeout", "browser navigation timed out", True, type(exc).__name__)
            ) from exc
        except Exception as exc:
            if tracker_ref is not None:
                blocked_url = await tracker_ref.check_ssrf()
                if blocked_url:
                    raise self._ssrf_failure(blocked_url) from exc
            raise self._classify_browser_error(exc) from exc

    async def _include_same_site_frames(
        self, page: Any, html: str, main_url: str, warnings: list[str]
    ) -> str:
        if len(page.frames) <= 1:
            return html
        soup = BeautifulSoup(html, "lxml")
        main_site = registrable_host(main_url)
        matched_iframes: set[int] = set()
        frame_count = 0
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
                if frame_count >= 3:
                    warnings.append("Same-domain iframe limit was reached.")
                    break
            except Exception:
                warnings.append("Same-domain iframe could not be accessed.")
        return str(soup)

    async def _teardown_locked(self) -> None:
        """Shut down Camoufox and Xvfb while already holding ``_start_lock``."""
        try:
            if self._manager is not None:
                manager = self._manager
                self._manager = None
                self._browser = None
                try:
                    await manager.__aexit__(None, None, None)
                except Exception:
                    pass
            elif self._browser is not None:
                browser = self._browser
                self._browser = None
                try:
                    await browser.close()
                except Exception:
                    pass
        finally:
            xvfb = self._xvfb
            self._xvfb = None
            if xvfb is not None:
                try:
                    await asyncio.shield(asyncio.to_thread(xvfb.stop))
                except BaseException:
                    pass

    async def _close_if_unused(self, *, max_active: int = 0) -> bool:
        """Close the browser only if active operations do not exceed *max_active*."""
        async with self._start_lock:
            async with self._active_lock:
                if self._active_count > max_active:
                    return False
                self._cancel_idle_task()
            await self._teardown_locked()
            return True

    async def close(self) -> None:
        self._cancel_idle_task()
        async with self._start_lock:
            await self._teardown_locked()
