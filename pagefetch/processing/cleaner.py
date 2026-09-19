"""Conservative DOM cleaning.

This module strips strongly identified non-content from HTML documents:
tracking pixels, cookie banners, share widgets, navigation, sidebars, and
site chrome. The rules are intentionally narrow — a tag is removed only
when there is unambiguous evidence that it is not part of the page's
editorial content. False positives (deleting real content) are far more
costly than false negatives (leaving some noise behind), so every
classifier errs on the side of caution.

Cleaning levels
---------------

- ``minimal``  — only universally safe removals (tracking pixels,
  already-redundant ``<noscript>`` fallbacks, ``aria-hidden`` chrome,
  ``display:none`` blocks).
- ``standard`` — adds cookie/consent banners, advertising slots, and
  comment sections. This is the default and matches the historical
  behavior.
- ``maximum``  — additionally strips navigation, asides, site chrome,
  and explicit comments / share / related-content blocks.

All levels are conservative: when in doubt, the tag is kept.
"""

from __future__ import annotations

import copy as _copy
import re
from typing import Literal

from bs4 import BeautifulSoup, Tag

_NOISE_RE = re.compile(
    r"(?:^|[-_\s])(cookie(?:[-_\s]?banner|[-_\s]?consent)?|advert(?:isement)?|ad-slot|"
    r"tracking-pixel|modal-overlay|onetrust(?:-consent-sdk)?|cybotcookiebot|"
    r"cookiebot|didomi(?:-host)?|klaro|sp_message|qc-cmp\d?|usercentrics|"
    r"trustarc|fc-consent-root|cookie-law-info|gdpr-banner|ccpa-banner)(?:$|[-_\s])",
    re.IGNORECASE,
)
_MAX_BLOCK_RE = re.compile(
    r"(?:^|[-_\s])(comments?|comment-section|disqus|share|sharing|social-share|"
    r"related|related-content|related-posts|recommend(?:ed|ation)?s?|"
    r"recommended-content)(?:$|[-_\s])",
    re.IGNORECASE,
)
_CONTENT_CONTAINER_RE = re.compile(
    r"(?:^|[-_\s])(entry[-_]content|post[-_]content|article[-_]body|story[-_]content|"
    r"article[-_]content|main[-_]content|post[-_]body)(?:$|[-_\s])",
    re.IGNORECASE,
)
# Tightened pattern: only match explicit site/page chrome class names, not
# the bare words ``header``/``footer`` (which would silently nuke a
# ``<div class="card-header">`` inside an article or product card).  We still
# rely on semantic ``role="banner"``/``role="contentinfo"`` and the
# ``<header>``/``<footer>`` tags to catch unstyled site chrome.
_SITE_CHROME_RE = re.compile(
    r"(?:^|[-_\s])(site-header|site-footer|page-header|page-footer|masthead)(?:$|[-_\s])",
    re.IGNORECASE,
)
# Recognized CSS declarations that hide a block from sighted users.  Operates
# on a pre-normalized style string (lower-cased, whitespace stripped, with
# declarations separated by ``;``) so matching stays cheap.  Anchoring on
# declaration boundaries prevents false positives like ``display:none-block``
# — a non-standard value but observed in the wild — from triggering the
# hidden-element branch.
_HIDDEN_STYLE_RE = re.compile(
    r"(?:^|;)(display|visibility):(none|hidden)(?:!important)?(?:;|$)",
    re.IGNORECASE,
)
_VALID_CLEANING_LEVELS = frozenset({"minimal", "standard", "maximum"})


def _class_list(tag: Tag) -> list[str]:
    """Return ``class`` as a list regardless of parser quirks.

    BeautifulSoup normally yields ``class`` as a list, but a few parsers
    (lxml-xml, html5lib in some modes) surface it as a plain string.
    ``" ".join("my-class")`` would then split into individual characters
    and silently corrupt the noise-regex matches downstream.
    ``get_attribute_list`` is the parser-agnostic accessor that always
    returns a list — ``or []`` covers the missing-attribute case.
    """
    return [str(item) for item in (tag.get_attribute_list("class") or []) if item]


def clean_html(
    html: str | BeautifulSoup,
    cleaning_level: Literal["minimal", "standard", "maximum"] = "standard",
) -> BeautifulSoup:
    """Remove only strongly identified non-content from an HTML document.

    ``minimal`` applies only the universally safe rules. ``standard`` retains
    the conservative cookie/ad/tracking cleanup used by earlier releases.
    ``maximum`` additionally removes navigation, sidebars, site chrome, and
    blocks explicitly marked as comments, sharing, or related content.

    .. note::

        ``clean_html`` never mutates its input. When ``html`` is a
        :class:`BeautifulSoup`, an independent copy is made (via BeautifulSoup's
        ``__copy__``) before any ``decompose()`` runs. Callers may safely reuse
        the original tree after cleaning.
    """
    if cleaning_level not in _VALID_CLEANING_LEVELS:
        raise ValueError(f"cleaning_level must be one of {sorted(_VALID_CLEANING_LEVELS)}")
    if not isinstance(html, str | BeautifulSoup):
        raise TypeError(
            f"clean_html expects str or BeautifulSoup, got {type(html).__name__}"
        )
    # Work on a copy so the caller's BeautifulSoup is never mutated by
    # ``decompose()`` side effects. BeautifulSoup implements ``__copy__`` to
    # walk the full subtree and produce a disconnected but fully independent
    # tree, which is exactly the contract we need.
    soup = _copy.copy(html) if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "lxml")

    # Build a visible-text snapshot only when noscript tags exist. This lets the
    # noscript rule distinguish a fallback that is already rendered elsewhere
    # from content that exists only in a noscript subtree without paying the cost
    # on documents that do not contain noscript blocks.
    noscripts = soup.find_all("noscript")
    if noscripts:
        rendered_strings: list[str] = [
            string for string in soup.stripped_strings
            if not any(
                parent.name == "noscript"
                for parent in getattr(string, "parents", ())
                if hasattr(parent, "name")
            )
        ]
        visible_text = " ".join(rendered_strings)
        for tag in list(noscripts):
            fallback = tag.get_text(" ", strip=True)
            if fallback and len(fallback) >= 20 and fallback in visible_text:
                tag.decompose()

    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag) or tag.parent is None:
            continue
        style = str(tag.get("style", "")).replace(" ", "").lower()
        hidden = tag.has_attr("hidden") or bool(_HIDDEN_STYLE_RE.search(style))
        tiny_image = tag.name == "img" and str(tag.get("width")) == "1" and str(tag.get("height")) == "1"
        common_noise = hidden or tiny_image
        # text_length requires traversing the whole subtree, which makes the
        # loop O(N * D) (effectively O(N^2) on nested trees).  Only run it
        # when the tag actually looks suspicious: either it carries an
        # aria-hidden hint or its class/id matches the noise regex.  Standard
        # body tags without any of those signals skip the text scan entirely.
        aria_hidden_attr = str(tag.get("aria-hidden", "")).lower() == "true"
        classes = " ".join(_class_list(tag))
        identity = f"{tag.get('id', '')} {classes}"
        matches_noise = _NOISE_RE.search(identity) is not None
        standard_noise = False
        if cleaning_level != "minimal" and (aria_hidden_attr or matches_noise):
            text_length = len(tag.get_text(" ", strip=True))
            if aria_hidden_attr:
                standard_noise = text_length < 200
            else:
                standard_noise = text_length < 500
        if common_noise or standard_noise:
            tag.decompose()

    if cleaning_level == "maximum":
        _remove_maximum_blocks(soup)

    return soup


def _inside_content(tag: Tag) -> bool:
    """Return whether a tag is inside an article's main content region."""
    for parent in tag.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name in {"main", "article"}:
            return True
        if str(parent.get("role", "")).lower() == "main":
            return True
        parent_id = str(parent.get("id", ""))
        parent_classes = " ".join(_class_list(parent))
        parent_ident = f"{parent_id} {parent_classes}"
        if _CONTENT_CONTAINER_RE.search(parent_ident) is not None:
            return True
    return False


def _remove_maximum_blocks(soup: BeautifulSoup) -> None:
    """Remove explicit page chrome and auxiliary content blocks."""
    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag) or tag.parent is None:
            continue
        classes = " ".join(_class_list(tag))
        identity = f"{tag.get('id', '')} {classes}"
        role = str(tag.get("role", "")).lower()
        is_navigation = tag.name == "nav" or role in {"navigation", "complementary"}
        is_sidebar = tag.name == "aside" or "sidebar" in classes.lower().split()
        is_site_chrome = (
            role in {"banner", "contentinfo"}
            or (_SITE_CHROME_RE.search(identity) is not None and not _inside_content(tag))
        )
        # Semantic headers/footers are site chrome only outside main/article;
        # preserve headers containing main heading tags (h1/h2) unless marked as site banner.
        has_primary_heading = tag.name == "header" and tag.find(["h1", "h2"]) is not None
        is_outside_content_header_or_footer = (
            tag.name in {"header", "footer"}
            and not _inside_content(tag)
            and not has_primary_heading
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
