"""Lazy single-instance Camoufox browser transport with ephemeral page contexts."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import random
import sys
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..config import VALID_SCREENSHOT_FORMATS, VALID_SCREENSHOT_MODES, merge_headers
from ..cookies import CookieProfile
from ..models import ActionResult, CapturedXHR, FetchErrorInfo, PageAction
from ..processing.detector import ConfidenceReport, analyze_html
from ..processing.sections import prepare_expanded_dom
from ..proxy.providers import ProxySettings
from ..utils.urls import registrable_host
from .expander import (
    expand_disclosures_only,
    iterate_dynamic_tabs,
    neutralize_overlays_and_unlock_scroll,
)
from .http import TransportFailure
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
        browser_pre_check_byte_margin: float = 1.5,
        confidence_threshold: float = 0.80,
        block_images: bool = True,
        block_level: str = "aggressive",
        humanize: bool = False,
        accept_language: str = "en-US,en;q=0.5",
        max_browser_requests: int = 50,
        idle_ttl: float | None = 180.0,
        max_scroll_steps: int = 25,
        max_scroll_height: int = 150_000,
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
        self.accept_language = accept_language
        self.max_browser_requests = max_browser_requests
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
                await self.close()
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
        primary_locale = self.accept_language.split(",")[0].split(";")[0].strip() or "en-US"
        options: dict[str, Any] = {
            "headless": not use_xvfb,
            "humanize": self.humanize,
            "enable_cache": True,
            "block_webrtc": not has_proxy,
            "locale": primary_locale,
        }
        has_geoip = bool(importlib.util.find_spec("maxminddb"))
        if has_proxy and effective_proxy is not None:
            options["geoip"] = has_geoip
            browser_proxy = effective_proxy.browser_config()
            if browser_proxy:
                options["proxy"] = browser_proxy
        else:
            options["geoip"] = False
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
        if self._browser is not None and getattr(self._browser, "is_connected", lambda: True)():
            return
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
            if self.max_browser_requests > 0 and self._request_count >= self.max_browser_requests:
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
                    report = analyze_html(result.html, soup=result.soup)
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
                            async with self._active_lock:
                                can_reset = self._active_count <= 1
                            if can_reset:
                                if new_proxy is not None:
                                    self.proxy = new_proxy
                                await self.close()
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
                    new_proxy = None
                    if callable(proxy_resolver):
                        new_proxy = proxy_resolver(attempt + 1)
                        if new_proxy is not None:
                            proxy = new_proxy
                    if exc.error.code in {"browser_launch_error", "browser_navigation_error", "browser_timeout"}:
                        async with self._active_lock:
                            can_reset = self._active_count <= 1
                        if can_reset:
                            if new_proxy is not None:
                                self.proxy = new_proxy
                            await self.close()
                        else:
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
                await self.close()
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
    ) -> AsyncIterator[tuple[Any, PageNetworkTracker, bool]]:
        """Create and deterministically tear down an isolated context + tracked page."""
        has_proxy = bool(effective_proxy and effective_proxy.url)
        # Never overwrite GeoIP Accept-Language with the default when a proxy is active.
        base_lang = (
            {"Accept-Language": self.accept_language}
            if not has_proxy and self.accept_language != "en-US,en;q=0.5"
            else None
        )
        context_headers = merge_headers(base_lang, headers)

        context_kwargs: dict[str, Any] = {
            "bypass_csp": False,
            "ignore_https_errors": True,
            "java_script_enabled": True,
            "accept_downloads": False,
        }
        browser_proxy = effective_proxy.browser_config() if effective_proxy else None
        if browser_proxy:
            context_kwargs["proxy"] = browser_proxy

        context = None
        page = None
        tracker = PageNetworkTracker(
            target_url=url,
            block_level=self.block_level,
            block_images=self.block_images,
            has_proxy=has_proxy,
            capture_xhr=capture_xhr,
            screenshot=screenshot,
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
            if context_headers:
                try:
                    await page.set_extra_http_headers(context_headers)
                except Exception:
                    pass
            await tracker.attach(page)
            has_auth_proxy = bool(browser_proxy and browser_proxy.get("username"))
            yield page, tracker, has_auth_proxy
        finally:
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

    async def _navigate_and_await_readiness(
        self,
        page: Any,
        tracker: PageNetworkTracker,
        url: str,
        *,
        has_auth_proxy: bool,
        page_deadline: float,
    ) -> tuple[Any, bool, bool]:
        rem_ms = max(5000.0, (page_deadline - time.monotonic()) * 1000)
        goto_timeout_ms = int(min(self.timeout * 1000, rem_ms))
        if has_auth_proxy:
            async with self._proxy_nav_lock:
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=goto_timeout_ms
                )
        else:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=goto_timeout_ms
            )
        unresolved_challenge, weak_main = await self._wait_initial_readiness(page, tracker)
        return response, unresolved_challenge, weak_main

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

        if len(html) > self.max_content_size * self.browser_pre_check_byte_margin:
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
        try:
            async with asyncio.timeout(page_timeout):
                async with self._open_isolated_page(
                    url,
                    effective_proxy=effective_proxy,
                    headers=headers,
                    cookie_profile=cookie_profile,
                    capture_xhr=capture_xhr,
                    screenshot=(screenshot != "none"),
                ) as (page, tracker, has_auth_proxy):
                    response, unresolved_challenge, weak_main = await self._navigate_and_await_readiness(
                        page,
                        tracker,
                        url,
                        has_auth_proxy=has_auth_proxy,
                        page_deadline=page_deadline,
                    )
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
                    )
        except TransportFailure:
            raise
        except TimeoutError as exc:
            raise TransportFailure(
                FetchErrorInfo("browser_timeout", "browser navigation timed out", True, type(exc).__name__)
            ) from exc
        except Exception as exc:
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

    async def close(self) -> None:
        self._cancel_idle_task()
        async with self._start_lock:
            if self._manager is not None:
                try:
                    await self._manager.__aexit__(None, None, None)
                finally:
                    self._manager = None
                    self._browser = None
            elif self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            xvfb = self._xvfb
            self._xvfb = None
            if xvfb is not None:
                try:
                    await asyncio.to_thread(xvfb.stop)
                except Exception:
                    pass
