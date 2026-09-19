"""Image metadata extraction."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import ImageInfo

# Attributes checked in priority order when ``src`` is missing or empty.
_SOURCE_ATTRIBUTES = (
    "src",
    "data-src",
    "data-lazy-src",
    "data-original",
    "data-high-res-src",
    "data-actualsrc",
)


def _first_from_srcset(value: Any) -> str | None:
    if not value:
        return None
    val = str(value).strip()
    if not val:
        return None
    if val.startswith("data:"):
        return val.split()[0].rstrip(",")
    parts = val.split(maxsplit=1)
    first_token = parts[0]
    if first_token.endswith(","):
        return first_token.rstrip(",")
    if len(parts) == 1:
        return first_token
    import re

    if re.match(r"^[\d.]+[wx](?:,|$|\s)", parts[1]):
        return first_token
    return first_token.rstrip(",")


def image_candidate(node: Tag) -> str | None:
    """Return the best URL candidate for an ``<img>`` element.

    Tries standard and common lazy-loading attributes first, then inspects
    ``srcset`` and ``data-srcset``. If enclosed in a ``<picture>`` tag,
    preceding ``<source>`` candidates are also considered.
    """
    for attr in _SOURCE_ATTRIBUTES:
        value = node.get(attr)
        if value:
            return str(value)
    for attr in ("srcset", "data-srcset"):
        candidate = _first_from_srcset(node.get(attr))
        if candidate:
            return candidate
    if node.parent and getattr(node.parent, "name", None) == "picture":
        for source in node.parent.find_all("source"):
            candidate = _first_from_srcset(source.get("srcset") or source.get("data-srcset"))
            if candidate:
                return candidate
    return None


def extract_images(soup: BeautifulSoup, base_url: str) -> list[ImageInfo]:
    images: list[ImageInfo] = []
    for image in soup.find_all("img"):
        source = image_candidate(image)
        if not source:
            continue
        images.append(
            ImageInfo(
                url=urljoin(base_url, source),
                alt=image.get("alt"),
                title=image.get("title"),
                index=len(images),
            )
        )
    return images