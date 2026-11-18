"""Canonical registry for anti-bot, WAF, CAPTCHA, and PoW detection rules."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

# Multilingual interstitial phrases indicating an active challenge gate.
STRONG_CHALLENGE_PHRASES: tuple[str, ...] = (
    "checking your browser",
    "verify you are human",
    "attention required",
    "just a moment",
    "enter the characters you see below",
    "type the characters you see in this image",
    "cookies must be enabled",
    "enable cookies for",
    # Turkish
    "bir dakika lütfen",
    "robot olmadığınızı doğrulayın",
    "robot olmadığınızı doğruluyoruz",
    "erişim engellendi",
    "güvenlik kontrolü",
    # Russian
    "проверка вашего браузера",
    "подтвердите, что вы не робот",
    "доступ запрещён",
    "доступ ограничен",
    # German
    "wir überprüfen ihren browser",
    "bitte bestätigen sie, dass sie kein bot sind",
    "zugriff verweigert",
    # French
    "nous vérifions votre navigateur",
    "vérifiez que vous êtes humain",
    "accès refusé",
    # Spanish / Portuguese / Italian
    "verificando tu navegador",
    "verifica que no eres un robot",
    "acceso denegado",
    # Chinese (Simplified)
    "正在验证您的浏览器",
    "请验证您是真人",
    "访问被拒绝",
    # Japanese
    "ブラウザを確認しています",
    "あなたが人間であることを確認",
    "アクセス拒否",
)

# Context-dependent phrases requiring structural/length gating in HTML analysis.
WEAK_CHALLENGE_PHRASES: tuple[str, ...] = (
    "access denied",
    "unusual traffic",
    "captcha",
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

# Extra tokens inspected by the fast in-page JS readiness probe alongside STRONG_CHALLENGE_PHRASES.
_EXTRA_JS_METRIC_PHRASES: tuple[str, ...] = (
    "security check",
    "robot olmadığınızı",
    "cf-chl-",
    "cf-turnstile",
    "challenge-form",
    "validatecaptcha",
    "recaptchachallengepageui",
)

IN_PAGE_METRIC_PHRASES: tuple[str, ...] = tuple(
    dict.fromkeys((*STRONG_CHALLENGE_PHRASES, *_EXTRA_JS_METRIC_PHRASES))
)

# Active interstitial/PoW DOM selectors monitored by browser readiness waits.
ACTIVE_CHALLENGE_SELECTORS: tuple[str, ...] = (
    "#challenge-running",
    "#challenge-stage",
    "#challenge-form",
    "#challenge-error-text",
    ".cf-turnstile",
    '[id^="cf-chl-widget"]',
    'iframe[src*="challenges.cloudflare.com"]',
    'form[action*="validateCaptcha" i]',
    'input[name="cf-turnstile-response"]',
    "#cookie-required",
    "#__identity_pow_cookie_check",
    '[id*="pow_cookie"]',
)

# Embedded CAPTCHA widgets that can appear on benign login forms (excluded from
# readiness stalls, guarded during overlay dismissal and gated in detector.py).
PASSIVE_CAPTCHA_WIDGET_SELECTORS: tuple[str, ...] = (
    ".g-recaptcha",
    ".h-captcha",
)

EXPANDER_CHALLENGE_GUARD_SELECTOR: str = ", ".join(
    dict.fromkeys((*ACTIVE_CHALLENGE_SELECTORS, *PASSIVE_CAPTCHA_WIDGET_SELECTORS))
)

# Tokens searched directly in raw HTML to detect challenge containers/scripts.
CHALLENGE_DOM_TOKENS: tuple[str, ...] = (
    "cf-chl-",
    "g-recaptcha",
    "h-captcha",
    "cf-turnstile",
    "challenge-form",
    "challenge-error-text",
    "challenge-stage",
    "challenge-running",
    "_cf_chl_opt",
    "validatecaptcha",
    "/_pow/solve",
    "__identity_pow_cookie_check",
    "challengeid",
    "challengedomain",
    "cookie-required",
    "recaptchachallengepageui",
    "grecaptcha.enterprise.execute",
)

# Script substrings that signal inline PoW / challenge bootstraps on low-text pages.
POW_SCRIPT_TOKENS: tuple[str, ...] = (
    "/_pow/solve",
    "__identity_pow_cookie_check",
    "RecaptchaChallengePageUi",
    "grecaptcha.enterprise.execute",
)

# Byte markers checked in the first 8 KiB of HTTP responses for fast WAF detection.
_WAF_BYTE_STRINGS: tuple[str, ...] = (
    "cf-chl-bypass",
    "cf_chl_opt",
    "__cf_chl_jschl_tk__",
    "cf-turnstile",
    "cf-error-details",
    "challenge-running",
    "checking your browser",
    "verify you are human",
    "just a moment",
    "attention required",
    "sorry, you have been blocked",
    "error code: 1020",
    "akamai bot manager",
    "perimeterx",
    "datadome",
    "kasada",
    "px-captcha",
    "/_pow/solve",
    "__identity_pow_cookie_check",
    "recaptchachallengepageui",
    "grecaptcha.enterprise.execute",
)
WAF_CHALLENGE_MARKERS: tuple[bytes, ...] = tuple(s.encode("ascii") for s in _WAF_BYTE_STRINGS)

# Response headers that unambiguously indicate an active WAF/anti-bot challenge or mitigation.
WAF_CHALLENGE_HEADERS: frozenset[str] = frozenset(
    {
        "cf-mitigated",
        "x-datadome",
        "x-dd-b",
        "x-dd-type",
        "x-amzn-waf-action",
        "x-kpsdk-ct",
        "x-kpsdk-cd",
        "x-vercel-mitigated",
    }
)

_NON_VISIBLE_HTML_RE = re.compile(
    r"<!--.*?-->|<(?:script|style|title|noscript)\b[^>]*>.*?</(?:script|style|title|noscript)>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_STRIP_RE = re.compile(r"<[^>]+>")

# Third-party challenge domains allowed through browser resource/request filters.
CHALLENGE_DOMAINS: frozenset[str] = frozenset(
    {
        "cloudflare.com",
        "challenges.cloudflare.com",
        "hcaptcha.com",
        "recaptcha.net",
        "gstatic.com",
        "recaptcha.google.com",
        "arkoselabs.com",
        "funcaptcha.com",
        "datadome.co",
        "perimeterx.net",
        "kasada.io",
        "geetest.com",
        "imperva.com",
        "incapsula.com",
        "botguard.net",
        "awswaf.com",
    }
)

CHALLENGE_PATH_PREFIXES: tuple[str, ...] = (
    "/cdn-cgi/",
    "/__cf_chl",
    "/_bm/",
    "/akam/",
    "/_sec/",
    "/_pow/",
)


def _is_empty_or_bare_shell(sample: str) -> bool:
    """Return True when *sample* is empty or an HTML shell with no visible text."""
    stripped = sample.strip()
    if not stripped:
        return True
    if not stripped.startswith("<"):
        return False
    without_invisible = _NON_VISIBLE_HTML_RE.sub("", stripped)
    return not _HTML_TAG_STRIP_RE.sub("", without_invisible).strip()


def has_waf_challenge_header(
    headers: Mapping[str, str] | None,
    *,
    status_code: int | None = None,
) -> bool:
    """Return True when *headers* or *status_code* explicitly signal an anti-bot challenge."""
    if status_code == 203:
        return True
    if not headers:
        return False
    lowered = {str(k).lower(): str(v).lower() for k, v in headers.items()}
    if any(h in lowered for h in WAF_CHALLENGE_HEADERS):
        return True
    if "caf" in lowered or "x-ncbi-captcha" in lowered.get("vary", ""):
        return True
    return False


def looks_like_waf_challenge(
    content: bytes,
    headers: Mapping[str, str] | None = None,
) -> bool:
    """Return True when *content* or *headers* carry WAF challenge markers."""
    if headers:
        lowered_keys = {str(k).lower() for k in headers}
        if any(h in lowered_keys for h in WAF_CHALLENGE_HEADERS):
            return True
    if not content:
        return False
    head = content[:8192].lower()
    return any(marker in head for marker in WAF_CHALLENGE_MARKERS)


def detect_challenge_provider(
    html: str,
    headers: Mapping[str, str] | None = None,
    *,
    status_code: int | None = None,
) -> str | None:
    """Identify the specific challenge or WAF provider from response headers, status, and HTML."""
    hdrs = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}
    sample = (html or "")[:65536].lower()
    server = hdrs.get("server", "")
    is_block_status = status_code in {403, 429}
    bare_body = _is_empty_or_bare_shell(sample)

    if (
        "x-datadome" in hdrs
        or "x-dd-b" in hdrs
        or "x-dd-type" in hdrs
        or "datadome.co" in sample
        or "dd_cid" in sample
        or "datadome=" in hdrs.get("set-cookie", "")
        or "datadome" in server
    ):
        return "datadome"
    if "x-amzn-waf-action" in hdrs or "awswaf.com" in sample or "aws-waf-token" in sample:
        return "aws_waf"
    if any(k in hdrs for k in ("x-kpsdk-ct", "x-kpsdk-cd", "x-kpsdk-r", "x-kpsdk-v")):
        return "kasada"
    if "x-vercel-mitigated" in hdrs:
        return "vercel"
    if (
        "perimeterx.net" in sample
        or "px-captcha" in sample
        or "_pxcaptcha" in sample
        or any(k.startswith("x-px-") for k in hdrs)
    ):
        return "perimeterx"
    if "arkoselabs.com" in sample or "funcaptcha.com" in sample:
        return "arkose"
    if "geetest.com" in sample or "gt_captcha" in sample:
        return "geetest"
    if "h-captcha" in sample or "hcaptcha.com" in sample:
        return "hcaptcha"
    if (
        "g-recaptcha" in sample
        or "recaptcha.net" in sample
        or "recaptcha.google.com" in sample
        or "recaptchachallengepageui" in sample
        or "grecaptcha.enterprise.execute" in sample
    ):
        return "recaptcha"
    if (
        "cf-mitigated" in hdrs
        or "cf-turnstile" in sample
        or "cf-chl-" in sample
        or "_cf_chl_opt" in sample
        or "challenges.cloudflare.com" in sample
        or (
            ("cf-ray" in hdrs or "cloudflare" in server)
            and (
                "just a moment" in sample
                or "attention required" in sample
                or "sorry, you have been blocked" in sample
                or "cf-error-details" in sample
                or "error code: 1020" in sample
                or (is_block_status and bare_body)
            )
        )
    ):
        return "cloudflare"
    if (
        "incapsula" in sample
        or "_incap_" in sample
        or "visid_incap" in sample
        or "x-iinfo" in hdrs
        or "imperva" in hdrs.get("x-cdn", "")
        or "incapsula" in hdrs.get("x-cdn", "")
    ):
        return "incapsula"
    if (
        "/_bm/" in sample
        or "ak_bmsc" in sample
        or "bm_sz" in sample
        or (
            "akamaighost" in server
            and (
                "access denied" in sample
                or "reference #" in sample
                or (is_block_status and bare_body)
            )
        )
    ):
        return "akamai"
    if (
        "x-sucuri-id" in hdrs
        or "sucuri/cloudproxy" in server
        or "sucuri website firewall" in sample
    ):
        return "sucuri"
    if ("cloudfront" in server or "x-amz-cf-id" in hdrs) and (
        "generated by cloudfront" in sample
        or "the request could not be satisfied" in sample
        or (is_block_status and bare_body)
    ):
        return "cloudfront"
    return None


def _build_js_in_page_metrics_probe() -> str:
    selector_literal = json.dumps(", ".join(ACTIVE_CHALLENGE_SELECTORS))
    pow_tokens_literal = json.dumps(POW_SCRIPT_TOKENS)
    phrases_literal = json.dumps(IN_PAGE_METRIC_PHRASES, ensure_ascii=False)
    return f"""() => {{
    const body = document.body;
    const bodyText = (body?.innerText || '').length;
    const mains = document.querySelectorAll('main, article, [role="main"]');
    let mainText = 0;
    for (const el of mains) {{
        mainText += (el.innerText?.length || 0);
    }}
    const lowered = (body?.innerText || '').toLowerCase();
    const title = (document.title || '').toLowerCase();
    const bodyStyle = body ? window.getComputedStyle(body) : null;
    const bodyHidden = !!(
        bodyStyle && (bodyStyle.visibility === 'hidden' || bodyStyle.display === 'none')
    );
    const hasChallengeDom = !!(document.querySelector({selector_literal}));
    const powTokens = {pow_tokens_literal};
    let hasChallengeScript = false;
    if (bodyText < 600) {{
        const scripts = document.querySelectorAll('script');
        for (const s of scripts) {{
            const sc = (s.textContent || '') + ' ' + (s.src || '');
            if (powTokens.some(tok => sc.includes(tok)) || (sc.includes('challengeId') && sc.includes('location.reload'))) {{
                hasChallengeScript = true;
                break;
            }}
        }}
    }}
    const host = (window.location.hostname || '').toLowerCase().replace(/^www\\./, '');
    const cleanTitle = title.trim().replace(/^www\\./, '');
    const isHostnameTitle = !!(host && cleanTitle === host && bodyText < 200);
    const patterns = {phrases_literal};
    const hasChallengeText = patterns.some(p => lowered.includes(p) || title.includes(p));
    const effectiveText = bodyHidden ? 0 : bodyText;
    const challenge = (
        hasChallengeDom ||
        hasChallengeScript ||
        hasChallengeText ||
        isHostnameTitle ||
        title.includes('challenge')
    ) && effectiveText < 600;
    return {{text: effectiveText, mainText: bodyHidden ? 0 : mainText, challenge}};
}}"""


JS_IN_PAGE_METRICS_PROBE: str = _build_js_in_page_metrics_probe()
