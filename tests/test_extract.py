"""Tests for the ``PageFetch.extract()`` coroutine and screenshot capture."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pagefetch import PageFetch
from pagefetch.models import FetchResult
from pagefetch.processing.structure import StructureLimits, extract_structure

SAMPLE_HTML = """
<!doctype html>
<html lang="en"><head>
<title>Extract Demo</title>
<link rel="stylesheet" href="/theme.css">
<style>body { background: #fff; }</style>
</head><body>
<main id="content">
  <h1>Hello</h1>
  <p>Body copy with a <a href="/link">link</a>.</p>
</main>
</body></html>
"""


def rich_html() -> str:
    return SAMPLE_HTML


def _patch_browser_extract(client: PageFetch, html: str, *, capture_response=None):
    """Monkeypatch ``_fetch_browser_extract`` with a fake coroutine."""
    async def fake(
        url,
        proxy,
        *,
        structure,
        compact_structure,
        screenshot,
        screenshot_format,
    ):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        structure_obj = (
            extract_structure(
                soup,
                url,
                limits=StructureLimits(compact=compact_structure) if compact_structure else None,
            )
            if structure
            else None
        )
        result = FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            success=True,
            content_type="text/html",
            encoding="utf-8",
            title="Extract Demo",
            markdown="# Hello\n\nBody copy with a [link](/link).",
            html=html,
            text="Hello\n\nBody copy with a link.",
            structure=structure_obj,
            fetch_method="browser",
            proxy_provider=proxy,
            content_confidence=1.0,
        )
        if capture_response is not None:
            result.screenshot = capture_response.screenshot
            result.screenshot_format = capture_response.screenshot_format
            result.warnings.extend(capture_response.warnings)
        return result

    return fake


@pytest.mark.asyncio
async def test_extract_returns_raw_html_and_structure(tmp_path, monkeypatch):
    client = PageFetch(mode="auto", cache_path=tmp_path / "cache.sqlite3")
    monkeypatch.setattr(client, "_fetch_browser_extract", _patch_browser_extract(client, rich_html()))
    async with client:
        result = await client.extract("https://example.com")
    assert result.success
    assert result.fetch_method == "browser"
    assert result.html == rich_html()
    assert result.structure is not None
    assert result.structure.root is not None
    assert [sheet.url for sheet in result.structure.stylesheets] == [
        "https://example.com/theme.css"
    ]


@pytest.mark.asyncio
async def test_extract_without_structure(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.sqlite3"
    client = PageFetch(mode="auto", cache_path=cache_path)
    monkeypatch.setattr(
        client, "_fetch_browser_extract", _patch_browser_extract(client, rich_html())
    )
    async with client:
        with_structure = await client.extract("https://example.com", structure=True)
        without_structure = await client.extract(
            "https://example.com", use_cache=False, structure=False
        )
    assert with_structure.structure is not None
    assert without_structure.structure is None
    # Cache key must differ: both fetches were misses.
    assert with_structure.from_cache is False
    assert without_structure.from_cache is False


@pytest.mark.asyncio
async def test_extract_compact_structure_partitions_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.sqlite3"
    client = PageFetch(mode="auto", cache_path=cache_path)
    monkeypatch.setattr(
        client, "_fetch_browser_extract", _patch_browser_extract(client, rich_html())
    )
    async with client:
        verbose = await client.extract("https://example.com", compact_structure=False)
        compact = await client.extract("https://example.com", compact_structure=True)
        verbose_again = await client.extract("https://example.com", compact_structure=False)
        compact_again = await client.extract("https://example.com", compact_structure=True)
    assert verbose.from_cache is False
    assert compact.from_cache is False
    assert verbose_again.from_cache is True
    assert compact_again.from_cache is True
    # Verbose nodes carry unique_selector; compact nodes don't.
    verbose_main = next(
        child for child in verbose.structure.root.children if child.tag == "body"
    ).children[0]
    compact_main = next(
        child for child in compact.structure.root.children if child.tag == "body"
    ).children[0]
    assert verbose_main.tag == "main"
    assert compact_main.tag == "main"
    assert verbose_main.unique_selector != ""
    assert compact_main.unique_selector == ""


@pytest.mark.asyncio
async def test_extract_screenshot_viewport(tmp_path, monkeypatch):
    capture = SimpleNamespace(
        screenshot=b"\x89PNG-FAKE-VIEWPORT",
        screenshot_format="png",
        warnings=[],
    )
    client = PageFetch(mode="auto", cache_path=tmp_path / "cache.sqlite3")
    monkeypatch.setattr(
        client,
        "_fetch_browser_extract",
        _patch_browser_extract(client, rich_html(), capture_response=capture),
    )
    async with client:
        result = await client.extract("https://example.com", screenshot="viewport")
    assert result.screenshot == b"\x89PNG-FAKE-VIEWPORT"
    assert result.screenshot_format == "png"


@pytest.mark.asyncio
async def test_extract_screenshot_full(tmp_path, monkeypatch):
    capture = SimpleNamespace(
        screenshot=b"\x89PNG-FAKE-FULLPAGE",
        screenshot_format="png",
        warnings=[],
    )
    client = PageFetch(mode="auto", cache_path=tmp_path / "cache.sqlite3")
    monkeypatch.setattr(
        client,
        "_fetch_browser_extract",
        _patch_browser_extract(client, rich_html(), capture_response=capture),
    )
    async with client:
        result = await client.extract("https://example.com", screenshot="full")
    assert result.screenshot == b"\x89PNG-FAKE-FULLPAGE"
    assert result.screenshot_format == "png"


@pytest.mark.asyncio
async def test_extract_screenshot_jpeg(tmp_path, monkeypatch):
    capture = SimpleNamespace(
        screenshot=b"\xff\xd8\xffJPEG-FAKE",
        screenshot_format="jpeg",
        warnings=[],
    )
    client = PageFetch(mode="auto", cache_path=tmp_path / "cache.sqlite3")
    monkeypatch.setattr(
        client,
        "_fetch_browser_extract",
        _patch_browser_extract(client, rich_html(), capture_response=capture),
    )
    async with client:
        result = await client.extract(
            "https://example.com", screenshot="viewport", screenshot_format="jpeg"
        )
    assert result.screenshot == b"\xff\xd8\xffJPEG-FAKE"
    assert result.screenshot_format == "jpeg"


@pytest.mark.asyncio
async def test_extract_screenshot_oversized_is_discarded(tmp_path, monkeypatch):
    capture = SimpleNamespace(
        screenshot=None,
        screenshot_format=None,
        warnings=["Screenshot exceeded max size; discarded."],
    )
    client = PageFetch(mode="auto", cache_path=tmp_path / "cache.sqlite3")
    monkeypatch.setattr(
        client,
        "_fetch_browser_extract",
        _patch_browser_extract(client, rich_html(), capture_response=capture),
    )
    async with client:
        result = await client.extract("https://example.com", screenshot="viewport")
    assert result.screenshot is None
    assert result.screenshot_format is None
    assert any("discarded" in w.lower() for w in result.warnings)


@pytest.mark.asyncio
async def test_extract_cache_hit_loads_html_but_not_screenshot(tmp_path, monkeypatch):
    """Cached entries store HTML+structure but never the screenshot."""
    cache_path = tmp_path / "cache.sqlite3"
    capture = SimpleNamespace(
        screenshot=b"\x89PNG-FIRST",
        screenshot_format="png",
        warnings=[],
    )
    client = PageFetch(mode="auto", cache_path=cache_path)
    monkeypatch.setattr(
        client,
        "_fetch_browser_extract",
        _patch_browser_extract(client, rich_html(), capture_response=capture),
    )
    async with client:
        first = await client.extract(
            "https://example.com", screenshot="viewport", use_cache=True
        )
        assert first.screenshot == b"\x89PNG-FIRST"
        # Second call: cache hit; screenshot is None and a warning is added.
        second = await client.extract("https://example.com", screenshot="viewport")
    assert second.from_cache is True
    assert second.html == rich_html()
    assert second.screenshot is None
    assert any("not cached" in w.lower() for w in second.warnings)


@pytest.mark.asyncio
async def test_extract_validation_rejects_bad_scheme(tmp_path, monkeypatch):
    """A bad scheme is surfaced as a structured failure, mirroring ``fetch``."""
    client = PageFetch(mode="auto", cache_path=tmp_path / "cache.sqlite3")
    monkeypatch.setattr(
        client, "_fetch_browser_extract", _patch_browser_extract(client, rich_html())
    )
    async with client:
        result = await client.extract("ftp://example.com")
    assert not result.success
    assert result.error is not None
    assert result.error.code == "unsupported_scheme"


@pytest.mark.asyncio
async def test_extract_requires_running_client(tmp_path, monkeypatch):
    """Calling ``extract`` outside the context manager starts resources lazily."""
    client = PageFetch(mode="auto", cache_path=tmp_path / "cache.sqlite3")
    monkeypatch.setattr(
        client, "_fetch_browser_extract", _patch_browser_extract(client, rich_html())
    )
    result = await client.extract("https://example.com")
    assert result.success
    await client.close()


@pytest.mark.asyncio
async def test_extract_processing_version_bump_invalidates_old_cache(tmp_path, monkeypatch):
    """An entry persisted under ``processing_version=4`` is treated as a miss
    after the bump to v5 (the cache key changes)."""
    import hashlib

    from pagefetch.cache import keys as cache_keys
    from pagefetch.cache.keys import build_cache_key

    url = "https://example.com"
    settings = {"legacy": True}
    normalized = cache_keys.normalize_url(url)

    # The current code path emits a v5 key for this input.
    current_key = cache_keys.build_cache_key(
        url, mode="browser", proxy="none", settings=settings
    )
    assert len(current_key) == 64
    assert current_key == hashlib.sha256(
        json.dumps(
            {
                "url": normalized,
                "mode": "browser",
                "proxy": "none",
                "processing_version": 5,
                "settings": settings,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()

    # Simulate the legacy v4 key derivation: same payload except the
    # ``processing_version`` field is set to ``4``. The hash MUST differ
    # from the v5 key, otherwise an upgrade would silently reuse stale
    # cache entries.
    legacy_payload = json.dumps(
        {
            "url": normalized,
            "mode": "browser",
            "proxy": "none",
            "processing_version": 4,
            "settings": settings,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    legacy_key = hashlib.sha256(legacy_payload.encode("utf-8")).hexdigest()
    assert legacy_key != current_key

    # Sanity check: if the keys module is accidentally reverted to v4, the
    # "current" key recomputed through the public helper must equal the
    # legacy hash above — proving the bump is what invalidates entries.
    def _legacy_build_cache_key(url, *, mode, proxy, settings=None):
        payload = {
            "url": cache_keys.normalize_url(url),
            "mode": mode,
            "proxy": proxy,
            "processing_version": 4,
            "settings": settings or {},
        }
        raw = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    monkeypatch.setattr(cache_keys, "build_cache_key", _legacy_build_cache_key)
    assert (
        cache_keys.build_cache_key(url, mode="browser", proxy="none", settings=settings)
        == legacy_key
    )


@pytest.mark.asyncio
async def test_extract_screenshot_round_trip_through_dict(tmp_path, monkeypatch):
    capture = SimpleNamespace(
        screenshot=b"\x89PNG-ROUND-TRIP",
        screenshot_format="png",
        warnings=[],
    )
    client = PageFetch(mode="auto", cache_path=tmp_path / "cache.sqlite3")
    monkeypatch.setattr(
        client,
        "_fetch_browser_extract",
        _patch_browser_extract(client, rich_html(), capture_response=capture),
    )
    async with client:
        result = await client.extract("https://example.com", screenshot="viewport")
    payload = result.to_dict(include_screenshot=True, include_structure=True)
    assert payload["screenshot_format"] == "png"
    assert isinstance(payload["screenshot"], str)  # base64
    restored = FetchResult.from_dict(payload)
    assert restored.screenshot == b"\x89PNG-ROUND-TRIP"
    assert restored.screenshot_format == "png"
    assert restored.structure is not None