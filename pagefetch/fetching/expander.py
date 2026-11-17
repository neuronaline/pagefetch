"""Browser overlay neutralization and automatic in-DOM content/tab expansion."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urldefrag

from ..processing.challenges import EXPANDER_CHALLENGE_GUARD_SELECTOR
from ..processing.sections import RawExpandedSection, fingerprint_text
from .readiness import wait_for_stability

logger = logging.getLogger("pagefetch.fetching.expander")


async def _wait_after_interaction(
    page: Any,
    *,
    deadline: float,
    network_activity: Callable[[], tuple[int, float]] | None = None,
    max_wait: float = 1.8,
) -> None:
    """Allow click-triggered in-DOM updates or XHRs to settle before snapshotting."""
    budget = min(max_wait, max(0.2, deadline - time.monotonic()))
    wait_end = time.monotonic() + budget
    await asyncio.sleep(min(0.06, max(0.01, wait_end - time.monotonic())))
    if network_activity is not None:
        net_cap = min(wait_end, time.monotonic() + 1.2)
        while time.monotonic() < net_cap:
            active, _ = network_activity()
            if active <= 0:
                break
            await asyncio.sleep(0.06)
    rem = max(0.15, wait_end - time.monotonic())
    await wait_for_stability(
        page,
        timeout=rem,
        stable_rounds=2,
        network_activity=network_activity,
    )


_CHALLENGE_GUARD_JSON = json.dumps(EXPANDER_CHALLENGE_GUARD_SELECTOR)

_JS_NEUTRALIZE_OVERLAYS = f"""() => {{
    const body = document.body;
    const docEl = document.documentElement;
    if (!body || !docEl) return {{dismissed: 0, unlocked: false}};

    if (document.querySelector({_CHALLENGE_GUARD_JSON})) {{
        return {{dismissed: 0, unlocked: false}};
    }}

    const overlayRe = /(?:^|[-_\\s])(onetrust|cybotcookiebot|cookiebot|didomi|klaro|qc-cmp|usercentrics|trustarc|fc-consent|efilli|kvkk|cookie[-_\\s]?(?:banner|consent|modal|popup|dialog|bar|overlay|notice)|consent[-_\\s]?(?:banner|modal|popup|overlay|dialog|bar)|gdpr[-_\\s]?(?:banner|modal|consent|overlay)|ccpa[-_\\s]?banner|newsletter[-_\\s]?(?:modal|popup|overlay)|modal[-_\\s]?backdrop)(?:$|[-_\\s])/i;
    const safeDismissRe = /^(?:×|✕|✖|x|close|dismiss|reject(?:\\s+all)?|decline|only\\s+necessary|essential\\s+only|continue\\s+without\\s+accepting|kapat|reddet|tümünü\\s+reddet|yalnızca\\s+zorunlu|sadece\\s+gerekli|zorunlu\\s+çerezler)$/i;
    const safeAriaRe = /(?:^|\\b)(close|dismiss|kapat|reddet|reject|decline)(?:\\b|$)/i;

    let dismissed = 0;
    const candidates = document.querySelectorAll(
        '[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i], ' +
        '[id*="onetrust" i], [id*="CybotCookiebot" i], [id*="didomi" i], [class*="kvkk" i], ' +
        '[id*="kvkk" i], [role="dialog"], [aria-modal="true"], [class*="modal-backdrop" i], ' +
        '[class*="popup" i], [id*="newsletter" i], [class*="newsletter" i]'
    );

    for (const el of candidates) {{
        const cls = typeof el.className === 'string' ? el.className : '';
        const ident = `${{el.id || ''}} ${{cls}} ${{el.getAttribute('aria-label') || ''}}`;
        const isModal = el.getAttribute('role') === 'dialog' || el.getAttribute('aria-modal') === 'true';
        const st = window.getComputedStyle(el);
        const isFloating = st.position === 'fixed' || st.position === 'sticky' || (parseInt(st.zIndex || '0', 10) >= 50 && st.position !== 'static');

        if (!overlayRe.test(ident) && !(isModal && isFloating)) continue;
        if (el.querySelector('main, article, [role="main"], nav a[href]')) {{
            if ((el.innerText || '').length > 1200) continue;
        }}

        let clickedBtn = false;
        const buttons = el.querySelectorAll('button, [role="button"], a[href="#"], input[type="button"]');
        for (const btn of buttons) {{
            if (btn.disabled) continue;
            const bText = (btn.innerText || btn.value || '').trim();
            const bAria = (btn.getAttribute('aria-label') || '').trim();
            if (safeDismissRe.test(bText) || safeAriaRe.test(bAria)) {{
                try {{
                    btn.click();
                    clickedBtn = true;
                    dismissed++;
                    break;
                }} catch (_e) {{}}
            }}
        }}

        if (!clickedBtn && st.display !== 'none') {{
            const area = el.offsetWidth * el.offsetHeight;
            const vpArea = Math.max(1, window.innerWidth * window.innerHeight);
            if (isFloating || area > vpArea * 0.12) {{
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('visibility', 'hidden', 'important');
                el.style.setProperty('pointer-events', 'none', 'important');
                dismissed++;
            }}
        }}
    }}

    let unlocked = false;
    for (const root of [body, docEl]) {{
        const st = window.getComputedStyle(root);
        if (st.overflow === 'hidden' || st.overflowY === 'hidden' || root.style.overflow === 'hidden') {{
            root.style.setProperty('overflow', 'visible', 'important');
            root.style.setProperty('overflow-y', 'auto', 'important');
            unlocked = true;
        }}
        if (st.position === 'fixed') {{
            root.style.setProperty('position', 'static', 'important');
            unlocked = true;
        }}
    }}

    for (const mainEl of document.querySelectorAll('main, [role="main"], #__next, #app, #root')) {{
        if (mainEl.hasAttribute('inert')) mainEl.removeAttribute('inert');
        if (mainEl.getAttribute('aria-hidden') === 'true') mainEl.removeAttribute('aria-hidden');
    }}

    return {{dismissed, unlocked}};
}}"""


async def neutralize_overlays_and_unlock_scroll(page: Any) -> None:
    """Dismiss non-content consent/popup overlays and release CSS scroll locks."""
    try:
        await page.evaluate(_JS_NEUTRALIZE_OVERLAYS)
    except Exception as exc:
        logger.debug("Overlay neutralization skipped: %s", exc)


_IN_DOM_HREF_JS = r"""const isInDomHref = (href) => {
    if (href === null || href === undefined) return true;
    const raw = String(href).trim();
    if (!raw || raw === '#' || raw.startsWith('#')) return true;
    const lowered = raw.toLowerCase();
    if (lowered.startsWith('javascript:')) {
        return !lowered.includes('location') && !lowered.includes('open(');
    }
    return false;
};"""

_JS_PREPARE_AND_TAG_DISCLOSURES = f"""(scopeSelector) => {{
    {_IN_DOM_HREF_JS}
    const scope = scopeSelector ? document.querySelector(scopeSelector) : (document.body || document.documentElement);
    if (!scope) return {{detailsOpened: 0, unclamped: 0, candidates: []}};

    const forbiddenRe = /(?:sepete\\s*ekle|satın\\s*al|hemen\\s*al|sipariş|ödeme|giriş\\s*yap|üye\\s*ol|kayıt\\s*ol|çıkış|şifre|(?:^|\\s)sil(?:\\s|$)|kaldır|abone\\s*ol|paylaş|(?:^|\\s)indir(?:\\s|$)|yazdır|kabul\\s*et|çerez|add\\s*to\\s*(?:cart|bag|basket|wishlist)|buy\\s*now|checkout|purchase|sign\\s*in|log\\s*in|login|sign\\s*up|register|logout|sign\\s*out|\\bdelete\\b|\\bremove\\b|subscribe|unsubscribe|\\bshare\\b|\\bdownload\\b|\\bprint\\b|accept\\s*all|cookie|\\breport\\b|\\breply\\b|\\bsubmit\\b|post\\s*comment)/i;

    const readMoreTextRe = /^(?:devamını\\s*(?:oku|gör|göster)|yazının\\s*devamı|haberin\\s*devamı|açıklamanın\\s*tamamı|daha\\s*fazla(?:\\s*(?:göster|gör|oku|yükle|yorum|bilgi|ürün|içerik))?|tümünü\\s*(?:gör|göster|oku)|hepsini\\s*(?:gör|göster)|tamamını\\s*(?:oku|gör|göster)|tüm\\s*özellikleri\\s*(?:gör|göster)|yorumları\\s*(?:gör|göster|yükle)|read\\s*more|show\\s*more|load\\s*more(?:\\s*\\w+)?|see\\s*more|view\\s*more|see\\s*all(?:\\s*\\w+)?|view\\s*all(?:\\s*\\w+)?|read\\s*full(?:\\s*\\w+)?|full\\s*(?:description|article|story|text|specs)|show\\s*all(?:\\s*\\w+)?|more\\s*(?:details|info|reviews|comments)|continue\\s*reading|expand(?:\\s*all)?|mehr\\s*anzeigen|weiterlesen|voir\\s*plus|lire\\s*la\\s*suite|ver\\s*más|leer\\s*más|mostrar\\s*más|показать\\s*ещё|читать\\s*далее)(?:\\s*[\\(\\[\\d+.,\\)\\]+»›→↓⌄v]+)?$/i;

    let detailsOpened = 0;
    for (const det of scope.querySelectorAll('details:not([open])')) {{
        if (det.closest('nav, header, footer, [role="navigation"]')) continue;
        det.open = true;
        detailsOpened++;
    }}

    let unclamped = 0;
    const clampClassRe = /(?:^|[-_\\s])(line-clamp-\\d+|clamped|is-clamped|text-truncate-multiline|read-more-collapsed|is-collapsed)(?:$|[-_\\s])/i;
    for (const el of scope.querySelectorAll('p, div, section, article, span')) {{
        if (el.children.length > 25) continue;
        const cls = typeof el.className === 'string' ? el.className : '';
        const st = window.getComputedStyle(el);
        const hasWebkitClamp = st.webkitLineClamp && st.webkitLineClamp !== 'none' && st.webkitLineClamp !== '0';
        const nextTxt = (el.nextElementSibling?.innerText || '').replace(/\\s+/g, ' ').trim();
        const hasAdjacentShowMore = nextTxt.length >= 4 && nextTxt.length <= 48 && readMoreTextRe.test(nextTxt);
        const hasMaxHeightClip = (
            hasAdjacentShowMore &&
            st.overflowY === 'hidden' &&
            st.maxHeight &&
            st.maxHeight !== 'none' &&
            el.scrollHeight > el.clientHeight + 10
        );
        if (hasWebkitClamp || hasMaxHeightClip || (clampClassRe.test(cls) && el.scrollHeight > el.clientHeight + 20)) {{
            el.style.setProperty('-webkit-line-clamp', 'unset', 'important');
            el.style.setProperty('line-clamp', 'unset', 'important');
            el.style.setProperty('max-height', 'none', 'important');
            el.style.setProperty('overflow', 'visible', 'important');
            unclamped++;
        }}
    }}

    const isVisible = (el) => {{
        const st = window.getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') === 0) return false;
        const r = el.getBoundingClientRect();
        return r.width > 4 && r.height > 4;
    }};

    let seq = Number(document.documentElement.getAttribute('data-pf-exp-seq') || '0');
    const candidates = [];

    const els = scope.querySelectorAll('button, [role="button"], [aria-expanded="false"], a');
    for (const el of els) {{
        if (el.closest('summary')) continue;
        if (el.disabled || el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true') continue;
        if (el.closest('nav, [role="navigation"], [role="menubar"], [role="menu"], footer, .site-header, .site-footer')) continue;
        const tag = el.tagName.toLowerCase();
        if (tag === 'input' || tag === 'select' || tag === 'textarea' || tag === 'summary') continue;
        if (tag === 'button' && (el.getAttribute('type') || '').toLowerCase() === 'submit') continue;
        if (el.closest('form') && tag === 'button' && !el.hasAttribute('type')) continue;
        if (el.getAttribute('role') === 'tab') continue;
        if (!isVisible(el)) continue;

        const href = tag === 'a' ? el.getAttribute('href') : null;
        if (tag === 'a' && (el.hasAttribute('download') || el.getAttribute('target') === '_blank' || !isInDomHref(href))) continue;

        const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
        const aria = (el.getAttribute('aria-label') || '').trim();
        const ident = `${{text}} ${{aria}} ${{el.id || ''}} ${{typeof el.className === 'string' ? el.className : ''}}`;
        if (forbiddenRe.test(ident)) continue;

        const isAccordion = (
            el.getAttribute('aria-expanded') === 'false' &&
            !['combobox', 'listbox', 'menuitem', 'tab'].includes((el.getAttribute('role') || '').toLowerCase()) &&
            el.getAttribute('aria-haspopup') !== 'menu' &&
            el.getAttribute('aria-haspopup') !== 'listbox' &&
            text.length <= 160
        );
        const isReadMore = (
            (text.length >= 4 && text.length <= 64 && readMoreTextRe.test(text)) ||
            (aria.length >= 4 && aria.length <= 64 && readMoreTextRe.test(aria))
        );

        if (!isAccordion && !isReadMore) continue;

        let expId = el.getAttribute('data-pf-exp-id');
        if (!expId) {{
            seq += 1;
            expId = String(seq);
            el.setAttribute('data-pf-exp-id', expId);
        }}
        candidates.push({{
            id: expId,
            kind: isAccordion ? 'accordion' : 'disclosure',
            text: text || aria,
            repeatable: !isAccordion && /(?:load\\s*more|show\\s*more|see\\s*more|daha\\s*fazla|yorumları)/i.test(text || aria),
        }});
    }}
    document.documentElement.setAttribute('data-pf-exp-seq', String(seq));
    return {{detailsOpened, unclamped, candidates}};
}}"""


async def _expand_disclosures_in_scope(
    page: Any,
    *,
    scope_selector: str | None = None,
    max_clicks: int = 20,
    max_repeats_per_button: int = 4,
    deadline: float,
    network_activity: Callable[[], tuple[int, float]] | None = None,
) -> int:
    """Expand ``<details>``, accordions, and in-DOM 'Read more / Show more' triggers."""
    clicks_done = 0
    clicked_counts: dict[str, int] = {}
    initial_doc_url = urldefrag(page.url or "")[0]

    for _round in range(max_repeats_per_button + 1):
        if time.monotonic() >= deadline or clicks_done >= max_clicks:
            break
        try:
            scan = await page.evaluate(_JS_PREPARE_AND_TAG_DISCLOSURES, scope_selector)
        except Exception:
            break
        if not isinstance(scan, dict):
            break
        candidates = scan.get("candidates") or []
        if not candidates:
            break

        clicked_in_round = 0
        navigated_away = False
        for cand in candidates:
            if time.monotonic() >= deadline or clicks_done >= max_clicks:
                break
            exp_id = str(cand.get("id") or "")
            if not exp_id:
                continue
            prev_clicks = clicked_counts.get(exp_id, 0)
            if prev_clicks >= (max_repeats_per_button if cand.get("repeatable") else 1):
                continue

            try:
                click_info = await page.evaluate(
                    """(id) => {
                        const el = document.querySelector(`[data-pf-exp-id="${id}"]`);
                        if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') {
                            return {clicked: false, prevLen: 0};
                        }
                        const st = window.getComputedStyle(el);
                        if (st.display === 'none' || st.visibility === 'hidden') {
                            return {clicked: false, prevLen: 0};
                        }
                        const prevLen = (document.body?.innerText || '').length;
                        el.scrollIntoView({block: 'center', inline: 'nearest'});
                        el.click();
                        return {clicked: true, prevLen};
                    }""",
                    exp_id,
                )
                if not isinstance(click_info, dict) or not click_info.get("clicked"):
                    clicked_counts[exp_id] = max_repeats_per_button
                    continue
                prev_len = int(click_info.get("prevLen") or 0)

                clicks_done += 1
                clicked_in_round += 1
                clicked_counts[exp_id] = prev_clicks + 1

                await _wait_after_interaction(
                    page,
                    deadline=deadline,
                    network_activity=network_activity,
                    max_wait=1.2,
                )
                if urldefrag(page.url or initial_doc_url)[0] != initial_doc_url:
                    # Distinguish full document reloads from SPA history.pushState/replaceState.
                    still_mounted = await page.evaluate(
                        "() => document.documentElement.hasAttribute('data-pf-exp-seq')"
                    )
                    if not still_mounted:
                        navigated_away = True
                        break
                    initial_doc_url = urldefrag(page.url or initial_doc_url)[0]

                if cand.get("repeatable") and prev_clicks >= 1:
                    new_len = int(
                        await page.evaluate("() => (document.body?.innerText || '').length")
                    )
                    if new_len <= prev_len + 15:
                        clicked_counts[exp_id] = max_repeats_per_button
            except Exception as exc:
                logger.debug("Disclosure click on exp_id=%s skipped: %s", exp_id, exc)
                clicked_counts[exp_id] = max_repeats_per_button

        if navigated_away or clicked_in_round == 0:
            break

    return clicks_done


_JS_RESOLVE_PANEL_HELPER = r"""const isPanelVisible = (el) => {
    if (!el) return false;
    if (el.hasAttribute('hidden')) return false;
    const st = window.getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden';
};
const findActivePanel = (groupId, controlsId) => {
    if (controlsId) {
        const byId = document.getElementById(controlsId);
        if (byId && isPanelVisible(byId)) return byId;
    }
    const widget = document.querySelector(`[data-pf-tab-widget="${groupId}"]`);
    const tablist = document.querySelector(`[data-pf-tablist-id="${groupId}"]`);
    if (widget) {
        const panels = Array.from(widget.querySelectorAll('[role="tabpanel"], .tab-pane, .ant-tabs-tabpane'));
        const matched = panels.find(p => isPanelVisible(p) && (!tablist || !tablist.contains(p)));
        if (matched) return matched;
    }
    if (widget && tablist) {
        const children = Array.from(widget.children).filter(
            ch => ch !== tablist && !ch.contains(tablist) && isPanelVisible(ch)
                && !['script', 'style', 'nav', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'].includes(ch.tagName.toLowerCase())
        );
        if (children.length === 1) return children[0];
        if (children.length > 1) {
            return children.reduce((best, cur) => ((cur.innerText || '').length > (best.innerText || '').length ? cur : best), children[0]);
        }
    }
    return null;
};"""

_JS_DISCOVER_TAB_GROUPS = f"""() => {{
    {_IN_DOM_HREF_JS}
    {_JS_RESOLVE_PANEL_HELPER}
    const isVisible = (el) => {{
        if (!el) return false;
        const st = window.getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') === 0) return false;
        const r = el.getBoundingClientRect();
        return r.width > 4 && r.height > 4;
    }};

    const isActiveTab = (tab) => {{
        if (tab.getAttribute('aria-selected') === 'true') return true;
        if ((tab.getAttribute('data-state') || '').toLowerCase() === 'active') return true;
        const cls = typeof tab.className === 'string' ? tab.className : '';
        if (/(?:^|\\s)(active|is-active|selected|is-selected|mui-selected|ant-tabs-tab-active)(?:\\s|$)/i.test(cls)) return true;
        const parentLi = tab.closest('li');
        if (parentLi && /(?:^|\\s)(active|is-active|selected)(?:\\s|$)/i.test(parentLi.className || '')) return true;
        return false;
    }};

    const containers = [];
    const seenContainers = new Set();
    const rawContainers = document.querySelectorAll(
        '[role="tablist"], .nav-tabs, .MuiTabs-flexContainer, .ant-tabs-nav-list, [data-tabs-list], .tab-list, .tabs-nav'
    );
    for (const c of rawContainers) {{
        if (seenContainers.has(c)) continue;
        if (c.closest('nav:not([role="tablist"]), header.site-header, footer, [role="navigation"], [role="menubar"]')) {{
            if (c.getAttribute('role') !== 'tablist' || !c.closest('main, article, [role="main"]')) {{
                continue;
            }}
        }}
        if (!isVisible(c)) continue;
        seenContainers.add(c);
        containers.push(c);
    }}

    let groupSeq = 0;
    let tabSeq = 0;
    const groups = [];

    for (const container of containers) {{
        let tabEls = Array.from(container.querySelectorAll('[role="tab"]')).filter(isVisible);
        if (tabEls.length < 2) {{
            tabEls = Array.from(container.querySelectorAll('button, a, [role="button"]')).filter(isVisible);
        }}
        tabEls = tabEls.filter(tab => {{
            if (tab.tagName.toLowerCase() !== 'a') return true;
            return !tab.hasAttribute('download') && tab.getAttribute('target') !== '_blank' && isInDomHref(tab.getAttribute('href'));
        }});
        if (tabEls.length < 2 || tabEls.length > 12) continue;

        groupSeq += 1;
        const groupId = `g${{groupSeq}}`;
        container.setAttribute('data-pf-tablist-id', groupId);

        let widgetRoot = null;
        let firstMultiChild = null;
        let cur = container.parentElement;
        for (let depth = 0; depth < 5 && cur && cur !== document.body; depth++) {{
            if (!firstMultiChild && cur.children.length >= 2) firstMultiChild = cur;
            const panelCandidate = Array.from(
                cur.querySelectorAll('[role="tabpanel"], .tab-content, .tab-pane, .ant-tabs-content-holder')
            ).find(el => !container.contains(el));
            if (panelCandidate) {{
                widgetRoot = cur;
                break;
            }}
            cur = cur.parentElement;
        }}
        if (!widgetRoot) widgetRoot = firstMultiChild;
        if (widgetRoot && widgetRoot !== document.body) {{
            widgetRoot.setAttribute('data-pf-tab-widget', groupId);
        }}

        const tabs = [];
        let activeIdx = -1;
        tabEls.forEach((tab, idx) => {{
            tabSeq += 1;
            const tid = `t${{tabSeq}}`;
            tab.setAttribute('data-pf-tab-id', tid);
            const title = (tab.innerText || tab.textContent || tab.getAttribute('aria-label') || `Tab ${{idx + 1}}`)
                .replace(/\\s+/g, ' ')
                .trim();
            const active = isActiveTab(tab);
            if (active && activeIdx === -1) activeIdx = idx;
            const href = tab.tagName.toLowerCase() === 'a' ? tab.getAttribute('href') : null;
            const controls = tab.getAttribute('aria-controls') ||
                (tab.getAttribute('data-bs-target') || tab.getAttribute('data-target') || '').replace(/^#/, '') ||
                (href && href.startsWith('#') && href.length > 1 ? href.slice(1) : '');
            tabs.push({{
                id: tid,
                title: title || `Tab ${{idx + 1}}`,
                active,
                controls: controls || null,
            }});
        }});
        if (activeIdx === -1) activeIdx = 0;

        let initialSnap = null;
        const activePanel = findActivePanel(groupId, tabs[activeIdx].controls);
        if (activePanel) {{
            activePanel.setAttribute('data-pf-panel-group', groupId);
            initialSnap = {{
                html: activePanel.innerHTML,
                text: (activePanel.innerText || '').replace(/\\s+/g, ' ').trim(),
            }};
        }}
        groups.push({{groupId, activeIdx, tabs, initialSnap}});
    }}
    return groups;
}}"""

_JS_SNAPSHOT_ACTIVE_PANEL = f"""(args) => {{
    const {{groupId, controlsId}} = args;
    document.querySelectorAll('[data-pf-active-panel]').forEach(el => el.removeAttribute('data-pf-active-panel'));
    {_JS_RESOLVE_PANEL_HELPER}
    const panel = findActivePanel(groupId, controlsId);
    if (!panel) return null;
    panel.setAttribute('data-pf-active-panel', '1');
    panel.setAttribute('data-pf-panel-group', groupId);
    const hasDisclosures = !!panel.querySelector(
        'details:not([open]), [aria-expanded="false"], button, [role="button"], a, [class*="clamp"]'
    );
    return {{
        html: panel.innerHTML,
        text: (panel.innerText || '').replace(/\\s+/g, ' ').trim(),
        hasDisclosures,
    }};
}}"""


async def expand_disclosures_only(
    page: Any,
    *,
    deadline: float,
    network_activity: Callable[[], tuple[int, float]] | None = None,
) -> int:
    """Run top-level '<details>', accordion, and in-DOM 'Read more' expansion before visual snapshot."""
    return await _expand_disclosures_in_scope(
        page,
        scope_selector=None,
        max_clicks=20,
        max_repeats_per_button=4,
        deadline=deadline,
        network_activity=network_activity,
    )


async def iterate_dynamic_tabs(
    page: Any,
    *,
    deadline: float,
    network_activity: Callable[[], tuple[int, float]] | None = None,
) -> tuple[str, list[RawExpandedSection]]:
    """Capture baseline DOM (marking panel anchors) and iterate client-side tab groups."""
    sections: list[RawExpandedSection] = []
    try:
        groups = await page.evaluate(_JS_DISCOVER_TAB_GROUPS)
    except Exception as exc:
        logger.debug("Tab discovery failed: %s", exc)
        groups = []

    if not isinstance(groups, list) or not groups:
        return await page.content(), []

    seen_by_group: dict[str, set[str]] = {}
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        group_id = str(grp.get("groupId") or "")
        tabs = grp.get("tabs") or []
        active_idx = int(grp.get("activeIdx") or 0)
        snap = grp.get("initialSnap")
        if not group_id or not tabs or active_idx >= len(tabs):
            continue
        active_tab = tabs[active_idx]
        if isinstance(snap, dict) and snap.get("text"):
            fp = fingerprint_text(str(snap["text"]))
            if fp:
                seen_by_group.setdefault(group_id, set()).add(fp)
                sections.append(
                    RawExpandedSection(
                        title=str(active_tab.get("title") or "Tab 1"),
                        kind="tab",
                        html=str(snap.get("html") or ""),
                        group_id=group_id,
                        controls_id=str(active_tab["controls"]) if active_tab.get("controls") else None,
                    )
                )

    baseline_html = await page.content()
    initial_doc_url = urldefrag(page.url or "")[0]
    reloaded = False

    for grp in groups:
        if reloaded or time.monotonic() >= deadline or not isinstance(grp, dict):
            break
        group_id = str(grp.get("groupId") or "")
        tabs = grp.get("tabs") or []
        active_idx = int(grp.get("activeIdx") or 0)
        group_seen = seen_by_group.setdefault(group_id, set())

        for idx, tab in enumerate(tabs[:12]):
            if idx == active_idx or time.monotonic() >= deadline:
                continue
            tab_id = str(tab.get("id") or "")
            if not tab_id:
                continue

            try:
                clicked = await page.evaluate(
                    """(tid) => {
                        const el = document.querySelector(`[data-pf-tab-id="${tid}"]`);
                        if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
                        el.scrollIntoView({block: 'nearest', inline: 'nearest'});
                        el.click();
                        return true;
                    }""",
                    tab_id,
                )
                if not clicked:
                    continue

                await _wait_after_interaction(
                    page,
                    deadline=deadline,
                    network_activity=network_activity,
                    max_wait=1.8,
                )
                if urldefrag(page.url or initial_doc_url)[0] != initial_doc_url:
                    # Only abort if the page actually reloaded away from our annotated DOM;
                    # SPAs updating query/path via history.replaceState keep markers intact.
                    still_mounted = await page.evaluate(
                        "(gid) => !!document.querySelector(`[data-pf-tablist-id=\"${gid}\"]`)",
                        group_id,
                    )
                    if not still_mounted:
                        reloaded = True
                        break
                    initial_doc_url = urldefrag(page.url or initial_doc_url)[0]

                controls_id = tab.get("controls")
                snap = await page.evaluate(
                    _JS_SNAPSHOT_ACTIVE_PANEL,
                    {"groupId": group_id, "controlsId": controls_id},
                )
                if isinstance(snap, dict) and snap.get("hasDisclosures") and time.monotonic() < deadline:
                    expanded = await _expand_disclosures_in_scope(
                        page,
                        scope_selector='[data-pf-active-panel="1"]',
                        max_clicks=6,
                        max_repeats_per_button=2,
                        deadline=deadline,
                        network_activity=network_activity,
                    )
                    if expanded > 0:
                        snap = await page.evaluate(
                            _JS_SNAPSHOT_ACTIVE_PANEL,
                            {"groupId": group_id, "controlsId": controls_id},
                        )

                if isinstance(snap, dict) and snap.get("text"):
                    fp = fingerprint_text(str(snap["text"]))
                    if fp and fp not in group_seen:
                        group_seen.add(fp)
                        sections.append(
                            RawExpandedSection(
                                title=str(tab.get("title") or f"Tab {idx + 1}"),
                                kind="tab",
                                html=str(snap.get("html") or ""),
                                group_id=group_id,
                                controls_id=str(controls_id) if controls_id else None,
                            )
                        )
            except Exception as exc:
                logger.debug("Dynamic tab %s click skipped: %s", tab_id, exc)

    counts_by_group: dict[str, int] = {}
    for sec in sections:
        counts_by_group[sec.group_id] = counts_by_group.get(sec.group_id, 0) + 1
    meaningful_sections = [
        sec for sec in sections if counts_by_group.get(sec.group_id, 0) >= 2
    ]
    return baseline_html, meaningful_sections
