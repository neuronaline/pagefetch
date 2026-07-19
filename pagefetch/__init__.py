"""Public PageFetch API."""

import logging

from .bootstrap import RuntimeBootstrapError, ensure_runtime_requirements

# NOTE: ensure_runtime_requirements() is NOT called here to avoid side effects
# on import (network calls / subprocess execution).  It is called lazily inside
# PageFetch.start() before the first fetch operation.  If you use the library
# without calling PageFetch.start() directly, call
#   pagefetch.ensure_runtime_requirements()
# explicitly, or set PAGEFETCH_AUTO_INSTALL=0 to disable auto-installation.
logging.getLogger("pagefetch").addHandler(logging.NullHandler())

from .client import PageFetch  # noqa: E402
from .exceptions import PageFetchError  # noqa: E402
from .models import FetchErrorInfo, FetchResult, ImageInfo, LinkInfo  # noqa: E402

__all__ = [
    "FetchErrorInfo",
    "FetchResult",
    "ImageInfo",
    "LinkInfo",
    "PageFetch",
    "PageFetchError",
    "RuntimeBootstrapError",
]

__version__ = "0.1.0"
