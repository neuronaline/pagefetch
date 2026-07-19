"""PageFetch exceptions."""

from __future__ import annotations

from .models import FetchErrorInfo


class PageFetchError(Exception):
    """Raised for a fetch failure when ``raise_on_error=True``."""

    def __init__(self, error: FetchErrorInfo, *, url: str | None = None) -> None:
        self.error = error
        self.url = url
        super().__init__(error.message)

