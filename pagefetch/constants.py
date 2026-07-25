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

# Small pool of Firefox User-Agent strings rotated per-domain so requests
# to different sites carry slightly different fingerprints.  All variants
# stay within the same browser family (Firefox on Windows/Linux/macOS).
_UA_POOL: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:115.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
)

# Per-country locale / timezone / Accept-Language mapping for proxy geo
# alignment.  Sources: IANA TZ database, CLDR locale data.
GEO_MAP: dict[str, dict[str, str]] = {
    "US": {"locale": "en-US", "timezone": "America/New_York", "accept_language": "en-US,en;q=0.9"},
    "GB": {"locale": "en-GB", "timezone": "Europe/London", "accept_language": "en-GB,en;q=0.9"},
    "DE": {"locale": "de-DE", "timezone": "Europe/Berlin", "accept_language": "de-DE,de;q=0.9,en;q=0.5"},
    "TR": {"locale": "tr-TR", "timezone": "Europe/Istanbul", "accept_language": "tr-TR,tr;q=0.9,en;q=0.5"},
    "FR": {"locale": "fr-FR", "timezone": "Europe/Paris", "accept_language": "fr-FR,fr;q=0.9,en;q=0.5"},
    "JP": {"locale": "ja-JP", "timezone": "Asia/Tokyo", "accept_language": "ja-JP,ja;q=0.9,en;q=0.5"},
    "BR": {"locale": "pt-BR", "timezone": "America/Sao_Paulo", "accept_language": "pt-BR,pt;q=0.9,en;q=0.5"},
    "IN": {"locale": "en-IN", "timezone": "Asia/Kolkata", "accept_language": "en-IN,en;q=0.9,hi;q=0.5"},
    "CA": {"locale": "en-CA", "timezone": "America/Toronto", "accept_language": "en-CA,en;q=0.9,fr;q=0.5"},
    "AU": {"locale": "en-AU", "timezone": "Australia/Sydney", "accept_language": "en-AU,en;q=0.9"},
    "NL": {"locale": "nl-NL", "timezone": "Europe/Amsterdam", "accept_language": "nl-NL,nl;q=0.9,en;q=0.5"},
    "ES": {"locale": "es-ES", "timezone": "Europe/Madrid", "accept_language": "es-ES,es;q=0.9,en;q=0.5"},
    "IT": {"locale": "it-IT", "timezone": "Europe/Rome", "accept_language": "it-IT,it;q=0.9,en;q=0.5"},
    "PL": {"locale": "pl-PL", "timezone": "Europe/Warsaw", "accept_language": "pl-PL,pl;q=0.9,en;q=0.5"},
    "SE": {"locale": "sv-SE", "timezone": "Europe/Stockholm", "accept_language": "sv-SE,sv;q=0.9,en;q=0.5"},
    "RU": {"locale": "ru-RU", "timezone": "Europe/Moscow", "accept_language": "ru-RU,ru;q=0.9,en;q=0.5"},
    "CN": {"locale": "zh-CN", "timezone": "Asia/Shanghai", "accept_language": "zh-CN,zh;q=0.9,en;q=0.5"},
    "KR": {"locale": "ko-KR", "timezone": "Asia/Seoul", "accept_language": "ko-KR,ko;q=0.9,en;q=0.5"},
    "MX": {"locale": "es-MX", "timezone": "America/Mexico_City", "accept_language": "es-MX,es;q=0.9,en;q=0.5"},
    "AR": {"locale": "es-AR", "timezone": "America/Argentina/Buenos_Aires", "accept_language": "es-AR,es;q=0.9,en;q=0.5"},
}

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
BLOCKED_STATUS_CODES = frozenset({403, 429})
XML_TYPES = ("application/xml", "text/xml", "+xml")
SAFE_RESPONSE_HEADERS = frozenset(
    {"cache-control", "content-language", "content-location", "date", "etag", "last-modified"}
)
