"""Content-preserving HTML-to-Markdown conversion."""

from __future__ import annotations

import html as html_module
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "details", "div", "figure", "figcaption",
    "footer", "header", "main", "nav", "p", "section", "summary",
}

_INLINE_TAGS = {
    "span", "small", "sub", "sup", "abbr", "cite", "dfn", "kbd", "mark",
    "q", "samp", "time", "var", "u", "ins", "bdi", "bdo", "label", "button",
}


class MarkdownConverter:
    """A small custom converter that controls preservation and whitespace."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._list_depth = 0

    def convert(self, soup: BeautifulSoup) -> str:
        root = soup.body or soup
        result = self._children(root)
        result = re.sub(r"[ \t]+\n", "\n", result)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()

    def _children(self, tag: Tag) -> str:
        return "".join(self._node(child) for child in tag.children)

    def _node(self, node: NavigableString | Tag) -> str:
        if isinstance(node, NavigableString):
            return re.sub(r"\s+", " ", str(node))
        if not isinstance(node, Tag):
            return ""
        name = node.name.lower()
        if name in {"script", "style", "template", "head"}:
            return ""
        if re.fullmatch(r"h[1-6]", name):
            return f"\n\n{'#' * int(name[1])} {self._inline_children(node).strip()}\n\n"
        if name == "br":
            return "  \n"
        if name == "hr":
            return "\n\n---\n\n"
        if name in {"strong", "b"}:
            value = self._inline_children(node).strip()
            return f"**{value}**" if value else ""
        if name in {"em", "i"}:
            value = self._inline_children(node).strip()
            return f"*{value}*" if value else ""
        if name in {"del", "s", "strike"}:
            value = self._inline_children(node).strip()
            return f"~~{value}~~" if value else ""
        if name == "code" and node.parent and node.parent.name != "pre":
            value = node.get_text()
            fence = "``" if "`" in value else "`"
            return f"{fence}{value}{fence}"
        if name == "pre":
            code = node.find("code")
            value = (code or node).get_text().rstrip("\n")
            language = self._code_language(code or node)
            fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", value)), default=0) + 1)
            return f"\n\n{fence}{language}\n{value}\n{fence}\n\n"
        if name == "a":
            label = self._inline_children(node).strip()
            href = node.get("href")
            if not href:
                return label
            target = urljoin(self.base_url, str(href))
            return f"[{label or target}]({target})"
        if name == "img":
            source = next(
                (node.get(attr) for attr in ("src", "data-src", "data-lazy-src", "data-original") if node.get(attr)),
                None,
            )
            if not source and node.get("srcset"):
                source = str(node["srcset"]).split(",")[0].strip().split()[0]
            if not source:
                return node.get("alt", "")
            alt = str(node.get("alt") or node.get("title") or "").replace("]", "\\]")
            title = f' "{str(node["title"]).replace(chr(34), "&quot;")}"' if node.get("title") else ""
            return f"![{alt}]({urljoin(self.base_url, str(source))}{title})"
        if name == "iframe":
            source = node.get("src")
            title = str(node.get("title") or "Embedded frame").strip()
            return f"[{title}]({urljoin(self.base_url, str(source))})" if source else ""
        if name in {"ul", "ol"}:
            return self._list(node, ordered=name == "ol")
        if name == "li":
            value = self._inline_children(node).strip()
            return f"\n- {value}\n" if value else ""
        if name == "blockquote":
            value = self._children(node).strip()
            return "\n\n" + "\n".join(f"> {line}" if line else ">" for line in value.splitlines()) + "\n\n"
        if name == "table":
            return self._table(node)
        if name == "dl":
            return f"\n\n{self._children(node).strip()}\n\n"
        if name == "dt":
            return f"\n**{self._inline_children(node).strip()}**\n"
        if name == "dd":
            return f": {self._children(node).strip()}\n"
        if name in _BLOCK_TAGS:
            value = self._children(node).strip()
            if name == "summary" and value:
                value = f"**{value}**"
            return f"\n\n{value}\n\n" if value else ""
        if name in _INLINE_TAGS:
            return self._inline_children(node)
        return self._children(node)

    def _inline_children(self, tag: Tag) -> str:
        return "".join(self._node(child) for child in tag.children).strip()

    @staticmethod
    def _code_language(node: Tag) -> str:
        for candidate in (node, node.parent if isinstance(node.parent, Tag) else None):
            if candidate is None:
                continue
            declared = candidate.get("data-language")
            if declared:
                return str(declared).strip().lower()
            for class_name in candidate.get("class", []):
                match = re.search(r"(?:language-|lang-)([\w#+.-]+)", str(class_name), re.IGNORECASE)
                if match:
                    return match.group(1).lower()
        return ""

    def _list(self, tag: Tag, *, ordered: bool) -> str:
        self._list_depth += 1
        lines: list[str] = []
        start = int(tag.get("start", 1)) if ordered and str(tag.get("start", "1")).isdigit() else 1
        items = tag.find_all("li", recursive=False)
        for offset, item in enumerate(items):
            nested = item.find_all(["ul", "ol"], recursive=False)
            # Collect content from non-list children only (avoids DOM mutation).
            content = "".join(
                self._node(child)
                for child in item.children
                if not (isinstance(child, Tag) and child.name in {"ul", "ol"})
            ).strip()
            marker = f"{start + offset}." if ordered else "-"
            indent = "  " * (self._list_depth - 1)
            lines.append(f"{indent}{marker} {content}")
            for child_list in nested:
                nested_output = self._list(child_list, ordered=child_list.name == "ol").strip("\n")
                if nested_output:
                    lines.append(nested_output)
        self._list_depth -= 1
        return "\n" + "\n".join(lines) + "\n"

    def _table(self, table: Tag) -> str:
        rows: list[list[str]] = []
        header_index: int | None = None
        row_spans: dict[int, tuple[int, str]] = {}
        table_rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
        for row in table_rows:
            cells = row.find_all(["th", "td"], recursive=False)
            if not cells:
                continue
            if any(cell.name == "th" for cell in cells) and header_index is None:
                header_index = len(rows)
            values_by_column: dict[int, str] = {}
            for column, (remaining, value) in list(row_spans.items()):
                values_by_column[column] = value
                if remaining <= 1:
                    del row_spans[column]
                else:
                    row_spans[column] = (remaining - 1, value)
            column = 0
            for cell in cells:
                while column in values_by_column:
                    column += 1
                value = re.sub(r"\s+", " ", self._children(cell).strip()).replace("|", "\\|")
                colspan = self._span_value(cell.get("colspan"))
                rowspan = self._span_value(cell.get("rowspan"))
                for offset in range(colspan):
                    target_column = column + offset
                    values_by_column[target_column] = value if offset == 0 else ""
                    if rowspan > 1:
                        row_spans[target_column] = (rowspan - 1, value if offset == 0 else "")
                column += colspan
            row_width = max(values_by_column, default=-1) + 1
            rows.append([values_by_column.get(column, "") for column in range(row_width)])
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        if header_index is None:
            rows.insert(0, ["" for _ in range(width)])
        elif header_index != 0:
            rows.insert(0, rows.pop(header_index))
        output = ["| " + " | ".join(row) + " |" for row in rows]
        output.insert(1, "| " + " | ".join(["---"] * width) + " |")
        return "\n\n" + "\n".join(output) + "\n\n"

    @staticmethod
    def _span_value(value: object) -> int:
        try:
            return max(1, min(int(str(value)), 50))
        except (TypeError, ValueError):
            return 1


def html_to_markdown(soup: BeautifulSoup, base_url: str) -> str:
    """Convert a cleaned document to Markdown."""
    return html_module.unescape(MarkdownConverter(base_url).convert(soup))
