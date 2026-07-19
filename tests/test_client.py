from __future__ import annotations

import asyncio

import httpx
import pytest

from pagefetch import PageFetch, PageFetchError
from pagefetch.fetching.http import HTTPFetcher


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
    assert result.final_url == "https://example.com/"


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

    async def forbidden(*args, **kwargs):
        raise AssertionError("browser must not be used")

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
    calls = 0

    async def fake_browser(url: str, proxy: str, status_code: int | None):
        nonlocal calls
        calls += 1
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
    assert calls == 1
    assert result.fetch_method == "browser"
    assert result.title == "Rendered Page"
    assert any("browser fallback" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_auto_preserves_low_confidence_http_content_if_browser_fails():
    client = PageFetch(mode="auto", cache_enabled=False)
    attach_transport(
        client,
        lambda request: httpx.Response(
            200,
            text="<html><body><p>Small but usable notice.</p></body></html>",
            headers={"Content-Type": "text/html"},
            request=request,
        ),
    )

    async def failed_browser(*args, **kwargs):
        from pagefetch.fetching.http import TransportFailure
        from pagefetch.models import FetchErrorInfo

        raise TransportFailure(FetchErrorInfo("browser_launch_error", "failed", True))

    client._fetch_browser = failed_browser
    async with client:
        result = await client.fetch("https://example.com/notice")
    assert result.success and result.fetch_method == "http"
    assert "Small but usable notice" in result.text
    assert any("Browser rendering failed" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_auto_falls_back_for_403(tmp_path):
    client = PageFetch(mode="auto", cache_enabled=False, retries_http=0)
    attach_transport(client, lambda request: httpx.Response(403, text="blocked", request=request))

    async def fake_browser(url: str, proxy: str, status_code: int | None):
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
    assert results[0] is results[2]


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
        xml, text = await client.fetch_many(["https://example.test/data.xml", "https://example.test/readme"])
    assert xml.fetch_method == "xml" and "```xml" in xml.markdown and "Value" in xml.text
    assert xml.html is not None and xml.html.startswith("<?xml")
    assert text.fetch_method == "text" and text.markdown == "Merhaba dünya\nİkinci satır"


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
async def test_raise_on_error_preserves_structured_error():
    client = PageFetch(mode="http", cache_enabled=False, raise_on_error=True, retries_http=0)
    attach_transport(client, lambda request: httpx.Response(404, request=request))
    async with client:
        with pytest.raises(PageFetchError) as caught:
            await client.fetch("https://example.com/missing")
    assert caught.value.error.code == "http_error"


@pytest.mark.asyncio
async def test_http_concurrency_is_bounded():
    active = 0
    peak = 0

    async def handler(request: httpx.Request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, text=rich_page(), headers={"Content-Type": "text/html"}, request=request)

    client = PageFetch(mode="http", cache_enabled=False, http_concurrency=2)
    attach_transport(client, handler)
    async with client:
        await client.fetch_many(f"https://example.test/{index}" for index in range(7))
    assert peak == 2


@pytest.mark.asyncio
async def test_failed_browser_result_does_not_discard_usable_http_content():
    client = PageFetch(mode="auto", cache_enabled=False)
    attach_transport(
        client,
        lambda request: httpx.Response(
            200,
            text="<html><body><p>Small but usable server notice.</p></body></html>",
            headers={"Content-Type": "text/html"},
            request=request,
        ),
    )

    async def blocked_browser(url: str, proxy: str, status_code: int | None):
        from pagefetch.models import FetchErrorInfo, FetchResult

        return FetchResult(
            url=url,
            success=False,
            proxy_provider=proxy,
            error=FetchErrorInfo("captcha_detected", "captcha", False),
        )

    client._fetch_browser = blocked_browser
    async with client:
        result = await client.fetch("https://example.com/notice")
    assert result.success and result.fetch_method == "http"
    assert "Small but usable" in result.text
    assert any("was not usable" in warning for warning in result.warnings)
