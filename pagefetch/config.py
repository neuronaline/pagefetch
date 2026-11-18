"""Validated client configuration with optional YAML file support."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_cache_path

from .utils.durations import parse_duration

VALID_PROXIES = frozenset({"none", "custom", "decodo", "byteful"})
VALID_CLEANING_LEVELS = frozenset({"minimal", "standard", "maximum"})
VALID_BLOCK_LEVELS = frozenset({"minimal", "balanced", "aggressive"})
VALID_SESSION_ROTATION = frozenset({"sticky", "rotate"})
VALID_STEALTH_LEVELS = frozenset({"off", "balanced", "max"})
VALID_SCREENSHOT_MODES = frozenset({"none", "viewport", "full"})
VALID_SCREENSHOT_FORMATS = frozenset({"png", "jpeg"})
VALID_OUTPUT_FORMATS = frozenset({"markdown", "json", "html", "structure", "raw"})

# Private sentinel letting callers/CLI distinguish omitted values from explicit defaults.
_UNSET: Any = object()

# Stealth presets override multiple individual options in one shot.
# Explicit field values always take precedence over the preset defaults.
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
        "session_rotation": "sticky",
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


def _coerce_duration_seconds(
    value: Any,
    *,
    name: str,
    allow_none: bool = False,
    allow_float: bool = False,
    positive_only: bool = False,
    zero_as_none: bool = False,
) -> int | float | None:
    """Validate and coerce an integer/float/duration-string field to seconds."""
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{name} cannot be None")
    if isinstance(value, str):
        parsed = parse_duration(value)
        num: int | float = float(parsed) if allow_float else parsed
    elif isinstance(value, bool) or (
        not isinstance(value, int | float)
        if allow_float
        else not isinstance(value, int)
    ):
        kind = "duration string or non-negative number" if allow_float else "duration string or integer"
        suffix = ", or None" if allow_none else ""
        raise ValueError(f"{name} must be a {kind}{suffix}")
    elif allow_float and not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    else:
        num = float(value) if allow_float else int(value)

    if zero_as_none and num == 0:
        return None
    if positive_only:
        if num <= 0:
            raise ValueError(f"{name} must be positive")
    elif num < 0:
        raise ValueError(f"{name} cannot be negative")
    return num


def _normalize_headers_input(headers: Any) -> dict[str, str]:
    """Normalize dict/Mapping, None, or ``'Name: Value'`` sequence into a stripped header dict."""
    if headers is None:
        return {}
    if isinstance(headers, Mapping):
        return {str(k).strip(): str(v).strip() for k, v in headers.items() if str(k).strip()}
    if isinstance(headers, list | tuple):
        return {
            k.strip(): v.strip()
            for item in headers
            if isinstance(item, str) and ":" in item
            for k, v in [item.split(":", 1)]
            if k.strip()
        }
    raise ValueError("headers must be a dictionary")


def merge_headers(*mappings: Mapping[str, str] | None) -> dict[str, str]:
    """Merge header mappings case-insensitively (later mappings override earlier keys)."""
    merged: dict[str, str] = {}
    for mapping in mappings:
        if not mapping:
            continue
        for k, v in mapping.items():
            lower_k = str(k).lower()
            for existing_k in list(merged):
                if existing_k.lower() == lower_k:
                    del merged[existing_k]
            merged[str(k)] = str(v)
    return merged


@dataclass(slots=True, frozen=True)
class PageFetchConfig:
    proxy: Literal["none", "custom", "decodo", "byteful"] = "none"
    cleaning_level: Literal["minimal", "standard", "maximum"] = "standard"
    concurrency: int = 4
    cache_enabled: bool = True
    cache_ttl: int | str = 86400
    cache_path: Path | str | None = None
    timeout: float = 45.0
    retries: int = 2
    max_content_size: int = 25 * 1024 * 1024
    confidence_threshold: float = 0.80
    block_images: bool | None = None
    block_level: Literal["minimal", "balanced", "aggressive"] | None = None
    accept_language: str = "en-US,en;q=0.5"
    humanize: bool | None = None
    session_rotation: Literal["sticky", "rotate"] | None = None
    # Sticky-session TTL forwarded to residential providers (DECODO_DOCS §4, BYTEFUL_DOCS §4).
    session_duration: int | str | None = None
    request_pacing: float | None = None
    stealth_level: Literal["off", "balanced", "max"] = "balanced"
    raise_on_error: bool = False
    screenshot_max_bytes: int = 50 * 1024 * 1024
    max_requests: int = 50
    idle_ttl: float | int | str | None = 180.0
    pre_check_byte_margin: float = 1.5
    max_scroll_steps: int = 25
    max_scroll_height: int = 150_000
    expand: bool = True
    cookie_file: Path | str | None = None
    cookie_cooldown: float | int | str = 120.0
    headers: dict[str, str] | Sequence[str] | None = field(default_factory=dict)
    cookies: Any = field(default_factory=dict)
    _cookie_profiles: tuple[Any, ...] = field(default=(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate and normalize all configuration fields in a single pass."""
        from .cookies import parse_cookie_pool

        if not isinstance(self.proxy, str) or self.proxy not in VALID_PROXIES:
            raise ValueError(f"proxy must be one of {sorted(VALID_PROXIES)}")
        if not isinstance(self.cleaning_level, str) or self.cleaning_level not in VALID_CLEANING_LEVELS:
            raise ValueError(f"cleaning_level must be one of {sorted(VALID_CLEANING_LEVELS)}")
        if not isinstance(self.stealth_level, str) or self.stealth_level not in VALID_STEALTH_LEVELS:
            raise ValueError(f"stealth_level must be one of {sorted(VALID_STEALTH_LEVELS)}")

        # Resolve stealth-coupled options (preset defaults yield to explicit values).
        preset = _STEALTH_PRESETS[self.stealth_level]
        for attr in ("block_level", "humanize", "session_rotation", "request_pacing"):
            val = getattr(self, attr)
            if val is None or val is _UNSET:
                object.__setattr__(self, attr, preset[attr])
        if self.block_images is _UNSET or self.block_images is None:
            # Image blocking leaks Canvas/CSS fingerprints under balanced/max stealth.
            object.__setattr__(self, "block_images", self.stealth_level not in {"balanced", "max"})

        # Normalize headers, cache_path, cookie_file, and parse cookies once.
        object.__setattr__(self, "headers", _normalize_headers_input(self.headers))
        if self.cache_path is None:
            norm_cache_path = user_cache_path("pagefetch") / "cache.sqlite3"
        elif isinstance(self.cache_path, str):
            if not self.cache_path.strip():
                raise ValueError("cache_path must be a non-empty string, Path, or None")
            norm_cache_path = Path(self.cache_path).expanduser()
        elif isinstance(self.cache_path, Path):
            norm_cache_path = self.cache_path.expanduser()
        else:
            raise ValueError("cache_path must be a string, Path, or None")
        object.__setattr__(self, "cache_path", norm_cache_path)

        if self.cookie_file is None:
            norm_cookie_file = None
        elif isinstance(self.cookie_file, str):
            if not self.cookie_file.strip():
                raise ValueError("cookie_file must be a non-empty string, Path, or None")
            norm_cookie_file = Path(self.cookie_file).expanduser()
        elif isinstance(self.cookie_file, Path):
            norm_cookie_file = self.cookie_file.expanduser()
        else:
            raise ValueError("cookie_file must be a string, Path, or None")
        object.__setattr__(self, "cookie_file", norm_cookie_file)

        if self.cookies is None:
            object.__setattr__(self, "cookies", {})
        elif isinstance(self.cookies, dict) and not type(self.cookies).__module__.startswith(
            "http.cookies"
        ):
            object.__setattr__(
                self,
                "cookies",
                {str(k).strip(): str(v).strip() for k, v in self.cookies.items()},
            )
        pool = parse_cookie_pool(self.cookies, cookie_file=norm_cookie_file)
        object.__setattr__(self, "_cookie_profiles", pool.profiles)

        # Normalize durations (strings like '24h', '30m', integers, or floats).
        object.__setattr__(
            self,
            "cache_ttl",
            _coerce_duration_seconds(self.cache_ttl, name="cache_ttl"),
        )
        object.__setattr__(
            self,
            "session_duration",
            _coerce_duration_seconds(
                self.session_duration,
                name="session_duration",
                allow_none=True,
                positive_only=True,
            ),
        )
        object.__setattr__(
            self,
            "idle_ttl",
            _coerce_duration_seconds(
                self.idle_ttl,
                name="idle_ttl",
                allow_none=True,
                allow_float=True,
                zero_as_none=True,
            ),
        )
        object.__setattr__(
            self,
            "cookie_cooldown",
            _coerce_duration_seconds(
                self.cookie_cooldown,
                name="cookie_cooldown",
                allow_float=True,
            ),
        )

        # Validate positive integer bounds.
        for name in (
            "concurrency",
            "max_content_size",
            "screenshot_max_bytes",
            "max_requests",
            "max_scroll_steps",
            "max_scroll_height",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

        # Validate non-negative integer bounds.
        if not isinstance(self.retries, int) or isinstance(self.retries, bool) or self.retries < 0:
            raise ValueError("retries must be a non-negative integer")

        # Validate positive finite float timeout.
        if (
            not isinstance(self.timeout, int | float)
            or isinstance(self.timeout, bool)
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise ValueError("timeout must be a positive finite number")
        object.__setattr__(self, "timeout", float(self.timeout))

        if (
            not isinstance(self.confidence_threshold, int | float)
            or isinstance(self.confidence_threshold, bool)
            or not math.isfinite(self.confidence_threshold)
            or not 0 <= self.confidence_threshold <= 1
        ):
            raise ValueError("confidence_threshold must be between 0 and 1")
        object.__setattr__(self, "confidence_threshold", float(self.confidence_threshold))

        if not all(
            isinstance(value, bool)
            for value in (
                self.cache_enabled,
                self.raise_on_error,
                self.humanize,
                self.block_images,
                self.expand,
            )
        ):
            raise ValueError(
                "cache_enabled, block_images, humanize, expand, and raise_on_error must be booleans"
            )

        if not isinstance(self.block_level, str) or self.block_level not in VALID_BLOCK_LEVELS:
            raise ValueError(f"block_level must be one of {sorted(VALID_BLOCK_LEVELS)}")
        if not isinstance(self.accept_language, str) or not self.accept_language.strip():
            raise ValueError("accept_language must be a non-empty string")
        object.__setattr__(self, "accept_language", self.accept_language.strip())

        if (
            not isinstance(self.session_rotation, str)
            or self.session_rotation not in VALID_SESSION_ROTATION
        ):
            raise ValueError(
                f"session_rotation must be one of {sorted(VALID_SESSION_ROTATION)}"
            )
        if (
            not isinstance(self.request_pacing, int | float)
            or isinstance(self.request_pacing, bool)
            or not math.isfinite(self.request_pacing)
            or self.request_pacing < 0
        ):
            raise ValueError("request_pacing must be a non-negative finite number")
        object.__setattr__(self, "request_pacing", float(self.request_pacing))

        if (
            not isinstance(self.pre_check_byte_margin, int | float)
            or isinstance(self.pre_check_byte_margin, bool)
            or not math.isfinite(self.pre_check_byte_margin)
            or self.pre_check_byte_margin < 1.0
        ):
            raise ValueError("pre_check_byte_margin must be a finite number >= 1.0")
        object.__setattr__(
            self,
            "pre_check_byte_margin",
            float(self.pre_check_byte_margin),
        )

    def build_cookie_pool(self) -> Any:
        """Construct a fresh :class:`~pagefetch.cookies.CookiePool` from pre-validated profiles."""
        from .cookies import CookiePool

        return CookiePool(self._cookie_profiles)

    @classmethod
    def _read_yaml_dict(cls, path: str | Path) -> dict[str, Any]:
        """Parse and validate YAML configuration keys into a flat dictionary."""
        import yaml

        with open(path, encoding="utf-8-sig") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}

        resolved = _interpolate_env(raw)
        if not isinstance(resolved, dict):
            raise ValueError("YAML configuration must contain a mapping")

        flat: dict[str, Any] = {}
        for section in ("cache",):
            if section in resolved and isinstance(resolved[section], dict):
                flat.update(resolved.pop(section))
        flat.update(resolved)
        # YAML 1.1 parsers treat plain scalar ``off`` as boolean False.
        if flat.get("stealth_level") is False:
            flat["stealth_level"] = "off"

        init_fields = {
            name
            for name, f in cls.__dataclass_fields__.items()
            if f.init
        }
        unsupported = sorted(set(flat).difference(init_fields))
        if unsupported:
            raise ValueError(f"unsupported configuration key(s): {', '.join(unsupported)}")
        return flat

    @classmethod
    def from_yaml(cls, path: str | Path) -> PageFetchConfig:
        """Load configuration from a YAML file with ``${ENV_VAR}`` interpolation."""
        return cls(**cls._read_yaml_dict(path))

    @classmethod
    def build(cls, **kwargs: Any) -> PageFetchConfig:
        """Compatibility alias constructing and normalizing :class:`PageFetchConfig` directly."""
        return cls(**kwargs)
