"""PageFetch — asynchronous Camoufox-powered web scraping and content extraction.

Quick start::

    import asyncio
    from pagefetch import PageFetch

    async def main():
        async with PageFetch() as client:
            result = await client.fetch("https://example.com")
            print(result.markdown)

    asyncio.run(main())
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .bootstrap import RuntimeBootstrapError, auto_bootstrap_browser, ensure_runtime_requirements
from .client import PageFetch
from .config import (
    VALID_BLOCK_LEVELS,
    VALID_CLEANING_LEVELS,
    VALID_PROXIES,
    VALID_SESSION_ROTATION,
    VALID_STEALTH_LEVELS,
    PageFetchConfig,
)
from .cookies import CookiePool, CookieProfile, CookieRecord, load_cookies, parse_cookie_pool
from .exceptions import PageFetchError
from .models import (
    ActionResult,
    CapturedXHR,
    ExpandedSection,
    FetchErrorInfo,
    FetchResult,
    ImageInfo,
    InlineScript,
    InlineStylesheet,
    LinkInfo,
    PageAction,
    PageStructure,
    ScriptInfo,
    StructureNode,
    StylesheetInfo,
    VALID_ACTION_TYPES,
)
from .fetching.actions import normalize_actions, parse_action
from .processing import StructureLimits, extract_document, extract_structure
from .proxy.providers import ProxyConfigurationError

# Attach a NullHandler so library consumers that do not configure logging
# never see "No handler found" warnings.
logging.getLogger("pagefetch").addHandler(logging.NullHandler())

# The Camoufox browser binary is downloaded via 'python -m camoufox fetch' by
# default, or opt-in automatic installation via PAGEFETCH_AUTO_INSTALL=1. Call
# ensure_runtime_requirements() for an up-front check without installation.

# Single source of truth is ``pyproject.toml``. ``importlib.metadata`` is the
# canonical lookup and works for installed wheels / sdists; the fallback keeps
# ``pagefetch.__version__`` usable in source-checkout / editable environments
# where the distribution metadata is not yet present.
try:
    __version__ = _pkg_version("pagefetch")
except PackageNotFoundError:  # pragma: no cover - source-checkout fallback
    # Mirrors the version declared in ``pyproject.toml`` so that
    # ``pagefetch.__version__`` is usable before the distribution metadata
    # is installed (editable checkouts, sdists).
    __version__ = "1.0.0"

__all__ = [
    "ActionResult",
    "CapturedXHR",
    "CookiePool",
    "CookieProfile",
    "CookieRecord",
    "ExpandedSection",
    "FetchErrorInfo",
    "FetchResult",
    "ImageInfo",
    "InlineScript",
    "InlineStylesheet",
    "LinkInfo",
    "PageAction",
    "PageFetch",
    "PageFetchConfig",
    "PageFetchError",
    "PageStructure",
    "ProxyConfigurationError",
    "RuntimeBootstrapError",
    "ScriptInfo",
    "StructureLimits",
    "StructureNode",
    "StylesheetInfo",
    "VALID_ACTION_TYPES",
    "VALID_BLOCK_LEVELS",
    "VALID_CLEANING_LEVELS",
    "VALID_PROXIES",
    "VALID_SESSION_ROTATION",
    "VALID_STEALTH_LEVELS",
    "auto_bootstrap_browser",
    "ensure_runtime_requirements",
    "extract_document",
    "extract_structure",
    "load_cookies",
    "normalize_actions",
    "parse_action",
    "parse_cookie_pool",
]
