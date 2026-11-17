"""Main PageFetch client and fetch pipeline."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, overload
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .cache import SQLiteCache, build_cache_key
from .config import _UNSET, VALID_MODES, VALID_PROXIES, PageFetchConfig, merge_headers
from .constants import (
    BLOCKED_STATUS_CODES,
    RETRYABLE_STATUS_CODES,
    VALID_HTTP_METHODS,
    looks_like_waf_challenge,
)
from .cookies import CookiePool, CookieSession, parse_cookie_pool
from .exceptions import PageFetchError
from .fetching import HTTPFetcher, HTTPResponse, TransportFailure
from .fetching.actions import normalize_actions
from .fetching.manager import TransportManager
from .fetching.policy import HostPolicy
from .models import FetchErrorInfo, FetchResult, PageAction
from .processing.detector import ConfidenceReport, analyze_html, detect_challenge_provider
from .processing.pipeline import (
    build_html_result,
    build_non_html_result,
    decode_response_body,
    detect_document_kind,
    is_pdf_content,
    parse_content_type,
)
from .proxy import ProxyConfigurationError, ProxySettings
from .proxy.providers import bump_domain_session
from .utils.durations import parse_duration
from .utils.urls import async_is_safe_host, normalize_url, registrable_host, validate_url

logger = logging.getLogger("pagefetch")


def _mapping_cache_token(mapping: dict[str, str], *, lower_keys: bool = False) -> str:
    parts = sorted((k.lower(), v) if lower_keys else (k, v) for k, v in mapping.items())
    token_str = "&".join(f"{k}={v}" for k, v in parts)
    return sha256(token_str.encode()).hexdigest()[:16]


def _coerce_cookie_pool(cookies: Any) -> CookiePool | None:
    if cookies is None:
        return None
    pool = cookies if isinstance(cookies, CookiePool) else parse_cookie_pool(cookies)
    return pool if pool else None


class PageFetch:
    """Asynchronous HTTP-first page fetcher with automatic Camoufox fallback.

    The client may be used as an async context manager. Calling :meth:`fetch`
    without an explicit context also starts resources lazily; call :meth:`close`
    when finished in that case.
    """

    @overload
    def __init__(self, *, config: PageFetchConfig) -> None: ...

    @overload
    def __init__(
        self,
        *,
        mode: Literal["auto", "http", "browser"] = "auto",
        proxy: Literal["none", "custom", "decodo", "byteful"] = "none",
        cleaning_level: Literal["minimal", "standard", "maximum"] = "standard",
        stealth_level: Literal["off", "balanced", "max"] = "off",
        expand: bool = False,
        cache_enabled: bool = True,
        cache_ttl: str | int = "24h",
        raise_on_error: bool = False,
        headers: dict[str, str] | None = None,
        cookies: Any = None,
    ) -> None: ...

    def __init__(
        self,
        *,
        config: PageFetchConfig | None = None,
        mode: Literal["auto", "http", "browser"] = _UNSET,
        proxy: Literal["none", "custom", "decodo", "byteful"] = _UNSET,
        cleaning_level: Literal["minimal", "standard", "maximum"] = _UNSET,
        stealth_level: Literal["off", "balanced", "max"] = _UNSET,
        expand: bool = _UNSET,
        cache_enabled: bool = _UNSET,
        cache_ttl: str | int = _UNSET,
        raise_on_error: bool = _UNSET,
        headers: dict[str, str] | None = _UNSET,
        cookies: Any = _UNSET,
    ) -> None:
        core_kwargs = {
            key: value
            for key, value in {
                "mode": mode,
                "proxy": proxy,
                "cleaning_level": cleaning_level,
                "stealth_level": stealth_level,
                "expand": expand,
                "cache_enabled": cache_enabled,
                "cache_ttl": cache_ttl,
                "raise_on_error": raise_on_error,
                "headers": headers,
                "cookies": cookies,
            }.items()
            if value is not _UNSET
        }
        if config is not None:
            if not isinstance(config, PageFetchConfig):
                raise TypeError(
                    f"config must be an instance of PageFetchConfig, got {type(config).__name__}"
                )
            if core_kwargs:
                raise ValueError(
                    "cannot pass keyword overrides alongside config=; configure them on PageFetchConfig"
                )
            self.config = config
        else:
            self.config = PageFetchConfig(**core_kwargs)

        self._cookie_pool: CookiePool = self.config.build_cookie_pool()
        self._policy = HostPolicy(request_pacing=self.config.request_pacing)
        self._transports = TransportManager(self.config)
        self._cache = SQLiteCache(self.config.cache_path) if self.config.cache_enabled else None
        self._started = False
        self._startup_warnings: list[str] = []

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
        async with self._transports.lifecycle_lock:
            if self._started and not self._transports.is_closed:
                return self
            if self._transports.is_closed:
                raise RuntimeError("PageFetch has already been closed")
            if self._cache:
                try:
                    await self._cache.start()
                except Exception as exc:
                    try:
                        await self._cache.close()
                    except Exception:
                        pass
                    self._cache = None
                    self._startup_warnings.append("Cache initialization failed; caching is disabled.")
                    logger.warning("cache initialization failed: %s", type(exc).__name__)
            self._started = True
            self._log_fingerprint_summary()
        return self

    def _log_fingerprint_summary(self) -> None:
        """Emit a one-line diagnostic summarising the anti-detection posture."""
        cfg = self.config
        parts = [
            f"stealth={cfg.stealth_level}",
            f"humanize={'on' if cfg.humanize else 'off'}",
            f"block={cfg.block_level}",
        ]
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
        """Release all HTTP clients, browser processes, and the cache database."""
        await self._transports.close(cache=self._cache)

    async def _begin_operation(self) -> bool:
        try:
            await self.start()
        except RuntimeError:
            return False
        return await self._transports.begin_operation()

    @property
    def cookie_pool(self) -> CookiePool:
        """Return the client-wide :class:`~pagefetch.cookies.CookiePool`."""
        return self._cookie_pool

    def _make_cookie_session(
        self,
        url: str,
        per_call_pool: CookiePool | None = None,
    ) -> CookieSession:
        return self._cookie_pool.session(
            url,
            rotation=self.config.session_rotation,
            cooldown=self.config.cookie_cooldown,
            override=per_call_pool,
        )

    @staticmethod
    def _build_block_error(
        code: str,
        message: str,
        retryable: bool,
        *,
        url: str,
        status_code: int | None = None,
        html: str = "",
        headers: Any = None,
    ) -> FetchErrorInfo:
        domain = registrable_host(url) or (urlsplit(url).hostname or "").lower() or None
        provider = detect_challenge_provider(html, headers)
        hint: str | None = None
        if code in {"captcha_detected", "blocked"}:
            target_label = domain or "target host"
            hint = (
                f"Access to {target_label} was gated ({provider or code}); "
                "supply valid profile/session cookies via cookies=... or cookie_file=..."
            )
        return FetchErrorInfo(
            code=code,
            message=message,
            retryable=retryable,
            provider=provider,
            domain=domain,
            status_code=status_code,
            hint=hint,
        )

    async def fetch(
        self,
        url: str,
        *,
        mode: Literal["auto", "http", "browser"] | None = None,
        proxy: Literal["none", "custom", "decodo", "byteful"] | None = None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        cookies: Any = None,
        content: bytes | str | None = None,
        data: dict[str, Any] | None = None,
        json: Any = None,
        actions: Sequence[PageAction | dict[str, Any] | str] | None = None,
        capture_xhr: bool | str | Sequence[str] = False,
        expand: bool | None = None,
        use_cache: bool = True,
        cache_ttl: str | int | None = None,
        raise_on_error: bool | None = None,
    ) -> FetchResult:
        """Fetch one URL and return a structured result."""
        normalized_method = method.upper()
        if normalized_method not in VALID_HTTP_METHODS:
            raise ValueError(f"method must be one of {sorted(VALID_HTTP_METHODS)}")

        normalized_actions = normalize_actions(actions) if actions else None
        effective_expand = self.config.expand if expand is None else bool(expand)
        per_call_pool = _coerce_cookie_pool(cookies)

        selected_mode = mode or self.config.mode
        if normalized_actions or capture_xhr or effective_expand:
            if mode == "http":
                raise ValueError(
                    "actions, capture_xhr, and expand cannot be executed in http mode; browser mode is required"
                )
            selected_mode = "browser"

        if selected_mode == "browser" and normalized_method != "GET":
            raise ValueError("browser mode does not support non-GET HTTP methods")

        selected_proxy = proxy or self.config.proxy
        should_raise = self.config.raise_on_error if raise_on_error is None else raise_on_error

        bypass_cache = (
            not use_cache
            or normalized_method not in {"GET", "HEAD"}
            or data is not None
            or json is not None
            or content is not None
            or bool(normalized_actions)
        )

        op_cache_settings: dict[str, Any] = {"method": normalized_method}
        if headers:
            op_cache_settings["req_headers"] = _mapping_cache_token(headers, lower_keys=True)
        if capture_xhr:
            op_cache_settings["capture_xhr"] = repr(capture_xhr)
        if effective_expand:
            op_cache_settings["expand"] = True

        async def transport(normalized_url: str) -> FetchResult:
            await self._policy.pace(normalized_url)
            cookie_session = self._make_cookie_session(normalized_url, per_call_pool)
            if is_pdf_content("", b"", url=normalized_url) and not normalized_actions and not effective_expand:
                return await self._fetch_http_or_auto(
                    normalized_url,
                    "http",
                    selected_proxy,
                    method=normalized_method,
                    headers=headers,
                    cookie_session=cookie_session,
                    content=content,
                    data=data,
                    json_data=json,
                )
            effective_mode = selected_mode
            is_remembered = self._policy.is_remembered(normalized_url)
            if effective_mode == "auto" and is_remembered:
                logger.debug("Host %s remembered in browser memory; bypassing HTTP", normalized_url)
                effective_mode = "browser"
            if effective_mode == "browser":
                return await self._fetch_browser(
                    normalized_url,
                    selected_proxy,
                    status_code=None,
                    proxy_salt="fallback" if is_remembered else "",
                    actions=normalized_actions,
                    capture_xhr=capture_xhr,
                    expand=effective_expand,
                    headers=headers,
                    cookie_session=cookie_session,
                )
            return await self._fetch_http_or_auto(
                normalized_url,
                effective_mode,
                selected_proxy,
                method=normalized_method,
                headers=headers,
                cookie_session=cookie_session,
                content=content,
                data=data,
                json_data=json,
            )

        return await self._run_operation(
            url=url,
            mode=selected_mode,
            proxy=selected_proxy,
            use_cache=not bypass_cache,
            cache_ttl=cache_ttl,
            should_raise=should_raise,
            cache_settings=self._cache_settings(**op_cache_settings),
            error_context="fetching the page",
            transport=transport,
            per_call_cookies=per_call_pool,
        )

    async def fetch_many(
        self,
        urls: Iterable[str],
        *,
        mode: Literal["auto", "http", "browser"] | None = None,
        proxy: Literal["none", "custom", "decodo", "byteful"] | None = None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        cookies: Any = None,
        content: bytes | str | None = None,
        data: dict[str, Any] | None = None,
        json: Any = None,
        actions: Sequence[PageAction | dict[str, Any] | str] | None = None,
        capture_xhr: bool | str | Sequence[str] = False,
        expand: bool | None = None,
        use_cache: bool = True,
        cache_ttl: str | int | None = None,
        raise_on_error: bool | None = None,
    ) -> list[FetchResult]:
        """Fetch unique URLs concurrently while preserving input order."""
        ordered = list(urls)
        unique = list(dict.fromkeys(ordered))
        shared_call_pool = _coerce_cookie_pool(cookies)

        fanout_limit = max(
            self.config.http_concurrency,
            self.config.browser_concurrency,
        ) * 2
        fanout_sem = asyncio.Semaphore(fanout_limit)

        async def run_one(item: str) -> FetchResult:
            async with fanout_sem:
                try:
                    return await self.fetch(
                        item,
                        mode=mode,
                        proxy=proxy,
                        method=method,
                        headers=headers,
                        cookies=shared_call_pool,
                        content=content,
                        data=data,
                        json=json,
                        actions=actions,
                        capture_xhr=capture_xhr,
                        expand=expand,
                        use_cache=use_cache,
                        cache_ttl=cache_ttl,
                        raise_on_error=raise_on_error,
                    )
                except PageFetchError as exc:
                    return FetchResult(
                        url=str(item),
                        success=False,
                        proxy_provider=proxy or self.config.proxy,
                        error=exc.error,
                        fetched_at=datetime.now(UTC),
                    )

        fetched = await asyncio.gather(*(run_one(item) for item in unique))
        by_url = dict(zip(unique, fetched, strict=True))
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
        headers: dict[str, str] | None = None,
        cookies: Any = None,
        actions: Sequence[PageAction | dict[str, Any] | str] | None = None,
        capture_xhr: bool | str | Sequence[str] = False,
        expand: bool | None = None,
        use_cache: bool = True,
        cache_ttl: str | int | None = None,
        raise_on_error: bool | None = None,
    ) -> FetchResult:
        """Fetch a page and return its RAW HTML, structural summary, and/or screenshot."""
        normalized_actions = normalize_actions(actions) if actions else None
        effective_expand = self.config.expand if expand is None else bool(expand)
        per_call_pool = _coerce_cookie_pool(cookies)
        bypass_cache = not use_cache or bool(normalized_actions)
        selected_proxy = proxy or self.config.proxy
        should_raise = self.config.raise_on_error if raise_on_error is None else raise_on_error

        op_cache_settings: dict[str, Any] = {
            "compact_structure": compact_structure,
            "screenshot": screenshot,
            "screenshot_format": screenshot_format,
            "structure": structure,
        }
        if headers:
            op_cache_settings["req_headers"] = _mapping_cache_token(headers, lower_keys=True)
        if capture_xhr:
            op_cache_settings["capture_xhr"] = repr(capture_xhr)
        if effective_expand:
            op_cache_settings["expand"] = True

        async def transport(normalized_url: str) -> FetchResult:
            await self._policy.pace(normalized_url)
            cookie_session = self._make_cookie_session(normalized_url, per_call_pool)
            return await self._fetch_browser(
                normalized_url,
                selected_proxy,
                status_code=None,
                actions=normalized_actions,
                capture_xhr=capture_xhr,
                expand=effective_expand,
                headers=headers,
                cookie_session=cookie_session,
                structure=structure,
                compact_structure=compact_structure,
                screenshot=screenshot,
                screenshot_format=screenshot_format,
            )

        return await self._run_operation(
            url=url,
            mode="browser",
            proxy=selected_proxy,
            use_cache=not bypass_cache,
            cache_ttl=cache_ttl,
            should_raise=should_raise,
            cache_settings=self._cache_settings(**op_cache_settings),
            requested_screenshot=screenshot != "none",
            error_context="extracting the page",
            transport=transport,
            per_call_cookies=per_call_pool,
        )

    def _cache_settings(self, **operation_settings: Any) -> dict[str, Any]:
        """Return cache-key settings shared by fetch and extraction operations."""
        settings = {
            "accept_language": self.config.accept_language,
            "block_images": self.config.block_images,
            "block_level": self.config.block_level,
            "cleaning_level": self.config.cleaning_level,
            "confidence_threshold": self.config.confidence_threshold,
            "max_redirects": self.config.max_redirects,
        }
        if self.config.headers:
            settings["cfg_headers"] = _mapping_cache_token(self.config.headers, lower_keys=True)
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
        per_call_cookies: CookiePool | None = None,
    ) -> FetchResult:
        """Run the validation, lifecycle, cache, and result pipeline for one operation."""
        started_at = time.perf_counter()
        try:
            self._validate_fetch_options(mode, proxy)
            effective_proxy = proxy if proxy is not None else self.config.proxy
            has_proxy = bool(effective_proxy and effective_proxy != "none")
            parsed = validate_url(url, check_ssrf=False)
            normalized_url = normalize_url(url, parsed=parsed)
            if parsed.hostname and not await async_is_safe_host(
                parsed.hostname, resolve_dns=not has_proxy
            ):
                raise ValueError(
                    "URL points to a restricted local or private network address (SSRF protection)"
                )
        except (TypeError, ValueError) as exc:
            code = "unsupported_scheme" if "scheme" in str(exc) else "invalid_url"
            return self._finish_error(
                url=str(url),
                proxy=proxy,
                error=FetchErrorInfo(code, str(exc), False, type(exc).__name__),
                started_at=started_at,
                should_raise=should_raise,
            )
        if not await self._begin_operation():
            result = self._finish_error(
                url=normalized_url,
                proxy=proxy,
                error=FetchErrorInfo("client_closed", "PageFetch client has been closed", False),
                started_at=started_at,
                should_raise=should_raise,
            )
            result.warnings[:0] = self._startup_warnings
            return result
        try:
            try:
                ttl = self.config.cache_ttl if cache_ttl is None else parse_duration(cache_ttl)
            except (TypeError, ValueError) as exc:
                return self._finish_error(
                    url=normalized_url,
                    proxy=proxy,
                    error=FetchErrorInfo(
                        "invalid_cache_ttl", f"Invalid cache_ttl: {exc}", False, type(exc).__name__
                    ),
                    started_at=started_at,
                    should_raise=should_raise,
                )
            effective_cache_settings = dict(cache_settings)
            cfg_cookie_token = self._cookie_pool.cache_token_for_url(normalized_url)
            if cfg_cookie_token:
                effective_cache_settings["cfg_cookies"] = cfg_cookie_token
            if per_call_cookies is not None:
                req_cookie_token = per_call_cookies.cache_token_for_url(normalized_url)
                if req_cookie_token:
                    effective_cache_settings["req_cookies"] = req_cookie_token
            cache_key = build_cache_key(
                normalized_url, mode=mode, proxy=proxy, settings=effective_cache_settings
            )
            warnings = list(self._startup_warnings)
            if self._cache and use_cache:
                try:
                    cached = await self._cache.get(cache_key, requested_screenshot=requested_screenshot)
                    if cached and not self._uncacheable(cached):
                        cached.duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                        logger.debug("cache hit for %s", normalized_url)
                        return cached
                except Exception as exc:
                    warnings.append("Cache read failed; content was fetched normally.")
                    logger.warning("cache read failed: %s", type(exc).__name__)
            try:
                result = await transport(normalized_url)
            except (TransportFailure, ProxyConfigurationError) as exc:
                error = (
                    exc.error
                    if isinstance(exc, TransportFailure)
                    else FetchErrorInfo("connection_error", str(exc), False, type(exc).__name__)
                )
                result = self._finish_error(
                    url=normalized_url,
                    proxy=proxy,
                    error=error,
                    status_code=getattr(exc, "status_code", None),
                    started_at=started_at,
                    should_raise=should_raise,
                )
                if getattr(exc, "actions_executed", None) is not None:
                    result.actions_executed = exc.actions_executed
            except Exception as exc:
                result = self._finish_error(
                    url=normalized_url,
                    proxy=proxy,
                    error=FetchErrorInfo(
                        "unknown_error",
                        f"An unexpected error occurred while {error_context}.",
                        False,
                        type(exc).__name__,
                    ),
                    started_at=started_at,
                    should_raise=should_raise,
                )
            else:
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
            await self._transports.finish_operation()

    async def _escalate_to_browser(
        self,
        url: str,
        proxy: str,
        status_code: int | None = None,
        is_bot_escalation: bool = False,
        retry_after: str | None = None,
        proxy_salt: str = "",
        headers: dict[str, str] | None = None,
        cookie_session: CookieSession | None = None,
    ) -> FetchResult:
        if is_bot_escalation or (status_code is not None and status_code in {403, 429, 503}):
            if cookie_session is not None:
                cookie_session.rotate()
            if retry_after:
                seconds = HTTPFetcher._parse_retry_after(retry_after)
                delay = min(seconds, 5.0) if seconds is not None else random.uniform(0.5, 2.5)
            else:
                delay = random.uniform(0.5, 2.5)
            await asyncio.sleep(delay)
        return await self._fetch_browser(
            url,
            proxy,
            status_code=status_code,
            proxy_salt=proxy_salt,
            headers=headers,
            cookie_session=cookie_session,
        )

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
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        cookie_session: CookieSession | None = None,
        content: bytes | str | None = None,
        data: dict[str, Any] | None = None,
        json_data: Any = None,
    ) -> FetchResult:
        direct_fallback_warning: str | None = None
        active_session = cookie_session or self._make_cookie_session(url)
        initial_site = registrable_host(url) or (urlsplit(url).hostname or "").lower()
        fetcher = await self._transports.get_http_fetcher(proxy)
        per_request_proxy = self._transports.resolve_proxy_url(proxy, url)
        retry_codes = RETRYABLE_STATUS_CODES - {429} if mode == "auto" else RETRYABLE_STATUS_CODES

        def _http_retry_proxy_resolver(attempt: int) -> str | None:
            bump_domain_session(registrable_host(url) or url)
            return self._transports.resolve_proxy_url(proxy, url, salt=f"http_r{attempt}")

        try:
            response = await fetcher.fetch(
                url,
                method=method,
                proxy_url=per_request_proxy,
                proxy_resolver=_http_retry_proxy_resolver if per_request_proxy else None,
                headers=self._transports.headers_for_url(url, extra_headers=headers),
                cookie_profile=active_session.current,
                cookie_rotator=active_session.rotate,
                initial_site=initial_site,
                content=content,
                data=data,
                json_data=json_data,
                retryable_status_codes=retry_codes,
            )
        except TransportFailure as exc:
            if per_request_proxy and exc.error and exc.error.code in {"proxy_forbidden", "proxy_error"}:
                logger.warning(
                    "Proxy failed with %s (%s); retrying %s directly without proxy",
                    exc.error.code,
                    exc.error.message,
                    url,
                )
                direct_fetcher = await self._transports.get_http_fetcher("none")
                response = await direct_fetcher.fetch(
                    url,
                    method=method,
                    proxy_url=None,
                    proxy_resolver=None,
                    headers=self._transports.headers_for_url(url, extra_headers=headers),
                    cookie_profile=active_session.current,
                    cookie_rotator=active_session.rotate,
                    initial_site=initial_site,
                    content=content,
                    data=data,
                    json_data=json_data,
                    retryable_status_codes=retry_codes,
                )
                direct_fallback_warning = "Proxy rejected connection to target host; fetched directly without proxy."
            else:
                raise

        final_url = str(response.url)
        content_type = parse_content_type(response.headers.get("Content-Type"))

        def _with_fallback_warning(res: FetchResult) -> FetchResult:
            if direct_fallback_warning and direct_fallback_warning not in res.warnings:
                res.warnings.append(direct_fallback_warning)
            return res

        if response.status_code >= 400:
            retryable = response.status_code in RETRYABLE_STATUS_CODES
            is_waf = looks_like_waf_challenge(response.content)
            kind = detect_document_kind(content_type, response.content, url=final_url)

            non_html = kind in ("pdf", "docx", "csv", "tsv", "json", "xml") or any(
                t in content_type for t in ("json", "xml")
            )
            if non_html and not is_waf:
                is_block = response.status_code in BLOCKED_STATUS_CODES
                active_session.finalize(success=False, blocked=is_block)
                code = "blocked" if is_block else "http_error"
                err = (
                    self._build_block_error(
                        code,
                        f"HTTP request returned status {response.status_code}",
                        retryable,
                        url=final_url,
                        status_code=response.status_code,
                        headers=response.headers,
                    )
                    if is_block
                    else FetchErrorInfo(code, f"HTTP request returned status {response.status_code}", retryable)
                )
                raise TransportFailure(err, status_code=response.status_code)

            escalate_to_browser = (
                mode == "auto"
                and method.upper() == "GET"
                and (
                    response.status_code in BLOCKED_STATUS_CODES
                    or (response.status_code == 503 and is_waf)
                    or is_waf
                )
            )
            if escalate_to_browser:
                logger.info("HTTP %s; using browser for %s", response.status_code, url)
                bump_domain_session(registrable_host(url) or url)
                self._policy.record_escalation(url)
                return await self._escalate_to_browser(
                    url,
                    proxy,
                    status_code=response.status_code,
                    is_bot_escalation=True,
                    retry_after=response.headers.get("Retry-After"),
                    proxy_salt="fallback",
                    headers=headers,
                    cookie_session=active_session,
                )
            is_block = response.status_code in BLOCKED_STATUS_CODES or is_waf
            active_session.finalize(success=False, blocked=is_block)
            decoded_err = decode_response_body(response) if is_block else ""
            code = "captcha_detected" if is_waf else ("blocked" if is_block else "http_error")
            err = (
                self._build_block_error(
                    code,
                    f"HTTP request returned status {response.status_code}",
                    retryable,
                    url=final_url,
                    status_code=response.status_code,
                    html=decoded_err,
                    headers=response.headers,
                )
                if is_block
                else FetchErrorInfo(code, f"HTTP request returned status {response.status_code}", retryable)
            )
            raise TransportFailure(err, status_code=response.status_code)

        kind = detect_document_kind(content_type, response.content, url=final_url)
        non_html_result = build_non_html_result(kind, url, response, proxy)
        if non_html_result is not None:
            active_session.finalize(success=True)
            return _with_fallback_warning(non_html_result)
        if kind != "html":
            raise TransportFailure(
                FetchErrorInfo(
                    "unsupported_content_type",
                    f"Content type {content_type!r} is not HTML; cannot process with HTTP mode",
                    False,
                ),
                status_code=response.status_code,
            )

        html = decode_response_body(response)
        raw_soup = BeautifulSoup(html, "lxml")
        report = analyze_html(html, soup=raw_soup)
        has_challenge_header = (
            response.status_code == 203
            or "caf" in response.headers
            or "x-ncbi-captcha" in response.headers.get("vary", "").lower()
        )
        if has_challenge_header and report.score < self.config.confidence_threshold and not report.challenge:
            report = ConfidenceReport(
                score=min(report.score, 0.08),
                reasons=tuple(dict.fromkeys((*report.reasons, "challenge or anti-bot page detected"))),
                challenge=True,
                javascript_shell=report.javascript_shell,
            )
        low_content_reasons = {"very little visible text", "short visible text", "document is mostly navigation"}
        should_escalate = (
            mode == "auto"
            and method.upper() == "GET"
            and (
                report.challenge
                or report.javascript_shell
                or (
                    report.score < self.config.confidence_threshold
                    and (bool(low_content_reasons.intersection(report.reasons)) or report.score < 0.40)
                )
            )
        )
        if should_escalate:
            logger.info("HTTP confidence %.3f; using browser for %s", report.score, url)
            if report.challenge:
                bump_domain_session(registrable_host(url) or url)
                self._policy.record_escalation(url)
            try:
                rendered = await self._escalate_to_browser(
                    url,
                    proxy,
                    status_code=response.status_code,
                    is_bot_escalation=report.challenge,
                    retry_after=response.headers.get("Retry-After"),
                    proxy_salt="fallback" if report.challenge else "",
                    headers=headers,
                    cookie_session=active_session,
                )
            except TransportFailure as exc:
                if report.challenge or not html.strip():
                    raise
                err_detail = f" ({exc.error.message})" if exc.error and exc.error.message else ""
                return _with_fallback_warning(self._degraded_http_result(
                    url,
                    response,
                    html,
                    raw_soup,
                    report,
                    content_type,
                    proxy,
                    f"Browser rendering failed{err_detail}; showing basic HTTP version instead.",
                ))
            if not rendered.success:
                if report.challenge or not html.strip():
                    return rendered
                err_detail = f" ({rendered.error.message})" if rendered.error and rendered.error.message else ""
                return _with_fallback_warning(self._degraded_http_result(
                    url,
                    response,
                    html,
                    raw_soup,
                    report,
                    content_type,
                    proxy,
                    f"Browser rendered content was not usable{err_detail}; showing HTTP version instead.",
                ))
            if report.javascript_shell:
                self._policy.record_escalation(url)
            rendered.warnings.insert(0, "HTTP content confidence was low; browser fallback was used.")
            return _with_fallback_warning(rendered)

        if mode == "http" and report.challenge:
            active_session.finalize(success=False, blocked=True)
            raise TransportFailure(
                self._build_block_error(
                    "captcha_detected",
                    "challenge page detected in HTTP response",
                    False,
                    url=final_url,
                    status_code=response.status_code,
                    html=html,
                    headers=response.headers,
                ),
                status_code=response.status_code,
            )

        active_session.finalize(success=True)
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
        return _with_fallback_warning(result)

    async def _fetch_browser(
        self,
        url: str,
        proxy: str,
        status_code: int | None,
        *,
        proxy_salt: str = "",
        actions: Sequence[PageAction] | None = None,
        capture_xhr: bool | str | Sequence[str] = False,
        expand: bool = False,
        headers: dict[str, str] | None = None,
        cookie_session: CookieSession | None = None,
        structure: bool = False,
        compact_structure: bool = False,
        screenshot: str = "none",
        screenshot_format: str = "png",
    ) -> FetchResult:
        """Unified browser-backed fetch path."""
        proxy_settings, fetcher = await self._transports.get_browser_fetcher(
            proxy, url, salt=proxy_salt
        )
        merged_headers = merge_headers(self.config.headers, headers)
        active_session = cookie_session or self._make_cookie_session(url)

        def _retry_proxy_resolver(attempt: int) -> ProxySettings:
            bump_domain_session(registrable_host(url) or url)
            retry_salt = f"{proxy_salt}:r{attempt}" if proxy_salt else f"r{attempt}"
            return ProxySettings(
                provider=proxy,
                url=self._transports.resolve_proxy_url(proxy, url, salt=retry_salt),
            )

        direct_browser_fallback_warning: str | None = None
        try:
            response = await fetcher.fetch(
                url,
                proxy=proxy_settings,
                proxy_resolver=_retry_proxy_resolver,
                actions=actions,
                capture_xhr=capture_xhr,
                headers=merged_headers if merged_headers else None,
                cookie_profile=active_session.current,
                cookie_rotator=active_session.rotate,
                expand=expand,
                screenshot=screenshot,
                screenshot_format=screenshot_format,
                screenshot_max_bytes=self.config.screenshot_max_bytes,
            )
        except TransportFailure as exc:
            if proxy_settings.url and exc.error and exc.error.code in {"proxy_forbidden", "proxy_error"}:
                logger.warning(
                    "Browser proxy failed with %s (%s); retrying %s directly without proxy",
                    exc.error.code,
                    exc.error.message,
                    url,
                )
                direct_proxy_settings, direct_fetcher = await self._transports.get_browser_fetcher("none", url)
                response = await direct_fetcher.fetch(
                    url,
                    proxy=direct_proxy_settings,
                    proxy_resolver=None,
                    actions=actions,
                    capture_xhr=capture_xhr,
                    headers=merged_headers if merged_headers else None,
                    cookie_profile=active_session.current,
                    cookie_rotator=active_session.rotate,
                    expand=expand,
                    screenshot=screenshot,
                    screenshot_format=screenshot_format,
                    screenshot_max_bytes=self.config.screenshot_max_bytes,
                )
                direct_browser_fallback_warning = "Browser proxy rejected connection to target host; fetched directly without proxy."
            else:
                raise

        is_pdf_result = await self._transports.recover_browser_pdf(
            url=url,
            response=response,
            proxy=proxy,
            headers=headers,
            cookie_session=active_session,
        )
        if is_pdf_result is not None:
            return is_pdf_result

        raw_soup = response.soup if response.soup is not None else BeautifulSoup(response.html, "lxml")
        report = response.confidence if response.confidence is not None else analyze_html(response.html, soup=raw_soup)
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
            confidence=report,
            include_structure=structure,
            compact_structure=compact_structure,
        )
        result.screenshot = response.screenshot
        result.screenshot_format = response.screenshot_format
        result.actions_executed = response.actions_executed
        result.xhr_responses = response.xhr_responses
        result.warnings.extend(response.warnings)
        if direct_browser_fallback_warning and direct_browser_fallback_warning not in result.warnings:
            result.warnings.append(direct_browser_fallback_warning)
        effective_status = result.status_code
        if report.challenge:
            active_session.finalize(success=False, blocked=True)
            result.success = False
            result.error = self._build_block_error(
                "captcha_detected",
                "challenge page remained after browser retries",
                False,
                url=response.url or url,
                status_code=effective_status,
                html=response.html,
            )
        elif effective_status is not None and effective_status >= 400:
            if (
                effective_status in (BLOCKED_STATUS_CODES | {503})
                and not report.challenge
                and report.score >= self.config.confidence_threshold
            ):
                active_session.finalize(success=True)
                result.status_code = 200
                result.warnings.append(
                    f"Initial navigation returned status {effective_status}, but challenge solved successfully."
                )
            else:
                is_block = effective_status in BLOCKED_STATUS_CODES
                active_session.finalize(success=False, blocked=is_block)
                result.success = False
                code = "blocked" if is_block else "http_error"
                result.error = (
                    self._build_block_error(
                        code,
                        f"browser navigation returned status {effective_status}",
                        effective_status in RETRYABLE_STATUS_CODES,
                        url=response.url or url,
                        status_code=effective_status,
                        html=response.html,
                    )
                    if is_block
                    else FetchErrorInfo(
                        code,
                        f"browser navigation returned status {effective_status}",
                        effective_status in RETRYABLE_STATUS_CODES,
                    )
                )
        else:
            active_session.finalize(success=True)
            if report.score < self.config.confidence_threshold:
                result.warnings.append("Rendered content may still be incomplete.")
        return result

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

    @staticmethod
    def _validate_fetch_options(mode: str, proxy: str) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        if proxy not in VALID_PROXIES:
            raise ValueError(f"proxy must be one of {sorted(VALID_PROXIES)}")

    @staticmethod
    def _uncacheable(result: FetchResult) -> bool:
        if (
            bool(result.error)
            or result.status_code in BLOCKED_STATUS_CODES
            or result.status_code == 203
            or (result.status_code is not None and result.status_code >= 500)
        ):
            return True
        md_stripped = (result.markdown or "").strip()
        txt_stripped = (result.text or "").strip()
        conf = result.content_confidence
        if conf is not None and conf < 0.25 and not md_stripped and not txt_stripped:
            return True
        host = (urlsplit(result.final_url or result.url).hostname or "").lower().removeprefix("www.")
        bodies = [b.lower().strip("./").removeprefix("www.") for b in (md_stripped, txt_stripped) if b]
        if host and bodies and all(b == host for b in bodies):
            return True
        return any(
            w.startswith(("Browser rendering failed", "Browser rendered content was not usable"))
            for w in result.warnings
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
