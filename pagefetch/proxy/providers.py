"""Environment-driven proxy provider resolution.

Supported provider values:

* ``none`` — no proxy is used.
* ``custom`` — any standard HTTP, HTTPS, or SOCKS5 proxy. The URL is read
  from ``CUSTOM_PROXY_URL`` and passed verbatim to the transport (no
  username rewriting, no session injection). Use this for self-hosted,
  datacenter, Tor, or any generic proxy.
* ``decodo`` — residential provider whose URLs embed a session ID in the
  username (``user_ses_<id>``). The URL is read from ``DECODO_PROXY_URL``
  and ``session_rotation`` controls whether the session is sticky (one
  exit per domain) or rotated per request.
* ``byteful`` — residential provider whose URLs embed a session ID as
  ``user_s_<id>``. The URL is read from ``BYTEFUL_PROXY_URL`` and
  ``session_rotation`` controls sticky vs. rotating exit selection.

Both residential providers document optional session-TTL tokens that the
injector appends when ``session_duration`` is configured:

* Decodo — ``sessionduration-<minutes>`` (DECODO_DOCS §4; range 1–1440).
* Byteful — ``_ttl_<number><unit>`` (BYTEFUL_DOCS §4; range 1 minute to
  7 days, units ``m``/``h``/``d``).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit, urlunsplit


class ProxyConfigurationError(ValueError):
    """Raised when a selected provider is missing required settings."""


# Public so the config layer and tests can keep their ``frozenset`` in sync
# without duplicating the literal.
VALID_PROXY_PROVIDERS = frozenset({"none", "custom", "decodo", "byteful"})
RESIDENTIAL_PROVIDERS = frozenset({"decodo", "byteful"})

# Schemes accepted by ``custom`` and residential providers. ``socks5h``
# is supported by httpx[http2,socks] via python-socks and resolves hostnames
# remotely (useful when local DNS cannot reach the target).
CUSTOM_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})

# Documented bounds for the optional session-TTL token. Both providers
# accept sticky sessions that survive longer than the implicit default
# only when the corresponding TTL token is appended to the username.
# DECODO_DOCS §4: ``sessionduration`` is "Range: 1 to 1440 (minutes)".
# BYTEFUL_DOCS §4: TTL "minimum 1 minute, maximum 7 days".
_DECODO_SESSION_DURATION_MINUTES = (1, 1440)  # inclusive
_BYTEFUL_SESSION_DURATION_SECONDS = (60, 7 * 24 * 3600)  # inclusive

# Process-scoped random seed so sticky domain sessions are consistent within
# a running process but do not permanently pin a domain to a burned IP across
# process restarts.
_PROCESS_SESSION_SEED: str = os.urandom(4).hex()

from ..utils.cache_dict import BoundedTTLCache

# Per-domain failure epoch counter. Incremented whenever a domain encounters
# an anti-bot block or challenge so subsequent requests and retries immediately
# rotate to a fresh residential exit IP.
_DOMAIN_FAILURE_EPOCHS: BoundedTTLCache[str, int] = BoundedTTLCache(max_size=2000)

# Repurposed 2-letter TLDs that are used globally rather than for a specific country.
_GENERIC_CCTLD_EXCLUSIONS: frozenset[str] = frozenset(
    {"io", "ai", "co", "me", "tv", "gg", "sh", "ly", "to", "cc", "fm", "ws", "is", "so", "vc", "la", "gl", "ac", "im"}
)

# Second-level government and military tokens across national ccTLDs
_RESTRICTED_GOV_TOKENS: frozenset[str] = frozenset({"gov", "mil", "gob", "gouv"})


def is_government_or_restricted_target(url_or_hostname: str) -> bool:
    """Return True if target host is a government or military domain restricted by residential proxies.

    Commercial residential proxy gateways (Byteful, Decodo, BrightData, etc.) reject CONNECT
    tunnels to .gov, .mil, and national government ccTLD domains (.gov.*, .mil.*, .gob.*, .gouv.*)
    at the gateway level with 403 Forbidden.
    """
    if not url_or_hostname:
        return False
    parsed = urlsplit(url_or_hostname)
    hostname = (parsed.hostname or url_or_hostname).lower().strip(".")
    if not hostname:
        return False
    parts = hostname.split(".")
    if len(parts) >= 2:
        tld = parts[-1]
        if tld in {"gov", "mil"}:
            return True
        if len(parts) >= 3 and parts[-2] in _RESTRICTED_GOV_TOKENS:
            return True
    return False


def _detect_env_country() -> str | None:
    """Return an explicit two-letter ISO country code from environment variables if set."""
    env_country = (os.getenv("PROXY_COUNTRY") or os.getenv("DEFAULT_COUNTRY") or "").strip().lower()
    if len(env_country) == 2 and env_country.isalpha():
        return env_country
    return None


def infer_target_country(url: str) -> str | None:
    """Infer a two-letter ISO country code from *url* or environment variables."""
    env_override = _detect_env_country()
    if env_override:
        return env_override
    parsed = urlsplit(url)
    hostname = (parsed.hostname or url).lower().strip(".")
    if hostname:
        parts = hostname.split(".")
        if len(parts) >= 2:
            tld = parts[-1]
            if len(tld) == 2 and tld.isalpha() and tld not in _GENERIC_CCTLD_EXCLUSIONS:
                return "gb" if tld == "uk" else tld
    return None


def accept_language_for_country(
    country: str | None,
    default: str = "en-US,en;q=0.5",
) -> str:
    """Return an ``Accept-Language`` header aligned with *country* unless *default* was customized."""
    if default != "en-US,en;q=0.5" or not country:
        return default
    cc = country.lower()
    if cc == "us":
        return default
    if cc in {"gb", "ca", "au", "nz", "ie"}:
        return f"en-{cc.upper()},en;q=0.9"
    return f"{cc}-{cc.upper()},{cc};q=0.9,en-US;q=0.8,en;q=0.7"


def bump_domain_session(domain: str) -> int:
    """Increment the failure epoch for *domain* so subsequent requests rotate to a new proxy session."""
    return _DOMAIN_FAILURE_EPOCHS.increment(domain.lower().strip("."))


def get_domain_epoch(domain: str) -> int:
    """Return the current failure epoch for *domain*."""
    return _DOMAIN_FAILURE_EPOCHS.get(domain.lower().strip("."), 0) or 0


@dataclass(slots=True, frozen=True)
class ProxySettings:
    provider: str
    url: str | None

    def browser_config(self) -> dict[str, str] | None:
        """Return Playwright ``server/username/password`` for the browser layer."""
        if not self.url:
            return None
        parsed = urlsplit(self.url)
        # Rebuild the server from the parsed components rather than splitting
        # ``netloc`` on ``"@"``; a raw ``@`` in the username or password would
        # otherwise be treated as the userinfo/host separator and corrupt the
        # resulting ``server`` value.
        port_part = f":{parsed.port}" if parsed.port else ""
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        config: dict[str, str] = {
            "server": f"{parsed.scheme}://{hostname}{port_part}"
        }
        if parsed.username:
            config["username"] = unquote(parsed.username)
        if parsed.password:
            config["password"] = unquote(parsed.password)
        return config


def parse_proxy_url(
    value: str,
    *,
    provider: str,
    require_credentials: bool = False,
) -> ProxySettings:
    """Parse a full proxy URL into :class:`ProxySettings`.

    ``require_credentials`` is True for residential providers (they always
    authenticate); False for ``custom`` because self-hosted proxies may
    accept unauthenticated traffic.
    """
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ProxyConfigurationError(
            f"{provider} proxy URL is not a valid URL"
        ) from exc
    if parsed.scheme not in CUSTOM_PROXY_SCHEMES or not parsed.hostname or not port:
        raise ProxyConfigurationError(
            f"{provider} proxy URL must use http, https, socks5, or socks5h "
            f"with a host and port (got scheme={parsed.scheme!r})"
        )
    if require_credentials and (not parsed.username or parsed.password is None):
        raise ProxyConfigurationError(
            f"{provider} proxy URL must include username and password"
        )
    return ProxySettings(provider=provider, url=value)


def _rewrite_username(proxy_url: str, new_raw_user: str) -> str:
    """Return *proxy_url* with the username replaced by *new_raw_user*.

    Reconstructs the authority from the fields ``urlsplit`` already extracted
    rather than re-splitting ``netloc`` on ``@``. Splitting naively breaks when
    a password contains a raw ``@`` (which urllib leaves in the decoded form);
    using the parsed fields keeps the userinfo and host boundary intact.
    """
    parsed = urlsplit(proxy_url)
    if not parsed.username or not parsed.hostname:
        return proxy_url
    new_user = quote(new_raw_user, safe="")
    password = quote(unquote(parsed.password), safe="") if parsed.password else ""
    hostport = f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname
    userinfo = f"{new_user}:{password}@{hostport}" if password else f"{new_user}@{hostport}"
    return urlunsplit(
        (parsed.scheme, userinfo, parsed.path, parsed.query, parsed.fragment)
    )


def _format_byteful_ttl(seconds: int) -> str:
    """Return Byteful's documented TTL token (``_ttl_<number><unit>``).

    BYTEFUL_DOCS §4 documents three accepted units: ``m``, ``h``, ``d`` with
    a maximum lifetime of 7 days. We pick the largest unit that yields a
    whole number so the token stays compact (``_ttl_30m`` rather than
    ``_ttl_1800m``) and clamp to the documented 7-day upper bound.
    """
    lo, hi = _BYTEFUL_SESSION_DURATION_SECONDS
    if seconds < lo:
        # Below the documented 1-minute minimum; surface it so the caller
        # gets a precise error rather than silently coercing to zero.
        raise ProxyConfigurationError(
            "byteful session_duration must be at least 1 minute (BYTEFUL_DOCS §4)"
        )
    if seconds > hi:
        raise ProxyConfigurationError(
            "byteful session_duration must be at most 7 days (BYTEFUL_DOCS §4)"
        )
    if seconds % (24 * 3600) == 0:
        return f"{seconds // (24 * 3600)}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def _inject_byteful_session(
    proxy_url: str,
    session_id: str | None = None,
    *,
    session_duration_seconds: int | None = None,
    country: str | None = None,
) -> str:
    """Append Byteful's documented ``_c_<code>`` and ``_s_<id>`` tokens.

    See BYTEFUL_DOCS §4: ``residential.byteful.com:8000:{username}_c_{country}_s_{id}:{password}``.
    Omitting ``session_id`` yields the Basic Random Residential Proxy form,
    which rotates the egress IP on every request. When ``session_duration_seconds``
    is provided the TTL token is appended as documented in BYTEFUL_DOCS §4
    (``_ttl_<number><unit>``); 1 minute minimum, 7 days maximum.
    """
    parsed = urlsplit(proxy_url)
    if not parsed.username:
        return proxy_url
    if not country and not session_id:
        return proxy_url
    username = unquote(parsed.username)
    if country and "_c_" not in username:
        username = f"{username}_c_{country.lower()}"
    if session_id:
        if session_duration_seconds is not None:
            ttl_token = _format_byteful_ttl(session_duration_seconds)
            username = f"{username}_s_{session_id}_ttl_{ttl_token}"
        else:
            username = f"{username}_s_{session_id}"
    return _rewrite_username(proxy_url, username)


def _inject_decodo_session(
    proxy_url: str,
    session_id: str | None = None,
    *,
    session_duration_seconds: int | None = None,
    country: str | None = None,
) -> str:
    """Append Decodo's documented ``-country-<code>`` and ``-session-<id>`` tokens.

    See DECODO_DOCS §3 (Scheme A) and §4 (parameter matrix). Targeting
    parameters are hyphen-delimited (``session-<id>``, ``country-us``,
    ``asn-20057`` …), and Decodo rejects credentials whose targeting block
    is not prefixed with the literal ``user-`` token.  We auto-prepend the
    prefix when the supplied username lacks it; an already-prefixed
    username is preserved so callers can pass either ``prodUser`` or
    ``user-prodUser``.  When ``session_duration_seconds`` is provided the
    ``sessionduration-<minutes>`` token is appended as documented in
    DECODO_DOCS §4 (1–1440 minutes inclusive).
    """
    parsed = urlsplit(proxy_url)
    if not parsed.username:
        return proxy_url
    raw_user = unquote(parsed.username)
    if not country and not session_id:
        return proxy_url
    base = raw_user if raw_user.startswith("user-") else f"user-{raw_user}"
    username = base
    if country and "-country-" not in username:
        username = f"{username}-country-{country.lower()}"
    if session_id:
        username = f"{username}-session-{session_id}"
        if session_duration_seconds is not None:
            minutes = session_duration_seconds // 60
            lo, hi = _DECODO_SESSION_DURATION_MINUTES
            if minutes < lo:
                raise ProxyConfigurationError(
                    "decodo session_duration must be at least 1 minute (DECODO_DOCS §4)"
                )
            if minutes > hi:
                raise ProxyConfigurationError(
                    "decodo session_duration must be at most 1440 minutes (DECODO_DOCS §4)"
                )
            username = f"{username}-sessionduration-{minutes}"
    return _rewrite_username(proxy_url, username)


# Provider-specific sticky-session injectors. ``custom`` proxies have no
# concept of session affinity — callers must pass the URL through
# verbatim — so they are intentionally absent here.  See DECODO_DOCS §3,
# §4 and BYTEFUL_DOCS §4 for the documented grammars.
_SESSION_INJECTORS: dict[str, Callable[..., str]] = {
    "decodo": _inject_decodo_session,
    "byteful": _inject_byteful_session,
}


def inject_session_id_for(
    provider: str,
    proxy_url: str,
    session_id: str | None = None,
    *,
    session_duration_seconds: int | None = None,
    country: str | None = None,
) -> str:
    """Inject a sticky session ID and optional country code using *provider* syntax.

    Dispatches to the provider-specific injector registered in
    :data:`_SESSION_INJECTORS`.  Unknown providers and ``custom`` are
    returned unchanged so callers cannot accidentally emit a token the
    upstream gateway will reject.  When ``session_duration_seconds`` is
    provided, the provider's documented TTL/duration token is appended
    after the session ID (``-sessionduration-<minutes>`` for Decodo,
    ``_ttl_<number><unit>`` for Byteful).
    """
    injector = _SESSION_INJECTORS.get(provider)
    if injector is None:
        return proxy_url
    return injector(
        proxy_url,
        session_id,
        session_duration_seconds=session_duration_seconds,
        country=country,
    )


def make_domain_session(domain: str, salt: str = "") -> str:
    """Return a session ID for *domain* (sticky: one exit per domain per epoch).

    Hex strings are a safe subset of the alphanumeric session-ID grammar
    documented for both DECODO (DECODO_DOCS §4) and Byteful (BYTEFUL_DOCS
    §4: "random alphanumeric"). Incorporates the process session seed and
    any domain failure epoch so burned IPs are automatically rotated.
    """
    epoch = get_domain_epoch(domain)
    parts = [domain, _PROCESS_SESSION_SEED]
    if epoch:
        parts.append(f"e{epoch}")
    if salt:
        parts.append(salt)
    token = ":".join(parts)
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def make_random_session() -> str:
    """Return a fresh random alphanumeric session ID for a single fetch operation."""
    return os.urandom(6).hex()


def resolve_proxy(provider: str) -> ProxySettings:
    """Resolve a provider from environment variables.

    * ``none`` returns an empty :class:`ProxySettings`.
    * ``custom`` reads ``CUSTOM_PROXY_URL``; credentials are optional.
    * ``decodo`` reads ``DECODO_PROXY_URL``; credentials are mandatory.
    * ``byteful`` reads ``BYTEFUL_PROXY_URL``; credentials are mandatory.

    Raises :class:`ProxyConfigurationError` for unknown providers or
    missing/invalid environment variables.
    """
    if provider == "none":
        return ProxySettings(provider="none", url=None)

    if provider == "custom":
        full_url = os.getenv("CUSTOM_PROXY_URL") or os.getenv("PROXY_URL")
        if not full_url:
            raise ProxyConfigurationError(
                "missing proxy settings: CUSTOM_PROXY_URL (or PROXY_URL) with a full "
                "proxy URL, e.g. socks5://user:pass@host:1080"
            )
        return parse_proxy_url(full_url, provider="custom")

    if provider in RESIDENTIAL_PROVIDERS:
        full_url = os.getenv(f"{provider.upper()}_PROXY_URL")
        if not full_url:
            raise ProxyConfigurationError(
                f"missing proxy settings: {provider.upper()}_PROXY_URL with "
                f"a full proxy URL including username and password"
            )
        return parse_proxy_url(
            full_url, provider=provider, require_credentials=True
        )

    raise ProxyConfigurationError(
        f"unsupported proxy provider: {provider!r}. "
        f"Use one of: {sorted(VALID_PROXY_PROVIDERS)}"
    )
