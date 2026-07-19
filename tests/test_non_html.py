"""PDF, XML, and plain-text processing tests."""

from __future__ import annotations

import io

from pypdf import PdfWriter

from pagefetch.processing.non_html import process_pdf, process_text, process_xml


def test_pdf_metadata_and_empty_page_handling():
    """Verify that a blank PDF extracts metadata and warns about empty pages."""
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_metadata({"/Title": "Test Document", "/Author": "PageFetch Tester"})
    buf = io.BytesIO()
    writer.write(buf)

    result = process_pdf(buf.getvalue())
    assert result.title == "Test Document"
    assert result.markdown.startswith("# Test Document")
    assert isinstance(result.text, str)
    assert result.metadata.get("Title") == "Test Document"
    assert result.metadata.get("Author") == "PageFetch Tester"


def test_pdf_with_empty_page_adds_warning():
    """Empty pages result in warnings, not crashes."""
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)

    result = process_pdf(buf.getvalue())
    assert "No extractable text was found on PDF page 1." in result.warnings


def test_xml_preserves_hierarchy_and_emits_fenced_block():
    xml_bytes = (
        b'<?xml version="1.0" encoding="utf-8"?>\n'
        b'<catalog>\n'
        b'  <book id="1"><title>One</title></book>\n'
        b'  <book id="2"><title>Two</title></book>\n'
        b"</catalog>"
    )
    result = process_xml(xml_bytes, "utf-8")
    assert result.title == "catalog"
    assert "```xml" in result.markdown
    assert "<book" in result.markdown
    assert "One" in result.text
    assert "Two" in result.text
    assert result.metadata["encoding"] == "utf-8"
    assert result.metadata["root_element"] == "catalog"


def test_xml_secure_parser_prevents_entity_expansion():
    """The parser with resolve_entities=False leaves entities as literal text."""
    malicious = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
        b"<root>&xxe;</root>"
    )
    result = process_xml(malicious)
    # Entity expansion is disabled; the literal "&xxe;" appears instead of file contents.
    assert "passwd" not in result.text


def test_plain_text_decodes_and_preserves_lines():
    raw = b"Line 1\r\nLine 2\r\n  Line 3\n"
    result = process_text(raw, "utf-8")
    assert result.text == "Line 1\nLine 2\n  Line 3\n"
    assert result.markdown == result.text
    assert result.title is None
    assert result.metadata["encoding"] == "utf-8"


def test_plain_text_falls_back_on_bad_encoding():
    raw = "Café résumé".encode("cp1252")
    result = process_text(raw, "ascii")  # impossible encoding
    # Falls back to utf-8 with replacement characters for non-decodable bytes
    assert "sum" in result.text
    assert result.metadata["encoding"] == "utf-8"
