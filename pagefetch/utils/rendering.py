"""Shared rendering utilities for CLI and interactive modes."""

from __future__ import annotations

import json

from ..models import FetchResult


def render_results(results: list[FetchResult], output_format: str, *, include_html: bool = False) -> str:
    """Render fetch results in the chosen format (markdown, json, or html)."""
    if output_format == "json":
        values = [result.to_dict(include_html=include_html) for result in results]
        return json.dumps(values, ensure_ascii=False, indent=2)
    if output_format == "html":
        documents = [result.html or result.text or "" for result in results]
    else:
        documents = [result.markdown or result.json() for result in results]
    return "\n\n---\n\n".join(documents)
