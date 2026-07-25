"""Environment-driven proxy provider support."""

from __future__ import annotations

import hashlib
import os
import random
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
        config = {
            "server": urlunsplit(
                (parsed.scheme, parsed.netloc.split("@")[-1], "", "", "")
            )
        }
        if parsed.username:
            config["username"] = unquote(parsed.username)
        if parsed.password:
            config["password"] = unquote(parsed.password)
        return config


def _inject_session_id(proxy_url: str, session_id: str) -> str:
    """Embed *session_id* in the proxy username.

    Most residential proxy providers support session affinity by
    appending an identifier to the credentials username
    (e.g. ``user_ses_abc123``).

    Any previous session suffix (``_ses_<id>``) is stripped first so
    the caller can rotate without ever-growing usernames.
    """
    parsed = urlsplit(proxy_url)
    if not parsed.username:
        return proxy_url

    raw_user = unquote(parsed.username)
    # Strip a previously embedded session suffix.
    base_user = raw_user.rsplit("_ses_", 1)[0]
    new_user = quote(f"{base_user}_ses_{session_id}", safe="")

    netloc = parsed.netloc
    user_host = netloc.split("@", 1)
    if len(user_host) != 2:
        return proxy_url
    hostpart = user_host[1]
    password = quote(unquote(parsed.password), safe="") if parsed.password else ""
    if password:
        new_netloc = f"{new_user}:{password}@{hostpart}"
    else:
        new_netloc = f"{new_user}@{hostpart}"

    return urlunsplit(
        (parsed.scheme, new_netloc, parsed.path, parsed.query, parsed.fragment)
    )


def make_domain_session(domain: str) -> str:
    """Return a stable session ID for *domain*.

    The result is a short hex digest so different fetches for the same
    domain share the same proxy exit node (sticky session).
    """
    digest = hashlib.sha256(domain.encode()).hexdigest()
    return digest[:12]


def make_random_session() -> str:
    """Return a random session ID for per-request proxy rotation."""
    return format(random.getrandbits(48), "012x")


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
            raise ProxyConfigurationError(
                f"{prefix}_PROXY_URL is not a valid proxy URL"
            ) from exc
        if (
            parsed.scheme not in {"http", "https", "socks5"}
            or not parsed.hostname
            or not port
            or not parsed.username
            or parsed.password is None
        ):
            raise ProxyConfigurationError(
                f"{prefix}_PROXY_URL is not a valid proxy URL"
            )
        return ProxySettings(provider=provider, url=full_url)
    names = ["HOST", "PORT", "USERNAME", "PASSWORD"]
    values = {name: os.getenv(f"{prefix}_{name}") for name in names}
    missing = [f"{prefix}_{name}" for name, value in values.items() if not value]
    if missing:
        raise ProxyConfigurationError(
            f"missing proxy settings: {', '.join(missing)}"
        )
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
