"""PageFetch exceptions."""

from __future__ import annotations

from .models import FetchErrorInfo


class PageFetchError(Exception):
    """Base exception for PageFetch errors and failed fetches."""

    default_code: str = "pagefetch_error"

    def __init__(
        self,
        error: FetchErrorInfo | str,
        *,
        url: str | None = None,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        if isinstance(error, FetchErrorInfo):
            info = error
        else:
            info = FetchErrorInfo(
                code=code or self.default_code,
                message=str(error),
                retryable=retryable,
                exception_type=type(self).__name__,
            )
        self.error = info
        self.url = url
        super().__init__(info.message)


class ProxyConfigurationError(PageFetchError, ValueError):
    """Raised when a selected proxy provider is missing required settings."""

    default_code = "proxy_configuration_error"


class RuntimeBootstrapError(PageFetchError, RuntimeError):
    """Raised when a required runtime dependency or browser binary is unavailable."""

    default_code = "browser_bootstrap_error"


class TransportFailure(Exception):
    """Internal transport exception carrying structured :class:`FetchErrorInfo`."""

    def __init__(self, error: FetchErrorInfo, *, status_code: int | None = None) -> None:
        self.error = error
        self.status_code = status_code
        super().__init__(error.message)
