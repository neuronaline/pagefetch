"""Shared rendering utilities for CLI and interactive modes."""

from __future__ import annotations

import json

from ..models import FetchResult


def render_results(
    results: list[FetchResult],
    output_format: str,
    *,
    include_html: bool = False,
    include_structure: bool = False,
    compact_structure: bool = False,
    include_screenshot: bool = False,
) -> str:
    """Render fetch results in the chosen format.

    Supported formats: ``markdown``, ``json``, ``html``, ``structure``, and
    ``raw``. ``raw`` emits a single JSON array document where each element is
    the serialized result (raw HTML, optional structure summary, optional
    base64-encoded screenshot) — the whole payload is parseable by
    ``json.load`` without custom splitting. ``compact_structure`` only
    affects JSON output and is forwarded to :meth:`FetchResult.to_dict`.
    """
    if output_format == "json":
        values = [
            result.to_dict(
                include_html=include_html,
                include_structure=include_structure,
                compact_structure=compact_structure,
                include_screenshot=include_screenshot,
            )
            for result in results
        ]
        return json.dumps(values, ensure_ascii=False, indent=2)
    if output_format == "raw":
        # ``raw`` emits a single, parseable JSON document carrying one
        # serialized result per item. The wrapper is a JSON array so
        # downstream consumers can pipe the output straight into
        # ``json.load`` without custom splitting. The element separator is
        # the standard JSON comma; ``_RAW_BOUNDARY`` is intentionally not
        # used between elements because it would invalidate the array —
        # base64 screenshot payloads are guaranteed not to produce
        # structural commas inside a JSON value.
        documents = [
            result.json(
                include_html=True,
                include_structure=result.structure is not None,
                include_screenshot=result.screenshot is not None,
                indent=2,
            )
            for result in results
        ]
        return "[" + ",".join(documents) + "]"
    if output_format == "html":
        documents = [result.html or result.text or "" for result in results]
    elif output_format == "structure":
        documents = [_render_structure(result) for result in results]
    else:
        documents = [result.markdown or result.json() for result in results]
    return "\n\n---\n\n".join(documents)


def _render_structure(result: FetchResult) -> str:
    """Return a Markdown view of the page structure when available."""
    structure = result.structure
    if structure is None:
        return "(structure not requested; pass --include-structure to capture it)"
    lines: list[str] = [
        f"# {result.title or result.url}",
        "",
        f"- Truncated: {structure.truncated}",
        f"- Nodes captured: {structure.node_count}",
        f"- Max depth: {structure.max_depth}",
        "",
    ]
    if structure.stylesheets:
        lines.append("## Stylesheets")
        lines.append("")
        for sheet in structure.stylesheets:
            media = f" media={sheet.media!r}" if sheet.media else ""
            lines.append(f"- {sheet.url}{media}")
        lines.append("")
    if structure.inline_styles:
        lines.append("## Inline styles")
        lines.append("")
        for index, style in enumerate(structure.inline_styles, 1):
            truncated = " (truncated)" if style.truncated else ""
            lines.append(f"```css{index}{truncated}")
            lines.append(style.content)
            lines.append("```")
        lines.append("")
    if structure.scripts:
        lines.append("## Scripts")
        lines.append("")
        for script in structure.scripts:
            flags = " ".join(
                flag
                for flag, on in (
                    ("async", script.async_),
                    ("defer", script.defer),
                )
                if on
            )
            type_part = f" type={script.type!r}" if script.type else ""
            flag_part = f" {flags}" if flags else ""
            lines.append(f"- {script.url}{type_part}{flag_part}")
        lines.append("")
    if structure.inline_scripts:
        lines.append("## Inline scripts")
        lines.append("")
        for index, script in enumerate(structure.inline_scripts, 1):
            truncated = " (truncated)" if script.truncated else ""
            type_part = f" type={script.type!r}" if script.type else ""
            lines.append(f"```js{index}{type_part}{truncated}")
            lines.append(script.content)
            lines.append("```")
        lines.append("")
    if structure.root is not None:
        lines.append("## DOM tree")
        lines.append("")
        lines.extend(_render_node(structure.root))
    return "\n".join(lines)


def _render_node(node, indent: int = 0) -> list[str]:
    """Render a DOM node tree with copyable, unique CSS selectors."""
    selector = node.unique_selector or node.path or node.selector
    bullet = "  " * indent + f"- `{selector}`"
    if node.text:
        bullet += f": {node.text}"
    lines = [bullet]
    for child in node.children:
        lines.extend(_render_node(child, indent + 1))
    return lines
