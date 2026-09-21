# PageFetch

**Async-first web scraping and content extraction library for Python 3.11+ — with
intelligent browser fallback, built-in caching, SSRF protection, and structured Markdown output.**

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/code%20style-ruff-261230" alt="Ruff">
</p>

PageFetch is a modern, asynchronous Python library for **fetching web pages** and
extracting clean, structured content. It attempts a pooled **HTTP request** first,
scores the returned HTML using multiple **completeness signals**, and lazily falls
back to a **Camoufox stealth browser** when the page looks blocked, empty, or
dependent on client-side JavaScript rendering. The result is always a consistent
`FetchResult` object carrying **Markdown**, raw HTML, extracted links, images,
metadata, and more — regardless of which method succeeded.

Perfect for **web scraping**, **content aggregation**, **LLM data pipelines**,
**SEO analysis**, **archiving**, and any workflow that needs reliable page
content without fighting bot detection or leaking internal infrastructure.

Supported environments: Windows 10/11 x64 and Ubuntu 22.04/24.04 x64 on
Python 3.11–3.13. Browser mode relies on the corresponding upstream Camoufox
artifact.

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
    async with PageFetch(mode="auto", cache_ttl="24h") as client:
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
async with PageFetch(mode="auto") as client:
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
| **Bot detection & blocking** | Camoufox stealth browser with automatic fallback when HTTP returns empty, blocked (403/429), or JavaScript-dependent pages |
| **Over-fetching with heavy browsers** | HTTP-first strategy — only ~5–15% of pages need the browser in auto mode |
| **Internal network vulnerabilities** | Built-in SSRF protection blocking private IPs, loopback, link-local, and cloud metadata endpoints across both HTTP and browser layers |
| **Inconsistent output formats** | Single `FetchResult` model: always get `.markdown`, `.html`, `.text`, `.links`, `.images`, `.metadata` |
| **Managing concurrency** | Built-in semaphores for HTTP (default 10) and browser (default 4) with bounded browser session rotation |
| **Repeated requests waste bandwidth** | SQLite disk cache with configurable TTL, shared across runs |
| **Proxy rotation complexity** | Native Decodo and Byteful integration — configure via env vars or code |
| **Content that isn't HTML** | PDFs auto-detected and extracted; XML documents parsed; plain text preserved |
| **Fast DOM inspection** | $O(N)$ single-pass structural summary generating verified unique CSS selectors |
| **Dependency management friction** | Core HTTP support stays lightweight; browser and PDF features use explicit extras |
| **Hard-to-match fingerprints** | `stealth_level` presets + `humanize`, `block_level`, `request_pacing`, and `session_rotation` |

---

## Security & SSRF Protection

PageFetch incorporates strict Server-Side Request Forgery (SSRF) defense at both the HTTP and browser layers:

- **Restricted Targets**: Any attempt to fetch private networks (e.g. `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), loopback addresses (`127.0.0.0/8`, `localhost`), link-local IPs, multicast ranges, or cloud provider metadata endpoints (`169.254.169.254`) is rejected immediately with an `invalid_url` error.
- **Redirect Validation**: HTTP redirect chains validate each hop against SSRF rules, preventing open redirect bypasses.
- **Browser Route Interception**: Camoufox browser instances intercept page-level and subresource network routes to block access to restricted hosts and internal IP ranges.
- **Opt-in Runtime Installs**: Dynamic package installation at runtime is disabled by default (`PAGEFETCH_AUTO_INSTALL=0`) to prevent unexpected execution of external install scripts.

---

## Stealth & Anti-Detection

When `mode` is `"auto"` or `"browser"`, PageFetch exposes a layered stealth
posture so the browser fingerprint can stay aligned with the proxy exit
country:

| Knob | Purpose |
|---|---|
| `stealth_level` | One-shot preset: `"off"` (default), `"balanced"`, or `"max"`. Sets `humanize`, `block_level`, `request_pacing`, and `session_rotation` together; explicit values still win. |
| `humanize` | Add small randomized delays to mimic human interaction. |
| `block_level` | `"minimal"`, `"balanced"`, or `"aggressive"` resource blocking (third-party trackers, fonts, media). |
| `session_rotation` | `"sticky"` reuses one proxy session per domain; `"rotate"` manages fresh sessions within a bounded browser pool. |
| `request_pacing` | Fixed seconds of delay between browser requests (`0.0` = none). |
| `accept_language` | Value sent as the `Accept-Language` header. |
| `cleaning_level` | How aggressively non-content DOM is stripped before extraction: `"minimal"` (display:none / hidden / 1×1 pixels only), `"standard"` (default — also drops cookie banners, ad slots, tracking pixels), `"maximum"` (removes layout chrome, nav, and sidebars while preserving article headers, `h1`/`h2`, tables, code blocks, and primary content). |

```python
# Quiet, fast default for open sites
async with PageFetch(mode="auto") as client:
    ...

# Maximum stealth for heavily protected targets
async with PageFetch(
    mode="browser",
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
- **Windows / macOS** — Firefox's native `headless=True`. No display server
  is required.

On Linux, `Xvfb` must be installed (e.g. `apt install xvfb` on Debian/Ubuntu,
`dnf install xorg-x11-server-Xvfb` on Fedora/RHEL). When the binary is
missing, browser fetches surface a `FetchErrorInfo` with code `xvfb_missing`.

---

## How It Works

1. **Normalize & Validate** — URL syntax, encoding, scheme, and host safety (SSRF checks) are validated before any network call.
2. **Check cache** — If a valid SQLite entry exists, return instantly.
3. **HTTP fetch** — Performed via `httpx` with HTTP/2, connection pooling, and bounded retries. Non-HTML responses (PDF, XML, JSON, plain text) are processed directly without browser overhead.
4. **Content analysis** — The `confidence` score evaluates HTML completeness using text density, structural markup, heading presence, link counts, and common blocking signals (multilingual captcha walls, empty bodies, access-denied patterns).
5. **Browser fallback** (auto mode) — Camoufox takes over when HTTP content confidence is below the threshold (default 0.80), when the server returns a blocked status (`403` or `429`), or on Cloudflare Under Attack (`503` with WAF challenge markers). Blocked responses bypass unnecessary HTTP retries and switch directly to browser rendering. Genuine timeouts, connection failures, 404s, and non-challenge 5xx responses fail fast at the HTTP layer.
6. **Processing pipeline** — Cleaned HTML → extracted links, images, metadata → converted to Markdown via an engine preserving tables (with proper escaping of table pipes), code blocks, and nested lists.
7. **Cache & return** — The structured `FetchResult` is persisted to SQLite and returned.

---

## Installation

### Prerequisites

- **Python** ≥ 3.11
- **Xvfb** (Linux only, for stealth browser mode: `sudo apt install xvfb`)

### Install

```bash
# Core HTTP/HTML support (lightweight)
pip install .

# Add browser fallback
pip install ".[browser]"
python -m camoufox fetch

# Add PDF extraction
pip install ".[pdf]"

# Or install every optional feature
pip install ".[all]"
python -m camoufox fetch
```

HTTP mode works without Camoufox. For browser fallback or `browser` mode, install
`pagefetch[browser]` and fetch the browser artifact via `python -m camoufox fetch`.
Automatic package and binary installation on first use is disabled by default for safety, but can be explicitly enabled with `PAGEFETCH_AUTO_INSTALL=1`.

---

## API Reference

### `PageFetch` Client

The main entry point. Use as an async context manager for automatic cleanup.

```python
from pagefetch import PageFetch

client = PageFetch(
    mode="auto",              # "auto" | "http" | "browser"
    proxy="none",             # "none" | "custom" | "decodo" | "byteful"
    cleaning_level="standard", # "minimal" | "standard" | "maximum"
    http_concurrency=10,      # Max parallel HTTP requests
    browser_concurrency=4,    # Max parallel browser instances
    cache_enabled=True,       # Enable SQLite disk cache
    cache_ttl="24h",          # TTL: "30m", "2h", "7d", or seconds as int
    cache_path=None,          # Custom SQLite cache path (None = platform default)
    http_timeout=20.0,        # Per-request HTTP timeout (seconds)
    browser_timeout=45.0,     # Per-page browser timeout (seconds)
    retries_http=3,           # Retry on 5xx (and HTTP-only 429) errors
    retries_browser=2,        # Retry on browser failure
    max_redirects=10,         # Maximum redirect chain
    max_content_size=25 * 1024 * 1024,  # Max response body bytes (25 MiB)
    confidence_threshold=0.80,    # Min confidence before browser fallback
    block_images=True,        # Block image loading in browser mode to save bandwidth
    block_level="aggressive", # "minimal" | "balanced" | "aggressive"
    accept_language="en-US,en;q=0.5",  # Accept-Language header
    humanize=False,          # Add small randomized delays to mimic a human
    session_rotation="sticky",# "sticky" | "rotate" proxy session strategy
    session_duration=None,    # Optional sticky-session TTL for residential providers
                              # ("30m", "2h", "1d" or integer seconds). Appends
                              # -sessionduration-<minutes> (Decodo) or
                              # _ttl_<n><unit> (Byteful) — see DECODO_DOCS §4,
                              # BYTEFUL_DOCS §4. Ignored under session_rotation="rotate".
    request_pacing=0.0,       # Seconds of delay between requests
    stealth_level="off",      # "off" | "balanced" | "max" preset
    raise_on_error=False,     # Raise PageFetchError instead of returning error result
    screenshot_max_bytes=50 * 1024 * 1024,  # Max bytes for extract(screenshot=…)
)
```

### Modes

| Mode | Behavior |
|---|---|
| `"auto"` | HTTP first; falls back to Camoufox if confidence < threshold or 403/429 received (default) |
| `"http"` | Pure HTTP/2 fetching — no browser, no confidence scoring |
| `"browser"` | Camoufox stealth browser for every request |

### Methods

**`fetch(url, *, mode=None, proxy=None, use_cache=True, cache_ttl=None, raise_on_error=None) → FetchResult`**

Fetch a single URL. All keyword arguments override client-level defaults for this request. Bare domains (e.g. `example.com`) are automatically normalized to `https://`.

**`fetch_many(urls, *, mode=None, proxy=None, use_cache=True, cache_ttl=None, raise_on_error=None) → list[FetchResult]`**

Fetch multiple URLs concurrently. Deduplicates identical inputs, preserves original ordering, and isolates failures across URLs.

**`extract(url, *, structure=True, compact_structure=False, screenshot="none", screenshot_format="png", proxy=None, use_cache=True, cache_ttl=None, raise_on_error=None) → FetchResult`**

Fetch a page in browser mode and return the rendered DOM, an optional structural summary, and/or a screenshot:

- `result.html` — Fully rendered HTML.
- `result.structure` — `PageStructure` summary ($O(N)$ extraction with verified `unique_selector`).
- `result.screenshot` / `result.screenshot_format` — Captured screenshot bytes (`screenshot="viewport"` or `screenshot="full"`).

Screenshots are bounded by `screenshot_max_bytes` (default 50 MiB) and are not cached in SQLite.

### Public API Exports

| Symbol | Purpose |
|---|---|
| `PageFetch`, `PageFetchConfig` | Client + validated configuration |
| `FetchResult`, `LinkInfo`, `ImageInfo`, `FetchErrorInfo` | Result dataclasses |
| `PageStructure`, `StructureNode`, `StylesheetInfo`, `InlineStylesheet`, `ScriptInfo`, `InlineScript` | Page-structure summary types |
| `StructureLimits`, `extract_structure` | Lower-level structure extraction engine |
| `PageFetchError`, `RuntimeBootstrapError` | Exception hierarchy |
| `ensure_runtime_requirements`, `auto_bootstrap_browser` | Runtime dependency validation & installation |
| `VALID_MODES`, `VALID_PROXIES` | Configuration constants |

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
    fetch_method: str | None    # "http", "browser", "text", "xml", or "pdf"
    proxy_provider: str         # "none", "custom", "decodo", or "byteful"
    content_confidence: float | None  # 0–1 completeness score (None for browser mode)
    from_cache: bool            # Was this served from cache?
    duration_ms: float | None   # Total fetch duration
    fetched_at: datetime | None # ISO 8601 timestamp
    warnings: list[str]         # Non-fatal issues (cache skip, etc.)
    error: FetchErrorInfo | None  # Error details when success=False
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
pagefetch https://example.com --mode browser -o output.md

# Process multiple URLs from a file
pagefetch urls.txt --format json --mode auto

# Use residential proxy with sticky session and explicit 2-hour TTL
pagefetch https://example.com --proxy byteful --session-duration 2h

# Apply stealth preset with session rotation
pagefetch https://example.com --mode browser --stealth-level balanced --session-rotation rotate

# Debug mode for troubleshooting
pagefetch https://example.com --debug
```

### CLI Options

| Flag | Maps to |
|---|---|
| `inputs` (positional) | One or more URLs or text files containing URLs |
| `--mode {auto,http,browser}` | `mode` |
| `--proxy {none,custom,decodo,byteful}` | `proxy` |
| `--http-concurrency N` / `--browser-concurrency N` | `http_concurrency` / `browser_concurrency` |
| `--timeout SECONDS` / `--browser-timeout SECONDS` | `http_timeout` / `browser_timeout` |
| `--cache-ttl DURATION` / `--no-cache` | `cache_ttl` / `cache_enabled=False` |
| `--block-images` / `--no-block-images` | `block_images` |
| `--block-level {minimal,balanced,aggressive}` | `block_level` |
| `--accept-language HEADER` | `accept_language` |
| `--humanize` / `--no-humanize` | `humanize` |
| `--session-rotation {sticky,rotate}` | `session_rotation` |
| `--session-duration DURATION` | `session_duration` |
| `--request-pacing SECONDS` | `request_pacing` |
| `--stealth-level {off,balanced,max}` | `stealth_level` |
| `--cleaning-level {minimal,standard,maximum}` | `cleaning_level` |
| `--include-html` | `FetchResult.json(include_html=…)` |
| `--screenshot {none,viewport,full}` | `PageFetch.extract(screenshot=…)` |
| `--screenshot-format {png,jpeg}` | `PageFetch.extract(screenshot_format=…)` |
| `--format {markdown,json,html,structure,raw}` | Output format renderer |
| `-o PATH` / `--output PATH` | Write output to a file |
| `-c PATH` / `--config PATH` | Load configuration from YAML file |
| `--debug` | Enable verbose DEBUG logging |

Exit codes: `0` all succeeded, `1` all failed, `2` usage/IO error, `3` partial failure.

### Interactive Menu

Running `python -m pagefetch` with **no arguments** launches a guided interactive terminal menu (mode, proxy, stealth, format, URL entry). Supplying any argument (e.g. `python -m pagefetch example.com`) directly runs the CLI.

---

## Caching

PageFetch uses a **SQLite-backed disk cache** stored in the platform's user cache directory (`platformdirs`). Cache keys incorporate the normalized URL, fetch mode, proxy configuration, and relevant stealth settings.

- **Default TTL**: 24 hours (configurable: `"30m"`, `"2h"`, `"7d"`, or integer seconds).
- **Network bypass**: Cache hits return instantly without network or browser overhead.
- **Graceful degradation**: Cache read/write issues produce warnings without interrupting fetches.

```python
# Bypass cache for a single request
result = await client.fetch("https://example.com", use_cache=False)

# Custom TTL per request
result = await client.fetch("https://example.com", cache_ttl="1h")

# Custom SQLite database location
client = PageFetch(cache_path="/path/to/custom_cache.sqlite3")
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
async with PageFetch(proxy="byteful", session_duration="2h") as client:
    result = await client.fetch("https://example.com")

# Rotate proxy sessions across requests
async with PageFetch(proxy="byteful", session_rotation="rotate") as client:
    results = await client.fetch_many([...])
```

Accepted schemes for `custom`: `http`, `https`, `socks5`, `socks5h`.
Credentials are never exposed in result objects, logs, or cache keys.

---

## Non-HTML Content

| Content Type | Detection | Extraction |
|---|---|---|
| **PDF** | Magic bytes + `Content-Type` | Clean text extraction via optional `pagefetch[pdf]` extra |
| **XML** | `+xml` or `application/xml` | Strictly parsed with `lxml`; text in `.text` and XML tree in fenced `.markdown` |
| **JSON** | `application/json` or `+json` | Raw payload preserved as `.text` and `.markdown` without browser overhead |
| **Plain text** | `text/plain` or fallback | Preserved as `.text` and `.markdown` |

Non-HTML content is handled immediately at the HTTP layer, bypassing all browser dependencies.

---

## Configuration Reference

| Parameter | Type | Default | Description |
|---|---|---|---|
| `mode` | `str` | `"auto"` | Fetch strategy: `"auto"`, `"http"`, or `"browser"` |
| `proxy` | `str` | `"none"` | Proxy provider: `"none"`, `"custom"` (any HTTP/HTTPS/SOCKS5/SOCKS5H), `"decodo"`, or `"byteful"` |
| `cleaning_level` | `str` | `"standard"` | DOM cleaning level: `"minimal"`, `"standard"`, or `"maximum"` |
| `http_concurrency` | `int` | `10` | Maximum concurrent HTTP connections |
| `browser_concurrency` | `int` | `4` | Maximum concurrent browser instances |
| `cache_enabled` | `bool` | `True` | Enable SQLite disk cache |
| `cache_ttl` | `str \| int` | `"24h"` | Cache time-to-live |
| `cache_path` | `str \| Path` | *platform default* | Custom SQLite cache file path |
| `http_timeout` | `float` | `20.0` | HTTP request timeout in seconds |
| `browser_timeout` | `float` | `45.0` | Browser page load timeout in seconds |
| `retries_http` | `int` | `3` | Automatic retries on retryable HTTP errors (5xx, HTTP-only 429) |
| `retries_browser` | `int` | `2` | Automatic retries on browser failures |
| `max_redirects` | `int` | `10` | Maximum redirect chain |
| `max_content_size` | `int` | `25 MiB` | Maximum response body in bytes |
| `confidence_threshold` | `float` | `0.80` | Minimum confidence score before browser fallback in auto mode |
| `block_images` | `bool` | `True`* | Block image loading in browser mode (*defaults to `False` in `balanced`/`max` stealth for fingerprint coherence) |
| `block_level` | `str` | `"aggressive"` | Resource blocking: `"minimal"`, `"balanced"`, or `"aggressive"` |
| `accept_language` | `str` | `"en-US,en;q=0.5"` | Value sent in `Accept-Language` header |
| `humanize` | `bool` | `False` | Add randomized delays mimicking human interactions |
| `session_rotation` | `str` | `"sticky"` | `"sticky"` or `"rotate"` proxy session rotation |
| `session_duration` | `str \| int \| None` | `None` | Optional sticky-session TTL forwarded to residential providers. Decodo → `-sessionduration-<minutes>` (1–1440 min); Byteful → `_ttl_<n><unit>` (1 min – 7 days). See DECODO_DOCS §4, BYTEFUL_DOCS §4. |
| `request_pacing` | `float` | `0.0` | Seconds of delay between browser requests |
| `stealth_level` | `str` | `"off"` | Anti-detection preset: `"off"`, `"balanced"`, or `"max"` |
| `raise_on_error` | `bool` | `False` | Raise `PageFetchError` on failure instead of returning error result |
| `screenshot_max_bytes` | `int` | `50 MiB` | Maximum allowed screenshot byte size |
| `browser_pre_check_byte_margin` | `float` | `1.5` | Multiplicative margin for browser pre-render size checks |

---

## Development

```bash
# Clone and set up
git clone https://github.com/neuronaline/pagefetch.git && cd pagefetch

# Install with test dependencies
pip install -e ".[test]"

# Run test suite
pytest

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
