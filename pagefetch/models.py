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

    def to_dict(self, *, include_html: bool = False) -> dict[str, Any]:
        """Return a JSON-compatible dictionary; raw HTML is excluded by default."""
        output: dict[str, Any] = {}
        for field in fields(self):
            if field.name == "html" and not include_html:
                continue
            value = getattr(self, field.name)
            if isinstance(value, datetime):
                output[field.name] = value.isoformat()
            elif field.name in {"links", "images"}:
                output[field.name] = [asdict(item) for item in value]
            elif field.name == "error" and value is not None:
                output[field.name] = asdict(value)
            else:
                output[field.name] = value
        return output

    def json(self, *, include_html: bool = False, indent: int | None = None) -> str:
        """Serialize the result as UTF-8 friendly JSON."""
        return json.dumps(self.to_dict(include_html=include_html), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FetchResult:
        """Reconstruct a result from cached serialized data."""
        values = dict(data)
        values["links"] = [LinkInfo(**item) for item in values.get("links", [])]
        values["images"] = [ImageInfo(**item) for item in values.get("images", [])]
        error = values.get("error")
        values["error"] = FetchErrorInfo(**error) if error else None
        fetched_at = values.get("fetched_at")
        if isinstance(fetched_at, str):
            values["fetched_at"] = datetime.fromisoformat(fetched_at)
        return cls(**values)

