"""Image metadata extraction."""

from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import ImageInfo

_SOURCE_ATTRIBUTES = ("src", "data-src", "data-lazy-src", "data-original")


def extract_images(soup: BeautifulSoup, base_url: str) -> list[ImageInfo]:
    images: list[ImageInfo] = []
    for image in soup.find_all("img"):
        source = next((image.get(attr) for attr in _SOURCE_ATTRIBUTES if image.get(attr)), None)
        if not source and image.get("srcset"):
            source = str(image["srcset"]).split(",")[0].strip().split()[0]
        if not source:
            continue
        images.append(
            ImageInfo(
                url=urljoin(base_url, str(source)),
                alt=image.get("alt"),
                title=image.get("title"),
                index=len(images),
            )
        )
    return images

