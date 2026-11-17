"""Playwright-compatible rendered-page readiness helpers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, TypedDict

from ..processing.challenges import JS_IN_PAGE_METRICS_PROBE


class PageMetrics(TypedDict):
    text: int
    main_text: int
    challenge: bool


async def in_page_metrics(page: Any) -> PageMetrics:
    """Cheap in-page probe returning text length and challenge markers.

    Runs a single lightweight ``page.evaluate`` so the caller can decide
    whether the DOM is already "good enough" before paying for scrolls
    and a second stability wait.
    """
    try:
        result: dict[str, Any] = await page.evaluate(JS_IN_PAGE_METRICS_PROBE)
    except Exception:
        return PageMetrics(text=0, main_text=0, challenge=False)
    if not isinstance(result, dict):
        result = {}
    return PageMetrics(
        text=int(result.get("text", 0)),
        main_text=int(result.get("mainText", 0)),
        challenge=bool(result.get("challenge", False)),
    )


async def wait_for_stability(
    page: Any,
    *,
    timeout: float,
    stable_rounds: int = 3,
    network_activity: Callable[[], tuple[int, float]] | None = None,
) -> None:
    """Wait for DOM stability while tolerating a small amount of network activity."""
    deadline = time.monotonic() + timeout
    previous: tuple[int, int] | None = None
    unchanged = 0
    # Start with a faster poll interval and relax if the page is slow.
    poll_interval = 0.08
    fast_polls = 2
    polls_done = 0
    while time.monotonic() < deadline:
        # Phase 3 Item 8: avoid serialising the entire DOM tree into a
        # string on every poll — ``getElementsByTagName('*').length``
        # walks the document once to count nodes without allocating a
        # single string, which is dramatically cheaper than
        # ``document.documentElement.outerHTML.length`` on a 10k-node page.
        # Node counts also more directly reflect structural changes
        # (script insertions, lazy-loaded comment sections) than string
        # length, so the tolerance can be tightened accordingly.
        try:
            metrics = await page.evaluate(
                """() => ({
                    text: (document.body?.innerText || '').length,
                    dom: document.getElementsByTagName('*').length
                })"""
            )
        except Exception:
            metrics = {}
        if not isinstance(metrics, dict):
            metrics = {}
        current = (int(metrics.get("text", 0)), int(metrics.get("dom", 0)))
        if previous and abs(current[0] - previous[0]) < 20 and abs(current[1] - previous[1]) < 5:
            unchanged += 1
            if unchanged >= stable_rounds:
                if network_activity is None:
                    return
                active, last_activity = network_activity()
                quiet_for = time.monotonic() - last_activity
                # A couple of analytics requests must not hold the page open. If
                # a busier page remains stable, allow two additional rounds.
                if active <= 2 or quiet_for >= 0.75 or unchanged >= stable_rounds + 2:
                    return
        else:
            unchanged = 0
        previous = current
        polls_done += 1
        if polls_done >= fast_polls:
            poll_interval = 0.15
        await asyncio.sleep(poll_interval)


async def controlled_scroll(
    page: Any,
    *,
    max_scrolls: int = 25,
    max_height: int = 150_000,
    sleep_early: float = 0.12,
    sleep_late: float = 0.18,
    deadline: float | None = None,
    network_activity: Callable[[], tuple[int, float]] | None = None,
) -> bool:
    """Scroll the main window to the bottom. Return True when a configured limit stops scrolling."""
    unchanged = 0
    previous_height = 0
    previous_dom = 0
    for index in range(max_scrolls):
        if deadline is not None and time.monotonic() >= deadline:
            try:
                await page.evaluate("() => window.scrollTo(0, 0)")
            except Exception:
                pass
            return True
        try:
            metrics = await page.evaluate(
                """() => {
                    for (const root of [document.documentElement, document.body]) {
                        if (!root) continue;
                        const st = window.getComputedStyle(root);
                        if (st.overflow === 'hidden' || st.overflowY === 'hidden') {
                            root.style.setProperty('overflow', 'visible', 'important');
                            root.style.setProperty('overflow-y', 'auto', 'important');
                        }
                    }
                    const vp = window.innerHeight || 800;
                    const prevY = window.scrollY || 0;
                    const dom = document.getElementsByTagName('*').length;
                    const step = Math.max(Math.floor(vp * 0.85), 400);
                    window.scrollBy(0, step);
                    const newY = window.scrollY || 0;
                    const newH = Math.max(
                        document.documentElement?.scrollHeight || 0,
                        document.body?.scrollHeight || 0
                    );
                    const atBottom = (newY + vp >= newH - 5) || (newY === prevY && newH <= vp + 5);
                    return {height: newH, y: newY, viewport: vp, dom, atBottom};
                }"""
            )
        except Exception:
            break
        if not isinstance(metrics, dict):
            break
        height = int(metrics.get("height", 0))
        viewport = int(metrics.get("viewport", 800))
        dom_count = int(metrics.get("dom", 0))
        at_bottom = bool(metrics.get("atBottom", False))

        if height > max_height:
            try:
                await page.evaluate("() => window.scrollTo(0, 0)")
            except Exception:
                pass
            return True

        await asyncio.sleep(sleep_early if index < 4 else sleep_late)

        if at_bottom:
            # Allow in-flight lazy-load XHRs triggered at bottom to settle and grow scrollHeight.
            if network_activity is not None:
                wait_cap = time.monotonic() + 0.8
                if deadline is not None:
                    wait_cap = min(wait_cap, deadline)
                while time.monotonic() < wait_cap:
                    active, _ = network_activity()
                    if active <= 0:
                        break
                    await asyncio.sleep(0.06)
                await asyncio.sleep(0.04)
            try:
                post = await page.evaluate(
                    """() => ({
                        height: Math.max(
                            document.documentElement?.scrollHeight || 0,
                            document.body?.scrollHeight || 0
                        ),
                        dom: document.getElementsByTagName('*').length
                    })"""
                )
            except Exception:
                post = {}
            post_height = int(post.get("height", height)) if isinstance(post, dict) else height
            post_dom = int(post.get("dom", dom_count)) if isinstance(post, dict) else dom_count
            if post_height > max(height, previous_height) + 16 or post_dom > max(dom_count, previous_dom) + 4:
                unchanged = 0
                previous_height = post_height
                previous_dom = post_dom
                continue

            unchanged += 1
            # Short single-viewport documents need only 1 verified bottom check; taller pages verify twice.
            if (height <= viewport + 5 and unchanged >= 1) or unchanged >= 2:
                try:
                    await page.evaluate("() => window.scrollTo(0, 0)")
                except Exception:
                    pass
                return False
        else:
            unchanged = 0

        previous_height = height
        previous_dom = dom_count

    try:
        await page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass
    return True
