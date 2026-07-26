from __future__ import annotations

import json

import pytest

from pagefetch import PageFetch
from pagefetch.cli import _build_config, _inputs, _render, build_parser
from pagefetch.interactive import _init_client
from pagefetch.models import FetchResult


@pytest.mark.parametrize("kwargs", [{"mode": "magic"}, {"proxy": "auto"}, {"http_concurrency": 0}, {"confidence_threshold": 2}])
def test_constructor_validates_immediately(kwargs):
    with pytest.raises(ValueError):
        PageFetch(**kwargs)


def test_cli_reads_url_files_and_renders_json(tmp_path):
    source = tmp_path / "urls.txt"
    source.write_text("https://a.test\n\nhttps://b.test\n", encoding="utf-8")
    assert _inputs(str(source)) == ["https://a.test", "https://b.test"]
    rendered = _render([FetchResult(url="x", success=True, html="secret")], "json", False)
    assert "html" not in json.loads(rendered)
    assert build_parser().parse_args(["https://example.com"]).format == "markdown"


def test_stealth_preset_applies_to_python_api_and_cli():
    config = PageFetch(stealth_level="max").config
    assert (
        config.stealth_level,
        config.humanize,
        config.block_level,
        config.request_pacing,
        config.session_rotation,
    ) == ("max", True, "minimal", 2.0, "rotate")

    args = build_parser().parse_args(
        ["https://example.com", "--stealth-level", "balanced"]
    )
    cli_config = _build_config(args)
    assert (
        cli_config.humanize,
        cli_config.block_level,
        cli_config.request_pacing,
        cli_config.session_rotation,
    ) == (True, "balanced", 0.5, "rotate")


def test_explicit_values_override_stealth_preset():
    config = PageFetch(
        stealth_level="max",
        humanize=False,
        block_level="aggressive",
        request_pacing=0,
        session_rotation="sticky",
    ).config
    assert not config.humanize
    assert config.block_level == "aggressive"
    assert config.request_pacing == 0
    assert config.session_rotation == "sticky"


def test_interactive_client_preserves_yaml_fetch_settings(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
mode: browser
proxy: none
cache_enabled: false
cache_ttl: 7d
cache_path: custom.sqlite3
http_concurrency: 3
browser_concurrency: 2
http_timeout: 11
browser_timeout: 22
retries_http: 1
retries_browser: 4
max_redirects: 6
max_content_size: 123456
confidence_threshold: 0.67
block_images: false
block_level: minimal
accept_language: tr-TR
humanize: true
session_rotation: rotate
request_pacing: 1.25
stealth_level: off
proxy_geo: TR
raise_on_error: true
""",
        encoding="utf-8",
    )
    client = _init_client(
        {
            "config_file": str(path),
            "mode": None,
            "proxy": None,
            "cache_ttl": None,
        }
    )
    config = client.config
    assert config.cache_enabled is False
    assert config.cache_ttl == 7 * 86400
    assert config.retries_http == 1
    assert config.retries_browser == 4
    assert config.max_redirects == 6
    assert config.max_content_size == 123456
    assert config.confidence_threshold == 0.67
    assert config.block_images is False
    assert config.block_level == "minimal"
    assert config.humanize is True
    assert config.session_rotation == "rotate"
    assert config.proxy_geo == "TR"
