"""PageFetch — asynchronous HTTP-first web fetching with browser fallback.

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
from .config import VALID_MODES, VALID_PROXIES, PageFetchConfig
from .exceptions import PageFetchError
from .models import (
    FetchErrorInfo,
    FetchResult,
    ImageInfo,
    InlineScript,
    InlineStylesheet,
    LinkInfo,
    PageStructure,
    ScriptInfo,
    StructureNode,
    StylesheetInfo,
)
from .processing import StructureLimits, extract_structure

# Attach a NullHandler so library consumers that do not configure logging
# never see "No handler found" warnings.
logging.getLogger("pagefetch").addHandler(logging.NullHandler())

# Browser dependencies (camoufox + browser binary) are auto-installed on
# first browser use.  Call ensure_runtime_requirements() for an up-front
# check without installation; call auto_bootstrap_browser() to force
# installation at any point.

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
    __version__ = "0.8.9"

__all__ = [
    "FetchErrorInfo",
    "FetchResult",
    "ImageInfo",
    "InlineScript",
    "InlineStylesheet",
    "LinkInfo",
    "PageFetch",
    "PageFetchConfig",
    "PageFetchError",
    "PageStructure",
    "RuntimeBootstrapError",
    "ScriptInfo",
    "StructureLimits",
    "StructureNode",
    "StylesheetInfo",
    "VALID_MODES",
    "VALID_PROXIES",
    "auto_bootstrap_browser",
    "ensure_runtime_requirements",
    "extract_structure",
]
