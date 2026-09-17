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
_DEFAULT_MAX_ASSET_ITEMS = 100
_DEFAULT_MAX_ASSET_BYTES = 64 * 1024

# Attribute whitelist used in compact mode to keep the tree payload small.
# We keep the selectors' building blocks (``id``/``class``) plus the fields an
# LLM or developer would reasonably look at first (``role``,
# ``data-testid``/``data-test``/``data-id``, ``aria-*``, ``href``,
# ``name``/``type``/``value``/``placeholder``/``title``/``alt``/``src``/
# ``for``/``disabled``/``hidden``/``target``/``rel``).
_COMPACT_ATTR_WHITELIST = frozenset(
    {
        "id",
        "class",
        "role",
        "data-testid",
        "data-test",
        "data-id",
        "aria-label",
        "aria-labelledby",
        "aria-describedby",
        "href",
        "name",
        "type",
        "value",
        "placeholder",
        "title",
        "alt",
        "src",
        "for",
        "disabled",
        "hidden",
        "target",
        "rel",
    }
)


@dataclass(slots=True, frozen=True)
class StructureLimits:
    """Bounds for the structure extractor.

    ``inline_source_limit`` is applied per ``<style>`` / ``<script>`` block.
    ``compact`` switches on the LLM/developer-friendly output: the selector
    triples are still emitted for developer ergonomics, but verbose fields
    (``unique_selector`` duplicates, all inline script content, non-essential
    attributes) are dropped or trimmed.
    """

    max_depth: int = _DEFAULT_MAX_DEPTH
    max_nodes: int = _DEFAULT_MAX_NODES
    text_preview: int = _DEFAULT_TEXT_PREVIEW
    inline_source_limit: int = _DEFAULT_INLINE_SOURCE_LIMIT
    max_asset_items: int = _DEFAULT_MAX_ASSET_ITEMS
    max_asset_bytes: int = _DEFAULT_MAX_ASSET_BYTES
    compact: bool = False

    def __post_init__(self) -> None:
        for name in ("max_depth", "max_nodes", "text_preview", "inline_source_limit", "max_asset_items", "max_asset_bytes"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(slots=True)
class _AssetBudget:
    items_left: int
    bytes_left: int
    truncated: bool = False

    def reserve_item(self) -> bool:
        if self.items_left <= 0:
            self.truncated = True
            return False
        self.items_left -= 1
        return True

    def content(self, text: str, per_item_limit: int) -> tuple[str, bool]:
        # Cap by *per_item_limit* and the remaining budget in one shot, then
        # decode the truncated byte slice so the result is always on a valid
        # UTF-8 character boundary (cheaper and safer than the previous
        # char-by-char binary search).
        limit = min(per_item_limit, self.bytes_left)
        encoded = text.encode("utf-8", errors="replace")
        truncated = len(encoded) > limit
        if truncated:
            encoded = encoded[:limit]
            content = encoded.decode("utf-8", errors="ignore")
        else:
            content = text
        self.bytes_left -= len(encoded)
        self.truncated = self.truncated or truncated
        return content, truncated


def extract_structure(
    html_or_soup: str | BeautifulSoup,
    base_url: str | None = None,
    *,
    limits: StructureLimits | None = None,
) -> PageStructure:
    """Return a bounded :class:`PageStructure` summary of the supplied HTML.

    *base_url* is used to absolutize external asset references; pass ``None``
    when the document was loaded from a local file or the absolute URLs are
    already present in the markup. Pass ``StructureLimits(compact=True)`` (or
    rely on the caller's choice in :meth:`PageFetch.fetch`) to receive the
    smaller, LLM-friendly variant.
    """
    limits = limits or StructureLimits()
    soup = (
        html_or_soup
        if isinstance(html_or_soup, BeautifulSoup)
        else BeautifulSoup(html_or_soup, "lxml")
    )

    root, node_count, truncated = _walk(soup, limits=limits)

    budget = _AssetBudget(limits.max_asset_items, limits.max_asset_bytes)
    stylesheets, inline_styles = _collect_styles(soup, base_url, limits=limits, budget=budget)
    scripts, inline_scripts = _collect_scripts(soup, base_url, limits=limits, budget=budget)

    truncated = budget.truncated or truncated or any(item.truncated for item in inline_styles) or any(
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
    # Single-pass pre-count of IDs and tag names avoids O(N^2) select queries per node
    id_counts: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    for el in soup.find_all(True):
        if isinstance(el, Tag):
            tag_counts[el.name] = tag_counts.get(el.name, 0) + 1
            ident = el.get("id")
            if ident and isinstance(ident, str):
                id_counts[ident] = id_counts.get(ident, 0) + 1
    node, truncated = _describe(
        root_tag,
        depth=depth,
        counters=counters,
        limits=limits,
        id_counts=id_counts,
        tag_counts=tag_counts,
    )
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
    parent_path: str = "",
    id_counts: dict[str, int] | None = None,
    tag_counts: dict[str, int] | None = None,
) -> tuple[StructureNode, bool]:
    """Recursively describe *tag* within the configured depth/node budget."""
    counters[0] += 1
    truncated = False
    text = _short_text(tag, limits.text_preview)
    attrs = _filtered_attrs(tag, compact=limits.compact)
    selector = _build_selector(tag, attrs)
    segment = _path_segment(tag, attrs)
    path = f"{parent_path} > {segment}" if parent_path else segment
    # In compact mode unique_selector is skipped. In normal mode, derive it in O(1)
    # using precomputed single-pass counts instead of O(N^2) full-document CSS queries.
    if limits.compact:
        unique_selector = ""
    else:
        ident = attrs.get("id")
        if ident and id_counts and id_counts.get(ident, 0) == 1:
            unique_selector = f"#{_css_escape(ident)}"
        elif tag_counts and tag_counts.get(tag.name, 0) == 1:
            unique_selector = tag.name
        else:
            unique_selector = path
    children: list[StructureNode] = []
    has_element_children = any(isinstance(child, Tag) for child in tag.children)
    if depth + 1 < limits.max_depth and counters[0] < limits.max_nodes:
        for child in tag.children:
            if not isinstance(child, Tag):
                continue
            child_node, child_truncated = _describe(
                child,
                depth=depth + 1,
                counters=counters,
                limits=limits,
                parent_path=path,
                id_counts=id_counts,
                tag_counts=tag_counts,
            )
            children.append(child_node)
            if child_truncated:
                truncated = True
            if counters[0] >= limits.max_nodes:
                truncated = True
                break
    node = StructureNode(
        tag=tag.name,
        selector=selector,
        attrs=attrs,
        text=text,
        children=children,
        path=path,
        unique_selector=unique_selector,
    )
    if depth + 1 >= limits.max_depth and has_element_children:
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


def _filtered_attrs(tag: Tag, *, compact: bool = False) -> dict[str, str]:
    """Return a JSON-safe attribute map, dropping event handlers and styles.

    In *compact* mode the map is additionally filtered to
    :data:`_COMPACT_ATTR_WHITELIST` so the tree payload stays focused on the
    fields an LLM or developer is most likely to ask about (selectors, ARIA,
    test hooks, form bindings). ``id`` and ``class`` are always kept because
    they are the building blocks of the generated CSS selectors.
    """
    attrs: dict[str, str] = {}
    for key, value in tag.attrs.items():
        if key.startswith("on") or key in _ATTR_BLACKLIST:
            continue
        if compact and key not in _COMPACT_ATTR_WHITELIST and not key.startswith("aria-"):
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
        parts.append(f"#{_css_escape(ident)}")
    classes = attrs.get("class")
    if classes:
        parts.extend(f".{_css_escape(token)}" for token in classes.split() if token)
    return "".join(parts)


def _path_segment(tag: Tag, attrs: dict[str, str]) -> str:
    """Build a deterministic path segment that identifies *tag* among siblings."""
    ident = attrs.get("id")
    if ident:
        return f"{tag.name}#{_css_escape(ident)}"

    segment = tag.name
    classes = attrs.get("class")
    if classes:
        segment += "".join(f".{_css_escape(token)}" for token in classes.split() if token)

    siblings = [sibling for sibling in tag.parent.children if isinstance(sibling, Tag)] if tag.parent else []
    matches = [sibling for sibling in siblings if sibling.name == tag.name]
    if len(matches) > 1:
        segment += f":nth-of-type({matches.index(tag) + 1})"
    return segment


def _unique_selector(tag: Tag, selector: str, path: str) -> str:
    """Return the shortest generated selector that uniquely matches *tag*."""
    soup = tag
    while soup.parent is not None:
        soup = soup.parent
    select = getattr(soup, "select", None)
    if not callable(select):
        return path
    for candidate in (selector, path):
        try:
            matches = select(candidate)
        except Exception:
            continue
        if len(matches) == 1 and matches[0] is tag:
            return candidate
    return path


def _css_escape(value: str) -> str:
    """Escape an identifier for safe use in a generated CSS selector."""
    escaped: list[str] = []
    for index, char in enumerate(value):
        if char.isalnum() or char in {"-", "_"}:
            if index == 0 and char.isdigit():
                escaped.append(f"\\3{char} ")
            else:
                escaped.append(char)
        else:
            escaped.append(f"\\{char}")
    return "".join(escaped)


def _collect_styles(
    soup: BeautifulSoup,
    base_url: str | None,
    *,
    limits: StructureLimits,
    budget: _AssetBudget,
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
            if not budget.reserve_item():
                break
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
            if not budget.reserve_item():
                break
            content, truncated = budget.content(_full_text(tag), limits.inline_source_limit)
            inline_styles.append(
                InlineStylesheet(
                    content=content,
                    truncated=truncated,
                )
            )
    return stylesheets, inline_styles


def _collect_scripts(
    soup: BeautifulSoup,
    base_url: str | None,
    *,
    limits: StructureLimits,
    budget: _AssetBudget,
) -> tuple[list[ScriptInfo], list[InlineScript]]:
    scripts: list[ScriptInfo] = []
    inline_scripts: list[InlineScript] = []
    for tag in soup.find_all("script"):
        if not budget.reserve_item():
            break
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
        content, truncated = budget.content(_full_text(tag), limits.inline_source_limit)
        inline_scripts.append(
            InlineScript(
                content=content,
                truncated=truncated,
                type=str(tag.get("type")) if tag.get("type") else None,
            )
        )
    return scripts, inline_scripts


def _full_text(tag: Tag) -> str:
    if tag.string is not None:
        return str(tag.string)
    return tag.get_text()


__all__ = ["StructureLimits", "extract_structure"]
