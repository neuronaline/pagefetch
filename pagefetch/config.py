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

VALID_MODES = frozenset({"auto", "http", "browser"})
VALID_PROXIES = frozenset({"none", "custom", "decodo", "byteful"})
VALID_CLEANING_LEVELS = frozenset({"minimal", "standard", "maximum"})
VALID_BLOCK_LEVELS = frozenset({"minimal", "balanced", "aggressive"})
VALID_SESSION_ROTATION = frozenset({"sticky", "rotate"})
VALID_STEALTH_LEVELS = frozenset({"off", "balanced", "max"})
VALID_SCREENSHOT_MODES = frozenset({"none", "viewport", "full"})
VALID_SCREENSHOT_FORMATS = frozenset({"png", "jpeg"})
VALID_OUTPUT_FORMATS = frozenset({"markdown", "json", "html", "structure", "raw"})

# Sentinel for ``build()`` parameters that should be resolved from the
# stealth preset rather than falling back to a hard-coded default.  Using a
# private sentinel (rather than ``None`` or a magic boolean) lets us tell
# "the caller did not specify this value" apart from a legitimate
# ``None`` / ``False`` they typed explicitly.
_UNSET: Any = object()

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
    proxy: Literal["none", "custom", "decodo", "byteful"] = "none"
    cleaning_level: Literal["minimal", "standard", "maximum"] = "standard"
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
    # Sticky-session TTL forwarded to the residential provider. ``None``
    # means "no TTL token", which is the provider's documented default
    # (Decodo: implicit 10-minute sticky window; Byteful: implicit attempt
    # to retain the IP for up to 7 days).  When set, the matching
    # documented token is appended during sticky-session injection
    # (``-sessionduration-<minutes>`` for Decodo, ``_ttl_<n><unit>`` for
    # Byteful).  See DECODO_DOCS §4 and BYTEFUL_DOCS §4.
    session_duration: int | None = None
    request_pacing: float = 0.0
    stealth_level: Literal["off", "balanced", "max"] = "off"
    raise_on_error: bool = False
    screenshot_max_bytes: int = 50 * 1024 * 1024
    # Browser-mode renders the page first and then measures the resulting DOM.
    # The char-length pre-check uses a multiplicative byte margin over the
    # configured max_content_size to account for multi-byte UTF-8. The
    # default of 1.5 mirrors the historical behaviour; lower values reject
    # pages earlier (saving render time) at the cost of more frequent
    # content_too_large errors on legitimate pages.
    browser_pre_check_byte_margin: float = 1.5

    def __post_init__(self) -> None:
        """Validate direct construction and normalize its public inputs."""
        if not isinstance(self.mode, str) or self.mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        if not isinstance(self.proxy, str) or self.proxy not in VALID_PROXIES:
            raise ValueError(f"proxy must be one of {sorted(VALID_PROXIES)}")
        if (
            not isinstance(self.cleaning_level, str)
            or self.cleaning_level not in VALID_CLEANING_LEVELS
        ):
            raise ValueError(
                f"cleaning_level must be one of {sorted(VALID_CLEANING_LEVELS)}"
            )
        if not isinstance(self.stealth_level, str) or self.stealth_level not in VALID_STEALTH_LEVELS:
            raise ValueError(
                f"stealth_level must be one of {sorted(VALID_STEALTH_LEVELS)}"
            )
        for name, value in {
            "http_concurrency": self.http_concurrency,
            "browser_concurrency": self.browser_concurrency,
            "max_redirects": self.max_redirects,
            "max_content_size": self.max_content_size,
            "screenshot_max_bytes": self.screenshot_max_bytes,
        }.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name, value in {
            "cache_ttl": self.cache_ttl,
            "retries_http": self.retries_http,
            "retries_browser": self.retries_browser,
        }.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name, value in {
            "http_timeout": self.http_timeout,
            "browser_timeout": self.browser_timeout,
        }.items():
            if (
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
        if (
            not isinstance(self.confidence_threshold, int | float)
            or isinstance(self.confidence_threshold, bool)
            or not math.isfinite(self.confidence_threshold)
            or not 0 <= self.confidence_threshold <= 1
        ):
            raise ValueError("confidence_threshold must be between 0 and 1")
        if not all(
            isinstance(value, bool)
            for value in (self.cache_enabled, self.raise_on_error, self.humanize, self.block_images)
        ):
            raise ValueError(
                "cache_enabled, block_images, humanize, and raise_on_error must be booleans"
            )
        if not isinstance(self.block_level, str) or self.block_level not in VALID_BLOCK_LEVELS:
            raise ValueError(f"block_level must be one of {sorted(VALID_BLOCK_LEVELS)}")
        if not isinstance(self.accept_language, str) or not self.accept_language.strip():
            raise ValueError("accept_language must be a non-empty string")
        if (
            not isinstance(self.session_rotation, str)
            or self.session_rotation not in VALID_SESSION_ROTATION
        ):
            raise ValueError(
                f"session_rotation must be one of {sorted(VALID_SESSION_ROTATION)}"
            )
        if self.session_duration is not None and (
            not isinstance(self.session_duration, int)
            or isinstance(self.session_duration, bool)
            or self.session_duration <= 0
        ):
            raise ValueError(
                "session_duration must be a positive integer (seconds) or None"
            )
        if (
            not isinstance(self.request_pacing, int | float)
            or isinstance(self.request_pacing, bool)
            or not math.isfinite(self.request_pacing)
            or self.request_pacing < 0
        ):
            raise ValueError("request_pacing must be a non-negative finite number")
        if (
            not isinstance(self.browser_pre_check_byte_margin, int | float)
            or isinstance(self.browser_pre_check_byte_margin, bool)
            or not math.isfinite(self.browser_pre_check_byte_margin)
            or self.browser_pre_check_byte_margin < 1.0
        ):
            raise ValueError("browser_pre_check_byte_margin must be a finite number >= 1.0")
        if not isinstance(self.cache_path, str | Path):
            raise ValueError("cache_path must be a string or Path")

        object.__setattr__(self, "cache_path", Path(self.cache_path).expanduser())
        object.__setattr__(self, "http_timeout", float(self.http_timeout))
        object.__setattr__(self, "browser_timeout", float(self.browser_timeout))
        object.__setattr__(self, "confidence_threshold", float(self.confidence_threshold))
        object.__setattr__(self, "accept_language", self.accept_language.strip())
        object.__setattr__(self, "request_pacing", float(self.request_pacing))
        object.__setattr__(
            self,
            "browser_pre_check_byte_margin",
            float(self.browser_pre_check_byte_margin),
        )

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
        with open(path, encoding="utf-8-sig") as fh:
            raw = yaml.safe_load(fh) or {}

        resolved = _interpolate_env(raw)
        if not isinstance(resolved, dict):
            raise ValueError("YAML configuration must contain a mapping")

        # Accept top-level keys plus an optional nested "cache" section.
        flat: dict[str, Any] = {}
        for section in ("cache",):
            if section in resolved and isinstance(resolved[section], dict):
                flat.update(resolved.pop(section))
        flat.update(resolved)
        # YAML 1.1 parsers treat the plain scalar ``off`` as boolean false.
        # Preserve the documented stealth-level spelling.
        if flat.get("stealth_level") is False:
            flat["stealth_level"] = "off"

        unsupported = sorted(set(flat).difference(cls.__dataclass_fields__))
        if unsupported:
            raise ValueError(f"unsupported configuration key(s): {', '.join(unsupported)}")

        return cls.build(
            mode=flat.get("mode", "auto"),
            proxy=flat.get("proxy", "none"),
            cleaning_level=flat.get("cleaning_level", "standard"),
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
            block_images=flat.get("block_images", _UNSET),
            block_level=flat.get("block_level"),
            accept_language=flat.get("accept_language", "en-US,en;q=0.5"),
            humanize=flat.get("humanize"),
            session_rotation=flat.get("session_rotation"),
            session_duration=flat.get("session_duration"),
            request_pacing=flat.get("request_pacing"),
            stealth_level=flat.get("stealth_level", "off"),
            raise_on_error=flat.get("raise_on_error", False),
            screenshot_max_bytes=flat.get("screenshot_max_bytes", 50 * 1024 * 1024),
            browser_pre_check_byte_margin=flat.get("browser_pre_check_byte_margin", 1.5),
        )

    @classmethod
    def build(
        cls,
        *,
        mode: Literal["auto", "http", "browser"] = "auto",
        proxy: Literal["none", "custom", "decodo", "byteful"] = "none",
        cleaning_level: Literal["minimal", "standard", "maximum"] = "standard",
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
        block_images: Any = _UNSET,
        block_level: Literal["minimal", "balanced", "aggressive"] | None = None,
        accept_language: str = "en-US,en;q=0.5",
        humanize: bool | None = None,
        session_rotation: Literal["sticky", "rotate"] | None = None,
        session_duration: str | int | None = None,
        request_pacing: float | None = None,
        stealth_level: Literal["off", "balanced", "max"] = "off",
        raise_on_error: bool = False,
        screenshot_max_bytes: int = 50 * 1024 * 1024,
        browser_pre_check_byte_margin: float = 1.5,
    ) -> PageFetchConfig:
        if not isinstance(stealth_level, str) or stealth_level not in VALID_STEALTH_LEVELS:
            raise ValueError(f"stealth_level must be one of {sorted(VALID_STEALTH_LEVELS)}")
        preset = _STEALTH_PRESETS[stealth_level]
        block_level = preset["block_level"] if block_level is None else block_level
        humanize = preset["humanize"] if humanize is None else humanize
        session_rotation = (
            preset["session_rotation"] if session_rotation is None else session_rotation
        )
        request_pacing = preset["request_pacing"] if request_pacing is None else request_pacing
        # Resolve ``block_images`` *after* the preset: the legacy default
        # was True, but image blocking inside Camoufox leaves CSS/Canvas
        # fingerprints inconsistent (bot-detection leak).  For ``balanced``
        # and ``max`` stealth levels we therefore default to ``False`` and
        # let the caller opt back in explicitly.  An explicit ``True`` /
        # ``False`` from the caller is honored regardless of stealth level.
        if block_images is _UNSET:
            block_images = stealth_level not in {"balanced", "max"}

        if not isinstance(mode, str) or mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
        if not isinstance(proxy, str) or proxy not in VALID_PROXIES:
            raise ValueError(f"proxy must be one of {sorted(VALID_PROXIES)}")
        if not isinstance(cleaning_level, str) or cleaning_level not in VALID_CLEANING_LEVELS:
            raise ValueError(f"cleaning_level must be one of {sorted(VALID_CLEANING_LEVELS)}")
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
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
        if (
            not isinstance(confidence_threshold, int | float)
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
        if session_duration is not None:
            if (
                isinstance(session_duration, bool)
                or not isinstance(session_duration, (str, int))
            ):
                raise ValueError(
                    "session_duration must be a duration string ('30m', '2h', '1d'), "
                    "a positive integer (seconds), or None"
                )
            if isinstance(session_duration, int) and session_duration <= 0:
                raise ValueError(
                    "session_duration must be a positive integer (seconds) or None"
                )
        if (
            not isinstance(request_pacing, int | float)
            or isinstance(request_pacing, bool)
            or not math.isfinite(request_pacing)
            or request_pacing < 0
        ):
            raise ValueError("request_pacing must be a non-negative finite number")
        if (
            not isinstance(screenshot_max_bytes, int)
            or isinstance(screenshot_max_bytes, bool)
            or screenshot_max_bytes <= 0
        ):
            raise ValueError("screenshot_max_bytes must be a positive integer")
        if (
            not isinstance(browser_pre_check_byte_margin, int | float)
            or isinstance(browser_pre_check_byte_margin, bool)
            or not math.isfinite(browser_pre_check_byte_margin)
            or browser_pre_check_byte_margin < 1.0
        ):
            raise ValueError("browser_pre_check_byte_margin must be a finite number >= 1.0")
        ttl = parse_duration(cache_ttl)
        path = Path(cache_path).expanduser() if cache_path is not None else user_cache_path("pagefetch") / "cache.sqlite3"
        # ``session_duration`` accepts either a duration string (``"30m"``,
        # ``"2h"``, ``"1d"``) or an integer-seconds value. ``None`` is
        # preserved so the dataclass field can carry the documented
        # "no TTL token" default.
        session_duration_seconds: int | None
        if session_duration is None:
            session_duration_seconds = None
        elif isinstance(session_duration, str):
            session_duration_seconds = parse_duration(session_duration)
        else:
            session_duration_seconds = int(session_duration)
        return cls(
            mode=mode,
            proxy=proxy,
            cleaning_level=cleaning_level,
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
            session_duration=session_duration_seconds,
            request_pacing=float(request_pacing),
            stealth_level=stealth_level,
            raise_on_error=raise_on_error,
            screenshot_max_bytes=screenshot_max_bytes,
            browser_pre_check_byte_margin=float(browser_pre_check_byte_margin),
        )
