"""Document metadata extraction."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..constants import SAFE_RESPONSE_HEADERS

_PUBLISHED_KEYS = {
    "article:published_time",
    "date",
    "datepublished",
    "dc.date",
    "dc.date.issued",
}
_MODIFIED_KEYS = {"article:modified_time", "datemodified", "last-modified"}
def extract_metadata(
    soup: BeautifulSoup,
    base_url: str | None = None,
    response_headers: Mapping[str, str] | None = None,
) -> tuple[str | None, dict[str, Any], list[str]]:
    """Extract normalized document metadata while retaining structured values."""
    warnings: list[str] = []
    title_tag = soup.find("title")
    title = title_tag.get_text(" ", strip=True) if title_tag else None
    metadata: dict[str, Any] = {
        "description": None,
        "canonical_url": None,
        "author": None,
        "published_at": None,
        "modified_at": None,
        "language": None,
        "open_graph": {},
        "twitter_card": {},
        "json_ld": [],
        "headers": {},
    }
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        metadata["language"] = str(html_tag.get("lang"))
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name")
        value = tag.get("content")
        if not key or value is None:
            continue
        normalized = str(key).lower()
        value = str(value).strip()
        if normalized == "description":
            metadata["description"] = value
        elif normalized in {"author", "article:author"}:
            metadata["author"] = value
        if normalized in _PUBLISHED_KEYS and not metadata["published_at"]:
            metadata["published_at"] = value
        if normalized in _MODIFIED_KEYS and not metadata["modified_at"]:
            metadata["modified_at"] = value
        if normalized.startswith("og:"):
            metadata["open_graph"][normalized.removeprefix("og:")] = value
        elif normalized.startswith("twitter:"):
            metadata["twitter_card"][normalized.removeprefix("twitter:")] = value
        if normalized in {"og:title", "twitter:title"} and not title:
            title = value
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical and canonical.get("href"):
        canonical_url = str(canonical["href"])
        metadata["canonical_url"] = urljoin(base_url, canonical_url) if base_url else canonical_url
    json_ld: list[Any] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            json_ld.append(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, TypeError):
            warnings.append("Invalid JSON-LD block was ignored.")
    metadata["json_ld"] = json_ld
    if response_headers:
        metadata["headers"] = {
            str(key).lower(): str(value)
            for key, value in response_headers.items()
            if str(key).lower() in SAFE_RESPONSE_HEADERS
        }
    return title, metadata, warnings
