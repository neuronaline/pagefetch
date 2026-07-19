"""Link extraction."""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..models import LinkInfo
from ..utils.urls import registrable_host


def extract_links(soup: BeautifulSoup, base_url: str) -> list[LinkInfo]:
    links: list[LinkInfo] = []
    seen: set[tuple[str, str, tuple[str, ...], str | None]] = set()
    base_site = registrable_host(base_url)
    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(base_url, str(anchor["href"]))
        if urlsplit(absolute).scheme not in {"http", "https"}:
            continue
        text = anchor.get_text(" ", strip=True)
        rel_value = anchor.get("rel") or []
        rel = rel_value.split() if isinstance(rel_value, str) else list(rel_value) if isinstance(rel_value, (list, tuple)) else []
        target = anchor.get("target")
        record = (text, absolute, tuple(rel), target)
        if record in seen:
            continue
        seen.add(record)
        links.append(
            LinkInfo(
                text=text,
                url=absolute,
                internal=registrable_host(absolute) == base_site,
                rel=rel,
                target=target,
                index=len(links),
            )
        )
    return links

