"""HTML processing orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from bs4 import BeautifulSoup

from ..models import ImageInfo, LinkInfo
from .cleaner import clean_html
from .detector import ConfidenceReport, analyze_html
from .images import extract_images
from .links import extract_links
from .markdown import html_to_markdown
from .metadata import extract_metadata


@dataclass(slots=True)
class ProcessedHTML:
    title: str | None
    markdown: str
    text: str
    metadata: dict[str, Any]
    links: list[LinkInfo]
    images: list[ImageInfo]
    confidence: ConfidenceReport
    warnings: list[str]


def process_html(
    html: str,
    base_url: str,
    response_headers: Mapping[str, str] | None = None,
    soup: BeautifulSoup | None = None,
    confidence: ConfidenceReport | None = None,
    cleaning_level: Literal["minimal", "standard", "maximum"] = "standard",
) -> ProcessedHTML:
    """Extract a high-fidelity structured representation from HTML.

    *soup* and *confidence* avoid redundant parse/analysis when the caller
    already has a BeautifulSoup tree or ConfidenceReport from an earlier step.
    *cleaning_level* selects how aggressively non-content DOM (cookie banners,
    navigation, sidebars, comments) is removed before extraction. ``standard``
    preserves the conservative behavior of earlier releases; ``minimal`` only
    applies universally safe rules; ``maximum`` additionally strips navigation,
    asides, site chrome, and explicit comments/share/related blocks.
    """
    raw_soup = soup or BeautifulSoup(html, "lxml")
    title, metadata, warnings = extract_metadata(raw_soup, base_url, response_headers)
    if confidence is None:
        confidence = analyze_html(html, soup=raw_soup)
    cleaned = clean_html(raw_soup, cleaning_level)
    links = extract_links(cleaned, base_url)
    images = extract_images(cleaned, base_url)
    markdown = html_to_markdown(cleaned, base_url)
    text = "\n".join(line.strip() for line in cleaned.get_text("\n").splitlines() if line.strip())
    return ProcessedHTML(title, markdown, text, metadata, links, images, confidence, warnings)
