"""Lazy single-instance Camoufox browser transport."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup

from ..models import FetchErrorInfo
from ..processing.detector import analyze_html
from ..proxy.providers import ProxySettings
from ..utils.urls import registrable_host
from .http import TransportFailure
from .readiness import controlled_scroll, wait_for_stability


@dataclass(slots=True)
class BrowserResponse:
    url: str
    status_code: int | None
    html: str
    warnings: list[str]


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
    ) -> None:
        self.semaphore = semaphore
        self.timeout = timeout
        self.retries = retries
        self.proxy = proxy
        self.max_content_size = max_content_size
        self.confidence_threshold = confidence_threshold
        self._manager: Any = None
        self._browser: Any = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._browser is not None:
            return
        async with self._start_lock:
            if self._browser is not None:
                return
            try:
                from camoufox.async_api import AsyncCamoufox

                options: dict[str, Any] = {"headless": True}
                browser_proxy = self.proxy.browser_config()
                if browser_proxy:
                    options["proxy"] = browser_proxy
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
                        "Camoufox could not be launched; install its browser with 'python -m camoufox fetch'",
                        True,
                        type(exc).__name__,
                    )
                ) from exc

    async def fetch(self, url: str) -> BrowserResponse:
        async with self.semaphore:
            last_failure: TransportFailure | None = None
            for attempt in range(self.retries + 1):
                try:
                    await self.start()
                    result = await self._fetch_page(url)
                    report = analyze_html(result.html)
                    incomplete = report.score < self.confidence_threshold
                    if (not result.html.strip() or report.challenge or incomplete) and attempt < self.retries:
                        await asyncio.sleep(0.4 * (2**attempt))
                        continue
                    return result
                except TransportFailure as exc:
                    last_failure = exc
                    if not exc.error.retryable or attempt >= self.retries:
                        raise
                    if exc.error.code in {"browser_launch_error", "browser_navigation_error"}:
                        await self.close()
                    await asyncio.sleep(0.4 * (2**attempt))
            assert last_failure is not None
            raise last_failure

    async def _fetch_page(self, url: str) -> BrowserResponse:
        try:
            async with asyncio.timeout(self.timeout):
                return await self._fetch_page_once(url)
        except TimeoutError as exc:
            raise TransportFailure(
                FetchErrorInfo("browser_timeout", "browser navigation timed out", True, type(exc).__name__)
            ) from exc

    async def _fetch_page_once(self, url: str) -> BrowserResponse:
        page: Any = None
        warnings: list[str] = []
        try:
            page = await self._browser.new_page()
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
                if request.resource_type in {"font", "media"} or external_frame:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", route_handler)
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(self.timeout * 1000),
            )
            await wait_for_stability(
                page,
                timeout=min(5.0, self.timeout / 3),
                network_activity=lambda: (network["active"], network["last_activity"]),
            )
            limit_reached = await controlled_scroll(page)
            await wait_for_stability(
                page,
                timeout=min(4.0, self.timeout / 4),
                stable_rounds=2,
                network_activity=lambda: (network["active"], network["last_activity"]),
            )
            if limit_reached:
                warnings.append("Maximum controlled-scroll limit was reached.")
            # Fast size pre-check – avoids serialising the whole DOM for huge pages.
            outer_html = await page.evaluate("() => document.documentElement.outerHTML")
            if len(outer_html.encode("utf-8")) > self.max_content_size:
                raise TransportFailure(
                    FetchErrorInfo("content_too_large", "rendered content exceeds maximum size", False)
                )
            html = await page.content()
            html = await self._include_same_site_frames(page, html, page.url or url, warnings)
            if len(html.encode("utf-8")) > self.max_content_size:
                raise TransportFailure(
                    FetchErrorInfo("content_too_large", "rendered content exceeds maximum size", False)
                )
            return BrowserResponse(
                url=page.url,
                status_code=response.status if response else None,
                html=html,
                warnings=warnings,
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
                    await page.close()
                except Exception:
                    pass

    async def _include_same_site_frames(
        self, page: Any, html: str, main_url: str, warnings: list[str]
    ) -> str:
        soup = BeautifulSoup(html, "lxml")
        main_site = registrable_host(main_url)
        matched_iframes: set[int] = set()
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
