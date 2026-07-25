"""Environment-driven proxy provider support."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit, urlunsplit


class ProxyConfigurationError(ValueError):
    """Raised when a selected provider is missing required settings."""


@dataclass(slots=True, frozen=True)
class ProxySettings:
    provider: str
    url: str | None

    def browser_config(self) -> dict[str, str] | None:
        if not self.url:
            return None
        parsed = urlsplit(self.url)
        config = {"server": urlunsplit((parsed.scheme, parsed.netloc.split("@")[-1], "", "", ""))}
        if parsed.username:
            config["username"] = unquote(parsed.username)
        if parsed.password:
            config["password"] = unquote(parsed.password)
        return config


def resolve_proxy(provider: str) -> ProxySettings:
    """Resolve a provider using environment variables without exposing secrets."""
    if provider == "none":
        return ProxySettings(provider="none", url=None)
    if provider not in {"decodo", "dataimpulse"}:
        raise ProxyConfigurationError("unsupported proxy provider")
    prefix = provider.upper()
    full_url = os.getenv(f"{prefix}_PROXY_URL")
    if full_url:
        try:
            parsed = urlsplit(full_url)
            port = parsed.port
        except ValueError as exc:
            raise ProxyConfigurationError(f"{prefix}_PROXY_URL is not a valid proxy URL") from exc
        if (
            parsed.scheme not in {"http", "https", "socks5"}
            or not parsed.hostname
            or not port
            or not parsed.username
            or parsed.password is None
        ):
            raise ProxyConfigurationError(f"{prefix}_PROXY_URL is not a valid proxy URL")
        return ProxySettings(provider=provider, url=full_url)
    names = ["HOST", "PORT", "USERNAME", "PASSWORD"]
    values = {name: os.getenv(f"{prefix}_{name}") for name in names}
    missing = [f"{prefix}_{name}" for name, value in values.items() if not value]
    if missing:
        raise ProxyConfigurationError(f"missing proxy settings: {', '.join(missing)}")
    try:
        port = int(values["PORT"] or "")
    except ValueError as exc:
        raise ProxyConfigurationError(f"{prefix}_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ProxyConfigurationError(f"{prefix}_PORT must be between 1 and 65535")
    username = quote(values["USERNAME"] or "", safe="")
    password = quote(values["PASSWORD"] or "", safe="")
    # Read protocol from env var; defaults to http for backward compatibility.
    scheme = os.getenv(f"{prefix}_PROTOCOL", "http")
    if scheme not in {"http", "https", "socks5"}:
        raise ProxyConfigurationError(
            f"{prefix}_PROTOCOL must be one of http, https, or socks5, got '{scheme}'"
        )
    return ProxySettings(
        provider=provider,
        url=f"{scheme}://{username}:{password}@{values['HOST']}:{port}",
    )


def redact_proxy_url(value: str) -> str:
    """Return a log-safe proxy URL."""
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"***:***@{host}{port}", "", "", ""))
