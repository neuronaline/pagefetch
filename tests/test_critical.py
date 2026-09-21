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
from pagefetch.fetching.http import HTTPFetcher
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


def test_custom_proxy_missing_env_raises(monkeypatch):
    monkeypatch.delenv("CUSTOM_PROXY_URL", raising=False)
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


