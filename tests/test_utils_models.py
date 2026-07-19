from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pagefetch.models import FetchErrorInfo, FetchResult, ImageInfo, LinkInfo
from pagefetch.utils.durations import parse_duration
from pagefetch.utils.urls import normalize_url, registrable_host, validate_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [(30, 30), ("30s", 30), ("30m", 1800), ("24h", 86400), ("7d", 604800)],
)
def test_parse_duration(value, expected):
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["", "24", "-1h", "potato", -1, True])
def test_invalid_duration(value):
    with pytest.raises((ValueError, TypeError)):
        parse_duration(value)


def test_url_validation_and_normalization():
    assert normalize_url("HTTPS://Example.COM:443?q=1#fragment") == "https://example.com/?q=1"
    assert normalize_url("http://example.com:8080") == "http://example.com:8080/"
    with pytest.raises(ValueError):
        validate_url("file:///tmp/page")


def test_registrable_hosts_use_public_suffix_rules():
    assert registrable_host("https://news.example.co.uk") == "example.co.uk"
    assert registrable_host("https://cdn.example.co.uk") == "example.co.uk"
    assert registrable_host("https://evil.co.uk") == "evil.co.uk"
    assert registrable_host("https://one.blogspot.com") != registrable_host("https://two.blogspot.com")


def test_result_serialization_excludes_html_by_default():
    result = FetchResult(
        url="https://example.com/",
        success=False,
        html="<p>large</p>",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        links=[LinkInfo("x", "https://example.com/x", True, [], None, 0)],
        images=[ImageInfo("https://example.com/x.png", "x", None, 0)],
        error=FetchErrorInfo("blocked", "blocked", False),
    )
    data = result.to_dict()
    assert "html" not in data
    assert result.to_dict(include_html=True)["html"] == "<p>large</p>"
    reconstructed = FetchResult.from_dict(result.to_dict(include_html=True))
    assert reconstructed.error == result.error
    assert reconstructed.links == result.links
    assert "2026-01-01" in result.json()
