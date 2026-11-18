"""Package constants."""

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
BLOCKED_STATUS_CODES = frozenset({403, 429})

SAFE_RESPONSE_HEADERS = frozenset(
    {"cache-control", "content-language", "content-location", "date", "etag", "last-modified"}
)

from .processing.challenges import (  # noqa: E402
    WAF_CHALLENGE_MARKERS as WAF_CHALLENGE_MARKERS,
    looks_like_waf_challenge as looks_like_waf_challenge,
)
