from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import pagefetch.fetching.browser as browser_module
import pagefetch.fetching.readiness as readiness_module
from pagefetch.fetching.browser import BrowserFetcher, BrowserResponse
from pagefetch.fetching.http import TransportFailure
from pagefetch.proxy.providers import ProxySettings


def make_fetcher(*, timeout: float = 1.0, retries: int = 0) -> BrowserFetcher:
    return BrowserFetcher(
        asyncio.Semaphore(4),
        timeout=timeout,
        retries=retries,
        proxy=ProxySettings("none", None),
        max_content_size=100_000,
    )


@pytest.mark.asyncio
async def test_browser_blocks_media_and_fonts_but_allows_images(monkeypatch):
    decisions: dict[str, str] = {}

    class Route:
        def __init__(self, resource_type: str) -> None:
            self.request = SimpleNamespace(resource_type=resource_type)

        async def abort(self) -> None:
            decisions[self.request.resource_type] = "abort"

        async def continue_(self) -> None:
            decisions[self.request.resource_type] = "continue"

    main_frame = SimpleNamespace(url="https://example.com/", parent_frame=None)

    class Page:
        url = "https://example.com/"
        frames = [main_frame]
        closed = False

        def on(self, _event, _handler):
            return None

        async def route(self, _pattern, handler):
            for resource_type in ("font", "media", "image", "script"):
                await handler(Route(resource_type))

        async def goto(self, *_args, **_kwargs):
            return SimpleNamespace(status=200)

        async def evaluate(self, _script):
            # Size pre-check: return a short HTML string well under max_content_size.
            return "<html><body></body></html>"

        async def content(self):
            return "<html><body><p>Rendered</p></body></html>"

        async def close(self):
            self.closed = True

    async def stable(*_args, **_kwargs):
        return None

    async def scroll(*_args, **_kwargs):
        return False

    page = Page()
    fetcher = make_fetcher()
    fetcher._browser = SimpleNamespace(new_page=lambda: None)

    async def new_page():
        return page

    fetcher._browser.new_page = new_page
    monkeypatch.setattr(browser_module, "wait_for_stability", stable)
    monkeypatch.setattr(browser_module, "controlled_scroll", scroll)
    result = await fetcher._fetch_page_once("https://example.com/")
    assert result.status_code == 200
    assert decisions == {"font": "abort", "media": "abort", "image": "continue", "script": "continue"}
    assert page.closed is True


@pytest.mark.asyncio
async def test_same_site_iframe_is_inserted_and_external_frame_is_skipped():
    main = SimpleNamespace(url="https://news.example.co.uk/", parent_frame=None)

    class Frame:
        def __init__(self, url: str, text: str) -> None:
            self.url = url
            self.text = text
            self.parent_frame = main

        async def content(self) -> str:
            return f"<html><body><p>{self.text}</p></body></html>"

    internal = Frame("https://widgets.example.co.uk/frame", "Internal frame text")
    external = Frame("https://evil.co.uk/frame", "External frame text")
    page = SimpleNamespace(frames=[main, internal, external])
    html = """
    <HTML><BODY><p>Before</p>
    <iframe src="https://widgets.example.co.uk/frame"></iframe>
    <p>After</p><iframe src="https://evil.co.uk/frame"></iframe>
    </BODY></HTML>
    """
    output = await make_fetcher()._include_same_site_frames(
        page, html, "https://news.example.co.uk/", []
    )
    assert "Internal frame text" in output
    assert "External frame text" not in output
    assert output.index("widgets.example.co.uk/frame") < output.index("Internal frame text") < output.index("After")


@pytest.mark.asyncio
async def test_browser_timeout_is_structured(monkeypatch):
    fetcher = make_fetcher(timeout=0.01)

    async def slow(_url: str) -> BrowserResponse:
        await asyncio.sleep(1)
        raise AssertionError("unreachable")

    monkeypatch.setattr(fetcher, "_fetch_page_once", slow)
    with pytest.raises(TransportFailure) as caught:
        await fetcher._fetch_page("https://example.com/")
    assert caught.value.error.code == "browser_timeout"


@pytest.mark.asyncio
async def test_incomplete_browser_render_is_retried(monkeypatch):
    fetcher = make_fetcher(retries=1)
    calls = 0

    async def start():
        return None

    async def fetch_page(_url: str) -> BrowserResponse:
        nonlocal calls
        calls += 1
        return BrowserResponse("https://example.com/", 200, "<html><body>tiny</body></html>", [])

    async def no_sleep(_delay: float):
        return None

    monkeypatch.setattr(fetcher, "start", start)
    monkeypatch.setattr(fetcher, "_fetch_page", fetch_page)
    monkeypatch.setattr(browser_module.asyncio, "sleep", no_sleep)
    result = await fetcher.fetch("https://example.com/")
    assert calls == 2
    assert result.html.endswith("</html>")


@pytest.mark.asyncio
async def test_controlled_scroll_stops_after_unchanged_bottom(monkeypatch):
    class Page:
        y = 0
        returned_to_top = False

        async def evaluate(self, script: str):
            if "scrollHeight" in script and "scrollBy" in script:
                # Merged evaluate: capture pre-scroll values, then simulate scroll.
                result = {"height": 1000, "y": self.y, "viewport": 600}
                self.y = 900
                return result
            if "scrollHeight" in script:
                return {"height": 1000, "y": self.y, "viewport": 600}
            if "scrollBy" in script:
                self.y = 900
            elif "scrollTo" in script:
                self.returned_to_top = True
            return None

    async def no_sleep(_delay: float):
        return None

    monkeypatch.setattr(readiness_module.asyncio, "sleep", no_sleep)
    page = Page()
    limit_reached = await readiness_module.controlled_scroll(page, max_scrolls=6)
    assert limit_reached is False
    assert page.returned_to_top is True
