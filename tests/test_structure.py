"""Tests for the static structure extractor and FetchResult integration."""

from __future__ import annotations

import json

import httpx
import pytest

from pagefetch import PageFetch
from pagefetch.fetching.http import HTTPFetcher
from pagefetch.processing.structure import StructureLimits, extract_structure


def rich_structure_html() -> str:
    return """
<!doctype html>
<html lang="en"><head>
<title>Structure Demo</title>
<link rel="stylesheet" href="/theme.css" media="all">
<link rel="preload" href="/font.css" as="font">
<link rel="icon" href="/favicon.ico">
<style>body { background: #fff; }</style>
<script src="/boot.js" defer></script>
<script type="module" src="/app.js" async></script>
<script type="application/json">{"config": true}</script>
</head><body>
<main id="content">
  <article class="post featured" data-id="42">
    <h1>Structure Demo</h1>
    <p>Body <strong>copy</strong> with a <a href="/link">link</a>.</p>
    <ul><li>One</li><li>Two</li></ul>
  </article>
</main>
</body></html>
"""


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


def test_dom_hierarchy_and_selector_generation():
    structure = extract_structure(rich_structure_html(), "https://example.com/")
    assert structure.root is not None
    assert structure.root.tag == "html"
    # Walk to the <article> tag and confirm scraper-oriented selectors are available.
    body = next(child for child in structure.root.children if child.tag == "body")
    main = next(child for child in body.children if child.tag == "main")
    assert main.selector == "main#content"
    assert main.unique_selector == "main#content"
    article = next(child for child in main.children if child.tag == "article")
    assert article.selector == "article.post.featured"
    assert article.path.endswith("main#content > article.post.featured")
    assert article.unique_selector == "article.post.featured"
    assert article.attrs == {
        "class": "post featured",
        "data-id": "42",
    }
    # Text preview stays compact and includes direct-text children only.
    paragraph = next(child for child in article.children if child.tag == "p")
    assert paragraph.text.startswith("Body")
    assert len(paragraph.text) <= StructureLimits().text_preview + 1


def test_stylesheet_and_script_discovery():
    structure = extract_structure(rich_structure_html(), "https://example.com/")
    assert [sheet.url for sheet in structure.stylesheets] == [
        "https://example.com/theme.css"
    ]
    theme = structure.stylesheets[0]
    assert theme.media == "all"

    scripts = structure.scripts
    assert [script.url for script in scripts] == [
        "https://example.com/boot.js",
        "https://example.com/app.js",
    ]
    assert scripts[0].defer is True and scripts[0].async_ is False
    assert scripts[1].async_ is True and scripts[1].defer is False
    assert scripts[1].type == "module"

    assert structure.inline_scripts[0].type == "application/json"
    assert json.loads(structure.inline_scripts[0].content) == {"config": True}
    assert structure.inline_styles[0].content.strip() == "body { background: #fff; }"


def test_inline_content_truncation():
    large_style = "<style>" + ("a" * 8_000) + "</style>"
    html = "<html><head>" + large_style + "</head><body><p>x</p></body></html>"
    limits = StructureLimits(inline_source_limit=128)
    structure = extract_structure(html, limits=limits)
    assert structure.inline_styles[0].truncated is True
    assert len(structure.inline_styles[0].content) == 128

    small_style = "<style>p { color: red; }</style>"
    html2 = "<html><head>" + small_style + "</head><body><p>x</p></body></html>"
    structure_small = extract_structure(html2)
    assert structure_small.inline_styles[0].truncated is False
    assert structure_small.inline_styles[0].content == "p { color: red; }"


def test_depth_and_node_limits_are_enforced():
    nested = "<html><body>" + "<div>" * 20 + "deep" + "</div>" * 20 + "</body></html>"
    limits = StructureLimits(max_depth=4, max_nodes=10)
    structure = extract_structure(nested, limits=limits)
    assert structure.truncated is True
    assert structure.node_count <= limits.max_nodes
    assert structure.max_depth == 4


def test_repeated_siblings_get_unique_css_paths():
    structure = extract_structure(
        "<html><body><ul><li>One</li><li>Two</li></ul></body></html>"
    )
    body = next(child for child in structure.root.children if child.tag == "body")
    items = next(child for child in body.children if child.tag == "ul").children
    assert items[0].path.endswith("li:nth-of-type(1)")
    assert items[1].path.endswith("li:nth-of-type(2)")
    assert items[0].unique_selector == items[0].path
    assert items[1].unique_selector == items[1].path


def test_selector_escapes_css_identifiers():
    structure = extract_structure(
        '<html><body><div id="product:42" class="md:hover">Item</div></body></html>'
    )
    body = next(child for child in structure.root.children if child.tag == "body")
    div = next(child for child in body.children if child.tag == "div")
    assert div.selector == r"div#product\:42.md\:hover"
    assert div.unique_selector == r"div#product\:42.md\:hover"


def test_selector_omits_classes_when_missing():
    structure = extract_structure(
        "<html><body><section><h1>Title</h1></section></body></html>"
    )
    body = next(child for child in structure.root.children if child.tag == "body")
    section = next(child for child in body.children if child.tag == "section")
    assert section.selector == "section"
    heading = next(child for child in section.children if child.tag == "h1")
    assert heading.selector == "h1"
    assert heading.text == "Title"


def test_no_event_handlers_or_inline_styles_appear_in_attrs():
    structure = extract_structure(
        '<html><body><a href="/x" onclick="alert(1)" style="color:red">x</a></body></html>'
    )
    body = next(child for child in structure.root.children if child.tag == "body")
    anchor = next(child for child in body.children if child.tag == "a")
    assert "onclick" not in anchor.attrs
    assert "style" not in anchor.attrs
    assert anchor.attrs == {"href": "/x"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["http", "auto"])
async def test_extract_structure_requires_browser_mode(tmp_path, mode):
    client = PageFetch(mode=mode, cache_path=tmp_path / f"{mode}.sqlite3")
    with pytest.raises(ValueError, match="requires mode='browser'"):
        await client.fetch("https://example.com", extract_structure=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["http", "auto"])
async def test_fetch_many_extract_structure_invalid_arg_returns_per_url_error(tmp_path, mode):
    """``fetch`` raises ValueError for non-browser modes; ``fetch_many``
    must convert each URL into a structured failure rather than aborting
    the entire batch."""
    client = PageFetch(mode=mode, cache_path=tmp_path / f"{mode}.sqlite3")
    async with client:
        results = await client.fetch_many(
            ["https://a.example/", "https://b.example/"],
            extract_structure=True,
        )
    assert len(results) == 2
    for result in results:
        assert not result.success
        assert result.error is not None
        assert result.error.code == "invalid_argument"
        assert "requires mode='browser'" in result.error.message


@pytest.mark.asyncio
async def test_browser_mode_attaches_structure_when_requested(tmp_path, monkeypatch):
    client = PageFetch(mode="browser", cache_path=tmp_path / "cache.sqlite3")

    async def fake_fetch_browser(url, proxy, status_code=None, *, extract_structure=False):
        return client._result_from_html(
            original_url=url,
            final_url=url,
            status_code=200,
            html=rich_structure_html(),
            content_type="text/html",
            encoding="utf-8",
            proxy=proxy,
            method="browser",
            include_structure=extract_structure,
        )

    monkeypatch.setattr(client, "_fetch_browser", fake_fetch_browser)
    async with client:
        structured = await client.fetch("https://example.com", extract_structure=True)
        without = await client.fetch("https://example.com", use_cache=False)
    assert structured.success
    assert structured.fetch_method == "browser"
    assert structured.structure is not None
    assert structured.structure.root is not None
    assert [sheet.url for sheet in structured.structure.stylesheets] == [
        "https://example.com/theme.css"
    ]
    assert without.structure is None


@pytest.mark.asyncio
async def test_structure_setting_partitions_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.sqlite3"

    client = PageFetch(mode="browser", cache_path=cache_path)

    async def fake_fetch_browser(url, proxy, status_code=None, *, extract_structure=False):
        return client._result_from_html(
            original_url=url,
            final_url=url,
            status_code=200,
            html=rich_structure_html(),
            content_type="text/html",
            encoding="utf-8",
            proxy=proxy,
            method="browser",
            include_structure=extract_structure,
        )

    monkeypatch.setattr(client, "_fetch_browser", fake_fetch_browser)
    async with client:
        plain = await client.fetch("https://example.com")
        structured = await client.fetch("https://example.com", extract_structure=True)
    assert plain.structure is None
    assert structured.structure is not None
    # Both runs were freshly fetched — different cache keys.
    assert plain.from_cache is False
    assert structured.from_cache is False

    cached_client = PageFetch(mode="browser", cache_path=cache_path)
    async with cached_client:
        reused = await cached_client.fetch("https://example.com", extract_structure=True)
    assert reused.from_cache is True
    assert reused.structure is not None


def test_result_serialization_round_trips_structure():
    from pagefetch.models import FetchResult

    html = rich_structure_html()
    structure = extract_structure(html, "https://example.com/")
    result = FetchResult(
        url="https://example.com/",
        final_url="https://example.com/",
        status_code=200,
        success=True,
        content_type="text/html",
        title="Structure Demo",
        structure=structure,
        proxy_provider="none",
        fetched_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    payload = result.to_dict(include_structure=True)
    assert "structure" in payload
    scripts = payload["structure"]["scripts"]
    assert all("async" in script for script in scripts)
    restored = FetchResult.from_dict(payload)
    assert restored.structure is not None
    assert restored.structure.scripts[1].async_ is True
    assert restored.structure.stylesheets[0].url == "https://example.com/theme.css"
