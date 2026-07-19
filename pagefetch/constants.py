"""Package constants."""

LOGGER_NAME = "pagefetch"

# NOTE: The version numbers in the User-Agent below (e.g. Chrome/131.0.0.0)
# will become stale over time.  Periodically refresh them against a current
# browser release to reduce the chance of server-side fingerprinting blocks.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
BLOCKED_STATUS_CODES = frozenset({403, 429})
HTML_TYPES = ("text/html", "application/xhtml+xml")
XML_TYPES = ("application/xml", "text/xml", "+xml")
SAFE_RESPONSE_HEADERS = frozenset(
    {"cache-control", "content-language", "content-location", "date", "etag", "last-modified"}
)
