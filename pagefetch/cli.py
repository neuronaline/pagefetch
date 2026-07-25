"""Small command-line interface for testing PageFetch."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Literal

from .client import PageFetch
from .config import PageFetchConfig
from .models import FetchResult
from .utils.rendering import render_results
from .utils.urls import read_urls_from_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pagefetch", description="Fetch complete web page content")
    parser.add_argument("input", metavar="URL_OR_FILE")
    parser.add_argument("--format", choices=("markdown", "json", "html"), default="markdown")
    parser.add_argument("-c", "--config", type=Path, metavar="PATH", help="Path to config.yaml")
    parser.add_argument("--mode", choices=("auto", "http", "browser"), default=argparse.SUPPRESS)
    parser.add_argument("--proxy", choices=("none", "decodo", "dataimpulse"), default=argparse.SUPPRESS)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--include-html", action="store_true")
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
    return parser


def _inputs(value: str) -> list[str]:
    if value.startswith(("http://", "https://")):
        return [value]
    path = Path(value)
    if not path.is_file():
        # If it looks like a file path (has an extension or a directory separator)
        # but doesn't exist, give a clear error instead of treating it as a URL.
        if path.suffix or "/" in value or "\\" in value:
            raise ValueError(f"file not found: {value!r}")
        # Otherwise assume it's a URL (downstream validation will catch bad ones).
        return [value]
    return read_urls_from_file(value)


def _render(results: list[FetchResult], output_format: str, include_html: bool) -> str:
    return render_results(results, output_format, include_html=include_html)


def _build_config(args: argparse.Namespace) -> PageFetchConfig:
    """Build configuration from YAML file (if provided) with CLI overrides."""
    # Start from YAML if --config is given, otherwise use built-in defaults
    if args.config:
        config = PageFetchConfig.from_yaml(args.config)
    else:
        config = PageFetchConfig()

    # CLI overrides — only apply when the user explicitly set the argument
    overrides: dict[str, object] = {}
    if hasattr(args, "mode"):
        overrides["mode"] = args.mode
    if hasattr(args, "proxy"):
        overrides["proxy"] = args.proxy
    if hasattr(args, "cache_ttl"):
        overrides["cache_ttl"] = args.cache_ttl
    if hasattr(args, "http_concurrency"):
        overrides["http_concurrency"] = args.http_concurrency
    if hasattr(args, "browser_concurrency"):
        overrides["browser_concurrency"] = args.browser_concurrency
    if hasattr(args, "timeout"):
        overrides["http_timeout"] = args.timeout
    if hasattr(args, "browser_timeout"):
        overrides["browser_timeout"] = args.browser_timeout
    if hasattr(args, "block_images"):
        overrides["block_images"] = args.block_images

    if not overrides and not args.no_cache:
        return config

    # Rebuild with overrides applied
    return PageFetchConfig.build(
        mode=str(overrides.get("mode", config.mode)),
        proxy=str(overrides.get("proxy", config.proxy)),
        http_concurrency=int(overrides.get("http_concurrency", config.http_concurrency)),
        browser_concurrency=int(overrides.get("browser_concurrency", config.browser_concurrency)),
        cache_enabled=not args.no_cache if args.no_cache else config.cache_enabled,
        cache_ttl=overrides.get("cache_ttl", config.cache_ttl),
        cache_path=config.cache_path,
        http_timeout=float(overrides.get("http_timeout", config.http_timeout)),
        browser_timeout=float(overrides.get("browser_timeout", config.browser_timeout)),
        retries_http=config.retries_http,
        retries_browser=config.retries_browser,
        max_redirects=config.max_redirects,
        max_content_size=config.max_content_size,
        confidence_threshold=config.confidence_threshold,
        block_images=bool(overrides.get("block_images", config.block_images)),
        raise_on_error=config.raise_on_error,
    )


async def _run(args: argparse.Namespace) -> int:
    urls = _inputs(args.input)
    if not urls:
        raise ValueError("the input file does not contain any URLs")
    config = _build_config(args)
    async with PageFetch(
        mode=config.mode,
        proxy=config.proxy,
        cache_enabled=config.cache_enabled,
        cache_ttl=config.cache_ttl,
        cache_path=config.cache_path,
        http_concurrency=config.http_concurrency,
        browser_concurrency=config.browser_concurrency,
        http_timeout=config.http_timeout,
        browser_timeout=config.browser_timeout,
        retries_http=config.retries_http,
        retries_browser=config.retries_browser,
        max_redirects=config.max_redirects,
        max_content_size=config.max_content_size,
        confidence_threshold=config.confidence_threshold,
        block_images=config.block_images,
        raise_on_error=config.raise_on_error,
    ) as client:
        results = await client.fetch_many(urls)
    rendered = _render(results, args.format, args.include_html)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        try:
            print(rendered)
        except UnicodeEncodeError:
            sys.stdout.buffer.write((rendered + "\n").encode("utf-8"))
    total = len(results)
    success_count = sum(1 for r in results if r.success)
    cache_count = sum(1 for r in results if r.from_cache)
    print(f"pagefetch: {total} URL(s) — {success_count} succeeded ({cache_count} from cache), {total - success_count} failed", file=sys.stderr)
    if not results:
        return 0
    if success_count == 0:
        return 1
    if success_count < total:
        return 3  # partial failure
    return 0


def main(argv: list[str] | None = None) -> int:
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
