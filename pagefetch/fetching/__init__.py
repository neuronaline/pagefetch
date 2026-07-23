"""HTTP and browser transport layers with automatic fallback."""

from .browser import BrowserFetcher, BrowserResponse
from .http import HTTPFetcher, HTTPResponse, TransportFailure
from .readiness import controlled_scroll, wait_for_stability

__all__ = [
    "BrowserFetcher",
    "BrowserResponse",
    "HTTPFetcher",
    "HTTPResponse",
    "TransportFailure",
    "controlled_scroll",
    "wait_for_stability",
]
