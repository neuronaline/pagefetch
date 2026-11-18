"""Small command-line interface for testing PageFetch."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .client import PageFetch
from .config import (
    VALID_BLOCK_LEVELS,
    VALID_CLEANING_LEVELS,
    VALID_OUTPUT_FORMATS,
    VALID_PROXIES,
    VALID_SCREENSHOT_FORMATS,
    VALID_SCREENSHOT_MODES,
    VALID_SESSION_ROTATION,
    VALID_STEALTH_LEVELS,
    PageFetchConfig,
    _normalize_headers_input,
    merge_headers,
)
from .fetching.actions import normalize_actions
from .models import PageAction
from .utils.rendering import render_results
from .utils.urls import read_urls_from_file


def _configure_unicode_streams() -> None:
    """Use UTF-8 output where the active streams support reconfiguration."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pagefetch", description="Fetch complete web page content")
    parser.add_argument("input", metavar="URL_OR_FILE")
    parser.add_argument("--format", choices=sorted(VALID_OUTPUT_FORMATS), default="markdown")
    parser.add_argument("-c", "--config", type=Path, metavar="PATH", help="Path to config.yaml")
    parser.add_argument(
        "--proxy",
        choices=sorted(VALID_PROXIES),
        default=argparse.SUPPRESS,
        help="Proxy provider: none | custom (HTTP/HTTPS/SOCKS5 via CUSTOM_PROXY_URL) | decodo | byteful",
    )
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--include-html", action="store_true")
    parser.add_argument(
        "--screenshot",
        choices=sorted(VALID_SCREENSHOT_MODES),
        default=argparse.SUPPRESS,
        help="Capture a screenshot. 'viewport' = visible area, 'full' = entire scrollable page.",
    )
    parser.add_argument(
        "--screenshot-format",
        choices=sorted(VALID_SCREENSHOT_FORMATS),
        default=argparse.SUPPRESS,
        help="Screenshot encoding (default: png).",
    )
    parser.add_argument(
        "--cache-ttl",
        metavar="DURATION",
        default=argparse.SUPPRESS,
        help="Cache time-to-live, e.g. 30s, 15m, 24h, 7d (default: 24h)",
    )
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--concurrency", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--retries", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--block-images", action="store_true", default=argparse.SUPPRESS, dest="block_images")
    parser.add_argument("--no-block-images", action="store_false", default=argparse.SUPPRESS, dest="block_images")
    parser.add_argument("--debug", action="store_true")
    # Stealth / fingerprint configuration
    parser.add_argument(
        "--block-level",
        choices=sorted(VALID_BLOCK_LEVELS),
        default=argparse.SUPPRESS,
        help="Resource blocking aggressiveness (default: balanced)",
    )
    parser.add_argument(
        "--accept-language",
        metavar="HEADER",
        default=argparse.SUPPRESS,
        help="Accept-Language header (default: en-US,en;q=0.5)",
    )
    parser.add_argument("--humanize", action="store_true", default=argparse.SUPPRESS, dest="humanize")
    parser.add_argument("--no-humanize", action="store_false", default=argparse.SUPPRESS, dest="humanize")
    parser.add_argument(
        "--session-rotation",
        choices=sorted(VALID_SESSION_ROTATION),
        default=argparse.SUPPRESS,
        help="Proxy session rotation strategy (default: sticky)",
    )
    parser.add_argument(
        "--session-duration",
        metavar="DURATION",
        default=argparse.SUPPRESS,
        help=(
            "Optional sticky-session TTL forwarded to the residential "
            "provider (e.g. '30m', '2h', '1d'). Appended as "
            "``-sessionduration-<minutes>`` for Decodo or "
            "``_ttl_<n><unit>`` for Byteful (DECODO_DOCS §4, "
            "BYTEFUL_DOCS §4). Omit to use the provider default."
        ),
    )
    parser.add_argument(
        "--request-pacing",
        type=float,
        metavar="SECONDS",
        default=argparse.SUPPRESS,
        help="Per-host randomized delay ceiling in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--stealth-level",
        choices=sorted(VALID_STEALTH_LEVELS),
        default=argparse.SUPPRESS,
        help="Anti-detection profile preset (default: balanced)",
    )
    parser.add_argument(
        "--cleaning-level",
        choices=sorted(VALID_CLEANING_LEVELS),
        default=argparse.SUPPRESS,
        help="How aggressively to strip non-content DOM (default: standard)",
    )
    # Interaction and cookie extensions
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        dest="headers",
        metavar="HEADER",
        help="Custom HTTP header in 'Name: Value' format (repeatable)",
    )
    parser.add_argument(
        "--cookie",
        action="append",
        dest="cookies",
        metavar="COOKIE",
        help="Custom cookie or header profile in 'name=value' or 'k1=v1; k2=v2' format (repeatable)",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        dest="cookie_file",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="Path to Netscape cookies.txt or line-delimited Cookie header file",
    )
    parser.add_argument(
        "--expand",
        action="store_true",
        default=argparse.SUPPRESS,
        dest="expand",
        help="Automatically expand 'Read more' disclosures, accordions, and dynamic tabs (default: enabled)",
    )
    parser.add_argument(
        "--no-expand",
        action="store_false",
        default=argparse.SUPPRESS,
        dest="expand",
        help="Disable automatic disclosure and tab expansion",
    )
    parser.add_argument(
        "--action",
        action="append",
        dest="actions",
        metavar="ACTION",
        help=(
            "In-page interaction action (repeatable). E.g. 'click:#btn', "
            "'fill:#user:admin', 'wait:2000', 'scroll:bottom', or JSON string"
        ),
    )
    parser.add_argument(
        "--capture-xhr",
        nargs="?",
        const="*",
        default=None,
        metavar="PATTERN",
        help=(
            "Capture background XHR/Fetch API responses matching glob/URL pattern "
            "(e.g. '*api*'). Omitting PATTERN captures all traffic."
        ),
    )
    return parser


def _inputs(value: str) -> list[str]:
    if value.startswith(("http://", "https://")):
        return [value]
    path = Path(value).expanduser()
    if path.is_file():
        return read_urls_from_file(path)
    # Check if it looks explicitly like a local file path
    file_exts = {".txt", ".csv", ".json", ".yaml", ".yml", ".urls", ".list", ".tsv", ".log", ".in"}
    is_explicit_path = value.startswith(("./", "../", "/", "~", ".\\", "..\\")) or "\\" in value
    if is_explicit_path or (path.suffix and path.suffix.lower() in file_exts):
        raise ValueError(f"file not found: {value!r}")
    # Bare domain or domain with path: auto-prefix with https://
    host_part = value.split("/", 1)[0]
    if "." in host_part and not host_part.startswith(".") and not host_part.endswith("."):
        return [f"https://{value}"]
    if "/" in value or path.suffix:
        raise ValueError(f"file not found: {value!r}")
    return [value]


def _build_config(args: argparse.Namespace) -> PageFetchConfig:
    """Build configuration from YAML file (if provided) with CLI overrides in one pass."""
    flat: dict[str, Any] = (
        PageFetchConfig._read_yaml_dict(args.config)
        if getattr(args, "config", None)
        else {}
    )

    known_fields = {
        name for name, f in PageFetchConfig.__dataclass_fields__.items() if f.init
    }
    cli_overrides: dict[str, Any] = {
        k: v for k, v in vars(args).items()
        if k in known_fields and v is not None
    }
    if getattr(args, "no_cache", False):
        cli_overrides["cache_enabled"] = False

    if "stealth_level" in cli_overrides:
        for preset_field in (
            "block_level",
            "humanize",
            "session_rotation",
            "request_pacing",
            "block_images",
        ):
            if preset_field not in cli_overrides:
                flat.pop(preset_field, None)

    if "headers" in cli_overrides and "headers" in flat:
        cli_overrides["headers"] = merge_headers(
            _normalize_headers_input(flat.get("headers")),
            _normalize_headers_input(cli_overrides["headers"]),
        )

    flat.update(cli_overrides)
    return PageFetchConfig(**flat)


async def run_batch(
    config: PageFetchConfig,
    urls: list[str],
    *,
    output_format: str = "markdown",
    include_html: bool = False,
    screenshot: str = "none",
    screenshot_format: str = "png",
    output: Path | None = None,
    headers: dict[str, str] | None = None,
    cookies: Any = None,
    actions: Sequence[PageAction | dict[str, Any] | str] | None = None,
    capture_xhr: bool | str | Sequence[str] = False,
) -> int:
    """Run a fetch/extract batch using *config* and write results.

    Returns 0 on full success, 1 if every URL failed, 3 on partial failure.
    Validation errors (empty URL list, bad inputs) raise ``ValueError``.
    """
    if not urls:
        raise ValueError("the input file does not contain any URLs")
    extract_structure = output_format in {"structure", "raw"}
    include_screenshot = screenshot != "none" and output_format in {"raw", "json"}
    use_extract = extract_structure or screenshot != "none"
    async with PageFetch(config=config) as client:
        if use_extract:
            results = [
                await client.extract(
                    item,
                    structure=extract_structure,
                    screenshot=screenshot,
                    screenshot_format=screenshot_format,
                    headers=headers,
                    cookies=cookies,
                    actions=actions,
                    capture_xhr=capture_xhr,
                )
                for item in urls
            ]
        else:
            results = await client.fetch_many(
                urls,
                headers=headers,
                cookies=cookies,
                actions=actions,
                capture_xhr=capture_xhr,
            )
    rendered = render_results(
        results,
        output_format,
        include_html=include_html,
        include_structure=extract_structure,
        include_screenshot=include_screenshot,
    )
    if output:
        output.write_text(rendered, encoding="utf-8")
    else:
        try:
            print(rendered)
        except UnicodeEncodeError:
            sys.stdout.buffer.write((rendered + "\n").encode("utf-8"))
    total = len(results)
    if total == 0:
        return 0
    success_count = sum(1 for r in results if r.success)
    cache_count = sum(1 for r in results if r.from_cache)
    print(f"pagefetch: {total} URL(s) — {success_count} succeeded ({cache_count} from cache), {total - success_count} failed", file=sys.stderr)
    if success_count == 0:
        return 1
    if success_count < total:
        return 3  # partial failure
    return 0


async def _run(args: argparse.Namespace) -> int:
    urls = _inputs(args.input)
    screenshot = getattr(args, "screenshot", "none") or "none"
    screenshot_format = getattr(args, "screenshot_format", "png") or "png"
    config = _build_config(args)

    raw_actions = getattr(args, "actions", None)
    actions = normalize_actions(raw_actions) if raw_actions else None

    raw_xhr = getattr(args, "capture_xhr", None)
    if raw_xhr is None:
        capture_xhr: bool | str = False
    elif raw_xhr == "*":
        capture_xhr = True
    else:
        capture_xhr = raw_xhr

    return await run_batch(
        config,
        urls,
        output_format=args.format,
        include_html=args.include_html,
        screenshot=screenshot,
        screenshot_format=screenshot_format,
        output=args.output,
        actions=actions,
        capture_xhr=capture_xhr,
    )


def main(argv: list[str] | None = None) -> int:
    _configure_unicode_streams()
    args = build_parser().parse_args(argv)
    if args.debug:
        handler = logging.StreamHandler()
        logging.getLogger("pagefetch").addHandler(handler)
        logging.getLogger("pagefetch").setLevel(logging.DEBUG)
    try:
        return asyncio.run(_run(args))
    except (ValueError, OSError) as exc:
        print(f"pagefetch: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
