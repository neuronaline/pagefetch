"""Conservative DOM cleaning."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

_NOISE_RE = re.compile(
    r"(?:^|[-_\s])(cookie(?:[-_\s]?banner|[-_\s]?consent)?|advert(?:isement)?|ad-slot|"
    r"tracking-pixel|modal-overlay)(?:$|[-_\s])",
    re.IGNORECASE,
)


def clean_html(html: str | BeautifulSoup) -> BeautifulSoup:
    """Remove only strongly identified non-content while preserving page structure."""
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "lxml")
    # Build visible-text snapshot without creating a full parsed copy:
    # we skip noscript subtrees so the visibility check below matches
    # the same "rendered" view that enabled-JS users see.
    rendered_strings: list[str] = []
    for string in soup.stripped_strings:
        if not any(
            parent.name == "noscript"
            for parent in getattr(string, "parents", ())
            if hasattr(parent, "name")
        ):
            rendered_strings.append(string)
    visible_text = " ".join(rendered_strings)
    for tag in list(soup.find_all("noscript")):
        fallback = tag.get_text(" ", strip=True)
        if fallback and len(fallback) >= 20 and fallback in visible_text:
            tag.decompose()
    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag) or tag.parent is None:
            continue
        style = str(tag.get("style", "")).replace(" ", "").lower()
        hidden = tag.has_attr("hidden") or "display:none" in style or "visibility:hidden" in style
        text_length = len(tag.get_text(" ", strip=True))
        aria_hidden = str(tag.get("aria-hidden", "")).lower() == "true" and text_length < 200
        classes = " ".join(tag.get("class", []))
        identity = f"{tag.get('id', '')} {classes}"
        tiny_image = tag.name == "img" and str(tag.get("width")) == "1" and str(tag.get("height")) == "1"
        if hidden or aria_hidden or tiny_image or (_NOISE_RE.search(identity) and text_length < 500):
            tag.decompose()
    return soup
