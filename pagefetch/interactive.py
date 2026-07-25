"""Interactive CLI menu for PageFetch."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from .client import PageFetch
from .config import VALID_MODES, VALID_PROXIES, PageFetchConfig
from .models import FetchResult
from .utils.rendering import render_results
from .utils.urls import read_urls_from_file


def _clear_screen() -> None:
    """Clear the terminal screen."""
    print("\033[2J\033[H", end="")


def _banner() -> None:
    """Print the PageFetch banner."""
    print("=" * 58)
    print("  PageFetch  -  Web Page Content Fetcher")
    print("=" * 58)
    print()


def _header(title: str) -> None:
    """Print a section header."""
    print()
    print(f"  --- {title} ---")
    print()


def _prompt(prompt_text: str, default: str = "") -> str:
    """Show a prompt and return the user's input (stripped)."""
    if default:
        display = f"{prompt_text} [{default}]: "
    else:
        display = f"{prompt_text}: "
    try:
        value = input(display).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return value or default


def _confirm(prompt_text: str, default: bool = True) -> bool:
    """Ask a yes/no question."""
    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        answer = input(prompt_text + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def _render_results(results: list[FetchResult], output_format: str, include_html: bool) -> str:
    """Render fetch results in the chosen format."""
    return render_results(results, output_format, include_html=include_html)


def _apply_debug(settings: dict) -> None:
    """Configure the pagefetch logger to match the current debug setting."""
    logger = logging.getLogger("pagefetch")
    if settings.get("debug"):
        if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
            handler = logging.StreamHandler()
            logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.WARNING)


async def _fetch_url(client: PageFetch, url: str, settings: dict) -> list[FetchResult]:
    """Fetch a single URL."""
    result = await client.fetch(
        url,
        mode=settings.get("mode"),
        proxy=settings.get("proxy"),
        use_cache=settings.get("use_cache", True),
        cache_ttl=settings.get("cache_ttl"),
    )
    return [result]


async def _fetch_file(client: PageFetch, filepath: str, settings: dict) -> list[FetchResult]:
    """Fetch all URLs listed in a file."""
    try:
        urls = read_urls_from_file(filepath)
    except FileNotFoundError:
        print(f"\n  Error: file not found: {filepath!r}")
        return []
    if not urls:
        print("\n  Error: the file does not contain any URLs.")
        return []
    print(f"\n  Fetching {len(urls)} URL(s)...\n")
    return await client.fetch_many(
        urls,
        mode=settings.get("mode"),
        proxy=settings.get("proxy"),
        use_cache=settings.get("use_cache", True),
        cache_ttl=settings.get("cache_ttl"),
    )


def _handle_fetch(client: PageFetch, settings: dict, loop: asyncio.AbstractEventLoop) -> None:
    """Interactive fetch flow: URL or file."""
    _clear_screen()
    _banner()
    _header("Fetch")

    print("  1. Enter a single URL")
    print("  2. Load URLs from a text file (one per line)")
    print("  3. Back to main menu")
    print()

    choice = _prompt("  Choose", "1")

    if choice == "3":
        return

    _apply_debug(settings)
    if choice == "2":
        filepath = _prompt("  File path")
        if not filepath:
            return
        results = loop.run_until_complete(_fetch_file(client, filepath, settings))
    else:
        url = _prompt("  URL")
        if not url:
            return
        # Auto-prefix with https:// if no scheme
        if not url.startswith(("http://", "https://")):
            if _confirm(f"  No scheme detected. Use 'https://{url}'?", default=True):
                url = f"https://{url}"
        results = loop.run_until_complete(_fetch_url(client, url, settings))

    if not results:
        return

    _clear_screen()
    _banner()
    _header("Results")

    total = len(results)
    success_count = sum(1 for r in results if r.success)
    cache_count = sum(1 for r in results if r.from_cache)

    print(f"  {total} URL(s) — {success_count} succeeded ({cache_count} from cache), "
          f"{total - success_count} failed\n")

    output_format = settings.get("format", "markdown")
    include_html = settings.get("include_html", False)

    rendered = _render_results(results, output_format, include_html)
    output_file = settings.get("output")

    if output_file:
        path = Path(output_file)
        path.write_text(rendered, encoding="utf-8")
        print(f"  Results saved to: {path.resolve()}\n")
    else:
        print(rendered)

    # Show per-URL summary
    print()
    print("  --- Summary ---")
    for i, result in enumerate(results, 1):
        status = "OK" if result.success else "FAIL"
        title = result.title or "(no title)"
        duration = f"{result.duration_ms:.0f}ms" if result.duration_ms else "N/A"
        cache_tag = " [cache]" if result.from_cache else ""
        method = result.fetch_method or "?"
        tag = f"[{method}]{cache_tag}"
        print(f"  {i:>3}. [{status}] {duration} {tag}")
        print(f"       {title}")
        print(f"       {result.url}")
        if result.error:
            print(f"       Error: {result.error.message}")
        print()

    input("\n  Press Enter to continue...")


def _settings_menu(settings: dict) -> None:
    """Interactive settings submenu."""
    while True:
        _clear_screen()
        _banner()
        _header("Settings")

        mode = settings.get("mode") or "auto"
        proxy = settings.get("proxy") or "none"
        fmt = settings.get("format", "markdown")
        cache_ttl = settings.get("cache_ttl", "24h")
        output = settings.get("output", "none")
        include_html = "yes" if settings.get("include_html") else "no"
        debug = "yes" if settings.get("debug") else "no"
        config_file = settings.get("config_file", "none")
        no_cache = "yes" if settings.get("no_cache") else "no"

        print(f"  1. Fetch mode          : {mode}")
        print(f"  2. Proxy provider      : {proxy}")
        print(f"  3. Output format       : {fmt}")
        print(f"  4. Cache TTL           : {cache_ttl}")
        print(f"  5. Output file         : {output}")
        print(f"  6. Include raw HTML    : {include_html}")
        print(f"  7. Debug logging       : {debug}")
        print(f"  8. Disable cache       : {no_cache}")
        print(f"  9. Config file (YAML)  : {config_file}")
        print(f"  0. Back to main menu")
        print()

        choice = _prompt("  Choose", "0")

        if choice == "0":
            return
        elif choice == "1":
            print(f"\n  Options: {', '.join(sorted(VALID_MODES))}")
            val = _prompt("  Fetch mode", mode)
            if val in VALID_MODES:
                settings["mode"] = val
            else:
                print(f"  Invalid mode: {val}")
                input("  Press Enter...")
        elif choice == "2":
            print(f"\n  Options: {', '.join(sorted(VALID_PROXIES))}")
            val = _prompt("  Proxy provider", proxy)
            if val in VALID_PROXIES:
                settings["proxy"] = val
            else:
                print(f"  Invalid proxy: {val}")
                input("  Press Enter...")
        elif choice == "3":
            print("\n  Options: markdown, json, html")
            val = _prompt("  Output format", fmt)
            if val in ("markdown", "json", "html"):
                settings["format"] = val
            else:
                print(f"  Invalid format: {val}")
                input("  Press Enter...")
        elif choice == "4":
            val = _prompt("  Cache TTL (e.g. 30s, 15m, 24h, 7d)", cache_ttl)
            if val:
                settings["cache_ttl"] = val
        elif choice == "5":
            val = _prompt("  Output file path (leave empty to print to console)", output if output != "none" else "")
            settings["output"] = val if val else None
        elif choice == "6":
            settings["include_html"] = _confirm("  Include raw HTML in output?", default=settings.get("include_html", False))
        elif choice == "7":
            settings["debug"] = _confirm("  Enable debug logging?", default=settings.get("debug", False))
            _apply_debug(settings)
        elif choice == "8":
            settings["no_cache"] = _confirm("  Disable cache?", default=settings.get("no_cache", False))
            if settings.get("no_cache"):
                settings["use_cache"] = False
            else:
                settings.pop("use_cache", None)
        elif choice == "9":
            val = _prompt("  Config file path (leave empty for defaults)", config_file if config_file != "none" else "")
            settings["config_file"] = val if val else None


def _view_config(settings: dict) -> None:
    """Display current settings."""
    _clear_screen()
    _banner()
    _header("Current Configuration")

    config_file = settings.get("config_file")
    if config_file:
        print(f"  Config file  : {config_file}")
        config = PageFetchConfig.from_yaml(config_file)
        print(f"  Mode         : {config.mode}")
        print(f"  Proxy        : {config.proxy}")
    else:
        print("  Config file  : (defaults)")

    mode = settings.get("mode") or "auto"
    proxy = settings.get("proxy") or "none"

    print(f"  Mode         : {mode}")
    print(f"  Proxy        : {proxy}")
    print(f"  Format       : {settings.get('format', 'markdown')}")
    print(f"  Cache TTL    : {settings.get('cache_ttl', '24h')}")
    print(f"  Output file  : {settings.get('output', 'none')}")
    print(f"  Include HTML : {'yes' if settings.get('include_html') else 'no'}")
    print(f"  Debug        : {'yes' if settings.get('debug') else 'no'}")
    print(f"  No cache     : {'yes' if settings.get('no_cache') else 'no'}")
    print()

    input("  Press Enter to continue...")


def _init_client(settings: dict) -> PageFetch:
    """Create a PageFetch client from current settings."""
    config_file = settings.get("config_file")
    if config_file:
        config = PageFetchConfig.from_yaml(config_file)
    else:
        config = PageFetchConfig()

    mode = settings.get("mode") if settings.get("mode") is not None else config.mode
    proxy = settings.get("proxy") if settings.get("proxy") is not None else config.proxy
    use_cache = settings.get("use_cache", config.cache_enabled)
    cache_ttl = settings.get("cache_ttl", config.cache_ttl)

    return PageFetch(
        mode=mode,
        proxy=proxy,
        cache_enabled=use_cache,
        cache_ttl=cache_ttl,
        http_concurrency=config.http_concurrency,
        browser_concurrency=config.browser_concurrency,
        http_timeout=config.http_timeout,
        browser_timeout=config.browser_timeout,
    )


def interactive_main() -> int:
    """Run the interactive CLI menu loop."""
    settings: dict = {
        "format": "markdown",
        "include_html": False,
        "debug": False,
        "cache_ttl": "24h",
        "output": None,
        "no_cache": False,
        "use_cache": True,
        "mode": None,
        "proxy": None,
        "config_file": None,
    }

    _apply_debug(settings)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = _init_client(settings)

    try:
        while True:
            _clear_screen()
            _banner()

            print("  1. Fetch URL(s)")
            print("  2. Settings")
            print("  3. View current config")
            print("  4. Exit")
            print()

            choice = _prompt("  Choose", "1")

            try:
                if choice == "1":
                    _handle_fetch(client, settings, loop)
                    # Re-create client in case settings changed
                    try:
                        loop.run_until_complete(client.close())
                    except Exception:
                        pass
                    client = _init_client(settings)
                elif choice == "2":
                    _settings_menu(settings)
                    # Re-create client with new settings
                    try:
                        loop.run_until_complete(client.close())
                    except Exception:
                        pass
                    client = _init_client(settings)
                elif choice == "3":
                    _view_config(settings)
                elif choice == "4":
                    print("\n  Goodbye!")
                    break
            except KeyboardInterrupt:
                print("\n\n  Interrupted. Goodbye!")
                break
            except Exception as exc:
                print(f"\n  Unexpected error: {exc}")
                input("  Press Enter to continue...")

        try:
            loop.run_until_complete(client.close())
        except Exception:
            pass
    finally:
        loop.close()
    return 0
