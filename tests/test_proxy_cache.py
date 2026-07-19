from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pagefetch.cache.keys import build_cache_key
from pagefetch.cache.sqlite import SQLiteCache
from pagefetch.models import FetchResult
from pagefetch.proxy.providers import ProxyConfigurationError, redact_proxy_url, resolve_proxy


def test_cache_keys_are_stable_and_provider_specific():
    first = build_cache_key("HTTPS://Example.com#x", mode="auto", proxy="none")
    second = build_cache_key("https://example.com/", mode="auto", proxy="none")
    proxied = build_cache_key("https://example.com/", mode="auto", proxy="decodo")
    assert first == second
    assert first != proxied
    assert "example" not in first


def test_proxy_environment(monkeypatch):
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


def test_missing_proxy_environment(monkeypatch):
    for suffix in ("PROXY_URL", "HOST", "PORT", "USERNAME", "PASSWORD"):
        monkeypatch.delenv(f"DATAIMPULSE_{suffix}", raising=False)
    with pytest.raises(ProxyConfigurationError, match="DATAIMPULSE_HOST"):
        resolve_proxy("dataimpulse")


@pytest.mark.asyncio
async def test_sqlite_cache_persists_and_ignores_failures(tmp_path):
    path = tmp_path / "cache.sqlite3"
    result = FetchResult(
        url="https://example.com/",
        success=True,
        markdown="hello",
        html="<p>hello</p>",
        fetched_at=datetime.now(UTC),
    )
    cache = SQLiteCache(path)
    await cache.start()
    await cache.set("good", result, 60)
    await cache.set("failed", FetchResult(url="x", success=False), 60)
    await cache.close()

    reopened = SQLiteCache(path)
    await reopened.start()
    cached = await reopened.get("good")
    assert cached is not None
    assert cached.from_cache is True
    assert cached.fetch_method == "cache"
    assert cached.html == "<p>hello</p>"
    assert await reopened.get("failed") is None
    await reopened.close()


@pytest.mark.asyncio
async def test_expired_cache_is_ignored(tmp_path):
    cache = SQLiteCache(tmp_path / "cache.sqlite3")
    await cache.start()
    await cache.set("expired", FetchResult(url="x", success=True), 0)
    assert await cache.get("expired") is None
    await cache.close()

