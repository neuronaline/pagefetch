"""PageFetch exceptions."""

from __future__ import annotations

from .models import FetchErrorInfo


class PageFetchError(Exception):
    """Raised for a fetch failure when ``raise_on_error=True``."""

    def __init__(self, error: FetchErrorInfo, *, url: str | None = None) -> None:
        self.error = error
        self.url = url
        super().__init__(error.message)


class TransportFailure(Exception):
    """Internal transport exception carrying structured :class:`FetchErrorInfo`."""

    def __init__(self, error: FetchErrorInfo, *, status_code: int | None = None) -> None:
        self.error = error
        self.status_code = status_code
        super().__init__(error.message)
