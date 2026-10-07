"""PDF, DOCX, CSV, JSON, XML, and plain-text document extraction and Markdown conversion."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Any

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DOCX_NS = {"w": W_NS, "r": R_NS}

CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"
DCTERMS_NS = "http://purl.org/dc/terms/"
CORE_NS = {"cp": CP_NS, "dc": DC_NS, "dcterms": DCTERMS_NS}


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


def process_pdf(content: bytes) -> ProcessedDocument:
    """Extract text and basic metadata from a PDF byte stream."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise MissingOptionalDependency(
            "PDF support requires the optional dependency: pip install 'pagefetch[pdf]'"
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

        # Clean line breaks and soft hyphens
        normalized = raw_text.replace("\xad", "").replace("\r\n", "\n").replace("\r", "\n")
        # Join words split by hyphenation at line breaks: "inter-\nnet" -> "internet"
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
        # Fall back to first non-empty line of page 1 if it looks like a document title
        first_line = page_texts[0].split("\n", 1)[0].strip()
        if first_line and len(first_line) <= 120 and not first_line.endswith("."):
            title = first_line

    full_text = "\n\n".join(page_texts)
    markdown_parts: list[str] = []
    if title:
        markdown_parts.append(f"# {title}")
    for index, text in enumerate(page_texts, 1):
        markdown_parts.append(f"## Page {index}\n\n{text}")

    return ProcessedDocument(title, "\n\n".join(markdown_parts), full_text, metadata, warnings)


def process_docx(content: bytes) -> ProcessedDocument:
    """Extract structured Markdown and metadata from a DOCX (OpenXML) byte stream."""
    warnings: list[str] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except Exception as exc:
        raise ValueError(f"Invalid DOCX archive: {exc}") from exc

    namelist = zf.namelist()
    if "word/document.xml" not in namelist:
        raise ValueError("Invalid DOCX format: missing word/document.xml")

    # 1. Extract Dublin Core metadata
    metadata: dict[str, Any] = {}
    title: str | None = None
    if "docProps/core.xml" in namelist:
        try:
            core_root = etree.fromstring(zf.read("docProps/core.xml"))
            dc_title = core_root.find(".//dc:title", CORE_NS)
            if dc_title is not None and dc_title.text:
                title = dc_title.text.strip()
                metadata["title"] = title
            dc_creator = core_root.find(".//dc:creator", CORE_NS)
            if dc_creator is not None and dc_creator.text:
                metadata["creator"] = dc_creator.text.strip()
            cp_modified_by = core_root.find(".//cp:lastModifiedBy", CORE_NS)
            if cp_modified_by is not None and cp_modified_by.text:
                metadata["last_modified_by"] = cp_modified_by.text.strip()
            created = core_root.find(".//dcterms:created", CORE_NS)
            if created is not None and created.text:
                metadata["created"] = created.text.strip()
            modified = core_root.find(".//dcterms:modified", CORE_NS)
            if modified is not None and modified.text:
                metadata["modified"] = modified.text.strip()
        except Exception as exc:
            warnings.append(f"Failed parsing DOCX core metadata: {exc}")

    # 2. Extract hyperlink relationships
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

    # 3. Parse main document
    try:
        doc_root = etree.fromstring(zf.read("word/document.xml"))
    except Exception as exc:
        raise ValueError(f"Failed parsing DOCX XML tree: {exc}") from exc

    def _parse_p(p_elem: Any) -> tuple[str, str]:
        """Return (markdown_str, plain_text_str) for a paragraph element."""
        style_val = ""
        style_elem = p_elem.find(".//w:pStyle", DOCX_NS)
        if style_elem is not None:
            style_val = style_elem.attrib.get(f"{{{W_NS}}}val", "")

        is_list = p_elem.find(".//w:numPr", DOCX_NS) is not None or "list" in style_val.lower()

        md_parts: list[str] = []
        txt_parts: list[str] = []

        for child in p_elem:
            tag = child.tag.split("}")[-1]
            if tag == "r":
                bold = child.find(".//w:b", DOCX_NS) is not None
                italic = child.find(".//w:i", DOCX_NS) is not None
                strike = child.find(".//w:strike", DOCX_NS) is not None
                run_text = "".join(child.xpath(".//w:t/text()", namespaces=DOCX_NS))
                if run_text:
                    txt_parts.append(run_text)
                    if bold or italic or strike:
                        l_ws = run_text[: len(run_text) - len(run_text.lstrip())]
                        r_ws = run_text[len(run_text.rstrip()) :]
                        core = run_text.strip()
                        if core:
                            if bold and italic:
                                core = f"***{core}***"
                            elif bold:
                                core = f"**{core}**"
                            elif italic:
                                core = f"*{core}*"
                            if strike:
                                core = f"~~{core}~~"
                            formatted = f"{l_ws}{core}{r_ws}"
                        else:
                            formatted = run_text
                    else:
                        formatted = run_text
                    md_parts.append(formatted)
            elif tag == "hyperlink":
                r_id = child.attrib.get(f"{{{R_NS}}}id")
                url = rels.get(r_id or "", "")
                link_text = "".join(child.xpath(".//w:t/text()", namespaces=DOCX_NS))
                if link_text:
                    txt_parts.append(link_text)
                    if url:
                        safe_label = link_text.replace("[", r"\[").replace("]", r"\]")
                        md_parts.append(f"[{safe_label}]({url})")
                    else:
                        md_parts.append(link_text)

        plain_line = "".join(txt_parts).strip()
        md_line = "".join(md_parts).strip()
        if not md_line:
            return "", ""

        # Map headings
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

    def _parse_tbl(tbl_elem: Any) -> tuple[str, str]:
        """Convert a table element into a GFM table."""
        rows: list[list[str]] = []
        raw_rows: list[list[str]] = []
        for tr in tbl_elem.findall("./w:tr", DOCX_NS):
            cells: list[str] = []
            raw_cells: list[str] = []
            for tc in tr.findall("./w:tc", DOCX_NS):
                cell_md: list[str] = []
                cell_txt: list[str] = []
                for p in tc.findall(".//w:p", DOCX_NS):
                    p_md, p_txt = _parse_p(p)
                    if p_md:
                        cell_md.append(p_md)
                    if p_txt:
                        cell_txt.append(p_txt)
                cell_value = " ".join(cell_md).replace("|", r"\|").replace("\n", " ").strip()
                cells.append(cell_value)
                raw_cells.append(" ".join(cell_txt).strip())
            if any(cells):
                rows.append(cells)
                raw_rows.append(raw_cells)

        if not rows:
            return "", ""

        width = max(len(r) for r in rows)
        padded = [r + [""] * (width - len(r)) for r in rows]
        md_lines = ["| " + " | ".join(padded[0]) + " |"]
        md_lines.append("| " + " | ".join(["---"] * width) + " |")
        for r in padded[1:]:
            md_lines.append("| " + " | ".join(r) + " |")

        txt_lines = ["\t".join(r) for r in raw_rows]
        return "\n".join(md_lines), "\n".join(txt_lines)

    body = doc_root.find("w:body", DOCX_NS)
    md_blocks: list[str] = []
    txt_blocks: list[str] = []

    if body is not None:
        for child in body:
            tag = child.tag.split("}")[-1]
            if tag == "p":
                p_md, p_txt = _parse_p(child)
                if p_md:
                    md_blocks.append(p_md)
                if p_txt:
                    txt_blocks.append(p_txt)
            elif tag == "tbl":
                t_md, t_txt = _parse_tbl(child)
                if t_md:
                    md_blocks.append(t_md)
                if t_txt:
                    txt_blocks.append(t_txt)

    if not title and md_blocks:
        first_line = md_blocks[0]
        if first_line.startswith("# "):
            title = first_line[2:].strip()

    markdown = "\n\n".join(md_blocks)
    text = "\n\n".join(txt_blocks)
    return ProcessedDocument(title, markdown, text, metadata, warnings)


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

    # Autodetect delimiter if not provided
    delim = delimiter
    if not delim:
        sample = decoded[:4096]
        try:
            sniffer = csv.Sniffer()
            dialect = sniffer.sniff(sample, delimiters=",\t;|")
            delim = dialect.delimiter
        except Exception:
            delim = "\t" if "\t" in sample and "," not in sample else ","

    reader = csv.reader(io.StringIO(decoded), delimiter=delim)
    try:
        raw_rows = list(reader)
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

    # Format into GFM Markdown table
    header = [cell.replace("|", r"\|").replace("\r", "").replace("\n", "<br>").strip() for cell in raw_rows[0]]
    if len(header) < col_count:
        header += [""] * (col_count - len(header))

    md_lines = ["| " + " | ".join(header) + " |"]
    md_lines.append("| " + " | ".join(["---"] * col_count) + " |")

    data_rows = raw_rows[1:]
    display_rows = data_rows[:max_table_rows]
    for row in display_rows:
        cleaned = [cell.replace("|", r"\|").replace("\r", "").replace("\n", "<br>").strip() for cell in row]
        if len(cleaned) < col_count:
            cleaned += [""] * (col_count - len(cleaned))
        md_lines.append("| " + " | ".join(cleaned) + " |")

    table_md = "\n".join(md_lines)
    if len(data_rows) > max_table_rows:
        table_md += (
            f"\n\n*Showing first {max_table_rows} of {len(data_rows)} data rows. "
            f"Complete data available in text.*"
        )
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
    markdown = f"```json\n{pretty_json}\n```"
    return ProcessedDocument(title, markdown, pretty_json, metadata, warnings)


def process_xml(content: bytes, encoding: str | None = None) -> ProcessedDocument:
    """Parse XML and retain its hierarchy in a fenced representation."""
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
