"""Validated client configuration with optional YAML file support."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import user_cache_path

from .utils.durations import parse_duration

VALID_MODES = frozenset({"auto", "http", "browser"})
VALID_PROXIES = frozenset({"none", "decodo", "dataimpulse"})

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
    mode: str = "auto"
    proxy: str = "none"
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
                flat.update(resolved.pop(section))
        flat.update(resolved)

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
            raise_on_error=flat.get("raise_on_error", False),
        )

    @classmethod
    def build(
        cls,
        *,
        mode: str = "auto",
        proxy: str = "none",
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
        if not isinstance(cache_enabled, bool) or not isinstance(raise_on_error, bool):
            raise ValueError("cache_enabled and raise_on_error must be booleans")
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
            raise_on_error=raise_on_error,
        )
