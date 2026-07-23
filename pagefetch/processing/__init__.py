"""Content processing pipeline — HTML parsing, Markdown conversion, non-HTML handling."""

from .cleaner import clean_html
from .detector import ConfidenceReport, analyze_html
from .html import ProcessedHTML, process_html
from .images import extract_images
from .links import extract_links
from .markdown import MarkdownConverter, html_to_markdown
from .metadata import extract_metadata
from .non_html import ProcessedDocument, process_pdf, process_text, process_xml

__all__ = [
    "ConfidenceReport",
    "MarkdownConverter",
    "ProcessedDocument",
    "ProcessedHTML",
    "analyze_html",
    "clean_html",
    "extract_images",
    "extract_links",
    "extract_metadata",
    "html_to_markdown",
    "process_html",
    "process_pdf",
    "process_text",
    "process_xml",
]
