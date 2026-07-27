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

from .bootstrap import RuntimeBootstrapError, auto_bootstrap_browser, ensure_runtime_requirements
from .client import PageFetch
from .config import VALID_MODES, VALID_PROXIES, PageFetchConfig
from .exceptions import PageFetchError
from .models import FetchErrorInfo, FetchResult, ImageInfo, LinkInfo

# Attach a NullHandler so library consumers that do not configure logging
# never see "No handler found" warnings.
logging.getLogger("pagefetch").addHandler(logging.NullHandler())

# Browser dependencies (camoufox + browser binary) are auto-installed on
# first browser use.  Call ensure_runtime_requirements() for an up-front
# check without installation; call auto_bootstrap_browser() to force
# installation at any point.

__all__ = [
    "FetchErrorInfo",
    "FetchResult",
    "ImageInfo",
    "LinkInfo",
    "PageFetch",
    "PageFetchConfig",
    "PageFetchError",
    "RuntimeBootstrapError",
    "VALID_MODES",
    "VALID_PROXIES",
    "auto_bootstrap_browser",
    "ensure_runtime_requirements",
]
