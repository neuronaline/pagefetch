"""Proxy provider resolution — generic URL and residential providers."""

from .providers import (
    CUSTOM_PROXY_SCHEMES,
    ProxyConfigurationError,
    ProxySettings,
    accept_language_for_country,
    bump_domain_session,
    get_domain_epoch,
    infer_target_country,
    inject_session_id_for,
    make_domain_session,
    make_random_session,
    parse_proxy_url,
    redact_proxy_url,
    resolve_proxy,
)

__all__ = [
    "CUSTOM_PROXY_SCHEMES",
    "ProxyConfigurationError",
    "ProxySettings",
    "accept_language_for_country",
    "bump_domain_session",
    "get_domain_epoch",
    "infer_target_country",
    "inject_session_id_for",
    "make_domain_session",
    "make_random_session",
    "parse_proxy_url",
    "redact_proxy_url",
    "resolve_proxy",
]
