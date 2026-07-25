"""Validated client configuration with optional YAML file support."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_cache_path

from .utils.durations import parse_duration

from .constants import GEO_MAP

VALID_MODES = frozenset({"auto", "http", "browser"})
VALID_PROXIES = frozenset({"none", "decodo", "dataimpulse"})
VALID_BLOCK_LEVELS = frozenset({"minimal", "balanced", "aggressive"})
VALID_SESSION_ROTATION = frozenset({"sticky", "rotate"})
VALID_STEALTH_LEVELS = frozenset({"off", "balanced", "max"})

# Stealth presets override multiple individual options in one shot.
# Individual fields given to `build()` take precedence over the preset
# defaults so callers can still fine-tune after choosing a level.
_STEALTH_PRESETS: dict[str, dict[str, object]] = {
    "off": {
        "humanize": False,
        "block_level": "aggressive",
        "request_pacing": 0.0,
        "session_rotation": "sticky",
    },
    "balanced": {
        "humanize": True,
        "block_level": "balanced",
        "request_pacing": 0.5,
        "session_rotation": "rotate",
    },
    "max": {
        "humanize": True,
        "block_level": "minimal",
        "request_pacing": 2.0,
        "session_rotation": "rotate",
    },
}

_ENV_VAR_RE = re.compile(r"\$\{(\w+)(?::([^}]*))?\}")


def _interpolate_env(value: Any) -> Any:
    """Recursively substitute ``${VAR}`` and ``${VAR:default}`` in strings."""
    if isinstance(value, str):
        def _replace(m: re.Match[str]) -> str:
            var, default = m.group(1), m.group(2)
            return os.getenv(var, default if default is not None else m.group(0))
        return _ENV_VAR_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


@dataclass(slots=True, frozen=True)
class PageFetchConfig:
    mode: Literal["auto", "http", "browser"] = "auto"
    proxy: Literal["none", "decodo", "dataimpulse"] = "none"
    http_concurrency: int = 10
    browser_concurrency: int = 4
    cache_enabled: bool = True
    cache_ttl: int = 86400
    cache_path: Path = user_cache_path("pagefetch") / "cache.sqlite3"
    http_timeout: float = 20.0
    browser_timeout: float = 45.0
    retries_http: int = 3
    retries_browser: int = 2
    max_redirects: int = 10
    max_content_size: int = 25 * 1024 * 1024
    confidence_threshold: float = 0.80
    block_images: bool = True
    block_level: Literal["minimal", "balanced", "aggressive"] = "aggressive"
    accept_language: str = "en-US,en;q=0.5"
    humanize: bool = False
    session_rotation: Literal["sticky", "rotate"] = "sticky"
    request_pacing: float = 0.0
    stealth_level: Literal["off", "balanced", "max"] = "off"
    proxy_geo: str | None = None
    raise_on_error: bool = False

    @classmethod
    def from_yaml(cls, path: str | Path) -> PageFetchConfig:
        """Load configuration from a YAML file with ``${ENV_VAR}`` interpolation.

        Environment variables referenced in the YAML file (e.g. ``${DECODO_PROXY_URL}``)
        are resolved at load time.  Use ``${VAR:default}`` syntax to provide fallbacks.

        Only keys that exist in the YAML file are used; all other settings keep
        their built-in defaults.
        """
        import yaml

        raw: dict[str, Any] = {}
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        resolved = _interpolate_env(raw)

        # Accept top-level keys plus an optional nested "proxy" / "cache" sections
        flat: dict[str, Any] = {}
        for section in ("proxy", "cache"):
            if section in resolved and isinstance(resolved[section], dict):
                section_data = resolved.pop(section)
                # A nested proxy section may use "provider" to select the provider;
                # map it to the top-level "proxy" key so it is not silently dropped.
                if section == "proxy" and "provider" in section_data:
                    flat["proxy"] = section_data.pop("provider")
                flat.update(section_data)
        flat.update(resolved)

        # ── stealth preset application ──
        stealth = flat.get("stealth_level", "off")
        if stealth != "off":
            preset = _STEALTH_PRESETS[stealth]
            for field, value in preset.items():
                # Preset values are defaults — explicit YAML keys win.
                flat.setdefault(field, value)

        return cls.build(
            mode=flat.get("mode", "auto"),
            proxy=flat.get("proxy", "none"),
            http_concurrency=flat.get("http_concurrency", 10),
            browser_concurrency=flat.get("browser_concurrency", 4),
            cache_enabled=flat.get("cache_enabled", True),
            cache_ttl=flat.get("cache_ttl", "24h"),
            cache_path=flat.get("cache_path"),
            http_timeout=flat.get("http_timeout", 20.0),
            browser_timeout=flat.get("browser_timeout", 45.0),
            retries_http=flat.get("retries_http", 3),
            retries_browser=flat.get("retries_browser", 2),
            max_redirects=flat.get("max_redirects", 10),
            max_content_size=flat.get("max_content_size", 25 * 1024 * 1024),
            confidence_threshold=flat.get("confidence_threshold", 0.80),
            block_images=flat.get("block_images", True),
            block_level=flat.get("block_level", "aggressive"),
            accept_language=flat.get("accept_language", "en-US,en;q=0.5"),
            humanize=flat.get("humanize", False),
            session_rotation=flat.get("session_rotation", "sticky"),
            request_pacing=flat.get("request_pacing", 0.0),
            stealth_level=flat.get("stealth_level", "off"),
            proxy_geo=flat.get("proxy_geo"),
            raise_on_error=flat.get("raise_on_error", False),
        )

    @classmethod
    def build(
        cls,
        *,
        mode: Literal["auto", "http", "browser"] = "auto",
        proxy: Literal["none", "decodo", "dataimpulse"] = "none",
        http_concurrency: int = 10,
        browser_concurrency: int = 4,
        cache_enabled: bool = True,
        cache_ttl: str | int = "24h",
        cache_path: str | Path | None = None,
        http_timeout: float = 20.0,
        browser_timeout: float = 45.0,
        retries_http: int = 3,
        retries_browser: int = 2,
        max_redirects: int = 10,
        max_content_size: int = 25 * 1024 * 1024,
        confidence_threshold: float = 0.80,
        block_images: bool = True,
        block_level: Literal["minimal", "balanced", "aggressive"] = "aggressive",
        accept_language: str = "en-US,en;q=0.5",
        humanize: bool = False,
        session_rotation: Literal["sticky", "rotate"] = "sticky",
        request_pacing: float = 0.0,
        stealth_level: Literal["off", "balanced", "max"] = "off",
        proxy_geo: str | None = None,
        raise_on_error: bool = False,
    ) -> PageFetchConfig:
        if not isinstance(mode, str) or mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        if not isinstance(proxy, str) or proxy not in VALID_PROXIES:
            raise ValueError(f"proxy must be one of {sorted(VALID_PROXIES)}")
        for name, value in {
            "http_concurrency": http_concurrency,
            "browser_concurrency": browser_concurrency,
            "max_redirects": max_redirects,
            "max_content_size": max_content_size,
        }.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name, value in {"retries_http": retries_http, "retries_browser": retries_browser}.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name, value in {"http_timeout": http_timeout, "browser_timeout": browser_timeout}.items():
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
        if (
            not isinstance(confidence_threshold, (int, float))
            or isinstance(confidence_threshold, bool)
            or not math.isfinite(confidence_threshold)
            or not 0 <= confidence_threshold <= 1
        ):
            raise ValueError("confidence_threshold must be between 0 and 1")
        if not isinstance(cache_enabled, bool) or not isinstance(raise_on_error, bool) or not isinstance(humanize, bool):
            raise ValueError("cache_enabled, humanize, and raise_on_error must be booleans")
        if not isinstance(block_images, bool):
            raise ValueError("block_images must be a boolean")
        if not isinstance(block_level, str) or block_level not in VALID_BLOCK_LEVELS:
            raise ValueError(f"block_level must be one of {sorted(VALID_BLOCK_LEVELS)}")
        if not isinstance(accept_language, str) or not accept_language.strip():
            raise ValueError("accept_language must be a non-empty string")
        if not isinstance(session_rotation, str) or session_rotation not in VALID_SESSION_ROTATION:
            raise ValueError(f"session_rotation must be one of {sorted(VALID_SESSION_ROTATION)}")
        if (
            not isinstance(request_pacing, (int, float))
            or isinstance(request_pacing, bool)
            or not math.isfinite(request_pacing)
            or request_pacing < 0
        ):
            raise ValueError("request_pacing must be a non-negative finite number")
        if not isinstance(stealth_level, str) or stealth_level not in VALID_STEALTH_LEVELS:
            raise ValueError(f"stealth_level must be one of {sorted(VALID_STEALTH_LEVELS)}")
        if proxy_geo is not None:
            if not isinstance(proxy_geo, str) or proxy_geo not in GEO_MAP:
                raise ValueError(f"proxy_geo must be one of {sorted(GEO_MAP)}")
        ttl = parse_duration(cache_ttl)
        path = Path(cache_path).expanduser() if cache_path is not None else user_cache_path("pagefetch") / "cache.sqlite3"
        return cls(
            mode=mode,
            proxy=proxy,
            http_concurrency=http_concurrency,
            browser_concurrency=browser_concurrency,
            cache_enabled=cache_enabled,
            cache_ttl=ttl,
            cache_path=path,
            http_timeout=float(http_timeout),
            browser_timeout=float(browser_timeout),
            retries_http=retries_http,
            retries_browser=retries_browser,
            max_redirects=max_redirects,
            max_content_size=max_content_size,
            confidence_threshold=float(confidence_threshold),
            block_images=block_images,
            block_level=block_level,
            accept_language=accept_language.strip(),
            humanize=humanize,
            session_rotation=session_rotation,
            request_pacing=float(request_pacing),
            stealth_level=stealth_level,
            proxy_geo=proxy_geo.strip() if proxy_geo else None,
            raise_on_error=raise_on_error,
        )
