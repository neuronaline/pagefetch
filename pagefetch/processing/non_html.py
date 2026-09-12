"""PDF, XML, and plain-text processing."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any

from lxml import etree


@dataclass(slots=True)
class ProcessedDocument:
    title: str | None
    markdown: str
    text: str
    metadata: dict[str, Any]
    warnings: list[str]


class MissingOptionalDependency(RuntimeError):
    """Raised when processing needs an extra that is not installed."""


def process_pdf(content: bytes) -> ProcessedDocument:
    """Extract text and basic metadata from a PDF byte stream."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise MissingOptionalDependency(
            "PDF support requires the optional dependency: pip install 'pagefetch[pdf]'"
        ) from exc

    reader = PdfReader(io.BytesIO(content))
    pages: list[str] = []
    warnings: list[str] = []
    for index, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"## Page {index}\n\n{text.strip()}")
        else:
            warnings.append(f"No extractable text was found on PDF page {index}.")
    raw_metadata = reader.metadata or {}
    metadata = {str(key).lstrip("/"): str(value) for key, value in raw_metadata.items() if value}
    title = metadata.get("Title")
    full_text = "\n\n".join(page.split("\n\n", 1)[-1].strip() for page in pages if page.split("\n\n", 1)[-1].strip())
    markdown_parts = ([f"# {title}"] if title else []) + pages
    return ProcessedDocument(title, "\n\n".join(markdown_parts), full_text, metadata, warnings)


def process_xml(content: bytes, encoding: str | None = None) -> ProcessedDocument:
    """Parse XML and retain its hierarchy in a fenced representation.

    Tries a strict parser first so well-formed documents surface no
    warnings.  When strict parsing fails (common for RSS feeds and
    sitemap.xml files with stray entities, unbalanced tags, or other
    real-world issues), the function falls back to lxml's recovery
    mode and surfaces a warning instead of raising.
    """
    strict_parser = etree.XMLParser(
        resolve_entities=False, no_network=True, recover=False
    )
    warnings: list[str] = []
    try:
        root = etree.fromstring(content, parser=strict_parser)
    except etree.XMLSyntaxError:
        lenient_parser = etree.XMLParser(
            resolve_entities=False, no_network=True, recover=True
        )
        root = etree.fromstring(content, parser=lenient_parser)
        warnings.append("Malformed XML parsed using recovery mode.")
    pretty = etree.tostring(root, encoding="unicode", pretty_print=True)
    text_parts = [part.strip() for part in root.itertext() if part.strip()]
    title = root.get("title") or root.tag.split("}")[-1]
    return ProcessedDocument(
        title=title,
        markdown=f"```xml\n{pretty.strip()}\n```",
        text="\n".join(text_parts),
        metadata={"root_element": root.tag, "encoding": encoding},
        warnings=warnings,
    )


def process_text(content: bytes, encoding: str | None = None) -> ProcessedDocument:
    """Decode plain text with minimal normalization."""
    selected = encoding or "utf-8"
    try:
        text = content.decode(selected)
    except (LookupError, UnicodeDecodeError):
        selected = "utf-8"
        text = content.decode("utf-8", errors="replace")
    text = re.sub(r"\r\n?", "\n", text)
    return ProcessedDocument(None, text, text, {"encoding": selected}, [])
