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
    page_texts: list[str] = []
    warnings: list[str] = []
    for index, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            page_texts.append(text)
        else:
            warnings.append(f"No extractable text was found on PDF page {index}.")
    raw_metadata = reader.metadata or {}
    metadata = {str(key).lstrip("/"): str(value) for key, value in raw_metadata.items() if value}
    title = metadata.get("Title")
    # ``full_text`` is the raw concatenation of per-page text; callers receive it
    # verbatim instead of having to parse a "## Page N" sentinel back out of the
    # rendered Markdown. The Markdown form is built independently below.
    full_text = "\n\n".join(page_texts)
    markdown_parts: list[str] = []
    if title:
        markdown_parts.append(f"# {title}")
    for index, text in enumerate(page_texts, 1):
        markdown_parts.append(f"## Page {index}\n\n{text}")
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
    root: Any = None
    try:
        root = etree.fromstring(content, parser=strict_parser)
    except etree.XMLSyntaxError:
        lenient_parser = etree.XMLParser(
            resolve_entities=False, no_network=True, recover=True
        )
        try:
            root = etree.fromstring(content, parser=lenient_parser)
        except etree.XMLSyntaxError:
            root = None
        if root is None:
            fallback_warnings, text = _decode_text(content, encoding)
            warnings.extend(fallback_warnings)
            return ProcessedDocument(
                title=None,
                markdown=text,
                text=text,
                metadata={"encoding": encoding or "utf-8"},
                warnings=warnings + ["XML could not be recovered; returned decoded text instead."],
            )
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


def _decode_text(content: bytes, encoding: str | None) -> tuple[list[str], str]:
    """Decode *content* to text, surfacing a warning when bytes are lost."""
    selected = encoding or "utf-8"
    # Try a strict decode first so we know whether the fallback path was
    # used.  If it succeeds, no replacement characters were inserted and we
    # can return the result without a warning — without needing a post-hoc
    # scan for U+FFFD that would misfire on legitimate input containing it.
    try:
        return [], content.decode(selected)
    except (LookupError, UnicodeDecodeError):
        pass
    text = content.decode("utf-8", errors="replace")
    return ["Text decoded with UTF-8 replacement; some bytes were lost."], text


def process_text(content: bytes, encoding: str | None = None) -> ProcessedDocument:
    """Decode plain text with minimal normalization."""
    warnings, text = _decode_text(content, encoding)
    text = re.sub(r"\r\n?", "\n", text)
    return ProcessedDocument(None, text, text, {"encoding": encoding or "utf-8"}, warnings)
