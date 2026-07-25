"""Playwright-compatible rendered-page readiness helpers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, TypedDict


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
    result: dict[str, Any] = await page.evaluate(
        """() => {
            const body = document.body;
            const bodyText = (body?.innerText || '').length;
            const mains = document.querySelectorAll('main, article, [role="main"]');
            let mainText = 0;
            for (const el of mains) {
                mainText += (el.innerText?.length || 0);
            }
            const lowered = (body?.innerText || '').toLowerCase();
            const challenge = lowered.includes('checking your browser') ||
                lowered.includes('verify you are human') ||
                (document.title || '').toLowerCase().includes('challenge');
            return {text: bodyText, mainText, challenge};
        }"""
    )
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
        metrics = await page.evaluate(
            """() => ({
                text: (document.body?.innerText || '').length,
                dom: document.documentElement?.outerHTML.length || 0
            })"""
        )
        current = (int(metrics.get("text", 0)), int(metrics.get("dom", 0)))
        if previous and abs(current[0] - previous[0]) < 20 and abs(current[1] - previous[1]) < 100:
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
    max_scrolls: int = 6,
    max_height: int = 80_000,
    sleep_early: float = 0.10,
    sleep_late: float = 0.15,
) -> bool:
    """Perform bounded viewport scrolling. Return true when the limit is reached."""
    unchanged = 0
    previous_height = 0
    for index in range(max_scrolls):
        # Single evaluate: capture pre-scroll metrics *then* scroll, saving one IPC roundtrip.
        metrics = await page.evaluate(
            """() => {
                const h = document.documentElement.scrollHeight;
                const vp = window.innerHeight;
                window.scrollBy(0, Math.max(vp * 0.9, 400));
                return {height: h, y: window.scrollY, viewport: vp};
            }"""
        )
        height = int(metrics.get("height", 0))
        if height > max_height:
            return True
        # Adaptive sleep: shorter early, shorter late (tightened from original 0.10/0.18/0.28).
        if index < 3:
            await asyncio.sleep(sleep_early)
        else:
            await asyncio.sleep(sleep_late)
        if height <= previous_height and int(metrics.get("y", 0)) + int(metrics.get("viewport", 0)) >= height:
            unchanged += 1
            if unchanged >= 2:
                await page.evaluate("() => window.scrollTo(0, 0)")
                return False
        else:
            unchanged = 0
        previous_height = height
    await page.evaluate("() => window.scrollTo(0, 0)")
    return True
