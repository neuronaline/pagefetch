"""Link extraction."""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString
from bs4.element import CData, TemplateString

from ..models import LinkInfo
from ..utils.urls import registrable_host

_LINK_TEXT_TYPES = (NavigableString, CData, TemplateString)


def extract_links(soup: BeautifulSoup, base_url: str) -> list[LinkInfo]:
    links: list[LinkInfo] = []
    seen: set[tuple[str, str, tuple[str, ...], str | None]] = set()
    base_site = registrable_host(base_url)
    host_cache: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        if anchor.find_parent("template") is not None:
            continue
        absolute = urljoin(base_url, str(anchor["href"]))
        if urlsplit(absolute).scheme not in {"http", "https"}:
            continue
        text = anchor.get_text(" ", strip=True, types=_LINK_TEXT_TYPES)
        rel_value = anchor.get("rel") or []
        rel = rel_value.split() if isinstance(rel_value, str) else list(rel_value) if isinstance(rel_value, list | tuple) else []
        target = anchor.get("target")
        record = (text, absolute, tuple(rel), target)
        if record in seen:
            continue
        seen.add(record)
        try:
            rh = host_cache[absolute]
        except KeyError:
            rh = host_cache[absolute] = registrable_host(absolute)
        links.append(
            LinkInfo(
                text=text,
                url=absolute,
                internal=rh == base_site,
                rel=rel,
                target=target,
                index=len(links),
            )
        )
    return links

