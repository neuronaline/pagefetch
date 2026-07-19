"""Multi-signal HTML completeness analysis."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

CHALLENGE_PATTERNS = (
    "checking your browser",
    "verify you are human",
    "attention required",
    "access denied",
    "unusual traffic",
    "cf-chl-",
    "captcha",
)
JS_PATTERNS = (
    "enable javascript",
    "javascript is required",
    "you need to enable javascript",
    "please turn on javascript",
)
FRAMEWORK_PATTERNS = (
    "__next_data__",
    "__nuxt__",
    "data-reactroot",
    "data-v-app",
    "ng-version",
    "__svelte",
    "astro-island",
    "__remixcontext",
)
PLACEHOLDER_PATTERNS = ("loading...", "loading…", "skeleton", "spinner", "please wait")
WALL_PATTERNS = ("sign in to continue", "log in to continue", "consent required", "accept cookies to continue")


@dataclass(slots=True, frozen=True)
class ConfidenceReport:
    score: float
    reasons: tuple[str, ...]
    challenge: bool = False
    javascript_shell: bool = False


def analyze_html(html: str, *, soup: BeautifulSoup | None = None) -> ConfidenceReport:
    """Estimate document completeness from several positive and negative signals."""
    if not html or not html.strip():
        return ConfidenceReport(0.0, ("empty document",), javascript_shell=True)
    if soup is None:
        soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    # Capture script payload size before decomposing tags.
    script_size = sum(len(str(script)) for script in soup.find_all("script"))

    for tag in soup(["script", "style", "template", "noscript"]):
        tag.decompose()
    text = " ".join((soup.body or soup).stripped_strings)
    lowered_text = text.lower()
    lowered_html = html.lower()
    text_len = len(text)
    word_count = len(re.findall(r"\w+", text, re.UNICODE))
    reasons: list[str] = []
    score = 0.20

    # Smooth length contribution: useful up to roughly 1,500 visible characters.
    score += min(0.38, math.log1p(text_len) / math.log(1501) * 0.38)
    paragraphs = soup.find_all("p")
    headings = soup.find_all(re.compile(r"^h[1-6]$"))
    semantic = soup.find_all(["main", "article", "section"])
    structured = soup.find_all(["ul", "ol", "table", "pre", "blockquote"])
    if paragraphs:
        score += min(0.12, len(paragraphs) * 0.025)
    if headings:
        score += min(0.07, len(headings) * 0.02)
    if semantic and any(len(tag.get_text(" ", strip=True)) > 120 for tag in semantic):
        score += 0.10
    if structured:
        score += min(0.06, len(structured) * 0.015)
    if word_count > 100 and len(set(text.lower().split())) / max(word_count, 1) > 0.35:
        score += 0.05
    if text_len > 500 and len(paragraphs) >= 3:
        score += 0.05
    title_tokens = {token for token in re.findall(r"\w+", title.lower()) if len(token) > 3}
    if title_tokens and title_tokens.intersection(set(re.findall(r"\w+", lowered_text))):
        score += 0.03
    if soup.find("meta", attrs={"name": re.compile(r"description", re.I)}):
        score += 0.02

    challenge = any(pattern in lowered_html for pattern in CHALLENGE_PATTERNS)
    explicit_js = any(pattern in lowered_text for pattern in JS_PATTERNS)
    framework = any(pattern in lowered_html for pattern in FRAMEWORK_PATTERNS)
    mounts = soup.select("#app:empty, #root:empty, #__next:empty, [data-reactroot]:empty")
    shell = explicit_js or bool(mounts) or (framework and text_len < 250) or (
        script_size > max(10_000, text_len * 8) and text_len < 400
    )
    placeholder = any(pattern in lowered_text for pattern in PLACEHOLDER_PATTERNS)
    wall = any(pattern in lowered_text for pattern in WALL_PATTERNS)
    refresh = bool(soup.find("meta", attrs={"http-equiv": re.compile(r"^refresh$", re.I)}))
    navigation_text = " ".join(tag.get_text(" ", strip=True) for tag in soup.find_all("nav"))
    main_text = " ".join(tag.get_text(" ", strip=True) for tag in soup.find_all(["main", "article"]))

    if text_len < 80:
        score -= 0.30
        reasons.append("very little visible text")
    elif text_len < 250:
        score -= 0.14
        reasons.append("short visible text")
    if challenge:
        score = min(score, 0.08)
        reasons.append("challenge or anti-bot page detected")
    if explicit_js:
        score -= 0.35
        reasons.append("document asks for JavaScript")
    if mounts:
        score -= 0.22
        reasons.append("empty application mount point")
    if framework and text_len < 400:
        score -= 0.15
        reasons.append("framework bootstrap with little rendered text")
    if script_size > max(10_000, text_len * 8) and text_len < 400:
        score -= 0.12
        reasons.append("large script payload with little visible content")
    if placeholder and text_len < 400:
        score -= 0.18
        shell = True
        reasons.append("loading placeholder detected")
    if wall and len(main_text) < 200:
        score -= 0.20
        reasons.append("login or consent wall detected")
    if refresh and text_len < 400:
        score -= 0.15
        reasons.append("suspicious redirect document")
    if text_len and len(navigation_text) / text_len > 0.75 and len(main_text) < 120:
        score -= 0.12
        reasons.append("document is mostly navigation")
    if not reasons:
        reasons.append("document contains substantial rendered content")
    return ConfidenceReport(round(max(0.0, min(1.0, score)), 3), tuple(reasons), challenge, shell)
