"""Content processing pipeline and FetchResult builders for different content kinds."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..constants import SAFE_RESPONSE_HEADERS
from ..exceptions import TransportFailure
from ..models import FetchErrorInfo, FetchResult
from .detector import ConfidenceReport
from .html import process_html
from .sections import pop_section_attrs, render_clean_raw_html, restore_section_attrs
from .non_html import (
    MissingOptionalDependency,
    process_csv,
    process_docx,
    process_json,
    process_pdf,
    process_text,
    process_xml,
)
from .structure import StructureLimits, extract_structure


_META_CHARSET_RE = re.compile(
    rb"""<meta\s+[^>]*?(?:charset\s*=\s*['"]?([a-zA-Z0-9_\-]+)|content\s*=\s*['"][^'"]*?charset=\s*['"]?([a-zA-Z0-9_\-]+))""",
    re.IGNORECASE,
)
_XML_ENCODING_RE = re.compile(
    rb"""^\s*<\?xml\b[^>]*?\bencoding\s*=\s*['"]([a-zA-Z0-9_\-]+)['"]""",
    re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class DocumentPayload:
    """In-memory document payload and headers ready for extraction."""

    url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    encoding: str | None = None


def _header_get(headers: Mapping[str, str] | None, name: str, default: str = "") -> str:
    if not headers:
        return default
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return default


def decode_response_body(response: DocumentPayload) -> str:
    """Decode raw response bytes into string using declared charset, response encoding, or utf-8."""
    # Sniff HTML <meta charset> or XML <?xml encoding="..."?> from the first 4096 bytes
    sniffed_encoding: str | None = None
    if response.content:
        head = response.content[:4096]
        match = _META_CHARSET_RE.search(head)
        raw_bytes = (match.group(1) or match.group(2)) if match else None
        if raw_bytes is None:
            xml_match = _XML_ENCODING_RE.search(head)
            if xml_match:
                raw_bytes = xml_match.group(1)
        if raw_bytes:
            raw_enc = raw_bytes.decode("ascii", errors="ignore").strip().lower()
            try:
                import codecs

                codecs.lookup(raw_enc)
                sniffed_encoding = raw_enc
            except (LookupError, ValueError):
                sniffed_encoding = None

    candidates: list[str] = []
    if sniffed_encoding:
        candidates.append(sniffed_encoding)
    resp_enc = response.encoding.lower() if response.encoding else None
    # Explicit Content-Type charset has high priority unless it is a generic ISO-8859-1 fallback.
    if resp_enc and resp_enc != "iso-8859-1" and resp_enc not in candidates:
        candidates.append(resp_enc)
    if "utf-8" not in candidates:
        candidates.append("utf-8")
    if resp_enc and resp_enc not in candidates:
        candidates.append(resp_enc)

    for enc in candidates:
        try:
            return response.content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return response.content.decode("utf-8", errors="replace")


SUPPORTED_DOC_EXTENSIONS: tuple[str, ...] = (
    ".pdf",
    ".docx",
    ".csv",
    ".tsv",
    ".json",
    ".xml",
    ".txt",
    ".md",
    ".html",
    ".htm",
)
_UNSUPPORTED_ARCHIVE_TYPES: frozenset[str] = frozenset(
    {
        "application/zip",
        "application/x-zip-compressed",
        "application/x-rar-compressed",
        "application/x-7z-compressed",
        "application/gzip",
        "application/x-tar",
    }
)
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_EXTENSION_KIND_MAP: dict[str, str] = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".json": "json",
    ".xml": "xml",
    ".txt": "text",
    ".md": "text",
}


def parse_content_type(header: str | None) -> str:
    """Extract normalized MIME type from Content-Type header."""
    return (header or "application/octet-stream").split(";", 1)[0].strip().lower()


def _effective_mime(headers: Mapping[str, str] | None, fallback: str) -> str:
    """Return normalized Content-Type header unless missing or generic octet-stream."""
    ct = parse_content_type(_header_get(headers, "Content-Type"))
    return fallback if ct == "application/octet-stream" else ct


def is_explicit_non_html_mime(content_type: str | None) -> bool:
    """Return True when *content_type* explicitly declares a supported non-HTML MIME."""
    if not content_type:
        return False
    ct = parse_content_type(content_type)
    return (
        ct
        in (
            "application/pdf",
            "application/x-pdf",
            _DOCX_MIME,
            "text/csv",
            "application/csv",
            "text/tab-separated-values",
            "text/tsv",
            "application/json",
            "text/json",
            "application/xml",
            "text/xml",
        )
        or ct.startswith("text/plain")
        or ct.endswith("+json")
        or ("+xml" in ct and ct != "application/xhtml+xml")
    )


def _clean_url_path(url: str | None) -> str:
    if not url:
        return ""
    return urlsplit(url).path.lower()


def is_unsupported_binary_response(
    content_type: str,
    url: str,
    content_disposition: str = "",
) -> bool:
    """Return True when HTTP response headers indicate a non-text binary stream to abort early."""
    normalized_ct = parse_content_type(content_type)
    clean_path = _clean_url_path(url)
    cd_lower = (content_disposition or "").lower()
    is_doc_ext = clean_path.endswith(SUPPORTED_DOC_EXTENSIONS) or any(
        ext in cd_lower for ext in SUPPORTED_DOC_EXTENSIONS
    )
    return (
        normalized_ct.startswith(("video/", "audio/"))
        or (normalized_ct.startswith("image/") and normalized_ct != "image/svg+xml")
        or (normalized_ct in _UNSUPPORTED_ARCHIVE_TYPES and not is_doc_ext)
    )


def _is_html_mime(content_type: str) -> bool:
    return content_type in ("text/html", "application/xhtml+xml")


def looks_like_html(content: bytes) -> bool:
    """Heuristic HTML sniff for responses with ambiguous content types."""
    stripped = content.lstrip()
    if not stripped:
        return False
    head = stripped[:512].lower()
    if head.startswith(b"<?xml"):
        return False
    return stripped.startswith(b"<") and any(
        marker in head
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


def is_pdf_content(
    content_type: str,
    content: bytes,
    url: str | None = None,
    *,
    filename: str | None = None,
) -> bool:
    """Check if content represents a PDF file by magic bytes, content-type, or filename/URL."""
    if content and content.startswith(b"%PDF-"):
        return True
    ct = parse_content_type(content_type)
    if _is_html_mime(ct) or looks_like_html(content):
        return False
    if ct in ("application/pdf", "application/x-pdf"):
        return True
    clean_name = (filename or "").strip().lower()
    has_pdf_ext = _clean_url_path(url).endswith(".pdf") or clean_name.endswith(".pdf")
    if has_pdf_ext and not content:
        return True
    return False


def detect_document_kind(
    content_type: str,
    content: bytes,
    url: str | None = None,
    *,
    filename: str | None = None,
) -> str:
    """Detect high-level document kind from magic bytes, declared MIME, filename, and URL."""
    ct = parse_content_type(content_type)
    clean_path = _clean_url_path(url)
    clean_name = (filename or "").strip().lower()

    # Unambiguous binary signatures always win even over misconfigured text/html headers.
    if content and content.startswith(b"%PDF-"):
        return "pdf"
    is_zip = bool(content and content.startswith(b"PK\x03\x04"))
    if is_zip and (b"word/document.xml" in content[:65536] or b"word/" in content[:65536]):
        return "docx"

    explicit_non_html = is_explicit_non_html_mime(ct)
    # Declared text/html or HTML byte sniff beats misleading URL extensions (.md, .pdf).
    if _is_html_mime(ct) or (not explicit_non_html and looks_like_html(content)):
        return "html"
    if is_pdf_content(ct, content, url=url, filename=filename):
        return "pdf"

    has_docx_ext = clean_path.endswith(".docx") or clean_name.endswith(".docx")
    if ct == _DOCX_MIME or (has_docx_ext and (not content or is_zip)):
        return "docx"

    if ct in ("text/csv", "application/csv"):
        return "csv"
    if ct in ("text/tab-separated-values", "text/tsv"):
        return "tsv"
    if ct in ("application/json", "text/json") or ct.endswith("+json"):
        return "json"
    if ct in ("application/xml", "text/xml") or ("+xml" in ct and ct != "application/xhtml+xml"):
        return "xml"
    if ct.startswith("text/plain"):
        return "text"

    if content and content.lstrip()[:64].lower().startswith((b"<?xml", b"<rss", b"<feed")):
        return "xml"

    for ext, kind in _EXTENSION_KIND_MAP.items():
        if clean_name.endswith(ext) or clean_path.endswith(ext):
            return kind
    if clean_name.endswith((".html", ".htm")):
        return "html"
    return "unknown"


def build_document_result(
    url: str,
    response: DocumentPayload,
    proxy: str,
    doc: Any,
    method: str,
    content_type: str,
    *,
    raw_source: str | None = None,
) -> FetchResult:
    """Wrap a processed non-HTML document into a FetchResult."""
    doc.metadata["headers"] = {
        str(key).lower(): str(value)
        for key, value in response.headers.items()
        if str(key).lower() in SAFE_RESPONSE_HEADERS
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
    response_headers: Mapping[str, str] | None = None,
    soup: BeautifulSoup | None = None,
    confidence: ConfidenceReport | None = None,
    include_structure: bool = False,
    compact_structure: bool = False,
) -> FetchResult:
    """Transform raw HTML and metadata into a fully structured FetchResult."""
    limits = StructureLimits(compact=compact_structure) if compact_structure else None
    # Capture clean raw HTML and structural summary BEFORE process_html mutates soup in place.
    if soup is not None:
        saved_section_attrs = pop_section_attrs(soup)
        try:
            clean_raw_html = str(soup) if saved_section_attrs else html
            structure = (
                extract_structure(soup, final_url, limits=limits)
                if include_structure
                else None
            )
        finally:
            if saved_section_attrs:
                restore_section_attrs(saved_section_attrs)
    else:
        clean_raw_html = render_clean_raw_html(html, None)
        structure = (
            extract_structure(clean_raw_html, final_url, limits=limits)
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
        html=clean_raw_html,
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
        sections=processed.sections or None,
    )


def build_pdf_result(url: str, response: DocumentPayload, proxy: str) -> FetchResult:
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


def build_docx_result(url: str, response: DocumentPayload, proxy: str) -> FetchResult:
    """Process a DOCX response and construct a FetchResult."""
    try:
        doc = process_docx(response.content)
    except Exception as exc:
        raise TransportFailure(
            FetchErrorInfo(
                "docx_parse_error",
                f"DOCX could not be parsed: {exc}",
                False,
                type(exc).__name__,
            )
        ) from exc
    return build_document_result(
        url,
        response,
        proxy,
        doc,
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


def build_csv_result(
    url: str,
    response: DocumentPayload,
    proxy: str,
    delimiter: str | None = None,
) -> FetchResult:
    """Process a CSV or TSV response and construct a FetchResult."""
    try:
        doc = process_csv(response.content, encoding=response.encoding, delimiter=delimiter)
    except Exception as exc:
        raise TransportFailure(
            FetchErrorInfo(
                "csv_parse_error",
                f"CSV could not be parsed: {exc}",
                False,
                type(exc).__name__,
            )
        ) from exc
    fallback_ct = "text/tab-separated-values" if delimiter == "\t" else "text/csv"
    return build_document_result(
        url,
        response,
        proxy,
        doc,
        "csv",
        _effective_mime(response.headers, fallback_ct),
    )


def build_json_result(url: str, response: DocumentPayload, proxy: str) -> FetchResult:
    """Process a JSON response and construct a FetchResult."""
    doc = process_json(response.content, encoding=response.encoding)
    return build_document_result(
        url,
        response,
        proxy,
        doc,
        "json",
        _effective_mime(response.headers, "application/json"),
    )


def build_xml_result(url: str, response: DocumentPayload, proxy: str) -> FetchResult:
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
        _effective_mime(response.headers, "application/xml"),
        raw_source=decode_response_body(response),
    )


def build_text_result(url: str, response: DocumentPayload, proxy: str) -> FetchResult:
    """Process a plain-text or Markdown response and construct a FetchResult."""
    doc = process_text(response.content, response.encoding)
    return build_document_result(
        url,
        response,
        proxy,
        doc,
        "text",
        _effective_mime(response.headers, "text/plain"),
    )


def build_non_html_result(
    kind: str,
    url: str,
    response: DocumentPayload,
    proxy: str,
) -> FetchResult | None:
    """Dispatch a classified non-HTML document response to its FetchResult builder."""
    if kind == "pdf":
        return build_pdf_result(url, response, proxy)
    if kind == "docx":
        return build_docx_result(url, response, proxy)
    if kind in ("csv", "tsv"):
        return build_csv_result(
            url,
            response,
            proxy,
            delimiter="\t" if kind == "tsv" else None,
        )
    if kind == "json":
        return build_json_result(url, response, proxy)
    if kind == "xml":
        return build_xml_result(url, response, proxy)
    if kind == "text":
        return build_text_result(url, response, proxy)
    return None


