"""Conservative URL validation and normalization."""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from urllib.parse import SplitResult, quote, unquote, urlsplit, urlunsplit

import tldextract

_TLD_EXTRACT: tldextract.TLDExtract | None = None


def _get_tld_extract() -> tldextract.TLDExtract:
    """Lazily initialise the TLDExtract instance on first use."""
    global _TLD_EXTRACT
    if _TLD_EXTRACT is None:
        _TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)
    return _TLD_EXTRACT


# DNS-resolved SSRF defence. The textual checks below stop obvious cases
# (e.g. ``localhost``, ``.local``) and reject literal private/loopback
# addresses, but a hostname like ``127.0.0.1.nip.io`` or ``localtest.me``
# resolves at the OS level to a restricted address. ``socket.getaddrinfo``
# is the only reliable source of truth — we use it on every non-literal
# hostname and reject the URL if any returned address falls inside an
# unsafe range. The result is cached briefly so a busy page's many
# sub-resource requests do not hammer the resolver.
_DNS_CACHE_TTL_SECONDS = 60.0
_DNS_CACHE_MAX_SIZE = 2048
_DNS_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}
_DNS_CACHE_LOCK = threading.Lock()


def _ip_is_unsafe(ip_str: str) -> bool:
    """Return True if *ip_str* is private/loopback/link-local/etc."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # An unparseable address is a hostile input by definition.
        return True
    ipv4_mapped = getattr(ip, "ipv4_mapped", None)
    if ipv4_mapped is not None and _ip_is_unsafe(str(ipv4_mapped)):
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _default_resolve_host_ips(hostname: str) -> list[str]:
    """Resolve *hostname* to its IPv4/IPv6 addresses via the system resolver."""
    now = time.monotonic()
    with _DNS_CACHE_LOCK:
        cached = _DNS_CACHE.get(hostname)
        if cached is not None and now - cached[0] < _DNS_CACHE_TTL_SECONDS:
            return list(cached[1])
    try:
        # ``SOCK_STREAM`` mirrors what httpx would actually use; passing
        # ``proto=0`` keeps the resolver free to pick any family.
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        resolved: tuple[str, ...] = ()
    else:
        resolved = tuple({info[4][0] for info in infos if info and info[4]})
    with _DNS_CACHE_LOCK:
        if len(_DNS_CACHE) >= _DNS_CACHE_MAX_SIZE:
            expired = [k for k, v in _DNS_CACHE.items() if now - v[0] >= _DNS_CACHE_TTL_SECONDS]
            for k in expired:
                del _DNS_CACHE[k]
            while len(_DNS_CACHE) >= _DNS_CACHE_MAX_SIZE:
                _DNS_CACHE.pop(next(iter(_DNS_CACHE)))
        _DNS_CACHE[hostname] = (now, resolved)
    return list(resolved)


def resolve_host_ips(hostname: str) -> list[str]:
    """Public DNS-resolution hook.

    Exposed at module scope so tests (and any future policy override) can
    monkeypatch the resolution source without reaching into private state.
    """
    return _default_resolve_host_ips(hostname)


def is_safe_host(hostname: str) -> bool:
    """Return False if hostname resolves to a restricted network address.

    Textual filters catch the obvious cases (``localhost``, ``.local``).
    Literal IP literals are inspected directly. Any other hostname is
    resolved through the system resolver and rejected if **any** of the
    returned addresses falls inside a private, loopback, link-local,
    multicast, reserved, or unspecified range. Cloud metadata endpoints
    (``169.254.169.254``, the IPv6 ULA block) are covered by the same
    ranges. This closes the DNS-rebinding / wildcard-hostname (e.g.
    ``127.0.0.1.nip.io``) SSRF bypass that the previous textual-only
    implementation allowed through ``except ValueError: pass``.
    """
    host = hostname.strip().lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    # Literal IP — inspect directly (strip optional brackets for IPv6).
    clean_ip = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(clean_ip)
        return not _ip_is_unsafe(clean_ip)
    except ValueError:
        pass
    # Domain name — resolve and check every returned address.
    resolved = resolve_host_ips(host)
    if not resolved:
        # Fail closed: if we cannot resolve the hostname we cannot prove
        # it is safe to dial.
        return False
    return not any(_ip_is_unsafe(ip) for ip in resolved)


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
    if not is_safe_host(parsed.hostname):
        raise ValueError("URL points to a restricted local or private network address (SSRF protection)")
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
        userinfo = quote(unquote(parsed.username), safe="")
        if parsed.password:
            userinfo += f":{quote(unquote(parsed.password), safe='')}"
        userinfo += "@"
    netloc = f"{userinfo}{hostname}"
    if port and not default_port:
        netloc += f":{port}"
    return urlunsplit(SplitResult(scheme, netloc, parsed.path or "/", parsed.query, ""))


def read_urls_from_file(path: str) -> list[str]:
    """Read newline-delimited URLs from a text file."""
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"file not found: {path!r}")
    return [line.strip() for line in p.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def registrable_host(url: str) -> str:
    """Return the registrable host using the bundled public-suffix snapshot."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    extracted = _get_tld_extract()(host)
    if extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}"
    return extracted.domain or host
