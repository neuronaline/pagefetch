"""Small command-line interface for testing PageFetch."""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from .client import PageFetch
from .config import (
    VALID_BLOCK_LEVELS,
    VALID_CLEANING_LEVELS,
    VALID_MODES,
    VALID_OUTPUT_FORMATS,
    VALID_PROXIES,
    VALID_SCREENSHOT_FORMATS,
    VALID_SCREENSHOT_MODES,
    VALID_SESSION_ROTATION,
    VALID_STEALTH_LEVELS,
    _UNSET,
    PageFetchConfig,
)
from .fetching.actions import normalize_actions
from .models import FetchResult, PageAction
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
    parser.add_argument("--mode", choices=sorted(VALID_MODES), default=argparse.SUPPRESS)
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
        help="Capture a screenshot (forces mode=browser). 'viewport' = visible area, 'full' = entire scrollable page.",
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
    parser.add_argument("--http-concurrency", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--browser-concurrency", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--browser-timeout", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--block-images", action="store_true", default=argparse.SUPPRESS, dest="block_images")
    parser.add_argument("--no-block-images", action="store_false", default=argparse.SUPPRESS, dest="block_images")
    parser.add_argument("--debug", action="store_true")
    # Stealth / fingerprint configuration
    parser.add_argument(
        "--block-level",
        choices=sorted(VALID_BLOCK_LEVELS),
        default=argparse.SUPPRESS,
        help="Resource blocking aggressiveness (default: aggressive)",
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
        help="Delay between requests (default: 0.0)",
    )
    parser.add_argument(
        "--stealth-level",
        choices=sorted(VALID_STEALTH_LEVELS),
        default=argparse.SUPPRESS,
        help="Anti-detection profile preset (default: off)",
    )
    parser.add_argument(
        "--cleaning-level",
        choices=sorted(VALID_CLEANING_LEVELS),
        default=argparse.SUPPRESS,
        help="How aggressively to strip non-content DOM (default: standard)",
    )
    # HTTP and interaction extensions
    parser.add_argument(
        "-X",
        "--method",
        default=None,
        help="HTTP request method: GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS (default: GET, or POST if payload is given)",
    )
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
        help="Custom cookie in 'name=value' format (repeatable)",
    )
    parser.add_argument(
        "-d",
        "--data",
        metavar="DATA",
        help="Request body payload for POST/PUT requests",
    )
    parser.add_argument(
        "--json",
        metavar="JSON",
        help="JSON payload string for POST/PUT requests",
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


parse_input_urls = _inputs



def _build_config(args: argparse.Namespace) -> PageFetchConfig:
    """Build configuration from YAML file (if provided) with CLI overrides."""
    config = (
        PageFetchConfig.from_yaml(args.config)
        if getattr(args, "config", None)
        else PageFetchConfig()
    )

    known_fields = set(PageFetchConfig.__dataclass_fields__.keys())
    cli_overrides = {
        k: v for k, v in vars(args).items()
        if k in known_fields and v is not None
    }
    if getattr(args, "timeout", None) is not None:
        cli_overrides["http_timeout"] = args.timeout
    if getattr(args, "no_cache", False):
        cli_overrides["cache_enabled"] = False

    if not cli_overrides:
        return config

    preset_override = "stealth_level" in cli_overrides
    preset_fields = {"block_level", "humanize", "session_rotation", "request_pacing"}

    kwargs: dict[str, object] = {}
    for field in known_fields:
        if field in cli_overrides:
            kwargs[field] = cli_overrides[field]
        elif preset_override and field in preset_fields:
            kwargs[field] = None
        elif preset_override and field == "block_images":
            kwargs[field] = _UNSET
        else:
            kwargs[field] = getattr(config, field)

    return PageFetchConfig.build(**kwargs)


def _parse_cli_headers(headers_list: list[str] | None) -> dict[str, str]:
    if not headers_list:
        return {}
    res: dict[str, str] = {}
    for h in headers_list:
        if ":" in h:
            k, v = h.split(":", 1)
            res[k.strip()] = v.strip()
    return res


def _parse_cli_cookies(cookies_list: list[str] | None) -> dict[str, str]:
    if not cookies_list:
        return {}
    res: dict[str, str] = {}
    for c in cookies_list:
        if "=" in c:
            k, v = c.split("=", 1)
            res[k.strip()] = v.strip()
    return res


async def run_batch(
    config: PageFetchConfig,
    urls: list[str],
    *,
    output_format: str = "markdown",
    include_html: bool = False,
    screenshot: str = "none",
    screenshot_format: str = "png",
    output: Path | None = None,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    content: bytes | str | None = None,
    data: dict[str, Any] | None = None,
    json_data: Any = None,
    actions: Sequence[PageAction | dict[str, Any] | str] | None = None,
    capture_xhr: bool | str | Sequence[str] = False,
) -> int:
    """Run a fetch/extract batch using *config* and write results.

    This is the shared execution core used by both the argparse ``cli.main``
    path and the interactive wizard.  Callers own prompting, validation, and
    any UI-specific concerns; this helper returns a process exit code so the
    interactive menu can also surface a meaningful status to the terminal.

    Returns 0 on full success, 1 if every URL failed, 3 on partial failure.
    Validation errors (empty URL list, bad inputs) raise ``ValueError``.
    """
    if not urls:
        raise ValueError("the input file does not contain any URLs")
    extract_structure = output_format in {"structure", "raw"}
    include_screenshot = screenshot != "none" and output_format in {"raw", "json"}
    use_extract = extract_structure or screenshot != "none"
    use_browser = use_extract or bool(actions) or bool(capture_xhr)
    effective_mode = "browser" if use_browser else config.mode
    if use_browser and config.mode != "browser":
        print(
            f"pagefetch: forced mode=browser (was {config.mode})",
            file=sys.stderr,
        )
    if effective_mode != config.mode:
        client_config = replace(config, mode=effective_mode)
    else:
        client_config = config
    async with PageFetch(config=client_config) as client:
        if use_extract:
            results = []
            pacing = config.request_pacing or 0.0
            for idx, item in enumerate(urls):
                # Stagger browser navigations to avoid a synchronized burst —
                # a stronger bot signal than the parallel fetch_many path.
                if pacing > 0 and idx > 0:
                    await asyncio.sleep(random.uniform(0, pacing))
                results.append(
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
                )
        else:
            results = await client.fetch_many(
                urls,
                method=method,
                headers=headers,
                cookies=cookies,
                content=content,
                data=data,
                json=json_data,
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

    headers = _parse_cli_headers(getattr(args, "headers", None)) or None
    cookies = _parse_cli_cookies(getattr(args, "cookies", None)) or None
    raw_actions = getattr(args, "actions", None)
    actions = normalize_actions(raw_actions) if raw_actions else None

    raw_xhr = getattr(args, "capture_xhr", None)
    if raw_xhr is None:
        capture_xhr: bool | str = False
    elif raw_xhr == "*":
        capture_xhr = True
    else:
        capture_xhr = raw_xhr

    json_payload = None
    if getattr(args, "json", None):
        import json as _json
        json_payload = _json.loads(args.json)

    data_payload = getattr(args, "data", None)
    method_arg = getattr(args, "method", None)
    if method_arg is None:
        method = "POST" if (data_payload is not None or json_payload is not None) else "GET"
    else:
        method = method_arg.upper()

    return await run_batch(
        config,
        urls,
        output_format=args.format,
        include_html=args.include_html,
        screenshot=screenshot,
        screenshot_format=screenshot_format,
        output=args.output,
        method=method,
        headers=headers,
        cookies=cookies,
        content=data_payload,
        json_data=json_payload,
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
