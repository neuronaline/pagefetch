from __future__ import annotations

from pagefetch.processing.detector import analyze_html
from pagefetch.processing.html import process_html

RICH_HTML = """
<!doctype html>
<html lang="en"><head>
<title>Example Article</title>
<meta name="description" content="A useful article">
<meta property="og:title" content="Example Article">
<link rel="canonical" href="https://example.com/article">
<script type="application/ld+json">{"@type":"Article","headline":"Example"}</script>
</head><body>
<nav><a href="/">Home</a></nav>
<main><article><h1>Example Article</h1>
<p>This is a substantial paragraph with enough useful text to describe the subject in detail.</p>
<p>Another paragraph preserves context, comments, links, and all meaningful page information.</p>
<blockquote>A quotation with <strong>emphasis</strong>.</blockquote>
<ul><li>First item<ul><li>Nested item</li></ul></li><li>Second item</li></ul>
<pre><code class="language-python">print("hello")</code></pre>
<table><tr><th>Name</th><th>Value</th></tr><tr><td>Alpha</td><td>42</td></tr></table>
<p><a href="/details" rel="next" target="_blank">Details</a></p>
<img data-src="/hero.jpg" alt="Hero" title="Image">
</article></main>
<section class="comments"><p>Repeated user comment</p><p>Repeated user comment</p></section>
<div class="cookie-banner">Accept cookies</div>
</body></html>
"""


def test_processing_preserves_content_and_converts_markdown():
    result = process_html(RICH_HTML, "https://example.com/article")
    assert result.title == "Example Article"
    assert "# Example Article" in result.markdown
    assert "[Details](https://example.com/details)" in result.markdown
    assert "![Hero](https://example.com/hero.jpg \"Image\")" in result.markdown
    assert "```python" in result.markdown
    assert "| Name | Value |" in result.markdown
    assert "  - Nested item" in result.markdown
    assert result.text.count("Repeated user comment") == 2
    assert "Accept cookies" not in result.text
    assert result.metadata["description"] == "A useful article"
    assert result.metadata["json_ld"][0]["@type"] == "Article"
    assert result.links[1].url == "https://example.com/details"
    assert result.links[1].internal is True
    assert result.images[0].url == "https://example.com/hero.jpg"


def test_detector_distinguishes_rich_content_spa_and_challenge():
    rich = analyze_html(RICH_HTML)
    spa = analyze_html("<html><body><div id='root'></div><script>" + "x" * 12000 + "</script></body></html>")
    challenge = analyze_html("<html><title>Attention Required</title><body>Verify you are human CAPTCHA</body></html>")
    assert rich.score >= 0.80
    assert spa.score < 0.50 and spa.javascript_shell
    assert challenge.score <= 0.08 and challenge.challenge


def test_external_link_is_identified():
    result = process_html('<html><body><a href="https://other.test/x">Other</a></body></html>', "https://example.com/")
    assert result.links[0].internal is False


def test_metadata_is_normalized_and_response_headers_are_filtered():
    html = """
    <html lang="tr"><head>
      <meta name="description" content="Açıklama">
      <meta name="author" content="Ada">
      <meta property="article:published_time" content="2026-01-02">
      <meta property="article:modified_time" content="2026-01-03">
      <meta property="og:title" content="Başlık">
      <meta name="twitter:card" content="summary">
      <link rel="canonical" href="/canonical">
    </head><body><p>İçerik</p></body></html>
    """
    result = process_html(
        html,
        "https://example.com/article",
        {"ETag": '"abc"', "Set-Cookie": "secret=1"},
    )
    assert result.metadata == {
        "description": "Açıklama",
        "canonical_url": "https://example.com/canonical",
        "author": "Ada",
        "published_at": "2026-01-02",
        "modified_at": "2026-01-03",
        "language": "tr",
        "open_graph": {"title": "Başlık"},
        "twitter_card": {"card": "summary"},
        "json_ld": [],
        "headers": {"etag": '"abc"'},
    }


def test_markdown_handles_spans_fences_and_image_title_fallback():
    html = """
    <html><body>
      <table>
        <tr><th rowspan="2">Name</th><th colspan="2">Values</th></tr>
        <tr><th>A</th><th>B</th></tr>
        <tr><td>Item</td><td>x|y</td><td>z</td></tr>
      </table>
      <pre data-language="python"><code>print(```)</code></pre>
      <img src="/photo.png" title="Photo title">
      <iframe src="https://frames.test/info" title="Frame info"></iframe>
    </body></html>
    """
    result = process_html(html, "https://example.com/")
    assert "| Name | Values |  |" in result.markdown
    assert "| Name | A | B |" in result.markdown
    assert "x\\|y" in result.markdown
    assert "````python" in result.markdown
    assert "![Photo title](https://example.com/photo.png \"Photo title\")" in result.markdown
    assert "[Frame info](https://frames.test/info)" in result.markdown
