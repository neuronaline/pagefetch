"""Playwright route interception, SSRF subresource checks, and XHR capture."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from ..models import CapturedXHR
from ..processing.challenges import CHALLENGE_DOMAINS, CHALLENGE_PATH_PREFIXES
from ..utils.urls import async_is_safe_host, registrable_host
from .actions import matches_xhr_pattern

# Resource-type blocking sets per stealth level.
# Note: stylesheets and websockets remain intact for layout & WAF verification.
BLOCK_LEVEL_SETS: dict[str, set[str]] = {
    "minimal": {"media", "beacon"},
    "balanced": {"media", "beacon", "ping"},
    "aggressive": {"media", "beacon", "ping", "image", "font"},
}

# Third-party tracking, ad, and session-recorder domains blocked across all frames.
_TRACKER_DOMAINS: frozenset[str] = frozenset(
    {
        "google-analytics.com",
        "googletagmanager.com",
        "doubleclick.net",
        "googlesyndication.com",
        "googleadservices.com",
        "hotjar.com",
        "clarity.ms",
        "criteo.com",
        "criteo.net",
        "outbrain.com",
        "taboola.com",
        "scorecardresearch.com",
        "segment.io",
        "segment.com",
        "amplitude.com",
        "mixpanel.com",
        "connect.facebook.net",
        "facebook.net",
    }
)


class PageNetworkTracker:
    """Per-page route interceptor, activity monitor, and background XHR collector."""

    def __init__(
        self,
        *,
        target_url: str,
        block_level: str,
        block_images: bool,
        has_proxy: bool,
        capture_xhr: bool | str | Sequence[str] = False,
        screenshot: bool = False,
    ) -> None:
        self._main_site = registrable_host(target_url)
        self._has_proxy = has_proxy
        self._capture_xhr = capture_xhr
        self._block_level = block_level
        blocked = set(BLOCK_LEVEL_SETS.get(block_level, BLOCK_LEVEL_SETS["balanced"]))
        if not block_images or screenshot:
            blocked.discard("image")
        self._blocked_types = frozenset(blocked)

        self.active: int = 0
        self.last_activity: float = time.monotonic()
        self.last_document_status: int | None = None
        self.captured_xhr: list[CapturedXHR] = []
        self.pending_xhr_tasks: set[asyncio.Task[Any]] = set()
        self._total_xhr_bytes = 0
        self._page: Any = None

    def activity(self) -> tuple[int, float]:
        """Return ``(in_flight_request_count, last_activity_monotonic)``."""
        return self.active, self.last_activity

    async def attach(self, page: Any) -> None:
        """Register request listeners and Playwright route filtering on *page*."""
        self._page = page
        page.on("request", self._on_request_started)
        page.on("requestfinished", self._on_request_finished)
        page.on("requestfailed", self._on_request_finished)
        page.on("response", self._on_response)
        await page.route("**/*", self._route_handler)

    async def detach(self, page: Any) -> None:
        """Cancel pending XHR tasks and flush Playwright routes before page close."""
        self.cancel_pending()
        if page is None:
            return
        try:
            await page.unroute("**/*")
        except Exception:
            pass
        # Flush in-flight route callbacks to prevent TargetClosedError warnings.
        await asyncio.sleep(0)

    def cancel_pending(self) -> None:
        """Cancel any unfinished XHR body capture tasks."""
        for task in list(self.pending_xhr_tasks):
            if not task.done():
                task.cancel()

    async def finalize_xhr(self) -> list[CapturedXHR] | None:
        """Await pending XHR body captures and return collected payloads if non-empty."""
        if self.pending_xhr_tasks:
            await asyncio.gather(*list(self.pending_xhr_tasks), return_exceptions=True)
        return self.captured_xhr or None

    def _on_request_started(self, _request: Any) -> None:
        self.active += 1
        self.last_activity = time.monotonic()

    def _on_request_finished(self, _request: Any) -> None:
        self.active = max(0, self.active - 1)
        self.last_activity = time.monotonic()

    def _on_response(self, resp: Any) -> None:
        try:
            req = getattr(resp, "request", None)
            if req and req.resource_type == "document":
                if getattr(resp, "frame", None) == self._page.main_frame:
                    self.last_document_status = resp.status
            if self._capture_xhr and req and req.resource_type in {"xhr", "fetch"}:
                if matches_xhr_pattern(resp.url, self._capture_xhr):
                    pending_count = len(self.pending_xhr_tasks)
                    captured_count = len(self.captured_xhr)
                    if pending_count + captured_count < 50 and self._total_xhr_bytes < 4 * 1024 * 1024:
                        try:
                            loop = asyncio.get_running_loop()
                            task = loop.create_task(self._capture_xhr_body(resp, req.method))
                            self.pending_xhr_tasks.add(task)
                            task.add_done_callback(self.pending_xhr_tasks.discard)
                        except RuntimeError:
                            pass
        except Exception:
            pass

    async def _capture_xhr_body(self, resp: Any, req_method: str) -> None:
        try:
            ct = (resp.headers.get("content-type") or "").lower()
            resp_headers = dict(resp.headers) if hasattr(resp, "headers") else None
            body_bytes = await resp.body()
            size = len(body_bytes)
            self._total_xhr_bytes += size
            truncated = size > 512 * 1024
            if truncated:
                body_bytes = body_bytes[: 512 * 1024]
            text = body_bytes.decode("utf-8", errors="replace")
            if "application/json" in ct or "+json" in ct:
                try:
                    body_val: Any = json.loads(text)
                except Exception:
                    body_val = text
            else:
                body_val = text
            self.captured_xhr.append(
                CapturedXHR(
                    url=resp.url,
                    method=req_method,
                    status_code=resp.status,
                    content_type=ct or None,
                    body=body_val,
                    truncated=truncated,
                    headers=resp_headers,
                )
            )
        except Exception:
            pass

    async def _route_handler(self, route: Any) -> None:
        request = route.request
        request_url = request.url
        res_type = request.resource_type
        req_host = ""
        req_path = ""
        if request_url.startswith(("http://", "https://", "ws://", "wss://")):
            req_parsed = urlsplit(request_url)
            req_host = (req_parsed.hostname or "").lower()
            req_path = (req_parsed.path or "").lower()

        is_challenge_host = any(
            req_host == d or req_host.endswith("." + d)
            for d in CHALLENGE_DOMAINS
        ) or any(req_path.startswith(p) for p in CHALLENGE_PATH_PREFIXES)

        is_blocked_type = False if is_challenge_host else (res_type in self._blocked_types)
        is_tracker = bool(
            not is_challenge_host
            and req_host
            and any(req_host == d or req_host.endswith("." + d) for d in _TRACKER_DOMAINS)
        )

        external_frame = False
        external_script = False
        main_frame = getattr(self._page, "main_frame", None)
        if (
            self._block_level != "minimal"
            and not is_challenge_host
            and main_frame is not None
            and getattr(request, "frame", None) != main_frame
            and request_url.startswith(("http://", "https://"))
        ):
            if res_type == "document":
                external_frame = registrable_host(request_url) != self._main_site
            elif res_type in {"script", "xhr", "fetch"}:
                external_script = registrable_host(request_url) != self._main_site

        try:
            if is_blocked_type or is_tracker or external_frame or external_script:
                await route.abort()
                return
            if req_host and not await async_is_safe_host(req_host, resolve_dns=not self._has_proxy):
                await route.abort()
                return
            await route.continue_()
        except Exception:
            pass
