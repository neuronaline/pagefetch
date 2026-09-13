"""Image metadata extraction."""

from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import ImageInfo

# Attributes checked in priority order when ``src`` is missing or empty.
_SOURCE_ATTRIBUTES = ("src", "data-src", "data-lazy-src", "data-original")


def image_candidate(node: Tag) -> str | None:
    """Return the best URL candidate for an ``<img>`` element.

    Tries the standard ``src`` attribute first, then common lazy-loading
    fallbacks (``data-src``, ``data-lazy-src``, ``data-original``). When
    none are populated but ``srcset`` is present, the first URL listed
    in ``srcset`` is used. Returns ``None`` when no candidate exists so
    callers can decide whether to emit alt-only output or skip the node
    entirely.
    """
    for attr in _SOURCE_ATTRIBUTES:
        value = node.get(attr)
        if value:
            return str(value)
    srcset = node.get("srcset")
    if srcset:
        first = str(srcset).split(",", 1)[0].strip().split()
        if first:
            return first[0]
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