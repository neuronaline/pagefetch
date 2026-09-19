"""Proxy provider resolution — generic URL and residential providers."""

from .providers import (
    CUSTOM_PROXY_SCHEMES,
    ProxyConfigurationError,
    ProxySettings,
    inject_session_id_for,
    parse_proxy_url,
    redact_proxy_url,
    resolve_proxy,
)

__all__ = [
    "CUSTOM_PROXY_SCHEMES",
    "ProxyConfigurationError",
    "ProxySettings",
    "inject_session_id_for",
    "parse_proxy_url",
    "redact_proxy_url",
    "resolve_proxy",
]
