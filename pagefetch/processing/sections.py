"""Unified DOM engine for static and dynamically expanded tab sections."""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup, Tag

from ..models import ExpandedSection
from .markdown import MarkdownConverter

INTERNAL_EXPANSION_ATTRS: frozenset[str] = frozenset(
    {
        "data-pf-exp-id",
        "data-pf-exp-seq",
        "data-pf-tablist-id",
        "data-pf-tab-id",
        "data-pf-tab-widget",
        "data-pf-active-panel",
        "data-pf-panel-group",
    }
)

SECTION_META_ATTRS: frozenset[str] = frozenset({"data-pf-section", "data-pf-section-title"})

_TAB_PANEL_CLASS_RE = re.compile(
    r"(?:^|[-_\s])(tab[-_]?pane|tab[-_]?panel|ant-tabs-tabpane)(?:$|[-_\s])",
    re.IGNORECASE,
)
HIDDEN_STYLE_RE = re.compile(
    r"(?:^|;)\s*(?:display|visibility)\s*:\s*(?:none|hidden)\s*(?:!important)?(?=\s*(?:;|$))",
    re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class RawExpandedSection:
    title: str
    kind: str
    html: str
    group_id: str = ""
    controls_id: str | None = None


def fingerprint_text(text: str) -> str:
    """Return a whitespace- and punctuation-insensitive comparison key."""
    return re.sub(r"\W+", "", text.lower())


def is_tab_panel_tag(tag: Tag) -> bool:
    """Return True when *tag* carries standard ARIA or CSS tab-panel semantics."""
    role = str(tag.get("role", "")).lower()
    if role == "tabpanel":
        return True
    if tag.has_attr("data-tab-panel") or tag.has_attr("data-tab-content"):
        return True
    raw_cls = tag.attrs.get("class") if tag.attrs else None
    if isinstance(raw_cls, list):
        classes = " ".join(str(c) for c in raw_cls)
    elif raw_cls is None:
        classes = ""
    else:
        classes = str(raw_cls)
    return bool(_TAB_PANEL_CLASS_RE.search(classes))


def unhide_tag(tag: Tag) -> None:
    """Remove HTML ``hidden``, ``aria-hidden="true"``, and inline hide declarations."""
    tag.attrs.pop("hidden", None)
    if str(tag.get("aria-hidden", "")).lower() == "true":
        tag.attrs.pop("aria-hidden", None)
    if tag.has_attr("style"):
        cleaned_style = HIDDEN_STYLE_RE.sub("", str(tag.get("style", ""))).strip("; ")
        if cleaned_style:
            tag["style"] = cleaned_style
        else:
            tag.attrs.pop("style", None)


def pop_section_attrs(soup: BeautifulSoup) -> list[tuple[Tag, str, Any]]:
    """Temporarily detach ``SECTION_META_ATTRS`` from tags in *soup* and return saved state."""
    saved: list[tuple[Tag, str, Any]] = []
    for tag in soup.find_all(attrs={"data-pf-section": True}):
        if isinstance(tag, Tag) and tag.attrs:
            for attr in SECTION_META_ATTRS:
                if attr in tag.attrs:
                    saved.append((tag, attr, tag.attrs.pop(attr)))
    return saved


def restore_section_attrs(saved: list[tuple[Tag, str, Any]]) -> None:
    """Restore ``SECTION_META_ATTRS`` previously popped via :func:`pop_section_attrs`."""
    for tag, attr, val in saved:
        tag.attrs[attr] = val


def strip_attrs(soup: BeautifulSoup, attrs: frozenset[str]) -> None:
    """Delete any attributes in *attrs* directly from parsed BeautifulSoup tags."""
    for tag in soup.find_all(True):
        if isinstance(tag, Tag) and tag.attrs:
            for attr in attrs:
                tag.attrs.pop(attr, None)


def ensure_section_heading(soup: BeautifulSoup, panel: Tag, title: str) -> None:
    """Prepend an ``<h2>`` matching *title* when no equivalent leading heading exists."""
    clean_title = html_lib.unescape(title).strip()
    if not clean_title:
        return
    existing_h = panel.find(["h1", "h2", "h3", "h4"])
    if (
        existing_h is not None
        and existing_h.get_text(" ", strip=True).strip().lower() == clean_title.lower()
    ):
        return
    h2 = soup.new_tag("h2")
    h2.string = clean_title
    panel.insert(0, h2)


def resolve_static_tab_title(soup: BeautifulSoup, panel: Tag, index: int) -> str:
    """Resolve the display title of a static tab panel via ARIA or target triggers."""
    labelled_by = str(panel.get("aria-labelledby") or "").strip()
    if labelled_by:
        for ref_id in labelled_by.split():
            label_el = soup.find(id=ref_id)
            if isinstance(label_el, Tag):
                txt = label_el.get_text(" ", strip=True)
                if txt:
                    return txt

    panel_id = str(panel.get("id") or "").strip()
    if panel_id:
        for attr in ("aria-controls", "data-bs-target", "data-target"):
            target_val = panel_id if attr == "aria-controls" else f"#{panel_id}"
            trigger = soup.find(attrs={attr: target_val})
            if isinstance(trigger, Tag):
                txt = trigger.get_text(" ", strip=True)
                if txt:
                    return txt
        link_trigger = soup.find("a", attrs={"href": f"#{panel_id}"})
        if isinstance(link_trigger, Tag):
            txt = link_trigger.get_text(" ", strip=True)
            if txt:
                return txt

    for attr in ("data-tab-title", "data-title", "aria-label", "title"):
        val = str(panel.get(attr) or "").strip()
        if val:
            return val

    return f"Tab {index}"


def promote_static_panels(
    soup: BeautifulSoup,
    *,
    is_substantive_hidden: Callable[[Tag], bool],
    is_hidden_style: Callable[[str], bool] | None = None,
) -> None:
    """Unhide and annotate static multi-tab panels already present in SSR HTML."""
    hidden_check = is_hidden_style or (lambda s: bool(HIDDEN_STYLE_RE.search(s)))
    candidates: list[Tag] = []
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag) or tag.attrs is None or tag.parent is None:
            continue
        if (
            tag.has_attr("data-pf-section")
            or tag.find_parent(attrs={"data-pf-section": True}) is not None
        ):
            continue
        if not is_tab_panel_tag(tag):
            continue
        style = str(tag.get("style", "")).lower()
        is_hidden = tag.has_attr("hidden") or hidden_check(style)
        if is_hidden and not is_substantive_hidden(tag):
            continue
        if not is_hidden and len(tag.get_text(" ", strip=True)) < 15:
            continue
        candidates.append(tag)

    # Exclude nested tab panels inside another selected candidate so sections never double-emit.
    candidate_ids = {id(c) for c in candidates}
    top_candidates = [
        panel
        for panel in candidates
        if not any(id(parent) in candidate_ids for parent in panel.parents)
    ]
    if len(top_candidates) < 2:
        return

    for idx, panel in enumerate(top_candidates, 1):
        title = resolve_static_tab_title(soup, panel, idx)
        unhide_tag(panel)
        panel["data-pf-section"] = "tab"
        panel["data-pf-section-title"] = title
        ensure_section_heading(soup, panel, title)


def prepare_expanded_dom(
    baseline_html: str,
    raw_sections: Sequence[RawExpandedSection],
) -> tuple[str, BeautifulSoup]:
    """Strip internal expansion markers, splice dynamic tab panels once, and return clean AST + HTML."""
    soup = BeautifulSoup(baseline_html, "lxml")

    if not raw_sections:
        strip_attrs(soup, INTERNAL_EXPANSION_ATTRS)
        return str(soup), soup

    by_group: dict[str, list[RawExpandedSection]] = {}
    for sec in raw_sections:
        by_group.setdefault(sec.group_id, []).append(sec)

    # Locate group anchors/widgets BEFORE stripping internal expansion attributes,
    # then strip internal attributes cleanly so downstream AST inspection sees zero data-pf-exp* attrs.
    group_targets: dict[str, tuple[Tag | None, Tag | None, bool]] = {}
    for group_id in by_group:
        if not group_id:
            group_targets[group_id] = (None, None, False)
            continue
        anchor = soup.find(attrs={"data-pf-panel-group": group_id})
        is_panel_group = anchor is not None
        if anchor is None:
            anchor = soup.find(attrs={"data-pf-tab-widget": group_id})
        if anchor is None:
            anchor = soup.find(attrs={"data-pf-tablist-id": group_id})
        widget = soup.find(attrs={"data-pf-tab-widget": group_id})
        group_targets[group_id] = (anchor, widget, is_panel_group)

    strip_attrs(soup, INTERNAL_EXPANSION_ATTRS)

    for group_id, group_sections in by_group.items():
        if not group_sections:
            continue
        anchor, widget, is_panel_group = group_targets[group_id]
        first = group_sections[0]

        # Collect visible static content fingerprints before pruning hidden static panels
        # so dynamically captured duplicates are skipped rather than appended twice.
        existing_fps: set[str] = set()
        if anchor is not None and is_panel_group:
            fp = fingerprint_text(anchor.get_text(" ", strip=True))
            if fp:
                existing_fps.add(fp)

        if widget is not None:
            for sib in list(widget.find_all(True)):
                if not isinstance(sib, Tag) or sib is anchor or sib.attrs is None:
                    continue
                if anchor is not None and (anchor in sib.parents or sib in anchor.parents):
                    continue
                if not is_tab_panel_tag(sib):
                    continue
                s_style = str(sib.get("style", "")).lower()
                s_hidden = sib.has_attr("hidden") or bool(HIDDEN_STYLE_RE.search(s_style))
                if s_hidden:
                    sib.decompose()
                else:
                    sib_fp = fingerprint_text(sib.get_text(" ", strip=True))
                    if sib_fp:
                        existing_fps.add(sib_fp)

        for sec in group_sections[1:]:
            if sec.controls_id:
                ctrl_el = soup.find(id=sec.controls_id)
                if isinstance(ctrl_el, Tag) and ctrl_el is not anchor:
                    if anchor is None or (anchor not in ctrl_el.parents and ctrl_el not in anchor.parents):
                        ctrl_el.decompose()

        if anchor is not None and is_panel_group:
            anchor["data-pf-section"] = first.kind
            anchor["data-pf-section-title"] = first.title
            ensure_section_heading(soup, anchor, first.title)
            extra_sections = group_sections[1:]
        else:
            extra_sections = group_sections

        insert_ref: Tag | None = anchor
        for sec in extra_sections:
            sec_frag = BeautifulSoup(sec.html, "lxml")
            sec_fp = fingerprint_text(sec_frag.get_text(" ", strip=True))
            if not sec_fp or sec_fp in existing_fps:
                continue
            existing_fps.add(sec_fp)

            wrapper = soup.new_tag("section")
            wrapper["data-pf-section"] = sec.kind
            wrapper["data-pf-section-title"] = sec.title
            frag_root = sec_frag.body or sec_frag
            for child in list(frag_root.contents):
                wrapper.append(child.extract())
            ensure_section_heading(soup, wrapper, sec.title)

            if insert_ref is not None and insert_ref.parent is not None:
                insert_ref.insert_after(wrapper)
                insert_ref = wrapper
            else:
                (soup.body or soup).append(wrapper)
                insert_ref = wrapper

    return str(soup), soup


def render_clean_raw_html(html: str, soup: BeautifulSoup | None) -> str:
    """Return raw HTML with internal ``data-pf-section*`` markers omitted without regexes."""
    if soup is None:
        if "data-pf-section" not in html:
            return html
        temp = BeautifulSoup(html, "lxml")
        if not pop_section_attrs(temp):
            return html
        return str(temp)
    saved = pop_section_attrs(soup)
    if not saved:
        return html
    try:
        return str(soup)
    finally:
        restore_section_attrs(saved)


def extract_sections(cleaned: BeautifulSoup, base_url: str) -> list[ExpandedSection]:
    """Extract structured :class:`ExpandedSection` records directly from *cleaned* tags."""
    sections: list[ExpandedSection] = []
    converter = MarkdownConverter(base_url)
    for sec_tag in cleaned.find_all(attrs={"data-pf-section": True}):
        kind = str(sec_tag.get("data-pf-section") or "tab").strip() or "tab"
        title = str(sec_tag.get("data-pf-section-title") or "").strip()

        first_child = sec_tag.find(["h1", "h2", "h3", "h4"])
        detached: tuple[Tag, Any, int] | None = None
        if (
            first_child is not None
            and title
            and first_child.get_text(" ", strip=True).strip().lower() == title.lower()
            and first_child.parent is not None
        ):
            parent = first_child.parent
            idx = parent.contents.index(first_child)
            detached = (parent, first_child.extract(), idx)

        try:
            sec_md = converter.convert(sec_tag)
            sec_text = "\n".join(
                line.strip() for line in sec_tag.get_text("\n").splitlines() if line.strip()
            )
            if not sec_text and not sec_md:
                continue
            sec_html = "".join(str(ch) for ch in sec_tag.contents).strip() or None
            sections.append(
                ExpandedSection(
                    title=title or "Section",
                    kind=kind,
                    markdown=sec_md,
                    text=sec_text,
                    html=sec_html,
                )
            )
        finally:
            if detached is not None:
                parent, node, idx = detached
                parent.insert(idx, node)
    return sections
