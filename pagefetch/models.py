"""Structured public result models."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger("pagefetch.models")

# Compact serialization bounds. Kept conservative so the LLM-facing JSON
# payload never balloons even for pages with megabyte-sized inline scripts.
_COMPACT_INLINE_PREVIEW = 160
_COMPACT_STRUCTURE_SCHEMA = "pagefetch.structure.compact.v1"


@dataclass(slots=True)
class LinkInfo:
    text: str
    url: str
    internal: bool
    rel: list[str]
    target: str | None
    index: int


@dataclass(slots=True)
class ImageInfo:
    url: str
    alt: str | None
    title: str | None
    index: int


@dataclass(slots=True)
class FetchErrorInfo:
    code: str
    message: str
    retryable: bool
    exception_type: str | None = None


@dataclass(slots=True)
class StructureNode:
    """A DOM node described with scraper-oriented selector paths."""

    tag: str
    selector: str
    attrs: dict[str, str]
    text: str
    children: list[StructureNode]
    path: str = ""
    unique_selector: str = ""


@dataclass(slots=True)
class StylesheetInfo:
    """An external stylesheet reference discovered on the page."""

    url: str
    media: str | None = None
    integrity: str | None = None
    crossorigin: str | None = None


@dataclass(slots=True)
class InlineStylesheet:
    """An inline ``<style>`` block with a bounded preview of its contents."""

    content: str
    truncated: bool


@dataclass(slots=True)
class ScriptInfo:
    """An external script reference discovered on the page."""

    url: str
    type: str | None = None
    async_: bool = False
    defer: bool = False
    integrity: str | None = None
    crossorigin: str | None = None

    def __init__(
        self,
        url: str,
        *,
        type: str | None = None,
        async_: bool = False,
        defer: bool = False,
        integrity: str | None = None,
        crossorigin: str | None = None,
    ) -> None:
        self.url = url
        self.type = type
        self.async_ = async_
        self.defer = defer
        self.integrity = integrity
        self.crossorigin = crossorigin


@dataclass(slots=True)
class InlineScript:
    """An inline ``<script>`` block with a bounded preview of its contents."""

    content: str
    truncated: bool
    type: str | None = None


@dataclass(slots=True)
class PageStructure:
    """Static structure summary of a fetched HTML page.

    The tree is built with bounded depth, node count, and inline-source sizes
    so that inspecting a page never produces a runaway payload.
    """

    root: StructureNode | None
    stylesheets: list[StylesheetInfo]
    inline_styles: list[InlineStylesheet]
    scripts: list[ScriptInfo]
    inline_scripts: list[InlineScript]
    truncated: bool
    node_count: int
    max_depth: int


@dataclass(slots=True)
class FetchResult:
    url: str
    final_url: str | None = None
    status_code: int | None = None
    success: bool = False
    content_type: str | None = None
    encoding: str | None = None
    title: str | None = None
    markdown: str | None = None
    html: str | None = None
    text: str | None = None
    metadata: dict[str, Any] | None = None
    links: list[LinkInfo] | None = None
    images: list[ImageInfo] | None = None
    structure: PageStructure | None = None
    screenshot: bytes | None = None
    screenshot_format: str | None = None
    fetch_method: str | None = None
    proxy_provider: str = "none"
    content_confidence: float | None = None
    from_cache: bool = False
    duration_ms: float | None = None
    fetched_at: datetime | None = None
    warnings: list[str] | None = None
    error: FetchErrorInfo | None = None

    def __post_init__(self) -> None:
        self.metadata = {} if self.metadata is None else self.metadata
        self.links = [] if self.links is None else self.links
        self.images = [] if self.images is None else self.images
        self.warnings = [] if self.warnings is None else self.warnings

    def to_dict(
        self,
        *,
        include_html: bool = False,
        include_structure: bool = False,
        compact_structure: bool = False,
        include_screenshot: bool = False,
    ) -> dict[str, Any]:
        """Return a JSON-compatible dictionary.

        Raw HTML and the page structure summary are excluded by default; opt
        in with ``include_html=True`` and ``include_structure=True``. When
        ``compact_structure=True`` (and ``include_structure=True``) the
        structure payload is trimmed to the fields most useful for LLM
        consumers and developer inspection: empty fields are dropped,
        stylesheet/script entries shrink to ``{"url": ...}``, and inline
        ``<style>``/``<script>`` previews are returned as ``{length, preview}``
        instead of the full content. Compact structures are inspection-only and
        cannot be reconstructed with :meth:`from_dict`, because their inline
        source content is deliberately lossy.

        ``include_screenshot=True`` opt-in emits the screenshot bytes as a
        base64-encoded ``screenshot`` field alongside ``screenshot_format``.
        Screenshots are never serialized by default.
        """
        output: dict[str, Any] = {}
        for field in fields(self):
            if field.name == "html" and not include_html:
                continue
            if field.name == "structure" and not include_structure:
                continue
            if field.name == "screenshot" and not include_screenshot:
                continue
            value = getattr(self, field.name)
            if isinstance(value, datetime):
                output[field.name] = value.isoformat()
            elif field.name in {"links", "images"}:
                output[field.name] = [asdict(item) for item in value]
            elif field.name == "error" and value is not None:
                output[field.name] = asdict(value)
            elif field.name == "structure" and value is not None:
                output[field.name] = _structure_to_dict(value, compact=compact_structure)
            elif field.name == "screenshot" and value is not None:
                output[field.name] = base64.b64encode(value).decode("ascii")
            else:
                output[field.name] = value
        return output

    def json(
        self,
        *,
        include_html: bool = False,
        include_structure: bool = False,
        compact_structure: bool = False,
        include_screenshot: bool = False,
        indent: int | None = None,
    ) -> str:
        """Serialize the result as UTF-8 friendly JSON.

        ``compact_structure`` mirrors :meth:`to_dict` and only affects the
        payload when ``include_structure=True``. ``include_screenshot=True``
        opt-in emits the screenshot bytes as base64.
        """
        return json.dumps(
            self.to_dict(
                include_html=include_html,
                include_structure=include_structure,
                compact_structure=compact_structure,
                include_screenshot=include_screenshot,
            ),
            ensure_ascii=False,
            indent=indent,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FetchResult:
        """Reconstruct a result from cached serialized data."""
        values = dict(data)
        values["links"] = [LinkInfo(**item) for item in values.get("links", [])]
        values["images"] = [ImageInfo(**item) for item in values.get("images", [])]
        error = values.get("error")
        values["error"] = FetchErrorInfo(**error) if error else None
        structure = values.get("structure")
        values["structure"] = _structure_from_dict(structure) if structure else None
        screenshot = values.get("screenshot")
        if isinstance(screenshot, str):
            try:
                values["screenshot"] = base64.b64decode(screenshot, validate=True)
            except (ValueError, TypeError):
                values["screenshot"] = None
        elif screenshot is not None:
            # Cache corruption / schema drift: ``screenshot`` should be a
            # base64 string or ``None`` — silently coercing other types
            # to ``None`` would mask the bug downstream as a misleading
            # "screenshot not cached" warning.
            _LOGGER.warning(
                "FetchResult.from_dict received unexpected screenshot type %s; "
                "expected str or None — dropping to None.",
                type(screenshot).__name__,
            )
            values["screenshot"] = None
        values["screenshot_format"] = values.get("screenshot_format")
        fetched_at = values.get("fetched_at")
        if isinstance(fetched_at, str):
            values["fetched_at"] = datetime.fromisoformat(fetched_at)
        return cls(**values)


def _structure_to_dict(value: PageStructure, *, compact: bool = False) -> dict[str, Any]:
    """Serialize a PageStructure, mapping ``async_`` back to ``async``.

    When ``compact=True`` the payload is trimmed for LLM/developer use: empty
    fields are omitted, stylesheets and scripts keep only ``url``, and inline
    ``<style>``/``<script>`` blocks expose ``{length, preview}`` instead of the
    full content. The verbose selector triples (``selector``/``path``/
    ``unique_selector``) are intentionally kept in compact mode because they
    are the most developer-actionable parts of the tree; callers that want
    the raw tree can ignore them.
    """

    def node_to_dict(node: StructureNode) -> dict[str, Any]:
        payload: dict[str, Any] = {"tag": node.tag}
        if node.selector:
            payload["selector"] = node.selector
        if node.path:
            payload["path"] = node.path
        # ``unique_selector`` is verbose-mode-only: callers that opted out of
        # the LLM-friendly variant rely on the field being present even when
        # it duplicates ``selector``. In compact mode the ``path`` already
        # uniquely addresses the node.
        if not compact and node.unique_selector:
            payload["unique_selector"] = node.unique_selector
        if node.attrs:
            payload["attrs"] = node.attrs
        if node.text:
            payload["text"] = node.text
        if node.children:
            payload["children"] = [node_to_dict(child) for child in node.children]
        return payload

    def script_to_dict(item: ScriptInfo) -> dict[str, Any]:
        payload: dict[str, Any] = {"url": item.url}
        if compact:
            return payload
        payload["type"] = item.type
        payload["async"] = item.async_
        payload["defer"] = item.defer
        if item.integrity:
            payload["integrity"] = item.integrity
        if item.crossorigin:
            payload["crossorigin"] = item.crossorigin
        return payload

    if compact:
        stylesheets = [{"url": item.url} for item in value.stylesheets]
        inline_styles = [_compact_inline(item) for item in value.inline_styles]
        inline_scripts = [_compact_inline(item) for item in value.inline_scripts]
    else:
        stylesheets = [asdict(item) for item in value.stylesheets]
        inline_styles = [asdict(item) for item in value.inline_styles]
        inline_scripts = [asdict(item) for item in value.inline_scripts]

    return {
        "schema": _COMPACT_STRUCTURE_SCHEMA if compact else "pagefetch.structure.v1",
        "root": node_to_dict(value.root) if value.root is not None else None,
        "stylesheets": stylesheets,
        "inline_styles": inline_styles,
        "scripts": [script_to_dict(item) for item in value.scripts],
        "inline_scripts": inline_scripts,
        "truncated": value.truncated,
        "node_count": value.node_count,
        "max_depth": value.max_depth,
    }


def _compact_inline(item: InlineStylesheet | InlineScript) -> dict[str, Any]:
    """Return a compact ``{length, preview, truncated?}`` view of inline content.

    ``preview`` is bounded to :data:`_COMPACT_INLINE_PREVIEW` chars so the JSON
    payload stays predictable even for huge inline ``<script>`` blocks.
    """
    content = item.content
    truncated = bool(getattr(item, "truncated", False)) or len(content) > _COMPACT_INLINE_PREVIEW
    preview = content[:_COMPACT_INLINE_PREVIEW]
    payload: dict[str, Any] = {"length": len(content), "preview": preview}
    if truncated:
        payload["truncated"] = True
    return payload


def _structure_from_dict(data: dict[str, Any]) -> PageStructure:
    """Inverse of the lossless structure representation.

    Compact structures carry the ``pagefetch.structure.compact.v1`` schema
    marker and contain previews instead of inline source content; reconstructing
    them would silently manufacture incomplete source. They are intentionally
    inspection-only and must not enter cache deserialization.
    """
    if data.get("schema") == _COMPACT_STRUCTURE_SCHEMA:
        raise ValueError(
            "compact structure payloads are inspection-only and cannot be reconstructed"
        )

    def node_from_dict(item: dict[str, Any]) -> StructureNode:
        return StructureNode(
            tag=item["tag"],
            selector=item.get("selector", item.get("path", "")),
            attrs=dict(item.get("attrs", {})),
            text=item.get("text", ""),
            children=[node_from_dict(child) for child in item.get("children", [])],
            path=item.get("path", ""),
            unique_selector=item.get("unique_selector", item.get("path", "")),
        )

    def script_from_dict(item: dict[str, Any]) -> ScriptInfo:
        return ScriptInfo(
            url=item["url"],
            type=item.get("type"),
            async_=bool(item.get("async", False)),
            defer=bool(item.get("defer", False)),
            integrity=item.get("integrity"),
            crossorigin=item.get("crossorigin"),
        )

    return PageStructure(
        root=node_from_dict(data["root"]) if data.get("root") is not None else None,
        stylesheets=[StylesheetInfo(**item) for item in data.get("stylesheets", [])],
        inline_styles=[InlineStylesheet(**item) for item in data.get("inline_styles", [])],
        scripts=[script_from_dict(item) for item in data.get("scripts", [])],
        inline_scripts=[InlineScript(**item) for item in data.get("inline_scripts", [])],
        truncated=bool(data.get("truncated", False)),
        node_count=int(data.get("node_count", 0)),
        max_depth=int(data.get("max_depth", 0)),
    )
