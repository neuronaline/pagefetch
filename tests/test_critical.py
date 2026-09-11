"""Critical-path tests for PageFetch."""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime

import httpx
import pytest

from pagefetch import PageFetch, PageFetchError, bootstrap
from pagefetch.cache.keys import build_cache_key
from pagefetch.cache.sqlite import SQLiteCache
from pagefetch.fetching.http import HTTPFetcher
from pagefetch.models import FetchErrorInfo, FetchResult, ImageInfo, LinkInfo
from pagefetch.processing.detector import analyze_html
from pagefetch.processing.html import process_html
from pagefetch.processing.non_html import process_pdf, process_text, process_xml
from pagefetch.processing.structure import StructureLimits, extract_structure
from pagefetch.proxy.providers import ProxyConfigurationError, redact_proxy_url, resolve_proxy
from pagefetch.utils.durations import parse_duration
from pagefetch.utils.urls import normalize_url, validate_url


@pytest.mark.parametrize(("value", "expected"), [(30, 30), ("30s", 30), ("30m", 1800), ("24h", 86400), ("7d", 604800)])
def test_parse_duration_accepts_canonical_forms(value, expected):
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["", "24", "-1h", "potato", -1, True])
def test_parse_duration_rejects_invalid_input(value):
    with pytest.raises((ValueError, TypeError)):
        parse_duration(value)


def test_url_validation_and_normalization():
    assert normalize_url("HTTPS://Example.COM:443?q=1#fragment") == "https://example.com/?q=1"
    assert normalize_url("http://example.com:8080") == "http://example.com:8080/"
    with pytest.raises(ValueError):
        validate_url("file:///tmp/page")


def test_result_serialization_excludes_html_by_default():
    result = FetchResult(
        url="https://example.com/",
        success=False,
        html="<p>large</p>",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        links=[LinkInfo("x", "https://example.com/x", True, [], None, 0)],
        images=[ImageInfo("https://example.com/x.png", "x", None, 0)],
        error=FetchErrorInfo("blocked", "blocked", False),
    )
    data = result.to_dict()
    assert "html" not in data
    assert result.to_dict(include_html=True)["html"] == "<p>large</p>"
    reconstructed = FetchResult.from_dict(result.to_dict(include_html=True))
    assert reconstructed.error == result.error
    assert reconstructed.links == result.links
    assert "2026-01-01" in result.json()


def test_cache_keys_are_stable_and_provider_specific():
    canonical = build_cache_key("HTTPS://Example.com#x", mode="auto", proxy="none")
    assert canonical == build_cache_key("https://example.com/", mode="auto", proxy="none")
    assert canonical != build_cache_key("https://example.com/", mode="auto", proxy="decodo")
    assert "example" not in canonical  # hostname must not leak into the key


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


def test_optional_requirement_check_is_read_only_and_actionable(monkeypatch):
    monkeypatch.setattr(
        bootstrap.importlib.util,
        "find_spec",
        lambda module: None if module in {"camoufox", "pypdf"} else object(),
    )
    with pytest.raises(bootstrap.RuntimeBootstrapError, match=r"pagefetch\[browser\]"):
        bootstrap.ensure_runtime_requirements(needs_browser=True)
    with pytest.raises(bootstrap.RuntimeBootstrapError, match=r"pagefetch\[pdf\]"):
        bootstrap.ensure_runtime_requirements(needs_browser=False, needs_pdf=True)


def test_core_http_mode_has_no_optional_requirements(monkeypatch):
    monkeypatch.setattr(bootstrap.importlib.util, "find_spec", lambda _module: None)
    bootstrap.ensure_runtime_requirements(needs_browser=False, needs_pdf=False)


@pytest.mark.asyncio
async def test_async_bootstrap_runs_sync_work_in_a_thread(monkeypatch):
    called = asyncio.Event()

    def install():
        called.set()

    monkeypatch.setenv("PAGEFETCH_AUTO_INSTALL", "1")
    monkeypatch.setattr(bootstrap, "_install_browser_sync", install)
    await bootstrap.bootstrap_browser()
    assert called.is_set()


@pytest.mark.asyncio
async def test_async_bootstrap_respects_opt_out(monkeypatch):
    monkeypatch.setenv("PAGEFETCH_AUTO_INSTALL", "off")
    monkeypatch.setattr(bootstrap, "_has_camoufox_binary", lambda: False)
    with pytest.raises(bootstrap.RuntimeBootstrapError, match="PAGEFETCH_AUTO_INSTALL"):
        await bootstrap.bootstrap_browser()


def test_pdf_metadata_extraction_and_empty_page_warning():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_metadata({"/Title": "Test Document", "/Author": "PageFetch Tester"})
    buf = io.BytesIO()
    writer.write(buf)

    result = process_pdf(buf.getvalue())
    assert result.title == "Test Document"
    assert result.markdown.startswith("# Test Document")
    assert "No extractable text was found on PDF page 1." in result.warnings
    assert result.metadata.get("Author") == "PageFetch Tester"


def test_xml_preserves_hierarchy_and_emits_fenced_block():
    xml_bytes = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b"<catalog>\n"
        b'  <book id="1"><title>One</title></book>\n'
        b'  <book id="2"><title>Two</title></book>\n'
        b"</catalog>"
    )
    result = process_xml(xml_bytes, "utf-8")
    assert result.title == "catalog"
    assert "```xml" in result.markdown and "<book" in result.markdown
    assert "One" in result.text and "Two" in result.text
    assert result.metadata["root_element"] == "catalog"


def test_plain_text_decodes_and_preserves_lines():
    raw = b"Line 1\r\nLine 2\r\n  Line 3\n"
    result = process_text(raw, "utf-8")
    assert result.text == "Line 1\nLine 2\n  Line 3\n"
    assert result.markdown == result.text
    assert result.metadata["encoding"] == "utf-8"


RICH_HTML = """
<!doctype html>
<html lang="en"><head>
<title>Example Article</title>
<meta name="description" content="A useful article">
<meta property="og:title" content="Example Article">
</head><body>
<main><article><h1>Example Article</h1>
<p>This is a substantial paragraph with enough useful text to describe the subject in detail.</p>
<p>Another paragraph preserves context, comments, links, and all meaningful page information.</p>
<blockquote>A quotation with <strong>emphasis</strong>.</blockquote>
<ul><li>First item<ul><li>Nested item</li></ul></li><li>Second item</li></ul>
<pre><code class="language-python">print("hello")</code></pre>
<table><tr><th>Name</th><th>Value</th></tr><tr><td>Alpha</td><td>42</td></tr></table>
<p><a href="/details" rel="next" target="_blank">Details</a></p>
<img data-src="/hero.jpg" alt="Hero" title="Image">
</article></main>
<section class="comments"><p>Repeated user comment</p></section>
<div class="cookie-banner">Accept cookies</div>
</body></html>
"""


def test_processing_preserves_content_and_converts_markdown():
    result = process_html(RICH_HTML, "https://example.com/article")
    assert result.title == "Example Article"
    assert "# Example Article" in result.markdown
    assert "[Details](https://example.com/details)" in result.markdown
    assert "![Hero](https://example.com/hero.jpg \"Image\")" in result.markdown
    assert "```python" in result.markdown
    assert "| Name | Value |" in result.markdown
    assert "  - Nested item" in result.markdown
    assert result.metadata["description"] == "A useful article"
    assert result.links[0].url == "https://example.com/details"
    assert result.links[0].internal is True
    assert result.images[0].url == "https://example.com/hero.jpg"


def test_detector_distinguishes_rich_content_spa_and_challenge():
    rich = analyze_html(RICH_HTML)
    spa = analyze_html("<html><body><div id='root'></div><script>" + "x" * 12000 + "</script></body></html>")
    challenge = analyze_html("<html><title>Attention Required</title><body>Verify you are human CAPTCHA</body></html>")
    assert rich.score >= 0.80
    assert spa.score < 0.50 and spa.javascript_shell
    assert challenge.score <= 0.08 and challenge.challenge


def test_challenge_terms_in_normal_article_are_not_enough():
    for term in ("CAPTCHA", "access denied", "unusual traffic"):
        html = f"""
        <html><head><title>How {term} Detection Works</title></head><body>
          <main><article><h1>How {term} Detection Works</h1>
          <p>{term} systems distinguish automated traffic from people. This article
          explains their history, accessibility tradeoffs, implementation, and common
          alternatives for protecting forms without frustrating legitimate visitors.</p>
          </article></main>
        </body></html>
        """
        report = analyze_html(html)
        assert not report.challenge
        assert report.score > 0.08


def test_cleaning_levels_remove_different_subsets():
    minimal = process_html(RICH_HTML, "https://example.com/article", cleaning_level="minimal")
    standard = process_html(RICH_HTML, "https://example.com/article", cleaning_level="standard")
    maximum = process_html(RICH_HTML, "https://example.com/article", cleaning_level="maximum")

    assert "Accept cookies" in minimal.text  # minimal keeps cookie banners
    assert "Accept cookies" not in standard.text
    assert "Accept cookies" not in maximum.text

    assert "Repeated user comment" in standard.text
    assert "Repeated user comment" not in maximum.text  # comments only removed at maximum

    for result in (minimal, standard, maximum):
        assert "# Example Article" in result.markdown
        assert "```python" in result.markdown
        assert "| Name | Value |" in result.markdown


def rich_page(title: str = "Test Page") -> str:
    paragraphs = "".join(
        f"<p>Paragraph {index} contains useful server-rendered information and enough words for completeness.</p>"
        for index in range(8)
    )
    return f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1>{paragraphs}</main></body></html>"


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
async def test_http_mode_fetches_and_processes_html(tmp_path):
    client = PageFetch(mode="http", cache_path=tmp_path / "cache.sqlite3")
    attach_transport(
        client,
        lambda request: httpx.Response(
            200,
            text=rich_page(),
            headers={"Content-Type": "text/html; charset=utf-8"},
            request=request,
        ),
    )
    async with client:
        result = await client.fetch("https://example.com")
    assert result.success
    assert result.fetch_method == "http"
    assert result.title == "Test Page"
    assert result.content_confidence >= 0.80
    assert "# Test Page" in result.markdown


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


@pytest.mark.asyncio
async def test_auto_falls_back_for_spa_shell(tmp_path):
    client = PageFetch(mode="auto", cache_enabled=False)
    attach_transport(
        client,
        lambda request: httpx.Response(
            200,
            text="<html><body><div id='root'></div><script>boot()</script></body></html>",
            headers={"Content-Type": "text/html"},
            request=request,
        ),
    )
    browser_calls = 0

    async def fake_browser(url: str, proxy: str, status_code: int | None, **_k):
        nonlocal browser_calls
        browser_calls += 1
        return client._result_from_html(
            original_url=url,
            final_url=url,
            status_code=200,
            html=rich_page("Rendered Page"),
            content_type="text/html",
            encoding="utf-8",
            proxy=proxy,
            method="browser",
        )

    client._fetch_browser = fake_browser
    async with client:
        result = await client.fetch("https://example.com/app")
    assert browser_calls == 1
    assert result.fetch_method == "browser"
    assert result.title == "Rendered Page"
    assert any("browser fallback" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_auto_falls_back_for_403(tmp_path):
    client = PageFetch(mode="auto", cache_enabled=False, retries_http=0)
    attach_transport(client, lambda request: httpx.Response(403, text="blocked", request=request))

    async def fake_browser(url: str, proxy: str, status_code: int | None, **_k):
        assert status_code == 403
        return client._result_from_html(
            original_url=url,
            final_url=url,
            status_code=200,
            html=rich_page(),
            content_type="text/html",
            encoding="utf-8",
            proxy=proxy,
            method="browser",
        )

    client._fetch_browser = fake_browser
    async with client:
        result = await client.fetch("https://example.com")
    assert result.success and result.fetch_method == "browser"


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


@pytest.mark.asyncio
async def test_success_is_loaded_from_persistent_cache(tmp_path):
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=rich_page(), headers={"Content-Type": "text/html"}, request=request)

    client = PageFetch(mode="http", cache_path=tmp_path / "cache.sqlite3")
    attach_transport(client, handler)
    async with client:
        first = await client.fetch("https://example.com")
        second = await client.fetch("https://example.com")
    assert first.fetch_method == "http"
    assert second.fetch_method == "cache" and second.from_cache
    assert calls == 1


@pytest.mark.asyncio
async def test_fetch_many_deduplicates_and_preserves_order():
    counts: dict[str, int] = {}

    def handler(request: httpx.Request):
        key = str(request.url)
        counts[key] = counts.get(key, 0) + 1
        return httpx.Response(200, text=rich_page(key), headers={"Content-Type": "text/html"}, request=request)

    client = PageFetch(mode="http", cache_enabled=False)
    attach_transport(client, handler)
    urls = ["https://a.test/", "https://b.test/", "https://a.test/"]
    async with client:
        results = await client.fetch_many(urls)
    assert [result.url for result in results] == urls
    assert counts == {"https://a.test/": 1, "https://b.test/": 1}
    assert results[0] == results[2]
    assert results[0] is not results[2]  # duplicates are deep-copied so callers don't get aliased data
    results[0].warnings.append("mutation in batch entry")
    assert "mutation in batch entry" not in results[2].warnings


@pytest.mark.asyncio
async def test_batch_failure_does_not_cancel_success():
    def handler(request: httpx.Request):
        if request.url.host == "bad.test":
            return httpx.Response(404, text="missing", request=request)
        return httpx.Response(200, text=rich_page(), headers={"Content-Type": "text/html"}, request=request)

    client = PageFetch(mode="http", cache_enabled=False, retries_http=0)
    attach_transport(client, handler)
    async with client:
        results = await client.fetch_many(["https://bad.test", "https://good.test"])
    assert not results[0].success and results[0].status_code == 404
    assert results[1].success


@pytest.mark.asyncio
async def test_plain_text_and_xml_are_processed_without_browser():
    def handler(request: httpx.Request):
        if request.url.path == "/data.xml":
            return httpx.Response(
                200,
                content=b"<?xml version='1.0'?><root><item>Value</item></root>",
                headers={"Content-Type": "application/xml"},
                request=request,
            )
        return httpx.Response(
            200,
            content="Merhaba dünya\r\nİkinci satır".encode(),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            request=request,
        )

    client = PageFetch(mode="auto", cache_enabled=False)
    attach_transport(client, handler)
    async with client:
        xml, text = await client.fetch_many(
            ["https://example.test/data.xml", "https://example.test/readme"]
        )
    assert xml.fetch_method == "xml" and "```xml" in xml.markdown and "Value" in xml.text
    assert text.fetch_method == "text" and text.markdown == "Merhaba dünya\nİkinci satır"


@pytest.mark.asyncio
async def test_raise_on_error_preserves_structured_error():
    client = PageFetch(mode="http", cache_enabled=False, raise_on_error=True, retries_http=0)
    attach_transport(client, lambda request: httpx.Response(404, request=request))
    async with client:
        with pytest.raises(PageFetchError) as caught:
            await client.fetch("https://example.com/missing")
    assert caught.value.error.code == "http_error"


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


def test_extract_structure_marks_truncation_when_limits_are_tight():
    long_html = "<main>" + "<p>text</p>" * 2000 + "</main>"
    structure = extract_structure(
        long_html, base_url="https://example.test/", limits=StructureLimits(max_nodes=5)
    )
    assert structure.truncated is True
    assert structure.node_count <= 5


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


@pytest.mark.asyncio
async def test_closed_client_error_carries_startup_warnings(tmp_path):
    # Point cache_path at a child of a regular file so SQLiteCache init fails;
    # the diagnostic must surface on every result the client returns.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    client = PageFetch(cache_enabled=True, cache_path=blocker / "cache.sqlite3")
    try:
        result = await client.fetch("https://example.com/anything", use_cache=False)
        assert not result.success
        assert result.error is not None
        assert any("Cache" in w for w in result.warnings)
    finally:
        await client.close()


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


# ---------------------------------------------------------------------------
# Virtual display (Xvfb) + platform-aware headless decision
# ---------------------------------------------------------------------------


def test_xvfb_start_reports_missing_binary(monkeypatch):
    """``XvfbDisplay.start()`` raises XvfbNotFound when ``Xvfb`` is not on PATH."""
    import shutil

    from pagefetch.fetching.virtual_display import XvfbDisplay, XvfbNotFound

    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(XvfbNotFound):
        XvfbDisplay().start()


def test_xvfb_display_free_respects_lockfile(tmp_path, monkeypatch):
    """A stale ``/tmp/.X{n}-lock`` file marks the display as occupied."""
    from pathlib import Path

    from pagefetch.fetching.virtual_display import XvfbDisplay

    # Make the helper look at our tmp_path instead of the real /tmp.
    class _FakePath(type(Path())):
        def __new__(cls, *args, **kwargs):  # noqa: D401 - thin wrapper
            return Path(*args, **kwargs)

    real_path = Path

    def _patched(name):
        return tmp_path / real_path(name).name

    monkeypatch.setattr(
        "pagefetch.fetching.virtual_display.Path",
        _patched,
    )
    (tmp_path / ".X123-lock").write_text("")
    assert XvfbDisplay._is_display_free(123) is False


@pytest.mark.skipif(
    __import__("shutil").which("Xvfb") is None,
    reason="Xvfb binary is not installed on this host",
)
def test_xvfb_lifecycle_starts_and_stops():
    """End-to-end: Xvfb writes its lock file and ``stop()`` removes the process."""
    from pathlib import Path

    from pagefetch.fetching.virtual_display import XvfbDisplay

    xvfb = XvfbDisplay(width=320, height=240)
    display = xvfb.start()
    try:
        assert xvfb.is_running
        assert display.startswith(":")
        num = int(display.lstrip(":"))
        # The server is up iff it has claimed its lock file.
        assert Path(f"/tmp/.X{num}-lock").exists()
    finally:
        xvfb.stop()
    assert not xvfb.is_running


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("win32", "windows"),
        ("darwin", "macos"),
        ("linux", "linux"),
        ("linux2", "linux"),
    ],
)
def test_browser_fetcher_detect_os_mapping(platform, expected, monkeypatch):
    """``_detect_os`` maps ``sys.platform`` to a Camoufox ``os`` string."""
    from pagefetch.fetching import browser as browser_mod

    monkeypatch.setattr(browser_mod.sys, "platform", platform)
    fetcher = browser_mod.BrowserFetcher.__new__(browser_mod.BrowserFetcher)
    assert fetcher._detect_os() == expected


@pytest.mark.asyncio
async def test_browser_fetcher_linux_spawns_xvfb_and_heads_browser(monkeypatch):
    """Linux path: Xvfb starts, DISPLAY is exported, ``headless=False``."""
    import os
    import sys
    import types
    from unittest.mock import MagicMock

    from pagefetch import bootstrap as bootstrap_mod
    from pagefetch.fetching import browser as browser_mod

    monkeypatch.setattr(browser_mod.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)

    started: list[object] = []

    class _FakeXvfb:
        display = ":123"
        is_running = True

        def start(self):
            started.append(self)
            return self.display

        def stop(self):
            pass

    monkeypatch.setattr(browser_mod, "XvfbDisplay", _FakeXvfb)

    captured: dict = {}

    class _FakeCamoufox:
        def __init__(self, **opts):
            captured.update(opts)

        async def __aenter__(self):
            return "fake-browser"

        async def __aexit__(self, *exc):
            return False

    fake_async_api = types.SimpleNamespace(AsyncCamoufox=_FakeCamoufox)
    monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.async_api", fake_async_api)

    async def _no_bootstrap():
        return None

    monkeypatch.setattr(bootstrap_mod, "bootstrap_browser", _no_bootstrap)

    proxy = MagicMock()
    proxy.browser_config.return_value = None

    fetcher = browser_mod.BrowserFetcher(
        semaphore=__import__("asyncio").Semaphore(1),
        timeout=30.0,
        retries=0,
        proxy=proxy,
        max_content_size=1_000_000,
    )
    try:
        await fetcher.start()
        assert len(started) == 1
        assert captured["headless"] is False
        assert os.environ.get("DISPLAY") == ":123"
        assert captured["os"] == "linux"
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_browser_fetcher_windows_keeps_native_headless(monkeypatch):
    """Windows path: no Xvfb, ``headless=True`` is preserved."""
    import sys
    import types
    from unittest.mock import MagicMock

    from pagefetch import bootstrap as bootstrap_mod
    from pagefetch.fetching import browser as browser_mod

    monkeypatch.setattr(browser_mod.sys, "platform", "win32")

    spawn_attempts: list[object] = []

    class _ShouldNotStart:
        def __init__(self):
            spawn_attempts.append(self)

        def start(self):  # pragma: no cover - defensive
            raise AssertionError("XvfbDisplay must not be constructed on Windows")

    monkeypatch.setattr(browser_mod, "XvfbDisplay", _ShouldNotStart)

    captured: dict = {}

    class _FakeCamoufox:
        def __init__(self, **opts):
            captured.update(opts)

        async def __aenter__(self):
            return "fake-browser"

        async def __aexit__(self, *exc):
            return False

    fake_async_api = types.SimpleNamespace(AsyncCamoufox=_FakeCamoufox)
    monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.async_api", fake_async_api)

    async def _no_bootstrap():
        return None

    monkeypatch.setattr(bootstrap_mod, "bootstrap_browser", _no_bootstrap)

    proxy = MagicMock()
    proxy.browser_config.return_value = None

    fetcher = browser_mod.BrowserFetcher(
        semaphore=__import__("asyncio").Semaphore(1),
        timeout=30.0,
        retries=0,
        proxy=proxy,
        max_content_size=1_000_000,
    )
    try:
        await fetcher.start()
        assert spawn_attempts == []
        assert captured["headless"] is True
        assert captured["os"] == "windows"
    finally:
        await fetcher.close()
