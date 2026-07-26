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

from .bootstrap import RuntimeBootstrapError, ensure_runtime_requirements
from .client import PageFetch
from .config import VALID_MODES, VALID_PROXIES, PageFetchConfig
from .exceptions import PageFetchError
from .models import FetchErrorInfo, FetchResult, ImageInfo, LinkInfo

# Attach a NullHandler so library consumers that do not configure logging
# never see "No handler found" warnings.
logging.getLogger("pagefetch").addHandler(logging.NullHandler())

# Optional dependencies are never installed implicitly. Call
# ensure_runtime_requirements() when an application wants an up-front check.

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
    "ensure_runtime_requirements",
]
