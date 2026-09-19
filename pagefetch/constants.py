"""Package constants."""

# NOTE: The User-Agent below is aligned with the Camoufox (Firefox-based)
# browser used for rendering.  Keeping HTTP and browser paths on the same
# browser family avoids a stacked fingerprint (Chrome UA → Firefox engine)
# in auto-mode double-hit scenarios.  Periodically refresh the version
# numbers to match a recent Firefox ESR release.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) "
        "Gecko/20100101 Firefox/136.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Per-OS Firefox User-Agent pools.  ``client.py`` picks the pool matching
# the runtime's ``sys.platform`` so outgoing HTTP requests declare an OS
# consistent with the host (mismatched OS fingerprints are a known
# bot-detection signal).  The Camoufox browser fallback sets its own
# User-Agent independently, so this pool only governs httpx requests.
_UA_POOL_BY_OS: dict[str, tuple[str, ...]] = {
    "windows": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:115.0) Gecko/20100101 Firefox/115.0",
    ),
    "macos": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:136.0) Gecko/20100101 Firefox/136.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:132.0) Gecko/20100101 Firefox/132.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
    ),
    "linux": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
    ),
}

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
BLOCKED_STATUS_CODES = frozenset({403, 429})

XML_TYPES = ("application/xml", "text/xml", "+xml")
SAFE_RESPONSE_HEADERS = frozenset(
    {"cache-control", "content-language", "content-location", "date", "etag", "last-modified"}
)
