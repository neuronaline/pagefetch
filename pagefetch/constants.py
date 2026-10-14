"""Package constants."""

# NOTE: The User-Agent below is aligned with the Camoufox (Firefox-based)
# browser used for rendering.  Keeping HTTP and browser paths on the same
# browser family avoids a stacked fingerprint (Chrome UA → Firefox engine)
# in auto-mode double-hit scenarios.  Periodically refresh the version
# numbers to match a recent Firefox ESR release.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
        "Gecko/20100101 Firefox/140.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Priority": "u=0, i",
    "TE": "trailers",
}

# Per-OS Firefox User-Agent pools.  ``client.py`` picks the pool matching
# the runtime's ``sys.platform`` so outgoing HTTP requests declare an OS
# consistent with the host (mismatched OS fingerprints are a known
# bot-detection signal).  The Camoufox browser fallback sets its own
# User-Agent independently, so this pool only governs httpx requests.
_UA_POOL_BY_OS: dict[str, tuple[str, ...]] = {
    "windows": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    ),
    "macos": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:138.0) Gecko/20100101 Firefox/138.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:136.0) Gecko/20100101 Firefox/136.0",
    ),
    "linux": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:138.0) Gecko/20100101 Firefox/138.0",
        "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
    ),
}

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
BLOCKED_STATUS_CODES = frozenset({403, 429})

VALID_HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)

# HTTP redirect status codes the manual redirect loop in ``HTTPFetcher._request``
# has to handle. ``follow_redirects=False`` is set on every httpx client so the
# SSRF guard can validate each ``Location`` header before the next hop.
REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})

XML_TYPES = ("application/xml", "text/xml", "+xml")
SAFE_RESPONSE_HEADERS = frozenset(
    {"cache-control", "content-language", "content-location", "date", "etag", "last-modified"}
)

WAF_CHALLENGE_MARKERS: tuple[bytes, ...] = (
    b"cf-chl-bypass",
    b"cf_chl_opt",
    b"__cf_chl_jschl_tk__",
    b"challenge-running",
    b"checking your browser",
    b"verify you are human",
    b"just a moment",
    b"attention required",
    b"access denied",
    b"akamai bot manager",
    b"perimeterx",
    b"datadome",
    b"kasada",
    b"px-captcha",
)


def looks_like_waf_challenge(content: bytes) -> bool:
    """Return True when *content* carries WAF challenge markers in the first 4 KiB."""
    if not content:
        return False
    head = content[:4096].lower()
    return any(marker in head for marker in WAF_CHALLENGE_MARKERS)
