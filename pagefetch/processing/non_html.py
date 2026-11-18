"""PDF, DOCX, CSV, JSON, XML, and plain-text document extraction and Markdown conversion."""

from __future__ import annotations

import csv
import html as html_module
import io
import json
import re
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DOCX_NS = {"w": W_NS, "r": R_NS}

CORE_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}

_DOCX_CORE_FIELDS: tuple[tuple[str, str], ...] = (
    (".//dc:title", "title"),
    (".//dc:creator", "creator"),
    (".//cp:lastModifiedBy", "last_modified_by"),
    (".//dcterms:created", "created"),
    (".//dcterms:modified", "modified"),
)

# Set generous CSV cell bound once at import time instead of mutating process globals per request.
try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2147483647)


@dataclass(slots=True)
class ProcessedDocument:
    title: str | None
    markdown: str
    text: str
    metadata: dict[str, Any]
    warnings: list[str]


class MissingOptionalDependency(RuntimeError):
    """Raised when processing needs an extra that is not installed."""


def _decode_text(content: bytes, encoding: str | None) -> tuple[list[str], str]:
    """Decode *content* to text, surfacing a warning when bytes are lost."""
    selected = encoding or "utf-8"
    try:
        return [], content.decode(selected)
    except (LookupError, UnicodeDecodeError):
        pass
    text = content.decode("utf-8", errors="replace")
    return ["Text decoded with UTF-8 replacement; some bytes were lost."], text


def _format_gfm_table(
    rows: Sequence[Sequence[str]],
    *,
    newline_replacement: str = " ",
    max_data_rows: int | None = None,
) -> tuple[str, bool]:
    """Render rectangular/ragged string rows as a GFM Markdown table.

    Returns ``(markdown_table, was_truncated)``.
    """
    if not rows:
        return "", False
    width = max(len(r) for r in rows)
    if width == 0:
        return "", False

    def _clean_row(row: Sequence[str]) -> list[str]:
        cleaned = [
            cell.replace("|", r"\|").replace("\r", "").replace("\n", newline_replacement).strip()
            for cell in row
        ]
        if len(cleaned) < width:
            cleaned.extend([""] * (width - len(cleaned)))
        return cleaned

    md_lines = [
        "| " + " | ".join(_clean_row(rows[0])) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    data_rows = rows[1:]
    truncated = max_data_rows is not None and len(data_rows) > max_data_rows
    display_rows = data_rows[:max_data_rows] if truncated else data_rows
    for row in display_rows:
        md_lines.append("| " + " | ".join(_clean_row(row)) + " |")

    table_md = "\n".join(md_lines)
    if truncated:
        table_md += (
            f"\n\n*Showing first {max_data_rows} of {len(data_rows)} data rows. "
            f"Complete data available in text.*"
        )
    return table_md, truncated


def process_pdf(content: bytes) -> ProcessedDocument:
    """Extract text and basic metadata from a PDF byte stream."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise MissingOptionalDependency(
            "PDF support requires pypdf: pip install pypdf"
        ) from exc

    reader = PdfReader(io.BytesIO(content))
    warnings: list[str] = []
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            warnings.append("PDF is password-protected; some or all content could not be decrypted.")

    page_texts: list[str] = []
    for index, page in enumerate(reader.pages, 1):
        try:
            raw_text = page.extract_text() or ""
        except Exception as exc:
            warnings.append(f"Failed extracting text on PDF page {index}: {exc}")
            continue

        normalized = raw_text.replace("\xad", "").replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(r"(\w+)-\n(\w+)", r"\1\2", normalized)
        text = normalized.strip()
        if text:
            page_texts.append(text)
        else:
            warnings.append(f"No extractable text was found on PDF page {index}.")

    raw_metadata = reader.metadata or {}
    metadata: dict[str, Any] = {
        str(key).lstrip("/"): str(value) for key, value in raw_metadata.items() if value
    }
    metadata["page_count"] = len(reader.pages)
    title = metadata.get("Title")
    if not title and page_texts:
        first_line = page_texts[0].split("\n", 1)[0].strip()
        if first_line and len(first_line) <= 120 and not first_line.endswith("."):
            title = first_line

    full_text = "\n\n".join(page_texts)
    markdown_parts = [f"# {title}"] if title else []
    markdown_parts.extend(f"## Page {index}\n\n{text}" for index, text in enumerate(page_texts, 1))
    return ProcessedDocument(title, "\n\n".join(markdown_parts), full_text, metadata, warnings)


def _wrap_markdown_formatting(text: str, *, bold: bool, italic: bool, strike: bool) -> str:
    if not (bold or italic or strike):
        return text
    l_ws = text[: len(text) - len(text.lstrip())]
    r_ws = text[len(text.rstrip()) :]
    core = text.strip()
    if not core:
        return text
    if bold and italic:
        core = f"***{core}***"
    elif bold:
        core = f"**{core}**"
    elif italic:
        core = f"*{core}*"
    if strike:
        core = f"~~{core}~~"
    return f"{l_ws}{core}{r_ws}"


def _docx_paragraph(p_elem: Any, rels: dict[str, str]) -> tuple[str, str]:
    style_elem = p_elem.find(".//w:pStyle", DOCX_NS)
    style_val = style_elem.attrib.get(f"{{{W_NS}}}val", "") if style_elem is not None else ""
    is_list = p_elem.find(".//w:numPr", DOCX_NS) is not None or "list" in style_val.lower()

    md_parts: list[str] = []
    txt_parts: list[str] = []
    for child in p_elem:
        if not isinstance(child.tag, str):
            continue
        tag = child.tag.split("}")[-1]
        if tag == "r":
            run_text = "".join(child.xpath(".//w:t/text()", namespaces=DOCX_NS))
            if not run_text:
                continue
            txt_parts.append(run_text)
            md_parts.append(
                _wrap_markdown_formatting(
                    run_text,
                    bold=child.find(".//w:b", DOCX_NS) is not None,
                    italic=child.find(".//w:i", DOCX_NS) is not None,
                    strike=child.find(".//w:strike", DOCX_NS) is not None,
                )
            )
        elif tag == "hyperlink":
            link_text = "".join(child.xpath(".//w:t/text()", namespaces=DOCX_NS))
            if not link_text:
                continue
            txt_parts.append(link_text)
            url = rels.get(child.attrib.get(f"{{{R_NS}}}id") or "", "")
            if url:
                safe_label = link_text.replace("[", r"\[").replace("]", r"\]")
                md_parts.append(f"[{safe_label}]({url})")
            else:
                md_parts.append(link_text)

    plain_line = "".join(txt_parts).strip()
    md_line = "".join(md_parts).strip()
    if not md_line:
        return "", ""

    lower_style = style_val.lower()
    if lower_style.startswith("heading"):
        level_char = style_val[-1] if style_val[-1].isdigit() else "1"
        level = max(1, min(6, int(level_char)))
        return f"{'#' * level} {md_line}", plain_line
    if lower_style == "title":
        return f"# {md_line}", plain_line
    if lower_style == "subtitle":
        return f"## {md_line}", plain_line
    if is_list:
        return f"- {md_line}", plain_line
    return md_line, plain_line


def _docx_table(tbl_elem: Any, rels: dict[str, str]) -> tuple[str, str]:
    rows: list[list[str]] = []
    raw_rows: list[list[str]] = []
    for tr in tbl_elem.findall("./w:tr", DOCX_NS):
        cells: list[str] = []
        raw_cells: list[str] = []
        for tc in tr.findall("./w:tc", DOCX_NS):
            cell_md: list[str] = []
            cell_txt: list[str] = []
            for p in tc.findall(".//w:p", DOCX_NS):
                p_md, p_txt = _docx_paragraph(p, rels)
                if p_md:
                    cell_md.append(p_md)
                if p_txt:
                    cell_txt.append(p_txt)
            cells.append(" ".join(cell_md).strip())
            raw_cells.append(" ".join(cell_txt).strip())
        if any(cells):
            rows.append(cells)
            raw_rows.append(raw_cells)
    if not rows:
        return "", ""
    table_md, _ = _format_gfm_table(rows, newline_replacement=" ")
    return table_md, "\n".join("\t".join(r) for r in raw_rows)


def process_docx(content: bytes) -> ProcessedDocument:
    """Extract structured Markdown and metadata from a DOCX (OpenXML) byte stream."""
    warnings: list[str] = []
    try:
        zf_ctx = zipfile.ZipFile(io.BytesIO(content))
    except Exception as exc:
        raise ValueError(f"Invalid DOCX archive: {exc}") from exc

    with zf_ctx as zf:
        namelist = zf.namelist()
        if "word/document.xml" not in namelist:
            raise ValueError("Invalid DOCX format: missing word/document.xml")

        metadata: dict[str, Any] = {}
        if "docProps/core.xml" in namelist:
            try:
                core_root = etree.fromstring(zf.read("docProps/core.xml"))
                for xpath, key in _DOCX_CORE_FIELDS:
                    elem = core_root.find(xpath, CORE_NS)
                    if elem is not None and elem.text:
                        metadata[key] = elem.text.strip()
            except Exception as exc:
                warnings.append(f"Failed parsing DOCX core metadata: {exc}")

        rels: dict[str, str] = {}
        if "word/_rels/document.xml.rels" in namelist:
            try:
                rels_root = etree.fromstring(zf.read("word/_rels/document.xml.rels"))
                for rel in rels_root:
                    r_id = rel.attrib.get("Id")
                    target = rel.attrib.get("Target")
                    if r_id and target:
                        rels[r_id] = target
            except Exception as exc:
                warnings.append(f"Failed parsing DOCX relationships: {exc}")

        try:
            doc_root = etree.fromstring(zf.read("word/document.xml"))
        except Exception as exc:
            raise ValueError(f"Failed parsing DOCX XML tree: {exc}") from exc

    title: str | None = metadata.get("title")
    body = doc_root.find("w:body", DOCX_NS)
    md_blocks: list[str] = []
    txt_blocks: list[str] = []
    if body is not None:
        for child in body:
            if not isinstance(child.tag, str):
                continue
            tag = child.tag.split("}")[-1]
            if tag == "p":
                p_md, p_txt = _docx_paragraph(child, rels)
            elif tag == "tbl":
                p_md, p_txt = _docx_table(child, rels)
            else:
                continue
            if p_md:
                md_blocks.append(p_md)
            if p_txt:
                txt_blocks.append(p_txt)

    if not title and md_blocks and md_blocks[0].startswith("# "):
        title = md_blocks[0][2:].strip()

    return ProcessedDocument(title, "\n\n".join(md_blocks), "\n\n".join(txt_blocks), metadata, warnings)


def process_csv(
    content: bytes,
    encoding: str | None = None,
    delimiter: str | None = None,
    max_table_rows: int = 500,
) -> ProcessedDocument:
    """Parse CSV or TSV tabular data into a GFM Markdown table and metadata."""
    warnings, decoded = _decode_text(content, encoding)
    if not decoded.strip():
        return ProcessedDocument(None, "", "", {"row_count": 0, "column_count": 0}, warnings)

    delim = delimiter
    if not delim:
        sample = decoded[:4096]
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        except Exception:
            delim = "\t" if "\t" in sample and "," not in sample else ","

    try:
        raw_rows = list(csv.reader(io.StringIO(decoded), delimiter=delim))
    except Exception as exc:
        warnings.append(f"CSV parser error: {exc}")
        return ProcessedDocument(None, decoded, decoded, {"delimiter": delim}, warnings)

    if not raw_rows:
        return ProcessedDocument(None, "", "", {"row_count": 0, "column_count": 0, "delimiter": delim}, warnings)

    row_count = len(raw_rows)
    col_count = max(len(r) for r in raw_rows)
    if col_count == 0:
        return ProcessedDocument(
            None, "", "", {"row_count": row_count, "column_count": 0, "delimiter": delim}, warnings
        )
    metadata = {
        "row_count": row_count,
        "column_count": col_count,
        "delimiter": delim,
        "encoding": encoding or "utf-8",
    }

    table_md, truncated = _format_gfm_table(
        raw_rows, newline_replacement="<br>", max_data_rows=max_table_rows
    )
    if truncated:
        warnings.append(f"Markdown table truncated to {max_table_rows} rows.")

    return ProcessedDocument(
        title=None,
        markdown=table_md,
        text=decoded,
        metadata=metadata,
        warnings=warnings,
    )


def process_json(content: bytes, encoding: str | None = None) -> ProcessedDocument:
    """Format JSON content as pretty-printed code block with structural metadata."""
    warnings, decoded = _decode_text(content, encoding)
    metadata: dict[str, Any] = {"encoding": encoding or "utf-8"}
    title: str | None = None
    try:
        parsed = json.loads(decoded)
    except Exception as exc:
        warnings.append(f"Malformed JSON: {exc}")
        return ProcessedDocument(None, f"```json\n{decoded}\n```", decoded, metadata, warnings)

    if isinstance(parsed, dict):
        metadata["type"] = "object"
        metadata["keys"] = list(parsed.keys())[:30]
        metadata["key_count"] = len(parsed)
        title_val = parsed.get("title") or parsed.get("name")
        if isinstance(title_val, str) and len(title_val) <= 120:
            title = title_val
    elif isinstance(parsed, list):
        metadata["type"] = "array"
        metadata["item_count"] = len(parsed)
    else:
        metadata["type"] = type(parsed).__name__

    pretty_json = json.dumps(parsed, indent=2, ensure_ascii=False)
    return ProcessedDocument(title, f"```json\n{pretty_json}\n```", pretty_json, metadata, warnings)


def _xml_local_name(elem: Any) -> str:
    tag = getattr(elem, "tag", None)
    return tag.split("}")[-1].lower() if isinstance(tag, str) else ""


def _xml_child_text(elem: Any, names: set[str]) -> str:
    for child in elem:
        if _xml_local_name(child) in names:
            txt = "".join(child.itertext()).strip()
            if txt:
                return txt
    return ""


def _xml_entry_link(elem: Any) -> str:
    fallback = ""
    for child in elem:
        if _xml_local_name(child) != "link":
            continue
        href = (child.get("href") or "").strip()
        rel = (child.get("rel") or "alternate").strip().lower()
        if href:
            if rel == "alternate":
                return href
            if not fallback:
                fallback = href
        txt = "".join(child.itertext()).strip()
        if txt and not fallback:
            fallback = txt
    return fallback


def _xml_entry_author(entry: Any) -> str:
    for child in entry:
        cname = _xml_local_name(child)
        if cname == "author":
            author = _xml_child_text(child, {"name"}) or "".join(child.itertext()).strip()
            if author:
                return author
        elif cname == "creator":
            author = "".join(child.itertext()).strip()
            if author:
                return author
    return ""


def _render_feed_fragment(raw_body: str, base_url: str = "") -> tuple[str, str]:
    body = raw_body.strip()
    if not body:
        return "", ""
    if "<" in body and ">" in body:
        from bs4 import BeautifulSoup
        from .cleaner import clean_html
        from .markdown import html_to_markdown

        soup = clean_html(BeautifulSoup(body, "lxml"), "standard", copy=False)
        md = html_to_markdown(soup, base_url)
        txt = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
        if md:
            return md, txt
    clean_txt = html_module.unescape(re.sub(r"\r\n?", "\n", body).strip())
    return clean_txt, clean_txt


def _process_syndication_feed(
    root: Any,
    encoding: str | None,
    warnings: list[str],
) -> ProcessedDocument | None:
    root_local = _xml_local_name(root)
    if root_local == "feed":
        feed_type, container = "atom", root
        entries = [c for c in root if _xml_local_name(c) == "entry"]
    elif root_local in {"rss", "rdf"}:
        feed_type = "rss"
        container = next((c for c in root if _xml_local_name(c) == "channel"), root)
        entries = [c for c in container if _xml_local_name(c) == "item"]
        if not entries and container is not root:
            entries = [c for c in root if _xml_local_name(c) == "item"]
    else:
        return None

    if not entries:
        return None

    feed_title = _xml_child_text(container, {"title"})
    feed_subtitle = _xml_child_text(container, {"subtitle", "description"})
    feed_link = _xml_entry_link(container)

    md_sections: list[str] = [f"# {feed_title}"] if feed_title else []
    text_sections: list[str] = [feed_title] if feed_title else []
    if feed_subtitle and feed_subtitle != feed_title:
        sub_md, sub_txt = _render_feed_fragment(feed_subtitle, feed_link)
        if sub_md:
            md_sections.append(sub_md)
            text_sections.append(sub_txt)

    rendered_count = 0
    for idx, entry in enumerate(entries):
        entry_title = _xml_child_text(entry, {"title"})
        entry_link = _xml_entry_link(entry) or feed_link
        raw_body = (
            _xml_child_text(entry, {"encoded", "content"})
            or _xml_child_text(entry, {"summary", "description"})
        )
        body_md, body_txt = _render_feed_fragment(raw_body, entry_link)
        if not entry_title and not body_md:
            continue

        rendered_count += 1
        entry_md_parts: list[str] = []
        entry_txt_parts: list[str] = []

        if entry_title and (idx == 0 or entry_title != feed_title):
            entry_md_parts.append(
                f"## [{entry_title}]({entry_link})" if entry_link else f"## {entry_title}"
            )
            entry_txt_parts.append(entry_title)

        author = _xml_entry_author(entry)
        timestamp = _xml_child_text(entry, {"published", "updated", "pubdate", "date"})
        meta_bits = [
            bit
            for bit in (
                f"**Author:** {author}" if author else "",
                f"**Date:** {timestamp}" if timestamp else "",
            )
            if bit
        ]
        if meta_bits:
            entry_md_parts.append(" | ".join(meta_bits))
            entry_txt_parts.append(" | ".join(b.replace("**", "") for b in meta_bits))

        if body_md:
            entry_md_parts.append(body_md)
            entry_txt_parts.append(body_txt)

        md_sections.append("\n\n".join(entry_md_parts))
        text_sections.append("\n".join(entry_txt_parts))

    if rendered_count == 0:
        return None

    return ProcessedDocument(
        title=feed_title or root_local,
        markdown="\n\n---\n\n".join(md_sections),
        text="\n\n".join(text_sections),
        metadata={
            "root_element": root.tag,
            "encoding": encoding,
            "feed_type": feed_type,
            "entry_count": rendered_count,
        },
        warnings=warnings,
    )


def process_xml(content: bytes, encoding: str | None = None) -> ProcessedDocument:
    """Parse XML and format syndication feeds as Markdown or generic XML as fenced code."""
    strict_parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    warnings: list[str] = []
    root: Any = None
    try:
        root = etree.fromstring(content, parser=strict_parser)
    except etree.XMLSyntaxError:
        lenient_parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
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

    feed_doc = _process_syndication_feed(root, encoding, warnings)
    if feed_doc is not None:
        return feed_doc

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
    warnings, text = _decode_text(content, encoding)
    text = re.sub(r"\r\n?", "\n", text)
    return ProcessedDocument(None, text, text, {"encoding": encoding or "utf-8"}, warnings)


def extract_document(
    content: bytes,
    content_type: str | None = None,
    url: str | None = None,
    encoding: str | None = None,
) -> ProcessedDocument:
    """Unified convenience extractor for non-HTML byte streams."""
    from .pipeline import detect_document_kind

    kind = detect_document_kind(content_type or "", content, url=url)
    if kind == "pdf":
        return process_pdf(content)
    if kind == "docx":
        return process_docx(content)
    if kind == "csv":
        return process_csv(content, encoding=encoding, delimiter=",")
    if kind == "tsv":
        return process_csv(content, encoding=encoding, delimiter="\t")
    if kind == "json":
        return process_json(content, encoding=encoding)
    if kind == "xml":
        return process_xml(content, encoding=encoding)
    return process_text(content, encoding=encoding)
