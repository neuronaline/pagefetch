# PageFetch

**Async-first web scraping and content extraction library for Python 3.11+ — with
intelligent browser fallback, built-in caching, and structured Markdown output.**

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
content without fighting bot detection.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Why PageFetch](#why-pagefetch)
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
| **Inconsistent output formats** | Single `FetchResult` model: always get `.markdown`, `.html`, `.text`, `.links`, `.images`, `.metadata` |
| **Managing concurrency** | Built-in semaphores for HTTP (default 10) and browser (default 4) — safe for hundreds of URLs |
| **Repeated requests waste bandwidth** | SQLite disk cache with configurable TTL, shared across runs |
| **Proxy rotation complexity** | Native Decodo and DataImpulse integration — configure via env vars |
| **Content that isn't HTML** | PDFs auto-detected and extracted; XML documents parsed; plain text preserved |
| **Dependency management friction** | Core HTTP support stays lightweight; browser and PDF features use explicit extras |

---

## How It Works

1. **Normalize** the URL (scheme, encoding, fragments).
2. **Check cache** — if a valid SQLite entry exists, return instantly.
3. **HTTP fetch** using `httpx` with HTTP/2, connection pooling, and configurable retries.  
   Non-HTML responses (PDF, XML, plain text) are handled directly without browser overhead.
4. **Content analysis** — the `confidence` score evaluates HTML completeness using  
   text density, structural markup, heading presence, link counts, and common blocking signals  
   (captcha walls, empty bodies, access-denied patterns).
5. **Browser fallback** (auto mode only) — Camoufox takes over only when HTTP content  
   confidence is below the threshold (default 0.80), or when the server returns a blocked  
   status (403/429). Timeouts, connection failures, 404s, and 5xx responses fail fast at  
   the HTTP layer instead of waiting on a browser navigation.
6. **Processing pipeline** — cleaned HTML → extracted links, images, metadata →  
   converted to Markdown via a custom converter that preserves tables, code blocks,  
   and nested lists.
7. **Cache & return** — the structured `FetchResult` is persisted to SQLite and returned.

---

## Installation

### Prerequisites

- **Python** ≥ 3.11

### Install

```bash
# Core HTTP/HTML support
pip install .

# Add browser fallback
pip install ".[browser]"
python -m camoufox fetch

# Add PDF extraction
pip install ".[pdf]"

# Or install every optional feature
pip install ".[all]"
```

PageFetch never runs `pip` or downloads browser binaries implicitly. HTTP mode
therefore works without Camoufox, while `auto` and `browser` users can provision
the browser feature explicitly.

---

## API Reference

### `PageFetch` Client

The main entry point. Use as an async context manager for automatic cleanup.

```python
from pagefetch import PageFetch

client = PageFetch(
    mode="auto",              # "auto" | "http" | "browser"
    proxy="none",             # "none" | "decodo" | "dataimpulse"
    http_concurrency=10,      # Max parallel HTTP requests
    browser_concurrency=4,    # Max parallel browser instances
    cache_enabled=True,       # Enable SQLite disk cache
    cache_ttl="24h",          # TTL: "30m", "2h", "7d", or seconds as int
    cache_path=None,          # Custom SQLite cache path (None = platform default)
    http_timeout=20.0,        # Per-request HTTP timeout (seconds)
    browser_timeout=45.0,     # Per-page browser timeout (seconds)
    retries_http=3,           # Retry on 429/5xx for HTTP
    retries_browser=2,        # Retry on browser failure
    max_redirects=10,         # Maximum redirect chain
    max_content_size=25 * 1024 * 1024,  # Max response body bytes (25 MiB)
    confidence_threshold=0.80,    # Min confidence before browser fallback
    block_images=True,         # Block image loading in browser mode to save bandwidth
    raise_on_error=False,     # Raise PageFetchError instead of returning error result
)
```

### Modes

| Mode | Behavior |
|---|---|
| `"auto"` | HTTP first; falls back to Camoufox if confidence < threshold (default) |
| `"http"` | Pure HTTP/2 fetching — no browser, no confidence scoring |
| `"browser"` | Camoufox stealth browser for every request |

### Methods

**`fetch(url, *, mode=None, proxy=None, use_cache=True, cache_ttl=None, raise_on_error=None) → FetchResult`**

Fetch a single URL. All keyword arguments override the client-level defaults
for this individual request only.

**`fetch_many(urls, *, mode=None, proxy=None, use_cache=True, cache_ttl=None, raise_on_error=None) → list[FetchResult]`**

Fetch multiple URLs concurrently. Deduplicates identical inputs internally,
preserves the original input order, and isolates individual failures — one
bad URL never affects the others.

---

## Output Model

Every fetch returns a `FetchResult` dataclass:

```python
from dataclasses import dataclass

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
    fetch_method: str | None    # "http" or "browser"
    proxy_provider: str         # "none", "decodo", or "dataimpulse"
    content_confidence: float | None  # 0–1 completeness score (None for browser mode)
    from_cache: bool            # Was this served from cache?
    duration_ms: float | None   # Total fetch duration
    fetched_at: datetime | None # ISO 8601 timestamp
    warnings: list[str]         # Non-fatal issues (cache skip, etc.)
    error: FetchErrorInfo | None  # Error details when success=False
```

### Serialization

```python
# JSON output (HTML excluded by default for compactness)
print(result.json(indent=2))
print(result.json(include_html=True))   # Include raw HTML

# Python dict
data = result.to_dict()
data = result.to_dict(include_html=True)

# Reconstruct from cached JSON
reconstructed = FetchResult.from_dict(data)
```

---

## CLI Usage

PageFetch ships with a command-line interface accessible via `pagefetch`:

```bash
# Fetch and print Markdown
pagefetch https://example.com --format markdown

# Fetch and print raw HTML
pagefetch https://example.com --format html

# Fetch from a list and output JSON
pagefetch urls.txt --format json --mode auto

# Save output to a file
pagefetch https://example.com --mode browser -o output.md

# Structured JSON with raw HTML included
pagefetch https://example.com --format json --include-html

# Multiple URLs from a file (one URL per line)
pagefetch urls.txt --format json --mode auto

# Load configuration from a YAML file with CLI overrides
pagefetch --config config.yaml --mode browser https://example.com

# Override cache TTL and disable image loading
pagefetch https://example.com --cache-ttl 1h --block-images
```

CLI arguments map directly to the Python API — `--mode`, `--proxy`, `--timeout`,
`--browser-timeout`, `--http-concurrency`, `--browser-concurrency`, `--cache-ttl`,
`--no-cache`, `--block-images` / `--no-block-images`, `--include-html`,
`--debug`, and `--config` are all supported.

---

## Caching

PageFetch uses a **SQLite-backed disk cache** (`platformdirs` user cache directory
by default). Cache entries are keyed by normalized URL + mode + proxy + relevant
fetch settings, so switching from `"auto"` to `"browser"` mode produces a
different cache key.

- **Default TTL**: 24 hours (configurable: `"30m"`, `"2h"`, `"7d"`, or integer seconds)
- **Automatic**: cache hits skip all network and browser work
- **Graceful degradation**: cache read/write failures never crash a fetch — they produce warnings

```python
# Disable caching for a single request
result = await client.fetch("https://example.com", use_cache=False)

# Override TTL per-request
result = await client.fetch("https://example.com", cache_ttl="1h")

# Use a custom cache location
client = PageFetch(cache_path="/path/to/custom_cache.sqlite3")
```

---

## Proxy Support

PageFetch natively supports **rotating residential proxy** providers:

| Provider | Env Var (Full URL) | Env Vars (Components) |
|---|---|---|
| **Decodo** | `DECODO_PROXY_URL` | `DECODO_HOST`, `DECODO_PORT`, `DECODO_USERNAME`, `DECODO_PASSWORD` |
| **DataImpulse** | `DATAIMPULSE_PROXY_URL` | `DATAIMPULSE_HOST`, `DATAIMPULSE_PORT`, `DATAIMPULSE_USERNAME`, `DATAIMPULSE_PASSWORD` |

```python
# Use a proxy provider
async with PageFetch(proxy="decodo") as client:
    result = await client.fetch("https://example.com")
```

Credentials are **never** included in results, logs, or cache keys. Configure
either a full proxy URL or the individual components — PageFetch validates
both forms automatically.

---

## Non-HTML Content

PageFetch handles content types beyond HTML natively:

| Content Type | Detection | Extraction |
|---|---|---|
| **PDF** | Magic bytes + `Content-Type` | Text via optional `pagefetch[pdf]` support |
| **XML** | `Content-Type` matching `+xml` or `application/xml` | Preserved as `.text` |
| **Plain text** | Fallback when no structured type matches | Served as `.text` directly |

No browser overhead is incurred for non-HTML content — detection happens
at the HTTP response level before any processing pipeline runs.

---

## Configuration Reference

| Parameter | Type | Default | Description |
|---|---|---|---|
| `mode` | `str` | `"auto"` | Fetch strategy: `"auto"`, `"http"`, or `"browser"` |
| `proxy` | `str` | `"none"` | Proxy provider: `"none"`, `"decodo"`, or `"dataimpulse"` |
| `http_concurrency` | `int` | `10` | Maximum concurrent HTTP connections |
| `browser_concurrency` | `int` | `4` | Maximum concurrent browser instances |
| `cache_enabled` | `bool` | `True` | Enable SQLite disk cache |
| `cache_ttl` | `str \| int` | `"24h"` | Cache time-to-live |
| `cache_path` | `str \| Path` | *platform default* | Custom SQLite cache file path |
| `http_timeout` | `float` | `20.0` | HTTP request timeout in seconds |
| `browser_timeout` | `float` | `45.0` | Browser page load timeout in seconds |
| `retries_http` | `int` | `3` | Automatic retries on retryable HTTP errors |
| `retries_browser` | `int` | `2` | Automatic retries on browser failures |
| `max_redirects` | `int` | `10` | Maximum redirect chain to follow |
| `max_content_size` | `int` | `25 MiB` | Maximum response body in bytes |
| `confidence_threshold` | `float` | `0.80` | Threshold for browser fallback in auto mode |
| `block_images` | `bool` | `True` | Block image loading in browser mode to save bandwidth |
| `raise_on_error` | `bool` | `False` | Raise `PageFetchError` on failure instead of returning error result |

---

## Development

```bash
# Clone and set up
git clone <repo-url> && cd pagefetch

# Install with test dependencies
pip install -e ".[test]"

# Run the test suite (browser integration tests are opt-in)
pytest

# Lint
ruff check .
```

Browser integration requires the separately downloaded Camoufox binary and is
therefore kept optional in deterministic test environments. The test suite is
designed to run fully offline — URLs are served via local fixtures.

---

## License

PageFetch is released under the [MIT License](LICENSE).

---

<p align="center">
  <sub>
    Built with ❤️ for developers who need reliable, structured web content
    without fighting bot detection.
  </sub>
</p>
