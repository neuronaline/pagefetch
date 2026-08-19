"""Structured public result models."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Any


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
    """A single node in the page DOM tree summary."""

    tag: str
    selector: str
    attrs: dict[str, str]
    text: str
    children: list[StructureNode]


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
    ) -> dict[str, Any]:
        """Return a JSON-compatible dictionary.

        Raw HTML and the page structure summary are excluded by default; opt
        in with ``include_html=True`` and ``include_structure=True``.
        """
        output: dict[str, Any] = {}
        for field in fields(self):
            if field.name == "html" and not include_html:
                continue
            if field.name == "structure" and not include_structure:
                continue
            value = getattr(self, field.name)
            if isinstance(value, datetime):
                output[field.name] = value.isoformat()
            elif field.name in {"links", "images"}:
                output[field.name] = [asdict(item) for item in value]
            elif field.name == "error" and value is not None:
                output[field.name] = asdict(value)
            elif field.name == "structure" and value is not None:
                output[field.name] = _structure_to_dict(value)
            else:
                output[field.name] = value
        return output

    def json(
        self,
        *,
        include_html: bool = False,
        include_structure: bool = False,
        indent: int | None = None,
    ) -> str:
        """Serialize the result as UTF-8 friendly JSON."""
        return json.dumps(
            self.to_dict(include_html=include_html, include_structure=include_structure),
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
        fetched_at = values.get("fetched_at")
        if isinstance(fetched_at, str):
            values["fetched_at"] = datetime.fromisoformat(fetched_at)
        return cls(**values)


def _structure_to_dict(value: PageStructure) -> dict[str, Any]:
    """Serialize a PageStructure, mapping ``async_`` back to ``async``."""

    def node_to_dict(node: StructureNode) -> dict[str, Any]:
        return {
            "tag": node.tag,
            "selector": node.selector,
            "attrs": node.attrs,
            "text": node.text,
            "children": [node_to_dict(child) for child in node.children],
        }

    def script_to_dict(item: ScriptInfo) -> dict[str, Any]:
        return {
            "url": item.url,
            "type": item.type,
            "async": item.async_,
            "defer": item.defer,
            "integrity": item.integrity,
            "crossorigin": item.crossorigin,
        }

    return {
        "root": node_to_dict(value.root) if value.root is not None else None,
        "stylesheets": [asdict(item) for item in value.stylesheets],
        "inline_styles": [asdict(item) for item in value.inline_styles],
        "scripts": [script_to_dict(item) for item in value.scripts],
        "inline_scripts": [asdict(item) for item in value.inline_scripts],
        "truncated": value.truncated,
        "node_count": value.node_count,
        "max_depth": value.max_depth,
    }


def _structure_from_dict(data: dict[str, Any]) -> PageStructure:
    """Inverse of :func:`_structure_to_dict`."""

    def node_from_dict(item: dict[str, Any]) -> StructureNode:
        return StructureNode(
            tag=item["tag"],
            selector=item["selector"],
            attrs=dict(item.get("attrs", {})),
            text=item.get("text", ""),
            children=[node_from_dict(child) for child in item.get("children", [])],
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

