from __future__ import annotations

import json

import pytest

from pagefetch import PageFetch
from pagefetch.cli import _inputs, _render, build_parser
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

