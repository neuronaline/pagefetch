"""HTML processing orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from bs4 import BeautifulSoup

from ..models import ExpandedSection, ImageInfo, LinkInfo
from .cleaner import clean_html, unwrap_fallbacks
from .detector import ConfidenceReport, analyze_html
from .images import extract_images
from .links import extract_links
from .markdown import html_to_markdown
from .metadata import extract_metadata
from .sections import extract_sections


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
    sections: list[ExpandedSection]


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
    unwrap_fallbacks(raw_soup)
    links = extract_links(raw_soup, base_url)
    images = extract_images(raw_soup, base_url)
    cleaned = clean_html(raw_soup, cleaning_level, copy=False, fallbacks_unwrapped=True)
    sections = extract_sections(cleaned, base_url)
    markdown = html_to_markdown(cleaned, base_url)
    text_root = cleaned.body if cleaned.body is not None else cleaned
    text = "\n".join(line.strip() for line in text_root.get_text("\n").splitlines() if line.strip())
    if cleaning_level != "minimal" and len(text) < 50 and confidence.score >= 0.40:
        fallback_soup = clean_html(BeautifulSoup(html, "lxml"), "minimal", copy=False)
        fallback_root = fallback_soup.body if fallback_soup.body is not None else fallback_soup
        fallback_text = "\n".join(
            line.strip() for line in fallback_root.get_text("\n").splitlines() if line.strip()
        )
        if len(fallback_text) >= max(100, len(text) * 3):
            markdown = html_to_markdown(fallback_soup, base_url)
            text = fallback_text
            sections = extract_sections(fallback_soup, base_url)
    if not text.strip() and not markdown.strip() and confidence.score >= 0.20:
        reasons = tuple(dict.fromkeys((*confidence.reasons, "empty body after cleaning")))
        confidence = ConfidenceReport(
            score=min(confidence.score, 0.10),
            reasons=reasons,
            challenge=confidence.challenge,
            javascript_shell=True,
        )
    return ProcessedHTML(title, markdown, text, metadata, links, images, confidence, warnings, sections)
