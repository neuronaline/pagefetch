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
from ..processing.pipeline import is_unsupported_binary_response, parse_content_type
from ..utils.urls import _precheck_host_safety, async_is_safe_host, registrable_host
from .actions import matches_xhr_pattern

_NETWORK_SCHEMES: frozenset[str] = frozenset({"http", "https", "ws", "wss"})
_SAFE_NON_NETWORK_SCHEMES: frozenset[str] = frozenset({"about", "data", "blob"})
_CREDENTIAL_HEADERS: frozenset[str] = frozenset(
    {"authorization", "cookie", "proxy-authorization"}
)


def _origin_tuple(url: str) -> tuple[str, str, int] | None:
    """Return normalized ``(http_scheme, host, port)`` for same-origin header scoping."""
    parsed = urlsplit(url)
    raw_scheme = parsed.scheme.lower()
    if raw_scheme not in _NETWORK_SCHEMES:
        return None
    norm_scheme = "https" if raw_scheme in {"https", "wss"} else "http"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return None
    default_port = 443 if norm_scheme == "https" else 80
    try:
        port = parsed.port or default_port
    except ValueError:
        return None
    return (norm_scheme, host, port)

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
        strip_cross_origin_credentials: bool = False,
        max_content_size: int | None = None,
    ) -> None:
        self._main_site = registrable_host(target_url)
        self._target_origin = _origin_tuple(target_url)
        self._strip_cross_origin_credentials = strip_cross_origin_credentials
        self._max_content_size = max_content_size
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
        self.last_document_url: str | None = None
        self.last_document_headers: dict[str, str] | None = None
        self.last_document_response: Any = None
        self.ssrf_blocked_url: str | None = None
        self.unsupported_content_type: str | None = None
        self.oversized_response: bool = False
        self.abort_event: asyncio.Event = asyncio.Event()
        self.captured_xhr: list[CapturedXHR] = []
        self.pending_xhr_tasks: set[asyncio.Task[Any]] = set()
        self._pending_ssrf_tasks: set[asyncio.Task[Any]] = set()
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
        self.last_document_response = None
        if page is None:
            return
        try:
            await page.unroute("**/*")
        except Exception:
            pass
        # Flush in-flight route callbacks to prevent TargetClosedError warnings.
        await asyncio.sleep(0)

    def cancel_pending(self) -> None:
        """Cancel any unfinished XHR or SSRF verification tasks."""
        for task in (*self.pending_xhr_tasks, *self._pending_ssrf_tasks):
            if not task.done():
                task.cancel()

    async def check_ssrf(self, final_url: str | None = None) -> str | None:
        """Await pending redirect host checks and return any SSRF-blocked URL."""
        if self._pending_ssrf_tasks:
            await asyncio.gather(*list(self._pending_ssrf_tasks), return_exceptions=True)
        if self.ssrf_blocked_url:
            return self.ssrf_blocked_url
        if final_url and final_url.startswith(("http://", "https://")):
            host = (urlsplit(final_url).hostname or "").lower()
            if host and not await async_is_safe_host(host, resolve_dns=not self._has_proxy):
                self.ssrf_blocked_url = final_url
                return final_url
        return None

    async def finalize_xhr(self) -> list[CapturedXHR] | None:
        """Await pending XHR body captures and return collected payloads if non-empty."""
        if self.pending_xhr_tasks:
            await asyncio.gather(*list(self.pending_xhr_tasks), return_exceptions=True)
        return self.captured_xhr or None

    async def _verify_redirect_dns(self, host: str, req_url: str) -> None:
        if not await async_is_safe_host(host, resolve_dns=not self._has_proxy):
            if self.ssrf_blocked_url is None:
                self.ssrf_blocked_url = req_url
                self.abort_event.set()

    def _inspect_redirect_request(self, request: Any) -> None:
        req_url = getattr(request, "url", "") or ""
        scheme = req_url.split(":", 1)[0].lower() if ":" in req_url else ""
        main_frame = getattr(self._page, "main_frame", None)
        is_main_doc = (
            getattr(request, "resource_type", None) == "document"
            and main_frame is not None
            and getattr(request, "frame", None) == main_frame
        )
        if scheme not in _NETWORK_SCHEMES:
            if scheme not in _SAFE_NON_NETWORK_SCHEMES and is_main_doc and self.ssrf_blocked_url is None:
                self.ssrf_blocked_url = req_url
                self.abort_event.set()
            return
        req_host = (urlsplit(req_url).hostname or "").lower()
        if not req_host:
            return
        verdict = _precheck_host_safety(req_host, resolve_dns=not self._has_proxy)
        if verdict is False:
            if is_main_doc and self.ssrf_blocked_url is None:
                self.ssrf_blocked_url = req_url
                self.abort_event.set()
        elif isinstance(verdict, str) and is_main_doc:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._verify_redirect_dns(verdict, req_url))
                self._pending_ssrf_tasks.add(task)
                task.add_done_callback(self._pending_ssrf_tasks.discard)
            except RuntimeError:
                pass

    def _on_request_started(self, request: Any) -> None:
        self.active += 1
        self.last_activity = time.monotonic()
        if getattr(request, "redirected_from", None) is not None:
            self._inspect_redirect_request(request)

    def _on_request_finished(self, _request: Any) -> None:
        self.active = max(0, self.active - 1)
        self.last_activity = time.monotonic()

    def _on_response(self, resp: Any) -> None:
        try:
            req = getattr(resp, "request", None)
            if req and req.resource_type == "document":
                if (
                    getattr(resp, "frame", None) == self._page.main_frame
                    and not (300 <= resp.status < 400)
                ):
                    self.last_document_status = resp.status
                    self.last_document_url = resp.url
                    self.last_document_response = resp
                    raw_headers = getattr(resp, "headers", None)
                    if isinstance(raw_headers, dict):
                        hdr_copy = dict(raw_headers)
                        self.last_document_headers = hdr_copy
                        if resp.status < 400:
                            ct_val = ""
                            cd_val = ""
                            cl_val = ""
                            for k, v in hdr_copy.items():
                                lk = str(k).lower()
                                if lk == "content-type":
                                    ct_val = str(v)
                                elif lk == "content-disposition":
                                    cd_val = str(v)
                                elif lk == "content-length":
                                    cl_val = str(v).strip()
                            if is_unsupported_binary_response(ct_val, resp.url, cd_val):
                                self.unsupported_content_type = parse_content_type(ct_val)
                                self.abort_event.set()
                            elif (
                                self._max_content_size is not None
                                and cl_val.isdigit()
                                and int(cl_val) > self._max_content_size
                            ):
                                self.oversized_response = True
                                self.abort_event.set()
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
        main_frame = getattr(self._page, "main_frame", None)
        is_main_doc = (
            res_type == "document"
            and main_frame is not None
            and getattr(request, "frame", None) == main_frame
        )
        scheme = request_url.split(":", 1)[0].lower() if ":" in request_url else ""
        if scheme not in _NETWORK_SCHEMES:
            try:
                if scheme in _SAFE_NON_NETWORK_SCHEMES:
                    await route.continue_()
                else:
                    if is_main_doc and self.ssrf_blocked_url is None:
                        self.ssrf_blocked_url = request_url
                    await route.abort()
            except Exception:
                pass
            return

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
        if (
            self._block_level != "minimal"
            and not is_challenge_host
            and main_frame is not None
            and getattr(request, "frame", None) != main_frame
            and scheme in {"http", "https"}
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
                if is_main_doc and self.ssrf_blocked_url is None:
                    self.ssrf_blocked_url = request_url
                await route.abort()
                return
            if (
                self._strip_cross_origin_credentials
                and _origin_tuple(request_url) != self._target_origin
            ):
                stripped_hdrs = {
                    k: v
                    for k, v in (await request.all_headers()).items()
                    if not k.startswith(":")
                    and k.lower() != "host"
                    and k.lower() not in _CREDENTIAL_HEADERS
                }
                await route.continue_(headers=stripped_hdrs)
            else:
                await route.continue_()
        except Exception:
            pass
