"""Static page structure extraction for developer inspection.

The extractor walks the DOM and reports a bounded tree of element summaries,
external stylesheet/script references, and inline ``<style>`` / ``<script>``
previews. It deliberately avoids:

* downloading external CSS or JavaScript files,
* capturing runtime state, event listeners, network traffic, or Shadow DOM,
* unbounded recursion, text, or inline source length.

These limits keep ``FetchResult.structure`` predictable in size regardless of
the page being inspected.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, Doctype, Tag

from ..models import (
    InlineScript,
    InlineStylesheet,
    PageStructure,
    ScriptInfo,
    StructureNode,
    StylesheetInfo,
)

# Default safety limits. Callers can override via ``StructureLimits`` if a
# project genuinely needs deeper trees or longer inline previews.
_DEFAULT_MAX_DEPTH = 12
_DEFAULT_MAX_NODES = 800
_DEFAULT_TEXT_PREVIEW = 120
_DEFAULT_INLINE_SOURCE_LIMIT = 4_096


@dataclass(slots=True, frozen=True)
class StructureLimits:
    """Bounds for the structure extractor.

    ``inline_source_limit`` is applied per ``<style>`` / ``<script>`` block.
    """

    max_depth: int = _DEFAULT_MAX_DEPTH
    max_nodes: int = _DEFAULT_MAX_NODES
    text_preview: int = _DEFAULT_TEXT_PREVIEW
    inline_source_limit: int = _DEFAULT_INLINE_SOURCE_LIMIT


def extract_structure(
    html_or_soup: str | BeautifulSoup,
    base_url: str | None = None,
    *,
    limits: StructureLimits | None = None,
) -> PageStructure:
    """Return a bounded :class:`PageStructure` summary of the supplied HTML.

    *base_url* is used to absolutize external asset references; pass ``None``
    when the document was loaded from a local file or the absolute URLs are
    already present in the markup.
    """
    limits = limits or StructureLimits()
    soup = (
        html_or_soup
        if isinstance(html_or_soup, BeautifulSoup)
        else BeautifulSoup(html_or_soup, "lxml")
    )

    root, node_count, truncated = _walk(soup, limits=limits)

    stylesheets, inline_styles = _collect_styles(soup, base_url, limits=limits)
    scripts, inline_scripts = _collect_scripts(soup, base_url, limits=limits)

    truncated = truncated or any(item.truncated for item in inline_styles) or any(
        item.truncated for item in inline_scripts
    )

    return PageStructure(
        root=root,
        stylesheets=stylesheets,
        inline_styles=inline_styles,
        scripts=scripts,
        inline_scripts=inline_scripts,
        truncated=truncated,
        node_count=node_count,
        max_depth=limits.max_depth,
    )


def _walk(
    soup: BeautifulSoup,
    *,
    limits: StructureLimits,
    depth: int = 0,
    counters: list[int] | None = None,
) -> tuple[StructureNode | None, int, bool]:
    """Build the bounded DOM tree starting at the document root."""
    counters = counters if counters is not None else [0]
    root_tag = _first_element(soup)
    if root_tag is None:
        return None, 0, False
    node, truncated = _describe(root_tag, depth=depth, counters=counters, limits=limits)
    return node, counters[0], truncated


def _first_element(soup: BeautifulSoup) -> Tag | None:
    """Return the first non-doctype / non-comment element in the document."""
    for child in soup.contents:
        if isinstance(child, Doctype):
            continue
        if isinstance(child, Comment):
            continue
        if isinstance(child, Tag):
            return child
    # Fall back to ``find`` for documents where the root is not the first child.
    if soup.html:
        return soup.html
    return soup.find()


def _describe(
    tag: Tag,
    *,
    depth: int,
    counters: list[int],
    limits: StructureLimits,
) -> tuple[StructureNode, bool]:
    """Recursively describe *tag* within the configured depth/node budget."""
    counters[0] += 1
    truncated = False
    text = _short_text(tag, limits.text_preview)
    attrs = _filtered_attrs(tag)
    children: list[StructureNode] = []
    if depth + 1 < limits.max_depth and counters[0] < limits.max_nodes:
        for child in tag.children:
            if not isinstance(child, Tag):
                continue
            child_node, child_truncated = _describe(
                child,
                depth=depth + 1,
                counters=counters,
                limits=limits,
            )
            children.append(child_node)
            if child_truncated:
                truncated = True
            if counters[0] >= limits.max_nodes:
                truncated = True
                break
    selector = _build_selector(tag, attrs)
    node = StructureNode(tag=tag.name, selector=selector, attrs=attrs, text=text, children=children)
    if depth + 1 >= limits.max_depth:
        truncated = True
    return node, truncated


def _short_text(tag: Tag, limit: int) -> str:
    """Return a collapsed, length-capped preview of the element's own text."""
    pieces: list[str] = []
    for child in tag.children:
        if isinstance(child, str):
            stripped = " ".join(child.split())
            if stripped:
                pieces.append(stripped)
            if sum(len(part) for part in pieces) >= limit:
                break
    text = " ".join(pieces).strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


_ATTR_BLACKLIST = frozenset(
    {
        "style",  # noisy inline CSS — kept in inline_styles when needed
        "onclick",
        "ondblclick",
        "onload",
        "onerror",
        "onmouseover",
        "onfocus",
        "onblur",
        "onsubmit",
    }
)


def _filtered_attrs(tag: Tag) -> dict[str, str]:
    """Return a JSON-safe attribute map, dropping event handlers and styles."""
    attrs: dict[str, str] = {}
    for key, value in tag.attrs.items():
        if key.startswith("on") or key in _ATTR_BLACKLIST:
            continue
        attrs[str(key)] = _stringify(value)
    return attrs


def _stringify(value: object) -> str:
    if isinstance(value, list):
        return " ".join(str(part) for part in value)
    return str(value)


def _build_selector(tag: Tag, attrs: dict[str, str]) -> str:
    """Build a compact CSS selector for *tag* using id/class hints."""
    parts: list[str] = [tag.name]
    ident = attrs.get("id")
    if ident:
        parts.append(f"#{ident}")
    classes = attrs.get("class")
    if classes:
        parts.extend(f".{token}" for token in classes.split() if token)
    return "".join(parts)


def _collect_styles(
    soup: BeautifulSoup,
    base_url: str | None,
    *,
    limits: StructureLimits,
) -> tuple[list[StylesheetInfo], list[InlineStylesheet]]:
    stylesheets: list[StylesheetInfo] = []
    inline_styles: list[InlineStylesheet] = []
    for tag in soup.find_all(["link", "style"]):
        if tag.name == "link":
            rel = tag.get("rel") or []
            rel_values = rel if isinstance(rel, list) else [str(rel)]
            if "stylesheet" not in {str(value).lower() for value in rel_values}:
                continue
            href = tag.get("href")
            if not href:
                continue
            url = urljoin(base_url, str(href)) if base_url else str(href)
            stylesheets.append(
                StylesheetInfo(
                    url=url,
                    media=str(tag.get("media")) if tag.get("media") else None,
                    integrity=str(tag.get("integrity")) if tag.get("integrity") else None,
                    crossorigin=str(tag.get("crossorigin")) if tag.get("crossorigin") else None,
                )
            )
        else:
            content = _bounded_text(tag, limits.inline_source_limit)
            inline_styles.append(
                InlineStylesheet(
                    content=content,
                    truncated=len(_full_text(tag)) > limits.inline_source_limit,
                )
            )
    return stylesheets, inline_styles


def _collect_scripts(
    soup: BeautifulSoup,
    base_url: str | None,
    *,
    limits: StructureLimits,
) -> tuple[list[ScriptInfo], list[InlineScript]]:
    scripts: list[ScriptInfo] = []
    inline_scripts: list[InlineScript] = []
    for tag in soup.find_all("script"):
        src = tag.get("src")
        if src:
            url = urljoin(base_url, str(src)) if base_url else str(src)
            scripts.append(
                ScriptInfo(
                    url=url,
                    type=str(tag.get("type")) if tag.get("type") else None,
                    async_=tag.has_attr("async"),
                    defer=tag.has_attr("defer"),
                    integrity=str(tag.get("integrity")) if tag.get("integrity") else None,
                    crossorigin=str(tag.get("crossorigin")) if tag.get("crossorigin") else None,
                )
            )
            continue
        content = _bounded_text(tag, limits.inline_source_limit)
        inline_scripts.append(
            InlineScript(
                content=content,
                truncated=len(_full_text(tag)) > limits.inline_source_limit,
                type=str(tag.get("type")) if tag.get("type") else None,
            )
        )
    return scripts, inline_scripts


def _bounded_text(tag: Tag, limit: int) -> str:
    text = _full_text(tag)
    if len(text) <= limit:
        return text
    return text[:limit]


def _full_text(tag: Tag) -> str:
    if tag.string is not None:
        return str(tag.string)
    return tag.get_text()


__all__ = ["StructureLimits", "extract_structure"]
