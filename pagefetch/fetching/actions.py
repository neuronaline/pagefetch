"""Declarative in-page browser interaction primitives."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import random
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from ..models import ActionResult, FetchErrorInfo, PageAction, VALID_ACTION_TYPES
from .http import TransportFailure
from .readiness import wait_for_stability

logger = logging.getLogger("pagefetch.fetching.actions")

_JS_TAG_INTERACTIVE_ELEMENTS = """() => {
    const isInteractive = (el) => {
        if (el.hasAttribute('disabled') || el.hasAttribute('hidden')) return false;
        if (el.getAttribute('aria-hidden') === 'true') return false;
        const tag = el.tagName.toLowerCase();
        if (['button', 'select', 'textarea', 'summary', 'option'].includes(tag)) return true;
        if (tag === 'a' && el.hasAttribute('href') && el.getAttribute('href').trim() !== '') return true;
        if (tag === 'input') {
            const t = (el.getAttribute('type') || 'text').toLowerCase();
            return t !== 'hidden';
        }
        const role = (el.getAttribute('role') || '').toLowerCase();
        if (['button', 'link', 'textbox', 'searchbox', 'combobox', 'checkbox', 'radio', 'switch', 'menuitem', 'tab', 'option'].includes(role)) {
            return true;
        }
        const ce = el.getAttribute('contenteditable');
        if (ce === '' || ce === 'true') return true;
        const ti = el.getAttribute('tabindex');
        if (ti !== null && !isNaN(parseInt(ti, 10)) && parseInt(ti, 10) >= 0) return true;
        return false;
    };
    const all = document.querySelectorAll('*');
    let count = 0;
    for (const el of all) {
        if (isInteractive(el)) {
            count += 1;
            el.setAttribute('data-pf-action-id', String(count));
        }
    }
    return count;
}"""


def parse_action(spec: str) -> PageAction:
    """Parse shorthand action strings like 'click:#btn', 'fill:#q=hello', 'fill:#user:admin', 'wait:2s'."""
    spec = spec.strip()
    if spec.startswith("{"):
        import json as _json

        return PageAction.from_dict(_json.loads(spec))

    optional = False
    if spec.endswith("?"):
        optional = True
        spec = spec[:-1].strip()

    if ":" not in spec:
        action_type = spec.strip().lower()
        if action_type not in VALID_ACTION_TYPES:
            raise ValueError(f"Unknown action type: {action_type!r}. Must be one of {sorted(VALID_ACTION_TYPES)}")
        return PageAction(type=action_type, optional=optional)

    action_type, rest = spec.split(":", 1)
    action_type = action_type.strip().lower()
    rest = rest.strip()

    if action_type not in VALID_ACTION_TYPES:
        raise ValueError(f"Unknown action type: {action_type!r}. Must be one of {sorted(VALID_ACTION_TYPES)}")

    if action_type in {"click", "check", "uncheck", "hover", "wait_for_selector"}:
        return PageAction(type=action_type, selector=rest, optional=optional)

    if action_type in {"fill", "type", "select"}:
        if "=" in rest:
            selector, value = rest.split("=", 1)
        elif ":" in rest:
            selector, value = rest.split(":", 1)
        else:
            selector, value = rest, ""
        return PageAction(
            type=action_type, selector=selector.strip(), value=value.strip(), optional=optional
        )

    if action_type == "press":
        if "=" in rest:
            selector, key = rest.split("=", 1)
            return PageAction(
                type=action_type, selector=selector.strip(), value=key.strip(), optional=optional
            )
        elif ":" in rest:
            selector, key = rest.split(":", 1)
            return PageAction(
                type=action_type, selector=selector.strip(), value=key.strip(), optional=optional
            )
        return PageAction(type=action_type, selector=None, value=rest, optional=optional)

    if action_type in {"wait", "scroll", "evaluate"}:
        return PageAction(type=action_type, value=rest, optional=optional)

    return PageAction(type=action_type, selector=rest, optional=optional)


_parse_action_string = parse_action


def normalize_actions(
    raw_actions: Sequence[PageAction | dict[str, Any] | str] | None,
) -> list[PageAction]:
    """Normalize action inputs (PageAction instances, dicts, or shorthand strings) into PageActions."""
    if not raw_actions:
        return []
    normalized: list[PageAction] = []
    for item in raw_actions:
        if isinstance(item, PageAction):
            normalized.append(item)
        elif isinstance(item, dict):
            normalized.append(PageAction.from_dict(item))
        elif isinstance(item, str):
            normalized.append(parse_action(item))
        else:
            raise TypeError(f"Unsupported action type: {type(item).__name__}")
    for action in normalized:
        if action.type.lower() not in VALID_ACTION_TYPES:
            raise ValueError(f"Unknown action type: {action.type!r}. Must be one of {sorted(VALID_ACTION_TYPES)}")
    return normalized


async def resolve_selector(page: Any, selector: str | int | None) -> str | None:
    """Resolve target selector, translating action_id numbers or @N / #N references to CSS selectors."""
    if selector is None:
        return None
    if isinstance(selector, int):
        return f'[data-pf-action-id="{selector}"]'

    sel_str = str(selector).strip()
    if sel_str.startswith("@") and sel_str[1:].isdigit():
        action_num = int(sel_str[1:])
        return f'[data-pf-action-id="{action_num}"]'

    if sel_str.startswith("#") and sel_str[1:].isdigit():
        action_num = int(sel_str[1:])
        has_literal_id = False
        if page is not None:
            has_literal_id = await page.evaluate(f"() => !!document.getElementById('{sel_str[1:]}')")
        return f'[id="{sel_str[1:]}"]' if has_literal_id else f'[data-pf-action-id="{action_num}"]'

    return sel_str


async def execute_page_actions(
    page: Any,
    actions: Sequence[PageAction],
    *,
    humanize: bool = False,
    default_timeout: float = 10.0,
    warnings: list[str] | None = None,
) -> list[ActionResult]:
    """Execute a sequence of browser actions on page."""
    if warnings is None:
        warnings = []
    results: list[ActionResult] = []

    # Tag interactive elements once if any action targets an action ID reference
    needs_action_ids = any(
        isinstance(a.selector, int)
        or (
            isinstance(a.selector, str)
            and (
                a.selector.startswith("@")
                or (a.selector.startswith("#") and a.selector[1:].isdigit())
            )
        )
        for a in actions
    )
    if needs_action_ids and page is not None:
        try:
            await page.evaluate(_JS_TAG_INTERACTIVE_ELEMENTS)
        except Exception as exc:
            logger.debug("Failed to tag interactive elements on page: %s", exc)

    for idx, action in enumerate(actions, 1):
        start_t = time.perf_counter()
        target: str | None = None
        action_timeout = (action.timeout or default_timeout) * 1000  # ms for Playwright

        try:
            target = await resolve_selector(page, action.selector)

            if humanize:
                await asyncio.sleep(random.uniform(0.04, 0.12))

            action_type = action.type.lower()
            return_value: Any = None

            if action_type == "click":
                if not target:
                    raise ValueError("click action requires a selector")
                await page.click(target, timeout=action_timeout)

            elif action_type == "fill":
                if not target:
                    raise ValueError("fill action requires a selector")
                val = "" if action.value is None else str(action.value)
                await page.fill(target, val, timeout=action_timeout)

            elif action_type == "type":
                if not target:
                    raise ValueError("type action requires a selector")
                val = "" if action.value is None else str(action.value)
                if humanize:
                    delay = random.uniform(25, 65)
                    await page.type(target, val, delay=delay, timeout=action_timeout)
                else:
                    await page.type(target, val, timeout=action_timeout)

            elif action_type == "press":
                key = str(action.value or "Enter")
                if target:
                    await page.press(target, key, timeout=action_timeout)
                else:
                    await page.keyboard.press(key)

            elif action_type == "select":
                if not target:
                    raise ValueError("select action requires a selector")
                val = action.value
                options = [str(val)] if isinstance(val, (str, int, float)) else [str(x) for x in val or []]
                await page.select_option(target, options, timeout=action_timeout)

            elif action_type == "check":
                if not target:
                    raise ValueError("check action requires a selector")
                await page.check(target, timeout=action_timeout)

            elif action_type == "uncheck":
                if not target:
                    raise ValueError("uncheck action requires a selector")
                await page.uncheck(target, timeout=action_timeout)

            elif action_type == "hover":
                if not target:
                    raise ValueError("hover action requires a selector")
                await page.hover(target, timeout=action_timeout)

            elif action_type == "wait_for_selector":
                if not target:
                    raise ValueError("wait_for_selector action requires a selector")
                await page.wait_for_selector(target, timeout=action_timeout)

            elif action_type == "wait":
                val = action.value if action.value is not None else 1.0
                delay_sec: float
                if isinstance(val, (int, float)):
                    delay_sec = float(val) / 1000.0 if float(val) >= 50.0 else float(val)
                else:
                    val_str = str(val).strip().lower()
                    if val_str.endswith("ms"):
                        delay_sec = float(val_str[:-2]) / 1000.0
                    elif val_str.endswith("s"):
                        delay_sec = float(val_str[:-1])
                    else:
                        num = float(val_str)
                        delay_sec = num / 1000.0 if num >= 50.0 else num
                await asyncio.sleep(max(0.0, delay_sec))

            elif action_type == "scroll":
                if target:
                    await page.locator(target).scroll_into_view_if_needed(timeout=action_timeout)
                elif action.value is not None:
                    val_str = str(action.value).strip().lower()
                    if val_str == "bottom":
                        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    elif val_str == "top":
                        await page.evaluate("() => window.scrollTo(0, 0)")
                    elif val_str == "down":
                        await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
                    elif val_str == "up":
                        await page.evaluate("() => window.scrollBy(0, -window.innerHeight)")
                    else:
                        try:
                            px = float(val_str)
                            await page.evaluate(f"() => window.scrollBy(0, {px})")
                        except ValueError:
                            pass
                else:
                    await page.evaluate("() => window.scrollBy(0, window.innerHeight)")

            elif action_type == "evaluate":
                expr = str(action.value or "")
                return_value = await page.evaluate(expr)

            else:
                raise ValueError(f"Unknown action type: {action_type!r}")

            # Post-action readiness wait on mutating actions
            if action_type in {"click", "fill", "type", "press", "select", "check", "uncheck", "evaluate"}:
                await wait_for_stability(page, timeout=min(2.0, default_timeout))

            duration_ms = round((time.perf_counter() - start_t) * 1000, 2)
            results.append(
                ActionResult(
                    index=idx,
                    type=action.type,
                    selector=str(action.selector) if action.selector is not None else None,
                    success=True,
                    duration_ms=duration_ms,
                    result=return_value,
                )
            )

        except Exception as exc:
            duration_ms = round((time.perf_counter() - start_t) * 1000, 2)
            err_msg = str(exc)
            results.append(
                ActionResult(
                    index=idx,
                    type=action.type,
                    selector=str(action.selector) if action.selector is not None else None,
                    success=False,
                    duration_ms=duration_ms,
                    error=err_msg,
                )
            )
            if action.optional:
                warnings.append(
                    f"Optional action #{idx} ({action.type}) failed: {err_msg}"
                )
                logger.debug("Optional action #%d (%s) failed: %s", idx, action.type, exc)
            else:
                failure = TransportFailure(
                    FetchErrorInfo(
                        "action_failed",
                        f"Action #{idx} ({action.type}) failed: {err_msg}",
                        False,
                        type(exc).__name__,
                    )
                )
                setattr(failure, "actions_executed", results)
                raise failure from exc

    return results


def matches_xhr_pattern(url: str, pattern: bool | str | Sequence[str]) -> bool:
    """Return True if url matches capture_xhr pattern(s)."""
    if pattern is True:
        return True
    if not pattern:
        return False
    if isinstance(pattern, str):
        patterns = (
            [p.strip() for p in pattern.split(",") if p.strip()]
            if ("," in pattern and not pattern.startswith("re:"))
            else [pattern]
        )
    else:
        patterns = list(pattern)
    norm_url = url.lower()
    parsed_path = urlsplit(url).path.lower()
    for pat in patterns:
        if pat.startswith("re:"):
            import re

            if re.search(pat[3:], url):
                return True
            continue
        p = pat.lower()
        if p.startswith("/"):
            if fnmatch.fnmatch(parsed_path, p) or parsed_path.startswith(p):
                return True
        elif "*" in p or "?" in p:
            if fnmatch.fnmatch(norm_url, p):
                return True
        elif p in norm_url:
            return True
    return False
