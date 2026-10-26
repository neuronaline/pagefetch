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
- ``standard`` — strips cookie/consent banners, advertising slots,
  navigation, sidebars, footers, and explicit comment/share sections.
- ``maximum``  — strips all standard non-content chrome plus any
  remaining auxiliary recommendation and related-content blocks.

All levels are conservative: when in doubt, the tag is kept.
"""

from __future__ import annotations

import copy as _copy
import re
from typing import Literal

from bs4 import BeautifulSoup, Tag

_NOISE_RE = re.compile(
    r"(?:^|[-_\s])(cookie[-_\s]?(?:banner|consent|notice|law|bar|modal|popup|dialog|overlay|wrap(?:per)?|container)|"
    r"consent[-_\s]?(?:banner|modal|popup|overlay|root|wrapper|bar|dialog)|"
    r"advert(?:isement)?|ad-slot|ad-container|ad-wrapper|ad-banner|"
    r"tracking-pixel|modal-overlay|onetrust(?:-consent-sdk)?|cybotcookiebot|"
    r"cookiebot|didomi(?:-host)?|klaro|sp_message|qc-cmp\d?|usercentrics|"
    r"trustarc|fc-consent-root|cookie-law-info|gdpr[-_\s]?(?:banner|modal|consent|overlay|popup)|"
    r"ccpa-banner)(?:$|[-_\s])",
    re.IGNORECASE,
)
_AD_RE = re.compile(
    r"(?:^|[-_\s])(advert(?:isement)?|ad-slot|ad-container|ad-wrapper|ad-banner|tracking-pixel)(?:$|[-_\s])",
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
    r"article[-_]content|main[-_]content|post[-_]body|article[-_]text|news[-_]content|"
    r"story[-_]body|page[-_]content|content[-_]area|content|prose|markdown-body|"
    r"documentation|article)(?:$|[-_\s])",
    re.IGNORECASE,
)
_SIDEBAR_RE = re.compile(
    r"(?:^|[-_\s])(sidebar|secondary|widget-area)(?:$|[-_\s])",
    re.IGNORECASE,
)
# Tightened pattern: only match explicit site/page chrome class names, not
# the bare words ``header``/``footer`` or ``page-header`` (which commonly wraps
# the primary ``<h1>`` article/page title in Bootstrap, Docusaurus, and blogs).
_SITE_CHROME_RE = re.compile(
    r"(?:^|[-_\s])(site-header|site-footer|page-footer|masthead)(?:$|[-_\s])",
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
    *,
    copy: bool = False,
) -> BeautifulSoup:
    """Remove only strongly identified non-content from an HTML document.

    ``minimal`` drops only hidden elements, 1x1 tracking pixels, and empty text blocks.
    ``standard`` (default) adds ads, consent banners, and aria-hidden overlays while preserving
    substantive article text.
    ``maximum`` additionally removes navigation, sidebars, site chrome, and
    blocks explicitly marked as comments, sharing, or related content.

    :param html: Input HTML string or parsed BeautifulSoup tree.
    :param cleaning_level: Aggressiveness of noise reduction.
    :param copy: When False (default) and *html* is a :class:`BeautifulSoup`, cleaning operates
        in-place directly on the tree to avoid deep copying overhead. Pass ``copy=True`` when the
        caller requires an independent copy and wishes to preserve the original tree unmodified.
    """
    if cleaning_level not in _VALID_CLEANING_LEVELS:
        raise ValueError(f"cleaning_level must be one of {sorted(_VALID_CLEANING_LEVELS)}")
    if not isinstance(html, str | BeautifulSoup):
        raise TypeError(
            f"clean_html expects str or BeautifulSoup, got {type(html).__name__}"
        )
    soup = (
        _copy.deepcopy(html)
        if (copy and isinstance(html, BeautifulSoup))
        else (html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "lxml"))
    )

    # Build a visible-text snapshot only when noscript tags exist.
    noscripts = soup.find_all("noscript")
    if noscripts:
        rendered_strings: list[str] = [
            stripped
            for node in soup.strings
            if (stripped := str(node).strip())
            and not any(
                getattr(parent, "name", None) in {"noscript", "script", "style", "template"}
                for parent in getattr(node, "parents", ())
            )
        ]
        visible_text = " ".join(rendered_strings)
        for tag in list(noscripts):
            if not tag.find(True) and "<" in str(tag.string or ""):
                frag = BeautifulSoup(str(tag.string), "lxml")
                if frag.find(True) is not None:
                    tag.clear()
                    frag_root = frag.body or frag
                    for child in list(frag_root.contents):
                        tag.append(child.extract())
            fallback = tag.get_text(" ", strip=True)
            fallback_lower = fallback.lower()
            is_js_notice = len(fallback) < 350 and any(
                phrase in fallback_lower
                for phrase in (
                    "enable javascript",
                    "requires javascript",
                    "javascript is disabled",
                    "javascript to run",
                    "turn on javascript",
                    "support javascript",
                )
            )
            has_tracking_iframe = bool(
                tag.find("iframe", src=re.compile(r"googletagmanager|analytics|facebook|doubleclick", re.I))
            )
            if is_js_notice or has_tracking_iframe or (fallback and len(fallback) >= 20 and fallback in visible_text):
                tag.decompose()
            elif len(visible_text) < 250 and len(fallback) >= 80:
                tag.unwrap()

    is_standard_or_max = cleaning_level in ("standard", "maximum")
    is_maximum = cleaning_level == "maximum"

    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag) or tag.parent is None or tag.attrs is None:
            continue
        style = str(tag.get("style", "")).replace(" ", "").lower()
        hidden = tag.has_attr("hidden") or bool(_HIDDEN_STYLE_RE.search(style))
        tiny_image = tag.name == "img" and str(tag.get("width")) == "1" and str(tag.get("height")) == "1"
        if hidden or tiny_image:
            tag.decompose()
            continue
        if not is_standard_or_max or tag.name in {"html", "body", "h1", "h2", "h3", "h4", "h5", "h6"}:
            continue

        classes = " ".join(_class_list(tag))
        identity = f"{tag.get('id', '')} {classes}"
        aria_hidden_attr = str(tag.get("aria-hidden", "")).lower() == "true"

        if _NOISE_RE.search(identity) is not None:
            is_ad = _AD_RE.search(identity) is not None
            is_safe_container = (
                tag.name in {"main", "article"}
                or _CONTENT_CONTAINER_RE.search(identity) is not None
                or tag.find(["main", "article"]) is not None
                or (not is_ad and _inside_content(tag))
            )
            if not is_safe_container:
                tag.decompose()
                continue
        elif aria_hidden_attr and len(tag.get_text(" ", strip=True)) < 200:
            tag.decompose()
            continue

        if _should_remove_boilerplate_tag(tag, identity, is_maximum=is_maximum):
            tag.decompose()

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


def _should_remove_boilerplate_tag(tag: Tag, identity: str, *, is_maximum: bool = False) -> bool:
    """Return True if *tag* is unambiguous navigation, sidebar, chrome, or auxiliary boilerplate."""
    role = str(tag.get("role", "")).lower()
    in_content = _inside_content(tag)
    has_primary_heading = tag.find(["h1", "h2"]) is not None

    is_navigation = (tag.name == "nav" and not in_content) or role in {"navigation", "menubar"}
    is_sidebar = (
        tag.name == "aside"
        or role == "complementary"
        or _SIDEBAR_RE.search(identity) is not None
    ) and not in_content
    is_site_chrome = (
        (role in {"banner", "contentinfo"} or _SITE_CHROME_RE.search(identity) is not None)
        and not in_content
        and not has_primary_heading
    )
    is_outside_content_header_or_footer = (
        tag.name in {"header", "footer"}
        and not in_content
        and not has_primary_heading
    )
    is_auxiliary_block = False
    if is_maximum and _MAX_BLOCK_RE.search(identity) is not None:
        is_safe_article = (
            tag.name in {"html", "body", "main", "article"}
            or _CONTENT_CONTAINER_RE.search(identity) is not None
            or tag.find(["main", "article"]) is not None
            or has_primary_heading
        )
        is_auxiliary_block = not is_safe_article
    return bool(
        is_navigation
        or is_sidebar
        or is_site_chrome
        or is_outside_content_header_or_footer
        or is_auxiliary_block
    )


def _remove_boilerplate(soup: BeautifulSoup, *, is_maximum: bool = False) -> None:
    """Remove explicit page chrome, navigation, footers, sidebars, and auxiliary content blocks."""
    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag) or tag.parent is None or tag.attrs is None:
            continue
        identity = f"{tag.get('id', '')} {' '.join(_class_list(tag))}"
        if _should_remove_boilerplate_tag(tag, identity, is_maximum=is_maximum):
            tag.decompose()


_remove_maximum_blocks = _remove_boilerplate
