"""Content processing pipeline and FetchResult builders for different content kinds."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from ..constants import SAFE_RESPONSE_HEADERS
from ..fetching.http import HTTPResponse, TransportFailure
from ..models import FetchErrorInfo, FetchResult
from .detector import ConfidenceReport
from .html import process_html
from .non_html import (
    MissingOptionalDependency,
    process_pdf,
    process_text,
    process_xml,
)
from .structure import StructureLimits, extract_structure


def decode_response_body(response: HTTPResponse) -> str:
    """Decode raw HTTP response bytes into string using response encoding or utf-8."""
    encoding = response.encoding or "utf-8"
    try:
        return response.content.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return response.content.decode("utf-8", errors="replace")


def parse_content_type(header: str | None) -> str:
    """Extract normalized MIME type from Content-Type header."""
    return (header or "application/octet-stream").split(";", 1)[0].strip().lower()


def is_pdf_content(content_type: str, content: bytes) -> bool:
    """Check if content represents a PDF file by content-type or magic bytes."""
    return content_type == "application/pdf" or content.startswith(b"%PDF-")


def is_xml_content(content_type: str) -> bool:
    """Check if content represents generic XML."""
    if content_type in ("application/xml", "text/xml"):
        return True
    return "+xml" in content_type and content_type != "application/xhtml+xml"


def is_html_like(content_type: str) -> bool:
    """Return True when content_type should be processed as HTML."""
    return content_type in ("text/html", "application/xhtml+xml")


def looks_like_html(content: bytes) -> bool:
    """Heuristic HTML sniff for responses with ambiguous content types."""
    stripped = content.lstrip()
    if not stripped:
        return False
    return stripped.startswith(b"<") and any(
        marker in stripped[:512].lower()
        for marker in (
            b"<!doctype html",
            b"<html",
            b"<head",
            b"<body",
            b"<title",
            b"<meta",
            b"<div",
            b"<p",
            b"<a ",
        )
    )


def detect_document_kind(content_type: str, content: bytes) -> str:
    """Detect high-level document kind from content type and byte sniff."""
    if is_pdf_content(content_type, content):
        return "pdf"
    if is_xml_content(content_type):
        return "xml"
    if (
        content_type.startswith("text/plain")
        or content_type == "application/json"
        or content_type.endswith("+json")
    ):
        return "text"
    if is_html_like(content_type) or looks_like_html(content):
        return "html"
    return "unknown"


def build_document_result(
    url: str,
    response: HTTPResponse,
    proxy: str,
    doc: Any,
    method: str,
    content_type: str,
    *,
    raw_source: str | None = None,
) -> FetchResult:
    """Wrap a processed non-HTML document into a FetchResult."""
    doc.metadata["headers"] = {
        key.lower(): value
        for key, value in response.headers.items()
        if key.lower() in SAFE_RESPONSE_HEADERS
    }
    return FetchResult(
        url=url,
        final_url=response.url,
        status_code=response.status_code,
        success=True,
        content_type=content_type,
        encoding=response.encoding,
        title=doc.title,
        markdown=doc.markdown,
        html=raw_source,
        text=doc.text,
        metadata=doc.metadata,
        fetch_method=method,
        proxy_provider=proxy,
        content_confidence=1.0,
        fetched_at=datetime.now(UTC),
        warnings=doc.warnings,
    )


def build_html_result(
    *,
    original_url: str,
    final_url: str,
    status_code: int | None,
    html: str,
    content_type: str,
    encoding: str | None,
    proxy: str,
    method: str,
    cleaning_level: str = "standard",
    response_headers: httpx.Headers | None = None,
    soup: BeautifulSoup | None = None,
    confidence: ConfidenceReport | None = None,
    include_structure: bool = False,
    compact_structure: bool = False,
) -> FetchResult:
    """Transform raw HTML and metadata into a fully structured FetchResult."""
    structure_source = soup if soup is not None else html
    limits = StructureLimits(compact=compact_structure) if compact_structure else None
    structure = (
        extract_structure(structure_source, final_url, limits=limits)
        if include_structure
        else None
    )
    try:
        processed = process_html(
            html,
            final_url,
            response_headers,
            soup=soup,
            confidence=confidence,
            cleaning_level=cleaning_level,
        )
    except Exception as exc:
        raise TransportFailure(
            FetchErrorInfo(
                "parse_error",
                "HTML content could not be processed",
                False,
                type(exc).__name__,
            )
        ) from exc

    return FetchResult(
        url=original_url,
        final_url=final_url,
        status_code=status_code,
        success=True,
        content_type=content_type,
        encoding=encoding,
        title=processed.title,
        markdown=processed.markdown,
        html=html,
        text=processed.text,
        metadata=processed.metadata,
        links=processed.links,
        images=processed.images,
        structure=structure,
        fetch_method=method,
        proxy_provider=proxy,
        content_confidence=processed.confidence.score,
        fetched_at=datetime.now(UTC),
        warnings=processed.warnings,
    )


def build_pdf_result(url: str, response: HTTPResponse, proxy: str) -> FetchResult:
    """Process a PDF response and construct a FetchResult."""
    try:
        doc = process_pdf(response.content)
    except MissingOptionalDependency as exc:
        raise TransportFailure(
            FetchErrorInfo("missing_dependency", str(exc), False, type(exc).__name__)
        ) from exc
    except Exception as exc:
        raise TransportFailure(
            FetchErrorInfo(
                "pdf_parse_error",
                "PDF could not be parsed",
                False,
                type(exc).__name__,
            )
        ) from exc
    return build_document_result(url, response, proxy, doc, "pdf", "application/pdf")


def build_xml_result(url: str, response: HTTPResponse, proxy: str) -> FetchResult:
    """Process an XML response and construct a FetchResult."""
    try:
        doc = process_xml(response.content, response.encoding)
    except Exception as exc:
        raise TransportFailure(
            FetchErrorInfo(
                "xml_parse_error",
                "XML could not be parsed",
                False,
                type(exc).__name__,
            )
        ) from exc
    return build_document_result(
        url,
        response,
        proxy,
        doc,
        "xml",
        parse_content_type(response.headers.get("Content-Type")),
        raw_source=decode_response_body(response),
    )


def build_text_result(url: str, response: HTTPResponse, proxy: str) -> FetchResult:
    """Process a text/plain or JSON response and construct a FetchResult."""
    doc = process_text(response.content, response.encoding)
    return build_document_result(
        url,
        response,
        proxy,
        doc,
        "text",
        parse_content_type(response.headers.get("Content-Type")),
    )

