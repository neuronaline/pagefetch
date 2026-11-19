# PageFetch

**Async-first Camoufox-powered web scraping and content extraction library for Python 3.11+ — with
built-in stealth, automatic DOM expansion, SQLite caching, SSRF protection, and structured Markdown output.**

<p align="center">
  <a href="https://pypi.org/project/pagefetch/"><img src="https://img.shields.io/pypi/v/pagefetch" alt="PyPI"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/code%20style-ruff-261230" alt="Ruff">
</p>

PageFetch is a modern, asynchronous Python library for **fetching web pages and documents**
through a **Camoufox stealth browser** and extracting clean, structured content. Every request
renders in an isolated browser context, scrolls and expands in-DOM disclosures/tabs by default,
and captures inline or downloaded documents (`PDF`, `DOCX`, `CSV`, `XML`, `JSON`, `TXT`) through
the same unified browser stack. The result is always a consistent `FetchResult` object carrying
**Markdown**, raw HTML, extracted links, images, metadata, and more.

Perfect for **web scraping**, **content aggregation**, **LLM data pipelines**,
**SEO analysis**, **archiving**, and any workflow that needs reliable page
content without fighting bot detection or leaking internal infrastructure.

Supported environments: Windows 10/11 x64 and Ubuntu 22.04/24.04 x64 on
Python 3.11–3.13, backed by the corresponding upstream Camoufox browser artifact.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Why PageFetch](#why-pagefetch)
- [Security & SSRF Protection](#security--ssrf-protection)
- [Stealth & Anti-Detection](#stealth--anti-detection)
- [How It Works](#how-it-works)
- [Installation](#installation)
- [API Reference](#api-reference)
- [Output Model](#output-model)
- [CLI Usage](#cli-usage)
- [Caching](#caching)
- [Proxy Support](#proxy-support)
- [Non-HTML Content](#non-html-content)
- [Configuration Reference](#configuration-reference)
- [Development](#development)
- [License](#license)

---

## Quick Start

```python
import asyncio

from pagefetch import PageFetch


async def main() -> None:
    async with PageFetch(cache_ttl="24h") as client:
        result = await client.fetch("https://example.com")
        if result.success:
            print(f"Title: {result.title}")
            print(f"Markdown length: {len(result.markdown or '')} chars")
            print(f"Links found: {len(result.links or [])}")
            print(f"Images found: {len(result.images or [])}")
        else:
            print(f"Failed: {result.error.message}")


asyncio.run(main())
```

### Fetch Multiple URLs in Parallel

```python
async with PageFetch() as client:
    results = await client.fetch_many([
        "https://example.com",
        "https://example.org",
        "https://httpbin.org/html",
    ])
    for r in results:
        status = "✓" if r.success else "✗"
        print(f"{status} {r.url} ({r.fetch_method}, {r.duration_ms:.0f}ms)")
```

---

## Why PageFetch

| Challenge | PageFetch Solution |
|---|---|
| **Bot detection & dynamic rendering** | Native Camoufox stealth browser for every request (`stealth_level="balanced"` and `expand=True` by default) |
| **Internal network vulnerabilities** | Built-in SSRF protection blocking private IPs, loopback, link-local, and cloud metadata endpoints across navigation and subresource routes |
| **Inconsistent output formats** | Single `FetchResult` model: always get `.markdown`, `.html`, `.text`, `.links`, `.images`, `.metadata` |
| **Managing concurrency** | Built-in browser semaphore (default 4) with bounded browser process rotation |
| **Repeated requests waste bandwidth** | SQLite disk cache with configurable TTL, shared across runs |
| **Proxy rotation complexity** | Native Decodo and Byteful integration — configure via env vars or code |
| **Content that isn't HTML** | PDFs, DOCX, CSV/TSV, XML, JSON, and plain text captured via inline browser payloads or native downloads |
| **Fast DOM inspection** | $O(N)$ single-pass structural summary generating verified unique CSS selectors |
| **Zero-setup dependencies** | `camoufox[geoip]` and `pypdf` included as core dependencies |
| **Hard-to-match fingerprints** | `stealth_level` presets + `humanize`, `block_level`, `request_pacing`, and `session_rotation` |

---

## Security & SSRF Protection

PageFetch incorporates strict Server-Side Request Forgery (SSRF) defense across navigation and browser route interception:

- **Restricted Targets**: Any attempt to fetch private networks (e.g. `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), loopback addresses (`127.0.0.0/8`, `localhost`), link-local IPs, multicast ranges, or cloud provider metadata endpoints (`169.254.169.254`) is rejected immediately with an `invalid_url` error.
- **Browser Route Interception**: Camoufox browser instances intercept page-level and subresource network routes to block access to restricted hosts and internal IP ranges.
- **Opt-in Runtime Installs**: Dynamic browser binary download at runtime is disabled by default (`PAGEFETCH_AUTO_INSTALL=0`) unless explicitly enabled.

---

## Stealth & Anti-Detection

PageFetch defaults to `stealth_level="balanced"` so the browser fingerprint stays aligned with the proxy exit country out of the box:

| Knob | Purpose |
|---|---|
| `stealth_level` | One-shot preset: `"balanced"` (default), `"off"`, or `"max"`. Sets `humanize`, `block_level`, `request_pacing`, and `session_rotation` together; explicit values still win. |
| `humanize` | Add small randomized delays to mimic human interaction. |
| `block_level` | `"minimal"`, `"balanced"` (default), or `"aggressive"` resource blocking (third-party trackers, fonts, media). |
| `session_rotation` | `"sticky"` reuses one proxy session per domain; `"rotate"` manages fresh sessions within a bounded browser pool. |
| `request_pacing` | Per-host randomized delay ceiling in seconds (`0.5` in `"balanced"`, `0.0` = none). |
| `accept_language` | Value sent as the `Accept-Language` header (explicit non-default values override proxy GeoIP locale). |
| `cleaning_level` | How aggressively non-content DOM is stripped before extraction: `"minimal"` (display:none / hidden / 1×1 pixels only), `"standard"` (default — also drops cookie banners, ad slots, tracking pixels), `"maximum"` (removes layout chrome, nav, and sidebars while preserving article headers, `h1`/`h2`, tables, code blocks, and primary content). |

```python
# Balanced stealth + tab/disclosure expansion (default)
async with PageFetch() as client:
    ...

# Maximum stealth with residential proxy
async with PageFetch(
    proxy="decodo",
    stealth_level="max",
) as client:
    result = await client.fetch("https://example.com")
```

The CLI exposes every knob via `--stealth-level`, `--block-level`,
`--humanize` / `--no-humanize`, `--session-rotation`, `--session-duration`,
`--request-pacing`, `--accept-language`, and `--cleaning-level`.

### Platform-aware headless mode

Camoufox launches in a different mode per OS so the fingerprint stays
realistic on every host:

- **Linux** — `headless=False` against an in-process `Xvfb`. The browser
  renders a real window into the virtual display, avoiding the
  fingerprinting tells that come with Firefox's `--headless` flag. Display
  lifecycles are strictly managed to avoid orphan processes.
- **Windows** — Firefox's native `headless=True`. No display server
  is required.

On Linux, `Xvfb` must be installed (e.g. `apt install xvfb` on Debian/Ubuntu,
`dnf install xorg-x11-server-Xvfb` on Fedora/RHEL). When the binary is
missing, browser fetches surface a `FetchErrorInfo` with code `xvfb_missing`.

---

## How It Works

1. **Normalize & Validate** — URL syntax, encoding, scheme, and host safety (SSRF checks) are validated before any network call.
2. **Check cache** — If a valid SQLite entry exists, return instantly.
3. **Camoufox Browser Fetch** — Opens an isolated context (with per-context proxy and cookie profile), navigates, waits for readiness / solves passive challenges, scrolls, and expands disclosures & dynamic tabs (`expand=True` by default).
4. **Non-HTML & Download Handling** — Inline non-HTML payloads (`PDF`, `JSON`, `XML`, `TXT`) and browser-triggered attachment downloads (`PDF`, `CSV`/`TSV`, `DOCX`) are extracted directly from the browser session and converted into structured `FetchResult` payloads.
5. **Processing pipeline** — Cleaned HTML → extracted links, images, metadata → converted to Markdown via an engine preserving tables (with proper escaping of table pipes), code blocks, and nested lists.
6. **Cache & return** — The structured `FetchResult` is persisted to SQLite and returned.

---

## Installation

### Prerequisites

- **Python** ≥ 3.11
- **Xvfb** (Linux only: `sudo apt install xvfb`)

### Install

```bash
pip install pagefetch
python -m camoufox fetch
```

Automatic binary download on first use is disabled by default for safety, but can be explicitly enabled with `PAGEFETCH_AUTO_INSTALL=1`.

---

## API Reference

### `PageFetch` Client

The main entry point. Use as an async context manager for automatic cleanup.
`PageFetch(...)` exposes the 9 everyday options directly; pass a
`PageFetchConfig` instance for advanced tuning:

```python
from pagefetch import PageFetch, PageFetchConfig

client = PageFetch(
    proxy="none",              # "none" | "custom" | "decodo" | "byteful"
    cleaning_level="standard", # "minimal" | "standard" | "maximum"
    stealth_level="balanced",  # "off" | "balanced" | "max" preset
    expand=True,               # Auto-expand in-DOM disclosures & client-side tabs
    cache_enabled=True,        # Enable SQLite disk cache
    cache_ttl="24h",           # TTL: "30m", "2h", "7d", or seconds as int
    raise_on_error=False,      # Raise PageFetchError instead of returning error result
    headers={},                # Default extra headers sent on every request
    cookies={},                # Default cookies / CookiePool source
)

# Advanced concurrency, timeout, retry, proxy session, and browser tuning
# goes through PageFetchConfig:
config = PageFetchConfig(
    proxy="byteful",
    concurrency=4,             # Max parallel browser contexts
    cache_path=None,           # Custom SQLite cache path (None = platform default)
    timeout=45.0,              # Per-page timeout (seconds)
    retries=2,                 # Retry on browser/challenge failure
    max_content_size=25 * 1024 * 1024,  # Max response body bytes (25 MiB)
    confidence_threshold=0.80, # Min confidence for challenge/completeness checks
    block_images=False,        # Block image loading (False in balanced/max stealth)
    block_level="balanced",    # "minimal" | "balanced" | "aggressive"
    accept_language="en-US,en;q=0.5",  # Accept-Language header
    humanize=True,             # Add randomized delays to mimic human interaction
    session_rotation="sticky", # "sticky" | "rotate" proxy session strategy
    session_duration=None,     # Optional sticky-session TTL for residential providers
    request_pacing=0.5,        # Per-host randomized delay ceiling (seconds)
    screenshot_max_bytes=50 * 1024 * 1024,  # Max bytes for extract(screenshot=…)
)
client = PageFetch(config=config)  # or PageFetch.from_config(config)

# Or load directly from a YAML file in one shot:
client = PageFetch.from_yaml("config.yaml")
```

### Methods

**`fetch(url, *, proxy=None, headers=None, cookies=None, actions=None, capture_xhr=False, expand=None, use_cache=True, cache_ttl=None, raise_on_error=None) → FetchResult`**

Fetch a single URL through Camoufox. Keyword arguments override client-level defaults for that request. Requires an absolute `http://` or `https://` URL (the CLI also accepts bare domains like `example.com`).

**`fetch_many(urls, *, proxy=None, ..., use_cache=True, cache_ttl=None, raise_on_error=None) → list[FetchResult]`**

Fetch multiple URLs concurrently. Deduplicates identical inputs, preserves original ordering, and isolates failures across URLs.

**`extract(url, *, structure=True, compact_structure=False, screenshot="none", screenshot_format="png", proxy=None, headers=None, cookies=None, actions=None, capture_xhr=False, expand=None, use_cache=True, cache_ttl=None, raise_on_error=None) → FetchResult`**

Fetch a page and return the rendered DOM, an optional structural summary, and/or a screenshot:

- `result.html` — Fully rendered HTML.
- `result.structure` — `PageStructure` summary ($O(N)$ extraction with verified `unique_selector`).
- `result.screenshot` / `result.screenshot_format` — Captured screenshot bytes (`screenshot="viewport"` or `screenshot="full"`).
- `result.sections` — Structured `ExpandedSection` items collected from static/expanded tabs and disclosures.

Screenshots are bounded by `screenshot_max_bytes` (default 50 MiB) and are not cached in SQLite.

**`extract_many(urls, *, structure=True, compact_structure=False, screenshot="none", screenshot_format="png", ..., use_cache=True, cache_ttl=None, raise_on_error=None) → list[FetchResult]`**

Extract multiple URLs concurrently. Deduplicates identical inputs, preserves original ordering, and isolates failures across URLs.

**`clear_cache(*, expired_only=False) → int`**

Delete all cached entries (or only expired entries when `expired_only=True`) from the configured SQLite cache and return the number of removed rows. Safe to call both inside `async with PageFetch()` and standalone.

### Public API Exports

| Symbol | Purpose |
|---|---|
| `PageFetch`, `PageFetchConfig` | Client + validated configuration |
| `FetchResult`, `ExpandedSection`, `LinkInfo`, `ImageInfo`, `FetchErrorInfo` | Result dataclasses |
| `CookiePool`, `CookieProfile`, `CookieRecord`, `load_cookies`, `parse_cookie_pool` | Stateless cookie-pool management & RFC 6265 matching |
| `PageStructure`, `StructureNode`, `StylesheetInfo`, `InlineStylesheet`, `ScriptInfo`, `InlineScript` | Page-structure summary types |
| `StructureLimits`, `extract_structure`, `extract_document` | Lower-level DOM structure & non-HTML extraction engines |
| `PageAction`, `ActionResult`, `CapturedXHR`, `parse_action`, `normalize_actions` | In-page browser interaction & XHR capture models |
| `PageFetchError`, `ProxyConfigurationError`, `RuntimeBootstrapError` | Unified exception hierarchy rooted at `PageFetchError` (`ProxyConfigurationError` also subclasses `ValueError`; `RuntimeBootstrapError` also subclasses `RuntimeError`) |
| `ensure_runtime_requirements`, `auto_bootstrap_browser` | Runtime dependency validation & installation |
| `VALID_PROXIES`, `VALID_STEALTH_LEVELS`, `VALID_BLOCK_LEVELS`, `VALID_CLEANING_LEVELS`, `VALID_SESSION_ROTATION`, `VALID_ACTION_TYPES` | Configuration & validation constants |

PageFetch is fully typed (PEP 561 compliant, shipping with `py.typed`). Type checkers like `mypy` and `pyright` will automatically discover in-line type annotations.

---

## Output Model

Every fetch returns a `FetchResult` dataclass:

```python
from dataclasses import dataclass
from datetime import datetime

@dataclass
class FetchResult:
    url: str                    # Normalized request URL
    final_url: str | None       # URL after all redirects
    status_code: int | None     # HTTP status code
    success: bool               # Did the fetch succeed?
    content_type: str | None    # e.g. "text/html", "application/pdf"
    encoding: str | None        # Detected charset
    title: str | None           # Page <title> or PDF title
    markdown: str | None        # Cleaned Markdown body
    html: str | None            # Raw HTML (excluded from JSON by default)
    text: str | None            # Plain-text body fallback
    metadata: dict              # OpenGraph, Twitter Cards, meta tags
    links: list[LinkInfo]       # All <a> tags with text, URL, rel
    images: list[ImageInfo]     # All <img> tags with url, alt, title
    structure: PageStructure | None  # DOM/stylesheet/script summary (when requested)
    screenshot: bytes | None         # PNG/JPEG screenshot bytes (when requested)
    screenshot_format: str | None    # "png" or "jpeg"
    fetch_method: str | None    # "browser", "pdf", "docx", "csv", "json", "xml", or "text"
    proxy_provider: str         # "none", "custom", "decodo", or "byteful"
    content_confidence: float | None  # 0.0–1.0 completeness score (1.0 for non-HTML documents)
    from_cache: bool            # Was this served from cache?
    duration_ms: float | None   # Total fetch duration
    fetched_at: datetime | None # ISO 8601 timestamp
    warnings: list[str]         # Non-fatal issues (cache skip, etc.)
    error: FetchErrorInfo | None  # Error details when success=False
    actions_executed: list[ActionResult] | None  # Browser action outcomes
    xhr_responses: list[CapturedXHR] | None      # Captured background XHR/fetch payloads
    sections: list[ExpandedSection] | None       # Static or dynamically expanded tab sections
```

### Serialization

```python
# JSON output (HTML, structure, and screenshot excluded by default)
print(result.json(indent=2))
print(result.json(include_html=True))                    # Include raw HTML
print(result.json(include_structure=True))               # Include PageStructure summary
print(result.json(include_structure=True, compact_structure=True)) # Compact summary for LLMs
print(result.json(include_screenshot=True))              # Include base64-encoded screenshot

# Python dict
data = result.to_dict()
data = result.to_dict(include_screenshot=True)

# Reconstruct from dictionary
reconstructed = FetchResult.from_dict(data)
```

---

## CLI Usage

PageFetch provides a powerful command-line interface accessible via `pagefetch` or `python -m pagefetch`:

```bash
# Fetch and print Markdown (bare domains like example.com are supported)
pagefetch example.com --format markdown

# Fetch raw HTML
pagefetch https://example.com --format html

# Output structured JSON
pagefetch https://example.com --format json

# Take a full-page screenshot and get JSON with base64 screenshot data
pagefetch https://example.com --format json --screenshot viewport

# Inspect page structure as Markdown
pagefetch https://example.com --format structure

# Raw page shell (HTML + structure + screenshot)
pagefetch https://example.com --format raw --screenshot full --screenshot-format png

# Save output to a file
pagefetch https://example.com -o output.md

# Process multiple URLs from a file
pagefetch urls.txt --format json

# Use residential proxy with sticky session and explicit 2-hour TTL
pagefetch https://example.com --proxy byteful --session-duration 2h

# Apply maximum stealth preset with session rotation
pagefetch https://example.com --stealth-level max --session-rotation rotate

# Clear cached entries (standalone or before fetching)
pagefetch --clear-cache
pagefetch https://example.com --clear-cache

# Debug mode for troubleshooting
pagefetch https://example.com --debug
```

### CLI Options

| Flag | Maps to |
|---|---|
| `input` (positional) | A single URL or path to a text file containing URLs (optional when `--clear-cache` is used) |
| `--proxy {none,custom,decodo,byteful}` | `proxy` |
| `--concurrency N` | `concurrency` |
| `--timeout SECONDS` | `timeout` |
| `--retries N` | `retries` |
| `--cache-ttl DURATION` / `--no-cache` | `cache_ttl` / `cache_enabled=False` |
| `--clear-cache` | `PageFetch.clear_cache()` (standalone or before fetching) |
| `--block-images` / `--no-block-images` | `block_images` |
| `--block-level {minimal,balanced,aggressive}` | `block_level` |
| `--accept-language HEADER` | `accept_language` |
| `--humanize` / `--no-humanize` | `humanize` |
| `--session-rotation {sticky,rotate}` | `session_rotation` |
| `--session-duration DURATION` | `session_duration` |
| `--request-pacing SECONDS` | `request_pacing` |
| `--stealth-level {off,balanced,max}` | `stealth_level` |
| `--cleaning-level {minimal,standard,maximum}` | `cleaning_level` |
| `--expand` / `--no-expand` | `expand` |
| `-H HEADER` / `--header HEADER` | `headers` (repeatable `'Name: Value'`) |
| `--cookie COOKIE` / `--cookie-file PATH` | `cookies` / `cookie_file` |
| `--action ACTION` | `actions` (repeatable interaction spec) |
| `--capture-xhr [PATTERN]` | `capture_xhr` |
| `--include-html` | `FetchResult.json(include_html=…)` |
| `--screenshot {none,viewport,full}` | `PageFetch.extract(screenshot=…)` |
| `--screenshot-format {png,jpeg}` | `PageFetch.extract(screenshot_format=…)` |
| `--format {markdown,json,html,structure,raw}` | Output format renderer |
| `-o PATH` / `--output PATH` | Write output to a file |
| `-c PATH` / `--config PATH` | Load configuration from YAML file |
| `--debug` | Enable verbose DEBUG logging |

Exit codes: `0` all succeeded, `1` all failed, `2` usage/IO error, `3` partial failure.

---

## Caching

PageFetch uses a **SQLite-backed disk cache** stored in the platform's user cache directory (`platformdirs`). Cache keys incorporate the normalized URL, proxy configuration, cookie/header tokens, and relevant extraction and stealth settings.

- **Default TTL**: 24 hours (configurable: `"30m"`, `"2h"`, `"7d"`, or integer seconds).
- **Network bypass**: Cache hits return instantly without network or browser overhead.
- **Graceful degradation**: Cache read/write issues produce warnings without interrupting fetches.

```python
# Bypass cache for a single request
result = await client.fetch("https://example.com", use_cache=False)

# Custom TTL per request
result = await client.fetch("https://example.com", cache_ttl="1h")

# Clear cached entries (returns deleted row count)
removed = await client.clear_cache()
expired_removed = await client.clear_cache(expired_only=True)

# Custom SQLite database location
from pagefetch import PageFetchConfig
client = PageFetch(config=PageFetchConfig(cache_path="/path/to/custom_cache.sqlite3"))
```

---

## Proxy Support

PageFetch supports any standard HTTP/HTTPS/SOCKS5/SOCKS5H proxy out of
the box, plus first-class integrations with two residential providers:

| Provider | Env Var (Full URL) | Notes |
|---|---|---|
| **`custom`** | `CUSTOM_PROXY_URL` (fallback `PROXY_URL`) | Any standard proxy — self-hosted, datacenter, corporate gateway, Tor. URL passed through verbatim, no username rewriting. |
| **Decodo** | `DECODO_PROXY_URL` | Residential; embeds session ID in username as `user-<user>-session-<id>`. Optional `session_duration` appends `-sessionduration-<minutes>` (DECODO_DOCS §4; 1–1440 minutes). |
| **Byteful** | `BYTEFUL_PROXY_URL` | Residential; embeds session ID in username as `<user>_s_<id>`. Optional `session_duration` appends `_ttl_<n><unit>` (BYTEFUL_DOCS §4; 1 minute – 7 days). |

```python
# Standard SOCKS5 proxy — credentials are optional
import os
os.environ["CUSTOM_PROXY_URL"] = "socks5://user:pass@proxy.example.com:1080"
async with PageFetch(proxy="custom") as client:
    result = await client.fetch("https://example.com")

# Decodo proxy with German exit node
async with PageFetch(proxy="decodo") as client:
    result = await client.fetch("https://example.de")

# Byteful sticky session with a 2-hour TTL token
async with PageFetch(config=PageFetchConfig(proxy="byteful", session_duration="2h")) as client:
    result = await client.fetch("https://example.com")

# Rotate proxy sessions across requests
async with PageFetch(config=PageFetchConfig(proxy="byteful", session_rotation="rotate")) as client:
    results = await client.fetch_many([...])
```

Accepted schemes for `custom`: `http`, `https`, `socks5`, `socks5h`.
Credentials are never exposed in result objects, logs, or cache keys.

---

## Non-HTML Content

| Content Type | Detection | Extraction |
|---|---|---|
| **PDF** | Magic bytes + `Content-Type` + `.pdf` | Clean page-structured text & metadata extraction via `pypdf` |
| **DOCX** | Archive signature / MIME / `.docx` | Headings, lists, hyperlinks, and GFM tables via `lxml` + `zipfile` |
| **CSV / TSV** | MIME / `.csv` / `.tsv` | Delimiter detection + GFM Markdown table in `.markdown` and full data in `.text` |
| **XML** | `+xml`, `application/xml`, or `<?xml` | Feeds (RSS/Atom) formatted as Markdown articles; generic XML in fenced code |
| **JSON** | `application/json` or `+json` | Pretty-printed `.text` and fenced `.markdown` with metadata |
| **Plain text** | `text/plain` or `.txt` / `.md` | Preserved as `.text` and `.markdown` |

Non-HTML content is captured directly from inline browser responses or native attachment downloads through the same Camoufox context, skipping HTML scroll/expansion passes.

---

## Configuration Reference

| Parameter | Type | Default | Description |
|---|---|---|---|
| `proxy` | `str` | `"none"` | Proxy provider: `"none"`, `"custom"` (any HTTP/HTTPS/SOCKS5/SOCKS5H), `"decodo"`, or `"byteful"` |
| `cleaning_level` | `str` | `"standard"` | DOM cleaning level: `"minimal"`, `"standard"`, or `"maximum"` |
| `concurrency` | `int` | `4` | Maximum concurrent browser contexts |
| `cache_enabled` | `bool` | `True` | Enable SQLite disk cache |
| `cache_ttl` | `str \| int` | `"24h"` | Cache time-to-live |
| `cache_path` | `str \| Path` | *platform default* | Custom SQLite cache file path |
| `timeout` | `float` | `45.0` | Browser page load timeout in seconds |
| `retries` | `int` | `2` | Automatic retries on browser/challenge failures |
| `max_content_size` | `int` | `25 MiB` | Maximum response body in bytes |
| `confidence_threshold` | `float` | `0.80` | Minimum confidence score for challenge/completeness scoring |
| `block_images` | `bool` | `False`* | Block image loading in browser mode (*defaults to `False` in `balanced`/`max` stealth, `True` in `off`) |
| `block_level` | `str` | `"balanced"` | Resource blocking: `"minimal"`, `"balanced"`, or `"aggressive"` |
| `accept_language` | `str` | `"en-US,en;q=0.5"` | Value sent in `Accept-Language` header (explicit non-default values override proxy GeoIP locale) |
| `humanize` | `bool` | `True` | Add randomized delays mimicking human interactions |
| `session_rotation` | `str` | `"sticky"` | `"sticky"` or `"rotate"` proxy session rotation |
| `session_duration` | `str \| int \| None` | `None` | Optional sticky-session TTL forwarded to residential providers. Decodo → `-sessionduration-<minutes>` (1–1440 min); Byteful → `_ttl_<n><unit>` (1 min – 7 days). See DECODO_DOCS §4, BYTEFUL_DOCS §4. |
| `request_pacing` | `float` | `0.5` | Per-host randomized delay ceiling (seconds) between requests |
| `stealth_level` | `str` | `"balanced"` | Anti-detection preset: `"off"`, `"balanced"`, or `"max"` |
| `expand` | `bool` | `True` | Automatically expand in-DOM disclosures & dynamic tabs |
| `max_scroll_steps` | `int` | `25` | Maximum main-window scroll steps per navigation |
| `max_scroll_height` | `int` | `150000` | Maximum main-window scroll height (pixels) |
| `cookie_file` | `str \| Path \| None` | `None` | Optional Netscape `cookies.txt` or Cookie header list file |
| `cookie_cooldown` | `float \| int \| str` | `120.0` | Base cooldown seconds for blocked cookie profiles |
| `headers` | `dict[str, str]` | `{}` | Default request headers (credential headers stay origin-scoped) |
| `cookies` | `Any` | `{}` | Default cookies or `CookiePool` source |
| `raise_on_error` | `bool` | `False` | Raise `PageFetchError` on failure instead of returning error result |
| `screenshot_max_bytes` | `int` | `50 MiB` | Maximum allowed screenshot byte size |
| `max_requests` | `int` | `50` | Max requests served before recycling the browser process |
| `idle_ttl` | `float \| str \| None` | `180.0` | Idle seconds before closing an unused browser process |
| `pre_check_byte_margin` | `float` | `1.5` | Multiplicative margin for browser pre-render size checks |

---

## Development

```bash
# Clone and set up
git clone https://github.com/neuronaline/pagefetch.git && cd pagefetch

# Install editable package
pip install -e .

# Code formatting & linting
ruff check .
```

---

## License

PageFetch is released under the [MIT License](LICENSE).

<p align="center">
  <sub>
    Built with ❤️ for developers who need reliable, structured web content without fighting bot detection.
  </sub>
</p>
