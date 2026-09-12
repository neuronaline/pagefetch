"""Critical-path tests for PageFetch.

This file is reserved for safety limits, authorization, and essential flows that
prevent data loss. Unit details and implementation-specific tests live elsewhere.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from pagefetch import PageFetch, PageFetchError
from pagefetch.cache.keys import build_cache_key
from pagefetch.cache.sqlite import SQLiteCache
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
    monkeypatch.setenv("DECODO_HOST", "proxy.example")
    monkeypatch.setenv("DECODO_PORT", "1234")
    monkeypatch.setenv("DECODO_USERNAME", "user@zone")
    monkeypatch.setenv("DECODO_PASSWORD", "secret:value")
    settings = resolve_proxy("decodo")
    assert settings.url == "http://user%40zone:secret%3Avalue@proxy.example:1234"
    assert settings.browser_config() == {
        "server": "http://proxy.example:1234",
        "username": "user@zone",
        "password": "secret:value",
    }
    assert redact_proxy_url(settings.url) == "http://***:***@proxy.example:1234"


def test_missing_proxy_environment_raises(monkeypatch):
    for suffix in ("PROXY_URL", "HOST", "PORT", "USERNAME", "PASSWORD"):
        monkeypatch.delenv(f"DATAIMPULSE_{suffix}", raising=False)
    with pytest.raises(ProxyConfigurationError, match="DATAIMPULSE_HOST"):
        resolve_proxy("dataimpulse")


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
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    first_task = asyncio.create_task(run_first_close())
    await asyncio.sleep(0.1)
    first_task.cancel()
    try:
        await first_task
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    try:
        await asyncio.wait_for(client.close(), timeout=1.0)
    finally:
        blocker.set()
        await client.aclose() if hasattr(client, "aclose") else None