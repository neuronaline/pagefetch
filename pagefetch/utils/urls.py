"""Conservative URL validation and normalization."""

from __future__ import annotations

import ipaddress
from urllib.parse import SplitResult, urlsplit, urlunsplit

import tldextract

_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)


def validate_url(url: str) -> SplitResult:
    """Validate that *url* is an absolute HTTP(S) URL.

    Returns the parsed ``SplitResult`` on success so callers can
    avoid a second ``urlsplit`` pass.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a non-empty string")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("only http and https URL schemes are supported")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("URL contains an invalid port") from exc
    return parsed


def normalize_url(url: str) -> str:
    """Normalize an HTTP URL without changing query semantics."""
    parsed = validate_url(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    try:
        hostname = ipaddress.ip_address(hostname).compressed
    except ValueError:
        hostname = hostname.encode("idna").decode("ascii")
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = parsed.port
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    netloc = f"{userinfo}{hostname}"
    if port and not default_port:
        netloc += f":{port}"
    return urlunsplit(SplitResult(scheme, netloc, parsed.path or "/", parsed.query, ""))


def registrable_host(url: str) -> str:
    """Return the registrable host using the bundled public-suffix snapshot."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    extracted = _TLD_EXTRACT(host)
    if extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}"
    return extracted.domain or host
