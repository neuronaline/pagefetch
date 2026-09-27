"""Declarative interactive wizard for PageFetch.

The wizard collects the user's choices through a small sequence of prompts
and then delegates to ``cli.run_batch`` for the actual fetch/extract work.
All formatting, validation, and config-building logic lives in ``cli.py``
so the CLI and interactive modes stay in lockstep.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from .cli import parse_input_urls, run_batch
from .config import (
    VALID_MODES,
    VALID_OUTPUT_FORMATS,
    VALID_PROXIES,
    VALID_SCREENSHOT_MODES,
    PageFetchConfig,
)


def _clear_screen() -> None:
    if not sys.stdout.isatty():
        return
    if os.name == "nt":
        os.system("cls")
    else:
        print("\033[2J\033[H", end="")


def _banner() -> None:
    print("=" * 58)
    print("  PageFetch  -  Web Page Content Fetcher")
    print("=" * 58)
    print()


def _prompt(text: str, default: str = "") -> str:
    display = f"{text} [{default}]: " if default else f"{text}: "
    try:
        value = input(display).strip()
    except EOFError:
        print()
        return ""
    return value or default


def _choose(text: str, options: frozenset[str], default: str) -> str:
    """Prompt until the user enters one of *options*; ``default`` is pre-selected."""
    options_str = ", ".join(sorted(options))
    while True:
        value = _prompt(f"{text} ({options_str})", default)
        if value in options:
            return value
        print(f"  invalid choice: {value!r}")


def _resolve_urls(raw: str) -> list[str]:
    """Return the URL list for *raw* (single URL, file path, or bare domain)."""
    if not raw:
        return []
    return parse_input_urls(raw)


def _run_wizard(config: PageFetchConfig) -> int:
    """Drive the URL(s) → run_batch flow and return a process exit code."""
    raw = _prompt("  URL or path to a URL list file")
    try:
        urls = _resolve_urls(raw)
    except ValueError as exc:
        print(f"\n  Error: {exc}")
        input("  Press Enter to continue...")
        return 2
    if not urls:
        return 0

    output_format = _choose("  Output format", VALID_OUTPUT_FORMATS, "markdown")
    include_html = output_format == "html"

    screenshot_mode = _choose(
        "  Screenshot mode", VALID_SCREENSHOT_MODES, "none"
    )
    effective_format = output_format
    if screenshot_mode != "none" and output_format in {"markdown", "html"}:
        # Screenshot bytes are only renderable in raw/json; auto-promote so
        # the wizard matches ``cli.run_batch``'s mode-promotion behaviour.
        effective_format = "json"

    output_raw = _prompt("  Output file path (empty = print to console)")
    output = Path(output_raw).expanduser() if output_raw else None

    return asyncio.run(
        run_batch(
            config,
            urls,
            output_format=effective_format,
            include_html=include_html,
            screenshot=screenshot_mode,
            output=output,
        )
    )


def _build_config() -> PageFetchConfig:
    """Ask the user for the few options that the wizard exposes."""
    print()
    proxy = _choose("  Proxy provider", VALID_PROXIES, "none")
    if proxy != "none":
        env_var = f"{proxy.upper()}_PROXY_URL"
        if not os.environ.get(env_var):
            print(f"  → set {env_var} to your proxy URL before running")
    mode = _choose("  Fetch mode", VALID_MODES, "auto")
    config = PageFetchConfig(proxy=proxy, mode=mode)
    print()
    return config


def interactive_main() -> int:
    """Run the interactive wizard loop."""
    config = PageFetchConfig()
    while True:
        _clear_screen()
        _banner()
        print("  1. Fetch URL(s)")
        print("  2. Configure & fetch")
        print("  3. Exit")
        print()
        choice = _prompt("  Choose", "1")
        try:
            if choice == "1":
                code = _run_wizard(config)
                if code:
                    print(f"\n  pagefetch exited with code {code}")
                input("  Press Enter to continue...")
            elif choice == "2":
                config = _build_config()
                code = _run_wizard(config)
                if code:
                    print(f"\n  pagefetch exited with code {code}")
                input("  Press Enter to continue...")
            elif choice == "3":
                print("\n  Goodbye!")
                return 0
            else:
                print(f"\n  Invalid choice: {choice!r}")
                input("  Press Enter to continue...")
        except KeyboardInterrupt:
            print("\n\n  Interrupted. Goodbye!")
            return 0
        except (ValueError, OSError) as exc:
            print(f"\n  Error: {exc}")
            input("  Press Enter to continue...")


if __name__ == "__main__":
    raise SystemExit(interactive_main())
