"""Conservative DOM cleaning."""

from __future__ import annotations

import re
from typing import Literal

from bs4 import BeautifulSoup, Tag

_NOISE_RE = re.compile(
    r"(?:^|[-_\s])(cookie(?:[-_\s]?banner|[-_\s]?consent)?|advert(?:isement)?|ad-slot|"
    r"tracking-pixel|modal-overlay)(?:$|[-_\s])",
    re.IGNORECASE,
)
_MAX_BLOCK_RE = re.compile(
    r"(?:^|[-_\s])(comments?|comment-section|disqus|share|sharing|social-share|"
    r"related|related-content|related-posts|recommend(?:ed|ation)?s?|"
    r"recommended-content)(?:$|[-_\s])",
    re.IGNORECASE,
)
_SITE_CHROME_RE = re.compile(
    r"(?:^|[-_\s])(site-header|site-footer|page-header|page-footer|masthead|"
    r"header|footer)(?:$|[-_\s])",
    re.IGNORECASE,
)
_VALID_CLEANING_LEVELS = frozenset({"minimal", "standard", "maximum"})


def clean_html(
    html: str | BeautifulSoup,
    cleaning_level: Literal["minimal", "standard", "maximum"] = "standard",
) -> BeautifulSoup:
    """Remove only strongly identified non-content from an HTML document.

    ``minimal`` applies only the universally safe rules. ``standard`` retains
    the conservative cookie/ad/tracking cleanup used by earlier releases.
    ``maximum`` additionally removes navigation, sidebars, site chrome, and
    blocks explicitly marked as comments, sharing, or related content.
    """
    if cleaning_level not in _VALID_CLEANING_LEVELS:
        raise ValueError(f"cleaning_level must be one of {sorted(_VALID_CLEANING_LEVELS)}")
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "lxml")

    # Build a visible-text snapshot before changing the tree. This lets the
    # noscript rule distinguish a fallback that is already rendered elsewhere
    # from content that exists only in a noscript subtree.
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
        common_noise = hidden or tiny_image
        standard_noise = aria_hidden or (
            _NOISE_RE.search(identity) is not None and text_length < 500
        )
        if common_noise or (cleaning_level != "minimal" and standard_noise):
            tag.decompose()

    if cleaning_level == "maximum":
        _remove_maximum_blocks(soup)

    return soup


def _inside_content(tag: Tag) -> bool:
    """Return whether a tag is inside an article's main content region."""
    return any(parent.name in {"main", "article"} for parent in tag.parents)


def _remove_maximum_blocks(soup: BeautifulSoup) -> None:
    """Remove explicit page chrome and auxiliary content blocks."""
    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag) or tag.parent is None:
            continue
        classes = " ".join(tag.get("class", []))
        identity = f"{tag.get('id', '')} {classes}"
        role = str(tag.get("role", "")).lower()
        is_navigation = tag.name == "nav" or role in {"navigation", "complementary"}
        is_sidebar = tag.name == "aside" or "sidebar" in classes.lower().split()
        is_site_chrome = (
            role in {"banner", "contentinfo"}
            or (_SITE_CHROME_RE.search(identity) is not None and not _inside_content(tag))
        )
        # Semantic headers/footers are site chrome only outside main/article;
        # article title/author header/footer elements must remain intact.
        is_outside_content_header_or_footer = (
            tag.name in {"header", "footer"}
            and not _inside_content(tag)
        )
        is_auxiliary_block = _MAX_BLOCK_RE.search(identity) is not None
        if (
            is_navigation
            or is_sidebar
            or is_site_chrome
            or is_outside_content_header_or_footer
            or is_auxiliary_block
        ):
            tag.decompose()
