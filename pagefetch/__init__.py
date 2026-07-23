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

# NOTE: ensure_runtime_requirements() is deliberately NOT called at import
# time — it performs network I/O (pip install, browser download).  It fires
# lazily inside PageFetch.start() before the first fetch.  If you bypass
# PageFetch.start(), call pagefetch.ensure_runtime_requirements() explicitly
# or set PAGEFETCH_AUTO_INSTALL=0 to opt out.

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

__version__ = "0.1.0"
