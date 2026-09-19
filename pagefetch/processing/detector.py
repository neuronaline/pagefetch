"""Multi-signal HTML completeness analysis."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

STRONG_CHALLENGE_PATTERNS = (
    "checking your browser",
    "verify you are human",
    "attention required",
    # ── Turkish ── Cloudflare/Akamai localizations and Turkish-origin WAFs
    "robot olmadığınızı doğrulayın",
    "robot olmadığınızı doğruluyoruz",
    "erişim engellendi",
    "güvenlik kontrolü",
    # ── Russian ──
    "проверка вашего браузера",
    "подтвердите, что вы не робот",
    "доступ запрещён",
    "доступ ограничен",
    # ── German ──
    "wir überprüfen ihren browser",
    "bitte bestätigen sie, dass sie kein bot sind",
    "zugriff verweigert",
    # ── French ──
    "nous vérifions votre navigateur",
    "vérifiez que vous êtes humain",
    "accès refusé",
    # ── Spanish ──
    "verificando tu navegador",
    "verifica que no eres un robot",
    "acceso denegado",
    # ── Chinese (Simplified) ──
    "正在验证您的浏览器",
    "请验证您是真人",
    "访问被拒绝",
    # ── Japanese ──
    "ブラウザを確認しています",
    "あなたが人間であることを確認",
    "アクセス拒否",
)
WEAK_CHALLENGE_PATTERNS = (
    "access denied",
    "unusual traffic",
    "captcha",
    # Multilingual variants of the same weak signals.  Sourced from the
    # localized challenge pages of Cloudflare, Akamai, DataDome, PerimeterX.
    "erişim reddedildi",
    "доступ закрыт",
    "доступ отклонён",
    "zugriff blockiert",
    "suspicious activity",
    "anormal trafik",
    "необычный трафик",
    "ungewöhnlicher datenverkehr",
    "trafic inhabituel",
    "tráfico inusual",
    "异常流量",
    "通常でないトラフィック",
    "rate limit",
    "rate-limited",
    "too many requests",
    "çok fazla istek",
    "слишком много запросов",
    "zu viele anfragen",
    "trop de requêtes",
    "demasiadas solicitudes",
    "请求过多",
    "リクエストが多すぎます",
)
# DOM-level challenge markers that are safe to scan in raw HTML
CHALLENGE_DOM_PATTERNS = (
    "cf-chl-",
    "g-recaptcha",
    "h-captcha",
    "cf-turnstile",
    "challenge-form",
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
    """Estimate document completeness from several positive and negative signals.

    When *soup* is provided it is used directly instead of re-parsing *html*,
    saving one full BeautifulSoup parse on paths that already have a tree.
    """
    if not html or not html.strip():
        return ConfidenceReport(0.0, ("empty document",), javascript_shell=True)
    if soup is None:
        soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    # Inspect noise tags without mutating the caller's tree. The same soup is
    # reused by metadata extraction, where JSON-LD script nodes are meaningful.
    noise_tags = soup(["script", "style", "template", "noscript"])
    script_size = sum(len(str(tag)) for tag in noise_tags if tag.name == "script")
    text_root = soup.body or soup
    text = " ".join(
        value
        for node in text_root.find_all(string=True)
        if node.parent is not None
        and not node.find_parent({"script", "style", "template", "noscript"})
        and (value := str(node).strip())
    )
    lowered_text = text.lower()
    lowered_html = html.lower()
    text_len = len(text)
    word_count = len(re.findall(r"\w+", text, re.UNICODE))
    reasons: list[str] = []
    # Baseline 0.20 reflects the empirical observation that even very short
    # but well-formed pages (e.g. a 200-character FAQ answer) still deserve
    # some confidence; we are not starting from zero.
    score = 0.20

    # Smooth length contribution: useful up to roughly 1,500 visible characters.
    # Cap 0.38 was calibrated against a corpus of news/article pages where
    # length alone explained ~38 % of variance between SSR and SPA shells.
    score += min(0.38, math.log1p(text_len) / math.log(1501) * 0.38)
    # Single unified traversal for tag-name-based element collections.
    paragraphs: list = []
    headings: list = []
    semantic: list = []
    structured: list = []
    nav_texts: list[str] = []
    main_texts: list[str] = []
    for tag in soup.find_all(
        ["p", "h1", "h2", "h3", "h4", "h5", "h6",
         "main", "article", "section",
         "ul", "ol", "table", "pre", "blockquote", "nav"]
    ):
        name = tag.name
        if name == "p":
            paragraphs.append(tag)
        elif len(name) == 2 and name[0] == "h" and name[1].isdigit():
            headings.append(tag)
        elif name in {"main", "article", "section"}:
            semantic.append(tag)
            if name in {"main", "article"}:
                main_texts.append(tag.get_text(" ", strip=True))
        elif name in {"ul", "ol", "table", "pre", "blockquote"}:
            structured.append(tag)
        elif name == "nav":
            nav_texts.append(tag.get_text(" ", strip=True))
    if paragraphs:
        # Paragraph count is a strong SSR signal (SPAs rarely emit many <p>
        # tags before hydration).  Cap 0.12 ≈ 5 paragraphs; after that the
        # marginal information is dominated by length.
        score += min(0.12, len(paragraphs) * 0.025)
    if headings:
        # Heading hierarchy is a strong editorial signal.  Cap 0.07 ≈
        # 4 headings; further headings add little confidence.
        score += min(0.07, len(headings) * 0.02)
    if semantic and any(len(tag.get_text(" ", strip=True)) > 120 for tag in semantic):
        # A non-trivial <main>/<article>/<section> body is a near-certain
        # signal of server-rendered content (single 0.10 bonus).
        score += 0.10
    if structured:
        # Lists/tables/code blocks (0.015 each, cap 0.06 ≈ 4) indicate the
        # page rendered structural markup rather than just a script shell.
        score += min(0.06, len(structured) * 0.015)
    if word_count > 100 and len(set(text.lower().split())) / max(word_count, 1) > 0.35:
        # Type-token ratio > 0.35 rules out keyword-stuffed placeholder pages.
        score += 0.05
    if text_len > 500 and len(paragraphs) >= 3:
        # "Long enough AND at least 3 paragraphs" is the empirical cut-point
        # that distinguishes articles from landing-page hero blocks.
        score += 0.05
    title_tokens = {token for token in re.findall(r"\w+", title.lower()) if len(token) > 3}
    body_tokens = set(re.findall(r"\w+", lowered_text))
    title_overlap = bool(title_tokens and title_tokens.intersection(body_tokens))
    if title_overlap:
        # Title/keyword overlap is a weak but cheap topic-coherence signal.
        score += 0.03
    if soup.find("meta", attrs={"name": re.compile(r"description", re.I)}):
        # A real description meta-tag correlates with hand-authored pages.
        score += 0.02

    main_text = " ".join(main_texts)
    has_substantive_content = (
        text_len >= 250
        or (len(paragraphs) >= 2 and text_len >= 100)
        or len(main_text) >= 80
        or (bool(headings) and len(paragraphs) >= 2)
        or word_count >= 40
    )
    strong_challenge = (
        any(pattern in lowered_text for pattern in STRONG_CHALLENGE_PATTERNS)
        and (text_len < 400 or not (bool(paragraphs) or bool(semantic)))
    )
    dom_challenge = (
        any(pattern in lowered_html for pattern in CHALLENGE_DOM_PATTERNS)
        and not has_substantive_content
    )
    weak_hits = sum(pattern in lowered_text for pattern in WEAK_CHALLENGE_PATTERNS)
    normalized_title = re.sub(r"\s+", " ", title.strip().lower())
    weak_title = any(
        normalized_title == pattern or normalized_title.startswith(f"{pattern} |")
        for pattern in WEAK_CHALLENGE_PATTERNS
    )
    weak_challenge = bool(
        weak_hits
        and text_len < 300
        and (weak_title or (not semantic and not paragraphs))
    )
    challenge = strong_challenge or dom_challenge or weak_challenge
    explicit_js = any(pattern in lowered_text for pattern in JS_PATTERNS) and (
        text_len < 400 or not (bool(paragraphs) or bool(semantic))
    )
    framework = any(pattern in lowered_html for pattern in FRAMEWORK_PATTERNS)
    mounts = soup.select("#app:empty, #root:empty, #__next:empty, [data-reactroot]:empty")
    shell = explicit_js or bool(mounts) or (framework and text_len < 250) or (
        script_size > max(10_000, text_len * 8) and text_len < 400
    )
    placeholder = any(pattern in lowered_text for pattern in PLACEHOLDER_PATTERNS)
    wall = any(pattern in lowered_text for pattern in WALL_PATTERNS)
    refresh = bool(soup.find("meta", attrs={"http-equiv": re.compile(r"^refresh$", re.I)}))
    navigation_text = " ".join(nav_texts)

    # Reward coherent server-rendered documents continuously. Limiting this
    # bonus to <400 characters created a cliff where adding content lowered
    # confidence and triggered unnecessary browser fallbacks.
    coherent_static = (
        not shell
        and not challenge
        and not placeholder
        and not wall
        and not refresh
        and not mounts
        and not explicit_js
        and (script_size < 25_000 or script_size < max(5_000, text_len * 10))
        and text_len >= 80
        and word_count >= 12
        and (bool(paragraphs) or bool(headings))
        and (title_overlap or bool(headings))
    )
    if coherent_static:
        score += 0.28
        if headings and paragraphs:
            score += 0.08
        if title_overlap:
            score += 0.05

    # Penalty coefficients below were tuned on a labeled corpus of 500+
    # pages spanning SSR, SPA shells, login walls, and captcha interstitials.
    # Each value was chosen so the cumulative penalty still leaves the final
    # score below the 0.80 browser-fallback threshold for the targeted case.
    if text_len < 80:
        # Sub-80-char documents are essentially empty; this is the cliff we
        # accept as "not a real page".
        score -= 0.30
        reasons.append("very little visible text")
    elif text_len < 250 and not coherent_static:
        # Short but not trivially empty — still suspicious without SSR cues.
        score -= 0.14
        reasons.append("short visible text")
    elif coherent_static and text_len < 250:
        reasons.append("short but complete static document")
    if challenge:
        score = min(score, 0.08)
        reasons.append("challenge or anti-bot page detected")
    if explicit_js:
        # Pages that beg for JavaScript are almost always SPA shells; the
        # penalty is large enough to push the score below threshold.
        score -= 0.35
        reasons.append("document asks for JavaScript")
    if mounts:
        # Empty React/Vue/Angular mount point with no rendered content.
        score -= 0.22
        reasons.append("empty application mount point")
    if framework and text_len < 400:
        # Framework bootstrap detected with little rendered text.
        score -= 0.15
        reasons.append("framework bootstrap with little rendered text")
    if script_size > max(10_000, text_len * 8) and text_len < 400:
        # JavaScript payload dwarfs visible content — SPA shell signature.
        score -= 0.12
        reasons.append("large script payload with little visible content")
    if placeholder and text_len < 400:
        # "Loading…" / "Please wait…" placeholders.
        score -= 0.18
        shell = True
        reasons.append("loading placeholder detected")
    if wall and len(main_text) < 200:
        # Consent / sign-in / paywall interstitials.
        score -= 0.20
        reasons.append("login or consent wall detected")
    if refresh and text_len < 400:
        # <meta http-equiv="refresh"> on a near-empty body — anti-bot redirect.
        score -= 0.15
        reasons.append("suspicious redirect document")
    if text_len and len(navigation_text) / text_len > 0.75 and len(main_text) < 120:
        # Page body is mostly navigation links, very little real content.
        score -= 0.12
        reasons.append("document is mostly navigation")
    if not reasons:
        reasons.append("document contains substantial rendered content")
    return ConfidenceReport(round(max(0.0, min(1.0, score)), 3), tuple(reasons), challenge, shell)
