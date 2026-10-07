"""Critical-path tests for PageFetch.

This file is reserved for safety limits, authorization, and essential flows that
prevent data loss. Unit details and implementation-specific tests live elsewhere.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from pagefetch import PageFetch, PageFetchError, PageStructure, StructureNode
from pagefetch.cache.keys import build_cache_key
from pagefetch.cache.sqlite import SQLiteCache
from pagefetch.cli import _build_config, build_parser
from pagefetch.config import PageFetchConfig
from pagefetch.fetching.http import HTTPFetcher, TransportFailure
from pagefetch.models import FetchResult
from pagefetch.proxy.providers import ProxyConfigurationError, redact_proxy_url, resolve_proxy
from pagefetch.utils.urls import normalize_url, validate_url

# ---------------------------------------------------------------------------
# URL validation: reject unsafe schemes (SSRF / data-exfiltration prevention)
# ---------------------------------------------------------------------------


def test_url_validation_and_normalization():
    assert normalize_url("HTTPS://Example.COM:443?q=1#fragment") == "https://example.com/?q=1"
    assert normalize_url("http://example.com:8080") == "http://example.com:8080/"
    with pytest.raises(ValueError):
        validate_url("file:///tmp/page")


def test_validate_url_blocks_hostname_resolving_to_private_ip(monkeypatch):
    """Phase 1 Item 1: ``validate_url`` must reject hostnames whose DNS
    resolves to a private/loopback/metadata address.  The previous textual
    filter let ``127.0.0.1.nip.io`` and ``localtest.me`` slip through by
    swallowing the ``ip_address`` ``ValueError`` and returning ``True``.
    """
    from pagefetch.utils import urls as urls_module

    def fake_resolve(host: str) -> list[str]:
        # ``nip.io`` and similar wildcard DNS services map the label to a
        # literal loopback address; emulate that resolver behaviour.
        if host == "127.0.0.1.nip.io":
            return ["127.0.0.1"]
        if host == "localtest.me":
            return ["127.0.0.1"]
        if host == "metadata.aws.example":
            return ["169.254.169.254"]
        if host == "example.com":
            return ["93.184.216.34"]
        return []

    monkeypatch.setattr(urls_module, "resolve_host_ips", fake_resolve)
    # Wildcard-style hostnames that resolve into the unsafe ranges are blocked.
    for hostile in (
        "https://127.0.0.1.nip.io/",
        "http://localtest.me/admin",
        "http://metadata.aws.example/latest/meta-data/",
    ):
        with pytest.raises(ValueError, match="SSRF"):
            validate_url(hostile)
    # A hostname resolving to a public address still validates.
    assert validate_url("https://example.com/").hostname == "example.com"


# ---------------------------------------------------------------------------
# Cache keys: cross-tenant / cross-proxy isolation (data integrity)
# ---------------------------------------------------------------------------


def test_cache_keys_are_stable_and_provider_specific():
    canonical = build_cache_key("HTTPS://Example.com#x", mode="auto", proxy="none")
    assert canonical == build_cache_key("https://example.com/", mode="auto", proxy="none")
    assert canonical != build_cache_key("https://example.com/", mode="auto", proxy="decodo")
    assert "example" not in canonical  # hostname must not leak into the key


# ---------------------------------------------------------------------------
# Proxy credentials: authorization + safe redaction
# ---------------------------------------------------------------------------


def test_proxy_environment_resolves_and_redacts(monkeypatch):
    """``decodo`` accepts a full URL with credentials and redacts them in logs."""
    monkeypatch.setenv(
        "DECODO_PROXY_URL",
        "http://user%40zone:secret%3Avalue@proxy.example:1234",
    )
    settings = resolve_proxy("decodo")
    assert settings.url == "http://user%40zone:secret%3Avalue@proxy.example:1234"
    assert settings.browser_config() == {
        "server": "http://proxy.example:1234",
        "username": "user@zone",
        "password": "secret:value",
    }
    assert redact_proxy_url(settings.url) == "http://***:***@proxy.example:1234"
    # Phase 4 Item 10: a raw ``@`` in the password must not be treated as
    # the userinfo/host separator when ``browser_config`` rebuilds the
    # ``server`` field. The previous implementation split ``netloc`` on
    # ``"@"`` and produced a corrupted ``server`` URL.
    monkeypatch.setenv(
        "DECODO_PROXY_URL",
        "http://user:p@ssword@proxy.example:1234",
    )
    raw_at = resolve_proxy("decodo")
    assert raw_at.browser_config()["server"] == "http://proxy.example:1234"
    assert raw_at.browser_config()["password"] == "p@ssword"
    monkeypatch.setenv(
        "DECODO_PROXY_URL",
        "http://user:pass@[::1]:8080",
    )
    ipv6_proxy = resolve_proxy("decodo")
    assert ipv6_proxy.browser_config()["server"] == "http://[::1]:8080"


def test_custom_proxy_url_resolves_for_each_scheme(monkeypatch):
    """``custom`` accepts http/https/socks5 with optional credentials."""
    monkeypatch.delenv("CUSTOM_PROXY_URL", raising=False)
    for scheme in ("http", "https", "socks5"):
        monkeypatch.setenv("CUSTOM_PROXY_URL", f"{scheme}://1.2.3.4:1080")
        settings = resolve_proxy("custom")
        assert settings.provider == "custom"
        assert settings.url == f"{scheme}://1.2.3.4:1080"
        # browser_config strips credentials — there are none, so only ``server``.
        assert settings.browser_config() == {"server": f"{scheme}://1.2.3.4:1080"}
    # PROXY_URL fallback is honored when CUSTOM_PROXY_URL is unset
    monkeypatch.delenv("CUSTOM_PROXY_URL", raising=False)
    monkeypatch.setenv("PROXY_URL", "socks5://1.2.3.4:1080")
    assert resolve_proxy("custom").url == "socks5://1.2.3.4:1080"


def test_custom_proxy_missing_env_raises(monkeypatch):
    monkeypatch.delenv("CUSTOM_PROXY_URL", raising=False)
    monkeypatch.delenv("PROXY_URL", raising=False)
    with pytest.raises(ProxyConfigurationError, match="CUSTOM_PROXY_URL"):
        resolve_proxy("custom")


def test_custom_proxy_invalid_scheme_raises(monkeypatch):
    monkeypatch.setenv("CUSTOM_PROXY_URL", "ftp://proxy.example.com:21")
    with pytest.raises(ProxyConfigurationError, match="http, https, socks5, or socks5h"):
        resolve_proxy("custom")


def test_custom_proxy_url_is_passed_verbatim(monkeypatch):
    """``custom`` URLs must reach the transport verbatim — no session injection, no rewriting.

    The previous implementation re-parsed the URL and re-issued a sticky token;
    that broke self-hosted proxies that don't understand the residential
    targeting grammar.  This test guards both transport paths (browser pool
    and HTTP helper) against the same regression.
    """
    monkeypatch.setenv("CUSTOM_PROXY_URL", "socks5://user:pass@proxy.example.com:1080")
    client = PageFetch(proxy="custom", cache_enabled=False)
    try:
        _, browser_proxy = client._browser_pool_target(
            "custom", "https://example.com/path"
        )
        assert browser_proxy.url == "socks5://user:pass@proxy.example.com:1080"
        # The HTTP path now goes through the same _resolve_proxy_url helper,
        # so the same URL flows through unchanged there as well.
        http_proxy = client._resolve_proxy_url("custom", "https://example.com/path")
        assert http_proxy == "socks5://user:pass@proxy.example.com:1080"
    finally:
        client._closed = True


def test_socks5h_scheme_is_accepted(monkeypatch):
    """``socks5h`` is documented in the README and supported by httpx[http2,socks]."""
    monkeypatch.setenv("CUSTOM_PROXY_URL", "socks5h://user:pass@proxy.example.com:1080")
    settings = resolve_proxy("custom")
    assert settings.url == "socks5h://user:pass@proxy.example.com:1080"


def test_residential_proxy_url_requires_credentials(monkeypatch):
    """The shared parser must reject credential-less URLs for residential providers."""
    monkeypatch.setenv("DECODO_PROXY_URL", "http://proxy.example.com:8080")
    with pytest.raises(ProxyConfigurationError, match="username and password"):
        resolve_proxy("decodo")
    monkeypatch.setenv("BYTEFUL_PROXY_URL", "http://proxy.example.com:8080")
    with pytest.raises(ProxyConfigurationError, match="username and password"):
        resolve_proxy("byteful")


def test_byteful_provider_resolves_and_redacts(monkeypatch):
    """``byteful`` reads ``BYTEFUL_PROXY_URL`` and redacts it like the others."""
    monkeypatch.setenv(
        "BYTEFUL_PROXY_URL",
        "https://user:secret@residential.byteful.com:8000",
    )
    settings = resolve_proxy("byteful")
    assert settings.provider == "byteful"
    assert settings.url == "https://user:secret@residential.byteful.com:8000"
    assert settings.browser_config() == {
        "server": "https://residential.byteful.com:8000",
        "username": "user",
        "password": "secret",
    }
    assert redact_proxy_url(settings.url) == "https://***:***@residential.byteful.com:8000"


def test_byteful_session_injection_uses_byteful_token():
    """``byteful`` URLs embed the session ID with ``_s_`` (not Decodo's ``-session-``)."""
    from pagefetch.proxy.providers import inject_session_id_for

    base = "https://user:secret@residential.byteful.com:8000"
    rewritten = inject_session_id_for("byteful", base, "abc123456789")
    # Byteful's documented sticky-session suffix is ``_s_<id>``:
    assert "_s_abc123456789" in rewritten
    # And the helper must NOT silently emit the Decodo hyphen syntax.
    assert "-session-" not in rewritten
    # Decodo uses its own hyphen-delimited syntax; the dispatch picks the right one.
    decodo = inject_session_id_for(
        "decodo", "http://user:secret@residential.decodo.com:8000", "abc123456789"
    )
    assert "-session-abc123456789" in decodo
    assert "_s_abc123456789" not in decodo


def test_decodo_session_injection_uses_documented_hyphen_syntax():
    """Decodo's documented grammar (DECODO_DOCS §3, §4) is hyphen-delimited
    and requires the ``user-`` prefix on the username whenever any targeting
    parameter is appended. The injector must auto-prepend the prefix when
    missing and preserve it when the caller has already supplied it.
    """
    from pagefetch.proxy.providers import inject_session_id_for

    # Username without the ``user-`` prefix gets one prepended.
    unprefixed = inject_session_id_for(
        "decodo", "http://prodUser:secret@gate.decodo.com:7000", "abc123456789"
    )
    assert unprefixed.startswith("http://user-prodUser-session-abc123456789:secret@")
    # Username already prefixed is preserved (no double ``user-``).
    prefixed = inject_session_id_for(
        "decodo", "http://user-prodUser:secret@gate.decodo.com:7000", "abc123456789"
    )
    assert prefixed.startswith("http://user-prodUser-session-abc123456789:secret@")
    # Pre-existing targeting parameters (e.g. ``country-us``) are kept intact
    # and the new ``-session-<id>`` token is appended at the tail.
    already_targeted = inject_session_id_for(
        "decodo",
        "http://user-prodUser-country-us:secret@gate.decodo.com:7000",
        "abc123456789",
    )
    assert (
        "user-prodUser-country-us-session-abc123456789"
        in already_targeted
    )


def test_residential_rotate_mode_skips_session_injection(monkeypatch):
    """Per DECODO_DOCS §2 and BYTEFUL_DOCS §3 / §4, both residential
    networks rotate the egress IP on every request when NO session token
    is appended. ``PageFetch.fetch`` must therefore pass the configured
    proxy URL through verbatim under ``session_rotation=rotate`` — never
    silently emit a sticky token.
    """
    from pagefetch.proxy.providers import inject_session_id_for

    # Sanity: the helper itself does not emit anything for unknown providers.
    assert (
        inject_session_id_for("custom", "http://user:pass@host:1234", "abc123456789")
        == "http://user:pass@host:1234"
    )

    client = PageFetch(
        proxy="decodo",
        session_rotation="rotate",
        cache_enabled=False,
    )
    monkeypatch.setenv(
        "DECODO_PROXY_URL", "http://user-prodUser:secret@gate.decodo.com:7000"
    )
    try:
        _, proxy = client._browser_pool_target(
            "decodo", "https://example.com/path"
        )
        # Decodo's rotate-by-default behaviour: no ``-session-`` token appended.
        assert proxy.url == "http://user-prodUser:secret@gate.decodo.com:7000"
        assert "-session-" not in (proxy.url or "")
    finally:
        client._closed = True

    monkeypatch.setenv(
        "BYTEFUL_PROXY_URL",
        "https://user:secret@residential.byteful.com:8000",
    )
    client2 = PageFetch(
        proxy="byteful",
        session_rotation="rotate",
        cache_enabled=False,
    )
    try:
        _, proxy = client2._browser_pool_target(
            "byteful", "https://example.com/path"
        )
        # Byteful's Basic Random Residential Proxy form rotates per request.
        assert proxy.url == "https://user:secret@residential.byteful.com:8000"
        assert "_s_" not in (proxy.url or "")
    finally:
        client2._closed = True



def test_unsupported_provider_lists_valid_options():
    from pagefetch.proxy.providers import VALID_PROXY_PROVIDERS

    with pytest.raises(ProxyConfigurationError) as exc:
        resolve_proxy("tor")
    # The error message must enumerate the valid providers so the user can
    # fix the config without consulting the docs.
    assert "custom" in str(exc.value)
    assert "decodo" in str(exc.value)
    assert "byteful" in str(exc.value)
    assert set(VALID_PROXY_PROVIDERS) == {"none", "custom", "decodo", "byteful"}


def test_decodo_session_duration_injects_documented_token():
    """DECODO_DOCS §4 documents ``sessionduration-<minutes>`` (1–1440 min)
    as the documented sticky-session TTL token. The injector must append it
    in the same hyphen-delimited targeting block as the session ID and
    auto-prepend ``user-`` when missing.
    """
    from pagefetch.proxy.providers import inject_session_id_for

    # Bare username: ``user-`` is auto-prepended; ``-sessionduration-30``
    # follows the existing ``-session-<id>`` block (DECODO_DOCS §4).
    rewritten = inject_session_id_for(
        "decodo",
        "http://prodUser:secret@gate.decodo.com:7000",
        "abc123456789",
        session_duration_seconds=30 * 60,
    )
    assert rewritten.startswith(
        "http://user-prodUser-session-abc123456789-sessionduration-30:secret@"
    )

    # Already-prefixed username stays prefixed (no double ``user-``).
    prefixed = inject_session_id_for(
        "decodo",
        "http://user-prodUser:secret@gate.decodo.com:7000",
        "abc123456789",
        session_duration_seconds=120,
    )
    assert prefixed.startswith(
        "http://user-prodUser-session-abc123456789-sessionduration-2:secret@"
    )

    # Pre-existing targeting tokens are preserved.
    preserved = inject_session_id_for(
        "decodo",
        "http://user-prodUser-country-us:secret@gate.decodo.com:7000",
        "abc123456789",
        session_duration_seconds=600,
    )
    assert (
        "user-prodUser-country-us-session-abc123456789-sessionduration-10"
        in preserved
    )

    # Out-of-range values fail loud with the documented limits in the message.
    with pytest.raises(ProxyConfigurationError, match="1440 minutes"):
        inject_session_id_for(
            "decodo",
            "http://prodUser:secret@gate.decodo.com:7000",
            "abc123456789",
            session_duration_seconds=1441 * 60,
        )
    with pytest.raises(ProxyConfigurationError, match="at least 1 minute"):
        inject_session_id_for(
            "decodo",
            "http://prodUser:secret@gate.decodo.com:7000",
            "abc123456789",
            session_duration_seconds=30,  # 30 seconds < 1 minute
        )


def test_byteful_session_duration_injects_documented_ttl_token():
    """BYTEFUL_DOCS §4 documents ``_ttl_<n><unit>`` (1 minute – 7 days)
    as the documented sticky-session TTL token. The injector must append
    it after ``_s_<id>`` and pick the largest unit that yields a whole
    number (so the token stays compact).
    """
    from pagefetch.proxy.providers import inject_session_id_for

    base = "https://user:secret@residential.byteful.com:8000"

    # 30 minutes → ``_ttl_30m``.
    thirty_minutes = inject_session_id_for(
        "byteful", base, "abc123456789", session_duration_seconds=30 * 60
    )
    assert "_s_abc123456789_ttl_30m" in thirty_minutes

    # 2 hours → ``_ttl_2h`` (largest whole-unit).
    two_hours = inject_session_id_for(
        "byteful", base, "abc123456789", session_duration_seconds=2 * 3600
    )
    assert "_s_abc123456789_ttl_2h" in two_hours

    # 1 day → ``_ttl_1d`` (BYTEFUL_DOCS §4 max documented unit).
    one_day = inject_session_id_for(
        "byteful", base, "abc123456789", session_duration_seconds=24 * 3600
    )
    assert "_s_abc123456789_ttl_1d" in one_day

    # 7 days (the documented maximum).
    seven_days = inject_session_id_for(
        "byteful", base, "abc123456789", session_duration_seconds=7 * 24 * 3600
    )
    assert "_s_abc123456789_ttl_7d" in seven_days

    # Below 1 minute and above 7 days must surface a documented-bounds error.
    with pytest.raises(ProxyConfigurationError, match="at least 1 minute"):
        inject_session_id_for(
            "byteful", base, "abc123456789", session_duration_seconds=30
        )
    with pytest.raises(ProxyConfigurationError, match="at most 7 days"):
        inject_session_id_for(
            "byteful", base, "abc123456789", session_duration_seconds=8 * 24 * 3600
        )


def test_session_duration_round_trips_through_pagefetch_resolve(monkeypatch):
    """``session_duration`` must propagate from ``PageFetchConfig`` all the
    way through ``_resolve_proxy_url`` so a sticky HTTP request actually
    carries the documented TTL token in the proxy URL.
    """
    monkeypatch.setenv(
        "DECODO_PROXY_URL",
        "http://user-prodUser:secret@gate.decodo.com:7000",
    )
    client = PageFetch(
        proxy="decodo",
        session_duration="45m",
        cache_enabled=False,
    )
    try:
        url = client._resolve_proxy_url("decodo", "https://example.com/path")
        assert url is not None
        assert "-sessionduration-45" in url
        assert "-session-" in url  # the documented sticky token is still present
    finally:
        client._closed = True

    monkeypatch.setenv(
        "BYTEFUL_PROXY_URL",
        "https://user:secret@residential.byteful.com:8000",
    )
    client_b = PageFetch(
        proxy="byteful",
        session_duration=2 * 3600,
        cache_enabled=False,
    )
    try:
        url_b = client_b._resolve_proxy_url("byteful", "https://example.com/path")
        assert url_b is not None
        assert "_s_" in url_b and "_ttl_2h" in url_b
    finally:
        client_b._closed = True

    # ``session_rotation=rotate`` must still skip the TTL token (no token is
    # appended in rotate mode per DECODO_DOCS §2 / BYTEFUL_DOCS §3).
    client_rotate = PageFetch(
        proxy="byteful",
        session_rotation="rotate",
        session_duration="30m",
        cache_enabled=False,
    )
    try:
        url_r = client_rotate._resolve_proxy_url(
            "byteful", "https://example.com/path"
        )
        assert url_r == "https://user:secret@residential.byteful.com:8000"
    finally:
        client_rotate._closed = True


def test_session_duration_config_validation():
    """The config layer must surface obvious input errors up-front so the
    user sees a clean ``ValueError`` rather than a downstream provider
    rejection at request time.
    """
    with pytest.raises(ValueError, match="positive integer"):
        PageFetchConfig(session_duration=-1)
    with pytest.raises(ValueError, match="positive integer"):
        PageFetchConfig(session_duration=True)
    # ``0`` is meaningless — ``None`` already means "no TTL token", and
    # both residential providers reject sub-minute TTLs at request time.
    with pytest.raises(ValueError, match="positive integer"):
        PageFetchConfig(session_duration=0)
    # Duration strings are parsed by ``parse_duration``; bad units must
    # surface the same error the rest of the library raises for malformed
    # duration strings.
    with pytest.raises(ValueError):
        PageFetchConfig(session_duration="bad-unit")
    # Phase 4 Item 12: bare-digit duration strings (``"3600"``, ``"0"``)
    # round-trip through ``parse_duration`` so YAML/CLI values that arrive
    # quoted as strings flow through the same code path as integers.
    from pagefetch.utils.durations import parse_duration

    assert parse_duration("3600") == 3600
    assert parse_duration("0") == 0


def test_session_duration_is_part_of_cache_key():
    """``session_duration`` must be part of the cache key so toggling the
    TTL between fetches produces a fresh upstream request instead of a
    stale hit from a different TTL bucket. Mirrors the existing
    ``session_rotation`` treatment.
    """
    base = build_cache_key("https://example.com/", mode="auto", proxy="byteful")
    # Same provider + URL, different TTL bucket → distinct key.
    with_ttl = build_cache_key(
        "https://example.com/",
        mode="auto",
        proxy="byteful",
        settings={"session_duration": 30 * 60},
    )
    with_other_ttl = build_cache_key(
        "https://example.com/",
        mode="auto",
        proxy="byteful",
        settings={"session_duration": 2 * 3600},
    )
    none_ttl = build_cache_key(
        "https://example.com/",
        mode="auto",
        proxy="byteful",
        settings={"session_duration": None},
    )
    assert base != with_ttl
    assert with_ttl != with_other_ttl
    assert with_ttl != none_ttl


# ---------------------------------------------------------------------------
# SQLite cache: failed results must never be persisted (data integrity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_cache_persists_and_ignores_failures(tmp_path):
    path = tmp_path / "cache.sqlite3"
    cache = SQLiteCache(path)
    await cache.start()
    await cache.set(
        "good",
        FetchResult(
            url="https://example.com/",
            success=True,
            markdown="hello",
            html="<p>hello</p>",
            fetched_at=datetime.now(UTC),
        ),
        60,
    )
    await cache.set("failed", FetchResult(url="x", success=False), 60)
    await cache.close()

    reopened = SQLiteCache(path)
    await reopened.start()
    try:
        cached = await reopened.get("good")
        assert cached is not None
        assert cached.from_cache is True
        assert cached.fetch_method == "cache"
        assert cached.html == "<p>hello</p>"
        assert await reopened.get("failed") is None
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_expired_cache_is_ignored(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    await cache.start()
    try:
        await cache.set("expired", FetchResult(url="x", success=True), 0)
        assert await cache.get("expired") is None
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_cache_migrates_legacy_created_at_column(tmp_path):
    """An on-disk fetch_cache table left over from an older release still
    carries a ``created_at REAL NOT NULL`` column. The current INSERT never
    writes that column, so without migration every cache write fails with
    ``IntegrityError: NOT NULL constraint failed: fetch_cache.created_at``
    (the user-visible symptom is the ``"Result could not be written to
    cache."`` warning on every successful fetch). ``SQLiteCache.start()``
    must drop the legacy column in place via ``ALTER TABLE … DROP COLUMN``
    (SQLite ≥ 3.35) and bump ``PRAGMA user_version`` so existing rows
    survive and new writes succeed.
    """
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    # Pre-create the legacy schema and seed a row that mimics what a previous
    # version of the library would have written.
    seed = sqlite3.connect(path)
    seed.execute(
        """
        CREATE TABLE fetch_cache (
            cache_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )
    seed.execute(
        "INSERT INTO fetch_cache VALUES (?, ?, ?, ?)",
        ("legacy-key", '{"url":"https://example.com/","success":true}', 0.0, 0.0),
    )
    seed.commit()
    seed.close()

    cache = SQLiteCache(path)
    await cache.start()
    try:
        with sqlite3.connect(path) as raw:
            columns = [row[1] for row in raw.execute("PRAGMA table_info(fetch_cache)")]
            assert "created_at" not in columns, columns
            user_version = raw.execute("PRAGMA user_version").fetchone()[0]
            assert user_version >= 1

        # Writing a fresh result must now succeed — no IntegrityError.
        await cache.set(
            "fresh-key",
            FetchResult(
                url="https://example.org/",
                success=True,
                markdown="hi",
                fetched_at=datetime.now(UTC),
            ),
            60,
        )

        cached = await cache.get("fresh-key")
        assert cached is not None
        assert cached.from_cache is True
        assert cached.markdown == "hi"
    finally:
        await cache.close()


# ---------------------------------------------------------------------------
# Transport retries: backoff must not occupy the shared request slot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_retry_backoff_releases_shared_semaphore(monkeypatch):
    backoff_started = asyncio.Event()
    release_backoff = asyncio.Event()
    healthy_request_started = asyncio.Event()
    retry_calls = 0

    async def backoff(*_args):
        backoff_started.set()
        await release_backoff.wait()

    def handler(request):
        nonlocal retry_calls
        if request.url.path == "/retry":
            retry_calls += 1
            if retry_calls == 1:
                return httpx.Response(503, request=request)
        healthy_request_started.set()
        return httpx.Response(200, request=request)

    monkeypatch.setattr(HTTPFetcher, "_backoff", staticmethod(backoff))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = HTTPFetcher(client, asyncio.Semaphore(1), retries=1, max_content_size=1024)
    try:
        retrying = asyncio.create_task(fetcher.fetch("https://example.com/retry"))
        await asyncio.wait_for(backoff_started.wait(), timeout=0.5)
        healthy = asyncio.create_task(fetcher.fetch("https://example.com/healthy"))
        await asyncio.wait_for(healthy_request_started.wait(), timeout=0.5)
        release_backoff.set()
        await asyncio.gather(retrying, healthy)
    finally:
        release_backoff.set()
        await client.aclose()


async def test_http_redirect_to_private_address_is_blocked_pre_flight():
    """Phase 1 Item 2: ``HTTPFetcher`` must validate every ``Location`` header
    before opening a new connection.  The pre-Phase-1 implementation let
    httpx follow redirects, which exposed a race window in which the SSRF
    guard ran only after the connection to a private/metadata target was
    already established.
    """
    from pagefetch.utils import urls as urls_module

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/leak":
            return httpx.Response(
                302, headers={"Location": "http://127.0.0.1:8200/secret"}, request=request
            )
        return httpx.Response(200, text="ok", request=request)

    # Inject a deterministic DNS resolver that maps ``attacker.example`` to a
    # public address so the initial URL validates; the SSRF guard must still
    # reject the redirect target ``127.0.0.1`` before any second hop.
    original_resolve = urls_module.resolve_host_ips

    def fake_resolve(host: str) -> list[str]:
        if host == "attacker.example":
            return ["203.0.113.10"]
        return original_resolve(host)

    urls_module.resolve_host_ips = fake_resolve
    try:
        transport = httpx.MockTransport(handler)
        # ``follow_redirects=False`` mirrors the production client setup.
        http_client = httpx.AsyncClient(transport=transport, follow_redirects=False)
        fetcher = HTTPFetcher(
            http_client,
            asyncio.Semaphore(1),
            retries=0,
            max_content_size=1024,
            max_redirects=5,
        )
        try:
            with pytest.raises(TransportFailure) as exc_info:
                await fetcher.fetch("https://attacker.example/leak")
            assert exc_info.value.error.code == "ssrf_blocked"
            assert exc_info.value.error.retryable is False
        finally:
            await http_client.aclose()
    finally:
        urls_module.resolve_host_ips = original_resolve


# ---------------------------------------------------------------------------
# Mode contracts: HTTP-only clients must never escalate to a real browser
# ---------------------------------------------------------------------------


def attach_transport(client: PageFetch, handler) -> None:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        headers={"User-Agent": "test"},
    )
    client._http_clients["none"] = http_client
    client._http_fetchers["none"] = HTTPFetcher(
        http_client,
        client._http_semaphore,
        retries=0,
        max_content_size=client.config.max_content_size,
    )


@pytest.mark.asyncio
async def test_http_mode_never_falls_back_on_low_confidence(tmp_path):
    client = PageFetch(mode="http", cache_enabled=False)
    attach_transport(
        client,
        lambda request: httpx.Response(
            200,
            text="<html><body><div id='root'></div></body></html>",
            headers={"Content-Type": "text/html"},
            request=request,
        ),
    )

    async def forbidden(*_a, **_k):
        raise AssertionError("browser must not be used in http mode")

    client._fetch_browser = forbidden
    async with client:
        result = await client.fetch("https://example.com")
    assert result.success
    assert result.fetch_method == "http"
    assert any("browser fallback is disabled" in warning for warning in result.warnings)


def _http_404(req): return httpx.Response(404, text="missing", request=req)
def _http_503(req): return httpx.Response(503, text="unavailable", request=req)
def _read_timeout(req): raise httpx.ReadTimeout("slow upstream", request=req)
def _connect_error(req): raise httpx.ConnectError("server disconnected", request=req)


@pytest.mark.parametrize(
    ("handler", "predicate"),
    [
        (_http_404, lambda r: r.status_code == 404 and r.error.code == "http_error"),
        (_http_503, lambda r: r.status_code == 503 and r.error.retryable is True),
        (_read_timeout, lambda r: r.error.code == "http_timeout"),
        (_connect_error, lambda r: r.error.code == "connection_error"),
    ],
    ids=["404", "503", "timeout", "connection"],
)
@pytest.mark.asyncio
async def test_auto_does_not_fall_back_for_http_failure(handler, predicate):
    client = PageFetch(mode="auto", cache_enabled=False, retries_http=0)
    attach_transport(client, handler)

    async def forbidden(*_a, **_k):
        raise AssertionError("browser must not be used for this HTTP failure")

    client._fetch_browser = forbidden
    async with client:
        result = await client.fetch("https://example.com/x")
    assert not result.success
    assert result.error is not None and predicate(result)


async def test_proxy_http_clients_are_pooled_per_proxy_url():
    """Phase 1 Item 3: the per-proxy ``httpx.AsyncClient`` must be reused
    across requests so TCP/TLS keep-alive and HTTP/2 multiplexing survive.
    The previous implementation constructed and tore down a fresh client on
    every proxied request, wasting handshakes and exhausting ``TIME_WAIT``
    sockets for large batches.
    """
    client = PageFetch(mode="http", proxy="custom", cache_enabled=False)
    try:
        url_a = "http://user:pass@proxy-a.example:1080"
        url_b = "http://user:pass@proxy-b.example:1080"
        first = await client._proxy_client_provider(url_a)
        second = await client._proxy_client_provider(url_a)
        third = await client._proxy_client_provider(url_b)
        # Same proxy URL → identical pooled client.
        assert first is second
        # Different proxy URL → distinct pooled client.
        assert first is not third
        # Both clients must be registered for ``close()`` to tear them down.
        assert len(client._proxy_http_clients) == 2
    finally:
        await client.close()
    # ``close()`` must drain the proxy pool without leaking clients.
    assert client._proxy_http_clients == {}


# ---------------------------------------------------------------------------
# Constructor: reject invalid configuration (safety limit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"mode": "magic"}, "mode"),
        ({"proxy": "auto"}, "proxy"),
        ({"http_concurrency": 0}, "http_concurrency"),
        ({"confidence_threshold": 2}, "confidence_threshold"),
    ],
)
def test_constructor_rejects_invalid_arguments(kwargs, match):
    with pytest.raises(ValueError, match=match):
        PageFetch(**kwargs)


def test_config_rejects_invalid_direct_values_and_unknown_yaml_keys(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        PageFetchConfig(mode="magic")

    config_path = tmp_path / "pagefetch.yaml"
    config_path.write_text("cache_enabld: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cache_enabld"):
        PageFetchConfig.from_yaml(config_path)

    # PageFetch supports direct PageFetchConfig instances and from_config
    valid_cfg = PageFetchConfig(mode="http")
    assert PageFetch(config=valid_cfg).config == valid_cfg
    assert PageFetch.from_config(valid_cfg).config == valid_cfg


def test_cli_override_preserves_yaml_safety_limits(tmp_path):
    config_path = tmp_path / "pagefetch.yaml"
    config_path.write_text(
        "screenshot_max_bytes: 12345\nbrowser_pre_check_byte_margin: 2.5\n",
        encoding="utf-8",
    )

    args = build_parser().parse_args(
        ["https://example.com", "--config", str(config_path), "--mode", "http"]
    )
    config = _build_config(args)
    client = PageFetch(
        cache_enabled=False,
        screenshot_max_bytes=config.screenshot_max_bytes,
        browser_pre_check_byte_margin=config.browser_pre_check_byte_margin,
    )

    assert config.screenshot_max_bytes == 12345
    assert config.browser_pre_check_byte_margin == 2.5
    assert client.config.screenshot_max_bytes == 12345
    assert client.config.browser_pre_check_byte_margin == 2.5


def test_compact_structure_rejects_lossy_deserialization_and_sparse_nodes_restore():
    structure = PageStructure(
        root=StructureNode(
            tag="main",
            selector="main",
            attrs={},
            text="",
            children=[],
            path="main",
        ),
        stylesheets=[],
        inline_styles=[],
        scripts=[],
        inline_scripts=[],
        truncated=False,
        node_count=1,
        max_depth=1,
    )
    result = FetchResult(url="https://example.com", structure=structure)

    compact = result.to_dict(include_structure=True, compact_structure=True)
    with pytest.raises(ValueError, match="inspection-only"):
        FetchResult.from_dict(compact)

    verbose = result.to_dict(include_structure=True)
    del verbose["structure"]["root"]["selector"]
    restored = FetchResult.from_dict(verbose)
    assert restored.structure is not None
    assert restored.structure.root is not None
    assert restored.structure.root.selector == "main"


# ---------------------------------------------------------------------------
# extract(): invalid URLs must never reach the browser (SSRF boundary)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_rejects_invalid_url_without_touching_browser():
    client = PageFetch(cache_enabled=False)
    try:
        result = await client.extract("not a url")
        assert result.success is False
        assert result.error is not None
        assert result.error.code in {"invalid_url", "unsupported_scheme"}
        assert client._active_fetches == 0
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# raise_on_error: in-flight counter must be released (no resource leak)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        lambda req: httpx.Response(404, request=req),
        lambda req: (_ for _ in ()).throw(httpx.ConnectError("network down", request=req)),
    ],
    ids=["http_error", "transport_failure"],
)
@pytest.mark.asyncio
async def test_fetch_with_raise_on_error_does_not_leak_counter(handler):
    client = PageFetch(
        mode="http", cache_enabled=False, raise_on_error=True, retries_http=0
    )
    attach_transport(client, handler)
    async with client:
        with pytest.raises(PageFetchError):
            await client.fetch("https://example.com/x")
    assert client._active_fetches == 0


# ---------------------------------------------------------------------------
# Teardown safety: close() must not hang forever after a hung child
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_close_does_not_hang_when_first_teardown_fails(tmp_path):
    """Regression guard: when one browser teardown hangs and the first
    ``close()`` caller is cancelled, a subsequent ``close()`` must still
    drain the rest of the pool and return. The per-resource close timeout
    (``_RESOURCE_CLOSE_TIMEOUT = 0.5`` s) bounds teardown delays so hung
    resources do not block completion.
    """
    client = PageFetch(cache_enabled=False, cache_path=tmp_path / "cache.sqlite3")

    blocker = asyncio.Event()
    hang_fetcher = type("HangingBrowser", (), {})()

    async def hang_close():
        await blocker.wait()  # never released: simulates a hung browser teardown

    hang_fetcher.close = hang_close
    client._browser_fetchers["stuck"] = hang_fetcher

    async def run_first_close():
        try:
            await asyncio.wait_for(client.close(), timeout=0.5)
        except (TimeoutError, asyncio.CancelledError):
            pass

    first_task = asyncio.create_task(run_first_close())
    await asyncio.sleep(0.1)
    first_task.cancel()
    try:
        await first_task
    except (TimeoutError, asyncio.CancelledError):
        pass

    try:
        await asyncio.wait_for(client.close(), timeout=1.0)
    finally:
        blocker.set()
        await client.aclose() if hasattr(client, "aclose") else None


@pytest.mark.asyncio
async def test_close_finishes_after_the_initial_caller_is_cancelled(tmp_path):
    client = PageFetch(cache_enabled=False, cache_path=tmp_path / "cache.sqlite3")
    blocker = asyncio.Event()
    fetcher = type("BlockingBrowser", (), {})()

    async def close_when_released():
        await blocker.wait()

    fetcher.close = close_when_released
    client._browser_fetchers["blocking"] = fetcher

    first_close = asyncio.create_task(client.close())
    await asyncio.sleep(0.05)
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_close

    blocker.set()
    await asyncio.wait_for(client.close(), timeout=1.0)
    assert client._closed is True
    assert client._closing is False
    assert not client._browser_fetchers


# ---------------------------------------------------------------------------
# Detector & Browser: false-positive retries and Linux Wayland isolation
# ---------------------------------------------------------------------------


def test_detector_does_not_leak_noscript_or_false_flag_recaptcha():
    from pagefetch.processing.detector import analyze_html

    # Case 1: Page with noscript tag containing nested tags must not trigger explicit_js
    html_noscript = (
        "<html><head><title>Articles Hub</title></head><body>"
        "<noscript><div>You must enable JavaScript to use this site.</div></noscript>"
        "<main><h1>Breaking News</h1>"
        "<p>This is a complete and substantive article with lots of detailed information.</p>"
        "<p>Economic data indicates strong growth across multiple key sectors this quarter.</p>"
        "</main></body></html>"
    )
    report_ns = analyze_html(html_noscript)
    assert "document asks for JavaScript" not in report_ns.reasons
    assert report_ns.score >= 0.40

    # Case 2: Content-rich page with a recaptcha or turnstile widget in footer/form must not be flagged as challenge
    html_captcha = (
        "<html><head><title>Company Portal</title></head><body>"
        "<main><h1>Quarterly Earnings Report</h1>"
        "<p>Revenue increased by 15% year-over-year driven by cloud service demand.</p>"
        "<p>Operating margins improved substantially while overhead expenses remained flat.</p>"
        "<div class='g-recaptcha'></div>"
        "<div class='cf-turnstile'></div>"
        "</main></body></html>"
    )
    report_captcha = analyze_html(html_captcha)
    assert report_captcha.challenge is False
    assert report_captcha.score >= 0.40

    # Case 3: Genuine challenge page (minimal text, only challenge) must still be detected
    html_real_challenge = (
        "<html><head><title>Just a moment...</title></head><body>"
        "<h1>Checking your browser</h1>"
        "<p>Please verify you are human to continue.</p>"
        "<div class='cf-turnstile'></div>"
        "</body></html>"
    )
    report_real = analyze_html(html_real_challenge)
    assert report_real.challenge is True
    assert report_real.score <= 0.10

    # Case 4: Duplicate tag structure across visible body and noscript must not drop visible text
    html_dup = (
        "<html><body><div><p>Please log in</p><p>Important update for all members with comprehensive details and verified facts.</p></div>"
        "<noscript><p>Please log in</p></noscript></body></html>"
    )
    report_dup = analyze_html(html_dup)
    assert report_dup.challenge is False
    assert report_dup.score >= 0.30


def test_xvfb_display_initial_state():
    from pagefetch.fetching.virtual_display import XvfbDisplay

    display = XvfbDisplay()
    assert display.is_running is False
    assert display.width == 1920


def test_cmp_cleaning_and_inside_content_isolation():
    from pagefetch.processing.cleaner import clean_html

    # CMP tags and !important hidden styles must be removed under standard cleaning
    html_cmp = (
        "<html><body>"
        "<div id='onetrust-consent-sdk'><p>We use cookies</p></div>"
        "<div class='qc-cmp2-container'><p>Consent</p></div>"
        "<div style='display: none !important;'><p>Hidden tracker</p></div>"
        "<main><h1>Actual Article</h1><p>Real content of the article.</p></main>"
        "</body></html>"
    )
    cleaned = clean_html(html_cmp, cleaning_level="standard")
    text = cleaned.get_text()
    assert "We use cookies" not in text
    assert "Hidden tracker" not in text
    assert "Real content of the article." in text

    # <div id="content"> wrapper must not prevent removal of site chrome under maximum cleaning
    html_chrome = (
        "<html><body>"
        "<div id='content'>"
        "<header class='site-header'><h1>Site Brand</h1><nav><a href='/'>Home</a></nav></header>"
        "<div class='entry-content'><p>The genuine story paragraph.</p></div>"
        "<footer class='site-footer'><p>Copyright 2026</p></footer>"
        "</div></body></html>"
    )
    cleaned_max = clean_html(html_chrome, cleaning_level="maximum")
    max_text = cleaned_max.get_text()
    assert "Site Brand" not in max_text
    assert "Copyright 2026" not in max_text
    assert "The genuine story paragraph." in max_text


def test_markdown_bracket_escaping():
    from bs4 import BeautifulSoup

    from pagefetch.processing.markdown import html_to_markdown

    soup = BeautifulSoup("<a href='https://example.com/doc.pdf'>[PDF] Annual Report [2026]</a>", "lxml")
    md = html_to_markdown(soup, "https://example.com/")
    assert md == r"[\[PDF\] Annual Report \[2026\]](https://example.com/doc.pdf)"


def test_srcset_comma_url_candidate():
    from bs4 import BeautifulSoup

    from pagefetch.processing.images import image_candidate

    soup = BeautifulSoup(
        "<img srcset='https://res.cloudinary.com/demo/image/upload/w_300,h_200/sample.jpg 300w, next.jpg 600w'>",
        "lxml",
    )
    assert (
        image_candidate(soup.find("img"))
        == "https://res.cloudinary.com/demo/image/upload/w_300,h_200/sample.jpg"
    )


# ---------------------------------------------------------------------------
# Phase 2 regression assertions (Süreç & Kaynak İzolasyonu)
# ---------------------------------------------------------------------------


def test_browser_start_does_not_mutate_caller_environ(monkeypatch):
    """Phase 2 Item 5: ``BrowserFetcher.start`` must pass the Xvfb ``DISPLAY``
    to the Camoufox subprocess via ``options["env"]`` rather than mutating
    the caller's ``os.environ``. Mutating process-global state across async
    tasks / threads broke unrelated components; the per-subprocess dict must
    now be the sole transport of ``DISPLAY`` to Firefox.
    """
    import os
    import sys
    import types

    monkey_display = ":42"
    monkeypatch.setenv("DISPLAY", monkey_display)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("X_PRIVILEGED_WAYLAND_SOCKET", raising=False)
    monkeypatch.delenv("GDK_BACKEND", raising=False)
    monkeypatch.delenv("MOZ_ENABLE_WAYLAND", raising=False)

    # Provide a stand-in ``camoufox.async_api`` module so ``start()`` does
    # not need the real browser runtime to exercise the env wiring.
    captured: dict[str, object] = {}

    class _FakeManager:
        async def __aenter__(self) -> "_FakeManager":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    def _fake_async_camoufox(**options: object) -> _FakeManager:
        captured["options"] = options
        return _FakeManager()

    fake_async_api = types.ModuleType("camoufox.async_api")
    fake_async_api.AsyncCamoufox = _fake_async_camoufox  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.async_api", fake_async_api)

    from pagefetch.fetching import browser as browser_module
    from pagefetch.proxy.providers import ProxySettings
    from pagefetch import bootstrap as bootstrap_module

    # Bypass the runtime bootstrap check — we are not actually launching the
    # browser, just exercising the env-wiring path.
    async def _noop_bootstrap() -> None:
        return None

    monkeypatch.setattr(bootstrap_module, "bootstrap_browser", _noop_bootstrap)

    # Build a minimal BrowserFetcher with a running Xvfb surrogate so
    # ``start()`` skips the Xvfb-start branch entirely.
    fetcher = browser_module.BrowserFetcher(
        asyncio.Semaphore(1),
        timeout=10.0,
        retries=0,
        proxy=ProxySettings(provider="none", url=None),
        max_content_size=1024,
    )
    fetcher._xvfb = browser_module.XvfbDisplay.__new__(browser_module.XvfbDisplay)
    fetcher._xvfb._display = ":99"  # type: ignore[attr-defined]
    # ``is_running`` is a property backed by ``_process.poll() is None``;
    # substitute a sentinel whose ``poll`` never reports a code.
    class _AliveSentinel:
        def poll(self) -> None:
            return None

    fetcher._xvfb._process = _AliveSentinel()  # type: ignore[attr-defined]

    asyncio.run(fetcher.start())

    # Caller's environment must be untouched.
    assert os.environ.get("DISPLAY") == monkey_display, (
        "BrowserFetcher.start must not mutate os.environ['DISPLAY']; "
        f"expected {monkey_display!r}, got {os.environ.get('DISPLAY')!r}"
    )
    assert "WAYLAND_DISPLAY" not in os.environ
    assert "X_PRIVILEGED_WAYLAND_SOCKET" not in os.environ
    # The subprocess env must carry the Xvfb display value.
    opts = captured["options"]
    assert isinstance(opts, dict)
    assert opts["env"]["DISPLAY"] == ":99"
    assert opts["env"]["GDK_BACKEND"] == "x11"
    assert opts["env"]["MOZ_ENABLE_WAYLAND"] == "0"
    assert "WAYLAND_DISPLAY" not in opts["env"]


def test_xvfb_popen_uses_pdeathsig_pre_exec_on_linux(monkeypatch):
    """Phase 2 Item 6: On Linux, ``XvfbDisplay.start`` must install a
    ``preexec_fn`` that calls ``prctl(PR_SET_PDEATHSIG, SIGTERM)`` so the
    kernel cleans up Xvfb if the parent Python process dies unexpectedly.
    Outside Linux the gate must be skipped so non-Linux CI / dev boxes still
    work.
    """
    import sys

    from pagefetch.fetching import virtual_display as vd

    monkeypatch.setattr(vd, "_set_pdeathsig", lambda: None, raising=True)

    captured: dict[str, object] = {}

    class _FakeProcess:
        pid = 4242

        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["kwargs"] = kwargs
            captured["args"] = args
            self._stdout = type("_Stdout", (), {"fileno": lambda self: 1})()

        @property
        def stdout(self) -> object:
            return self._stdout

        def poll(self) -> None:
            return None

        def send_signal(self, sig: int) -> None:  # pragma: no cover
            pass

        def wait(self, timeout: float | None = None) -> int:  # pragma: no cover
            return 0

        def kill(self) -> None:  # pragma: no cover
            pass

    monkeypatch.setattr(vd.subprocess, "Popen", _FakeProcess)
    monkeypatch.setattr(vd.shutil, "which", lambda _: "/usr/bin/Xvfb")
    monkeypatch.setattr(vd.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(vd.os, "set_blocking", lambda *a, **k: None)
    monkeypatch.setattr(vd.os, "read", lambda *_a, **_k: b"")
    monkeypatch.setattr(vd.time, "sleep", lambda *_a, **_k: (_ for _ in ()).throw(vd.XvfbLaunchError("timeout")))
    # Path.exists on the X11 socket must succeed so the launch loop returns early.
    monkeypatch.setattr(vd, "Path", type("_P", (), {"exists": staticmethod(lambda self: True)}))

    display = vd.XvfbDisplay()
    try:
        if sys.platform.startswith("linux"):
            try:
                display.start()
            except vd.XvfbLaunchError:
                pass
            kwargs = captured["kwargs"]
            assert "preexec_fn" in kwargs, (
                "Xvfb.start must register preexec_fn on Linux so the kernel "
                "delivers SIGTERM when the parent Python process dies."
            )
            assert kwargs["preexec_fn"] is vd._set_pdeathsig
        # On non-Linux, preexec_fn must be absent so the gate is skipped.
        if not sys.platform.startswith("linux"):
            kwargs = captured["kwargs"]
            assert "preexec_fn" not in kwargs
    finally:
        try:
            display.stop()
        except Exception:
            pass


def test_fetch_result_clone_isolates_mutable_containers():
    """Phase 2 Item 7: ``FetchResult.clone`` must yield an independent copy
    whose mutable containers (``warnings``, ``links``, ``images``,
    ``metadata``) can be mutated without aliasing the original, while
    keeping immutable payloads (HTML, screenshot bytes, structure) shared
    by reference. The previous ``deepcopy`` walked the entire DOM tree and
    blocked the event loop; ``clone`` is a ``dataclasses.replace`` over the
    four mutable fields only.
    """
    result = FetchResult(
        url="https://example.com/",
        success=True,
        html="<html>large payload</html>",
        markdown="# large payload",
        text="large payload",
        warnings=["w0"],
        links=[],
        images=[],
        metadata={"k": "v"},
    )

    twin = result.clone()
    # Independent mutable containers, identical contents.
    assert twin is not result
    assert twin.warnings is not result.warnings and twin.warnings == ["w0"]
    assert twin.links is not result.links and twin.links == []
    assert twin.images is not result.images and twin.images == []
    assert twin.metadata is not result.metadata and twin.metadata == {"k": "v"}
    # Immutable payloads are shared by reference.
    assert twin.html is result.html
    assert twin.markdown is result.markdown
    assert twin.text is result.text

    # Mutating the clone must not bleed back into the original.
    twin.warnings.append("w1")
    twin.links.append("l1")
    twin.images.append("i1")
    twin.metadata["k2"] = "v2"
    assert result.warnings == ["w0"]
    assert result.links == []
    assert result.images == []
    assert result.metadata == {"k": "v"}


# ---------------------------------------------------------------------------
# Phase 3 regression assertions (Algoritmik Sadeleştirme)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_pool_is_a_plain_dict_and_reuses_fetcher_per_provider():
    """Phase 3 Item 7: the browser pool must be a plain ``dict[str,
    BrowserFetcher]`` (max ~4 keys — one per provider) backed by a single
    ``_browser_init_lock``. The previous implementation carried a 32-entry
    LRU ring plus an ``asyncio.Condition`` whose waiters were never woken
    during ``close()`` (deadlock risk) and whose eviction logic added
    hundreds of lines for a pool that could never exceed four entries.
    """
    client = PageFetch(cache_enabled=False, browser_concurrency=1)
    try:
        assert isinstance(client._browser_fetchers, dict)
        # No OrderedDict / Condition / user-counter / numeric pool limit.
        assert not hasattr(client, "_browser_pool_condition")
        assert not hasattr(client, "_browser_fetcher_users")
        assert not hasattr(client, "_browser_pool_limit")
        # The remaining lock is a plain ``asyncio.Lock`` so two coroutines
        # racing for the same provider key cannot double-spawn a browser.
        assert isinstance(client._browser_init_lock, asyncio.Lock)

        # Two acquires for the same provider must hand back identical
        # fetcher objects — provider is the cache key, so concurrency
        # capping belongs on ``_browser_semaphore``, not on per-key
        # bookkeeping.
        first_cache_key, _, first_fetcher = await client._acquire_browser_fetcher(
            "none", "https://example.com/a"
        )
        second_cache_key, _, second_fetcher = await client._acquire_browser_fetcher(
            "none", "https://example.com/b"
        )
        await client._release_browser_fetcher(first_cache_key)
        await client._release_browser_fetcher(second_cache_key)
        assert first_cache_key == "none" == second_cache_key
        assert first_fetcher is second_fetcher
    finally:
        await client.close()


def test_wait_for_stability_uses_node_count_not_outerhtml():
    """Phase 3 Item 8: the readiness poll must NOT serialise the entire
    DOM into a string every 80–150 ms. Inspecting the actual JavaScript
    expression passed to ``page.evaluate`` guarantees we paid that cost
    only when reviewing the source — the old ``outerHTML.length``
    evaluation allocated megabytes of string per poll on large pages and
    was the dominant CPU cost in the readiness wait.
    """
    import inspect
    import re

    from pagefetch.fetching import readiness

    # The two probe functions are full Python coroutines; the metric we
    # really care about lives inside the ``page.evaluate(...)`` literal.
    # Grab the JS literal so the surrounding docstring (which mentions the
    # obsolete metric for context) is excluded from the check.
    metrics_match = re.search(
        r"metrics\s*=\s*await\s+page\.evaluate\(\s*((?:\"\"\"(?:.|\n)*?\"\"\"|'''(?:.|\n)*?'''))",
        inspect.getsource(readiness.wait_for_stability),
        flags=re.MULTILINE,
    )
    assert metrics_match is not None, "wait_for_stability must call page.evaluate to read DOM metrics"
    js_literal = metrics_match.group(1)
    assert "getElementsByTagName('*').length" in js_literal
    assert "outerHTML" not in js_literal


def test_maximum_cleaning_preserves_links_and_images_for_page_graph():
    """Phase 3 Item 9: links and images must be extracted from the
    ORIGINAL soup, not the cleaned one. ``cleaning_level="maximum"``
    removes ``<nav>``, ``<header>``, ``<footer>`` and ``<aside>`` — any
    navigation, footer, or sidebar edge the page carries would silently
    disappear from ``FetchResult.links`` / ``FetchResult.images`` if we
    extracted from the cleaned tree. Markdown continues to drop the chrome
    so the summary stays reader-friendly; structural data mirrors reality.
    """
    from pagefetch.processing.html import process_html

    html = (
        "<html><body>"
        "<nav><a href='/about'>About</a><a href='/contact'>Contact</a></nav>"
        "<aside><a href='/promo'>Promo</a></aside>"
        "<footer>"
        "<a href='/privacy'>Privacy</a>"
        "<img src='/logo.png' alt='Logo'>"
        "</footer>"
        "<main><h1>Real Article</h1><img src='/hero.jpg' alt='Hero'></main>"
        "</body></html>"
    )
    processed = process_html(
        html,
        "https://example.com/",
        cleaning_level="maximum",
    )

    # Markdown faithfully drops the chrome (this is the contract).
    assert "/about" not in processed.markdown
    assert "/privacy" not in processed.markdown
    # But the link & image graphs still expose every edge from the page,
    # regardless of the cleanup level — duplication-free relative URLs.
    link_hrefs = sorted({link.url for link in processed.links})
    image_srcs = sorted({image.url for image in processed.images})
    assert link_hrefs == sorted(
        {
            "https://example.com/about",
            "https://example.com/contact",
            "https://example.com/promo",
            "https://example.com/privacy",
        }
    )
    assert image_srcs == sorted(
        {"https://example.com/logo.png", "https://example.com/hero.jpg"}
    )


def test_stealth_preset_and_cli_override_honors_block_images_default():
    """Stealth levels balanced and max must default block_images to False unless overridden."""
    client_preset = PageFetch(stealth_level="balanced")
    assert client_preset.config.block_images is False
    client_explicit = PageFetch(stealth_level="balanced", block_images=True)
    assert client_explicit.config.block_images is True

    args_preset = build_parser().parse_args(["https://example.com", "--stealth-level", "balanced"])
    assert _build_config(args_preset).block_images is False
    args_explicit = build_parser().parse_args(
        ["https://example.com", "--stealth-level", "balanced", "--block-images"]
    )
    assert _build_config(args_explicit).block_images is True


def test_csv_extraction_to_markdown_table():
    """CSV extraction must convert rows to GFM tables, escape pipes, and handle delimiters."""
    from pagefetch.processing.non_html import process_csv

    # Comma-separated with pipes and newlines inside quotes
    sample = (
        'Product,Price,Details\n'
        'Widget,"$10.00","High quality | durable"\n'
        'Gadget,"$20.00","Multi-line\r\ndescription"\n'
    ).encode("utf-8")

    doc = process_csv(sample)
    assert doc.metadata["row_count"] == 3
    assert doc.metadata["column_count"] == 3
    assert doc.metadata["delimiter"] == ","
    assert "| Product | Price | Details |" in doc.markdown
    assert "| --- | --- | --- |" in doc.markdown
    assert r"High quality \| durable" in doc.markdown
    assert "Multi-line<br>description" in doc.markdown

    # TSV data
    tsv_sample = b"ColA\tColB\nVal1\tVal2\n"
    tsv_doc = process_csv(tsv_sample, delimiter="\t")
    assert tsv_doc.metadata["delimiter"] == "\t"
    assert "| ColA | ColB |" in tsv_doc.markdown

    # Truncation limit check
    many_rows = "h1,h2\n" + "\n".join(f"val{i},val{i}" for i in range(10))
    trunc_doc = process_csv(many_rows.encode("utf-8"), max_table_rows=3)
    assert "*Showing first 3 of 10 data rows" in trunc_doc.markdown
    assert any("truncated to 3 rows" in w for w in trunc_doc.warnings)


def test_docx_extraction_to_markdown():
    """DOCX extraction must parse headings, styling, links, lists, tables, and metadata."""
    import io
    import zipfile
    from pagefetch.processing.non_html import process_docx

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Project Alpha</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>Check out </w:t></w:r>
      <w:hyperlink r:id="rId1">
        <w:r><w:t>PageFetch</w:t></w:r>
      </w:hyperlink>
      <w:r><w:t> with </w:t></w:r>
      <w:r><w:rPr><w:b/></w:rPr><w:t>bold</w:t></w:r>
      <w:r><w:t> and </w:t></w:r>
      <w:r><w:rPr><w:i/></w:rPr><w:t>italic</w:t></w:r>
      <w:r><w:t> and </w:t></w:r>
      <w:r><w:rPr><w:strike/></w:rPr><w:t>deleted</w:t></w:r>
      <w:r><w:t>.</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>
      <w:r><w:t>Bullet item</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Score</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Alice</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>100</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>""",
        )
        zf.writestr(
            "word/_rels/document.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
               Target="https://example.com/pagefetch" TargetMode="External"/>
</Relationships>""",
        )
        zf.writestr(
            "docProps/core.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
                   xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>Project Alpha Document</dc:title>
  <dc:creator>Test Contributor</dc:creator>
</cp:coreProperties>""",
        )

    doc = process_docx(buf.getvalue())
    assert doc.title == "Project Alpha Document"
    assert doc.metadata["creator"] == "Test Contributor"
    assert "# Project Alpha" in doc.markdown
    assert "[PageFetch](https://example.com/pagefetch)" in doc.markdown
    assert "**bold**" in doc.markdown
    assert "*italic*" in doc.markdown
    assert "~~deleted~~" in doc.markdown
    assert "- Bullet item" in doc.markdown
    assert "| Name | Score |" in doc.markdown
    assert "| --- | --- |" in doc.markdown
    assert "| Alice | 100 |" in doc.markdown


def test_json_extraction_to_markdown():
    """JSON extraction must format valid JSON into code fences and extract structural metadata."""
    from pagefetch.processing.non_html import process_json

    payload = b'{"name": "test-item", "count": 42, "items": ["a", "b"]}'
    doc = process_json(payload)
    assert doc.title == "test-item"
    assert doc.metadata["type"] == "object"
    assert doc.metadata["key_count"] == 3
    assert "```json" in doc.markdown
    assert '"name": "test-item"' in doc.markdown


def test_detect_document_kind_and_routing():
    """Kind detection must accurately detect PDF, DOCX, CSV, TSV, JSON, XML, HTML, and text."""
    from pagefetch.processing.pipeline import detect_document_kind

    # PDF detection
    assert detect_document_kind("application/pdf", b"") == "pdf"
    assert detect_document_kind("application/octet-stream", b"%PDF-1.4...") == "pdf"
    assert detect_document_kind("application/octet-stream", b"", url="https://site.org/doc.pdf") == "pdf"

    # DOCX detection
    docx_ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert detect_document_kind(docx_ct, b"") == "docx"
    assert detect_document_kind("application/octet-stream", b"PK\x03\x04...word/document.xml") == "docx"
    assert detect_document_kind("application/octet-stream", b"", url="https://site.org/file.docx") == "docx"

    # CSV / TSV detection
    assert detect_document_kind("text/csv", b"") == "csv"
    assert detect_document_kind("application/csv", b"") == "csv"
    assert detect_document_kind("text/plain", b"", url="https://site.org/data.csv") == "csv"
    assert detect_document_kind("text/tab-separated-values", b"") == "tsv"
    assert detect_document_kind("text/plain", b"", url="https://site.org/data.tsv") == "tsv"

    # JSON detection
    assert detect_document_kind("application/json", b"") == "json"
    assert detect_document_kind("application/problem+json", b"") == "json"
    assert detect_document_kind("text/plain", b"", url="https://site.org/api.json") == "json"

    # XML and HTML
    assert detect_document_kind("application/xml", b"") == "xml"
    assert detect_document_kind("text/html", b"") == "html"
    assert detect_document_kind("text/plain", b"") == "text"


def test_unified_extract_document_convenience():
    """extract_document must seamlessly route bytes based on hints."""
    from pagefetch.processing.non_html import extract_document

    csv_bytes = b"k,v\n1,2"
    doc_csv = extract_document(csv_bytes, url="test.csv")
    assert "| k | v |" in doc_csv.markdown

    json_bytes = b'{"status": "ok"}'
    doc_json = extract_document(json_bytes, content_type="application/json")
    assert "```json" in doc_json.markdown


def test_pdf_extraction_normalization_and_dependency_error():
    """process_pdf must raise MissingOptionalDependency without pypdf, and normalize text with pypdf."""
    import sys
    from unittest.mock import MagicMock
    from pagefetch.processing.non_html import MissingOptionalDependency, process_pdf

    # Test missing dependency when pypdf is not in sys.modules
    old_mod = sys.modules.get("pypdf")
    try:
        sys.modules["pypdf"] = None  # force ImportError
        try:
            process_pdf(b"%PDF-1.4")
            assert False, "Expected MissingOptionalDependency"
        except MissingOptionalDependency as exc:
            assert "pagefetch[pdf]" in str(exc)
    finally:
        if old_mod is not None:
            sys.modules["pypdf"] = old_mod
        else:
            sys.modules.pop("pypdf", None)

    # Test extraction with mock pypdf
    mock_pypdf = MagicMock()
    mock_reader = MagicMock()
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "Quarterly Re-\nport 2026\nClean body line."
    mock_reader.pages = [mock_page]
    mock_reader.metadata = {"/Title": "Quarterly Report", "/Author": "PageFetch"}
    mock_reader.is_encrypted = False
    mock_pypdf.PdfReader.return_value = mock_reader

    try:
        sys.modules["pypdf"] = mock_pypdf
        doc = process_pdf(b"%PDF-1.4...")
        assert doc.title == "Quarterly Report"
        assert doc.metadata["page_count"] == 1
        assert "Quarterly Report 2026" in doc.markdown
    finally:
        if old_mod is not None:
            sys.modules["pypdf"] = old_mod
        else:
            sys.modules.pop("pypdf", None)


def test_camoufox_binary_detection_and_disconnected_recovery():
    """Verify Camoufox binary check uses launch_path and BrowserFetcher recovers from disconnect."""
    import asyncio
    from unittest.mock import MagicMock
    from pagefetch.bootstrap import _has_camoufox_binary
    from pagefetch.fetching.browser import BrowserFetcher
    from pagefetch.proxy.providers import ProxySettings

    # 1. Detection via launch_path
    assert _has_camoufox_binary() is True

    # 2. BrowserFetcher restart on disconnected browser
    fetcher = BrowserFetcher(
        asyncio.Semaphore(1),
        timeout=5.0,
        retries=0,
        proxy=ProxySettings(provider="none", url=None),
        max_content_size=1024,
    )
    dead_browser = MagicMock()
    dead_browser.is_connected.return_value = False
    fetcher._browser = dead_browser

    fake_manager = MagicMock()
    fake_manager.__aexit__ = MagicMock(return_value=asyncio.sleep(0))
    fetcher._manager = fake_manager

    # When start() runs, it must detect disconnected state and reset stale instance
    async def run_test():
        async with fetcher._start_lock:
            if fetcher._browser is not None and getattr(fetcher._browser, "is_connected", lambda: True)():
                return
            if fetcher._manager is not None:
                await fetcher._manager.__aexit__(None, None, None)
                fetcher._manager = None
            fetcher._browser = None

    asyncio.run(run_test())
    assert fetcher._browser is None
    assert fetcher._manager is None

