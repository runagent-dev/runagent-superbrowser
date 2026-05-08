"""Typed block detection for anti-bot responses.

Pattern catalog and scoring ported from crawl4ai's `antibot_detector.py`
(reference: /root/agentic-browser/crawl4ai/crawl4ai/antibot_detector.py:26-281).
Re-implemented in-repo so we own the detection surface.

Detection philosophy: false positives are cheap (the fallback rescues them),
false negatives are catastrophic (user gets garbage). Err toward detection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

BlockClass = Literal[
    "akamai", "cloudflare", "perimeterx", "datadome",
    "imperva", "sucuri", "kasada", "generic", "empty",
    "rate_limited", "structural", "",
]


@dataclass(frozen=True)
class BlockVerdict:
    blocked: bool
    klass: BlockClass = ""
    reason: str = ""
    signals: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return self.blocked


# --- Tier 1 patterns: high-confidence structural markers. Any page size. -----
_TIER1 = [
    (re.compile(r"Reference\s*#\s*[\d]+\.[0-9a-f]+\.\d+\.[0-9a-f]+", re.I),
     "akamai", "Akamai block (Reference #)"),
    (re.compile(r"Pardon\s+Our\s+Interruption", re.I),
     "akamai", "Akamai (Pardon Our Interruption)"),
    (re.compile(r"challenge-form.*?__cf_chl_f_tk=", re.I | re.S),
     "cloudflare", "Cloudflare challenge form"),
    (re.compile(r'<span\s+class="cf-error-code">\d{4}</span>', re.I),
     "cloudflare", "Cloudflare firewall block"),
    (re.compile(r"/cdn-cgi/challenge-platform/\S+orchestrate", re.I),
     "cloudflare", "Cloudflare JS challenge"),
    (re.compile(r"window\._pxAppId\s*=", re.I),
     "perimeterx", "PerimeterX block"),
    (re.compile(r"captcha\.px-cdn\.net", re.I),
     "perimeterx", "PerimeterX captcha CDN"),
    (re.compile(r"captcha-delivery\.com", re.I),
     "datadome", "DataDome captcha"),
    (re.compile(r"_Incapsula_Resource", re.I),
     "imperva", "Imperva/Incapsula block"),
    (re.compile(r"Incapsula\s+incident\s+ID", re.I),
     "imperva", "Imperva/Incapsula incident"),
    (re.compile(r"Sucuri\s+WebSite\s+Firewall", re.I),
     "sucuri", "Sucuri firewall block"),
    (re.compile(r"KPSDK\.scriptStart\s*=\s*KPSDK\.now\(\)", re.I),
     "kasada", "Kasada challenge"),
    (re.compile(r"blocked\s+by\s+network\s+security", re.I),
     "generic", "Network security block"),
]

# --- Tier 2 patterns: match only on short pages (< 10KB) ---------------------
_TIER2 = [
    (re.compile(r"Access\s+Denied", re.I), "generic", "Access Denied (short page)"),
    (re.compile(r"Checking\s+your\s+browser", re.I), "cloudflare", "Cloudflare browser check"),
    (re.compile(r"<title>\s*Just\s+a\s+moment", re.I), "cloudflare", "Cloudflare interstitial"),
    (re.compile(r'class=["\']g-recaptcha["\']', re.I), "generic", "reCAPTCHA on block page"),
    (re.compile(r'class=["\']h-captcha["\']', re.I), "generic", "hCaptcha on block page"),
    (re.compile(r"Access\s+to\s+This\s+Page\s+Has\s+Been\s+Blocked", re.I),
     "perimeterx", "PerimeterX block page"),
    (re.compile(r"blocked\s+by\s+security", re.I), "generic", "Blocked by security"),
    (re.compile(r"Request\s+unsuccessful", re.I), "imperva", "Imperva request unsuccessful"),
]

_TIER2_MAX = 10_000
_STRUCTURAL_MAX = 50_000
_EMPTY_THRESHOLD = 100

_BODY_RE = re.compile(r"<body\b", re.I)
_SCRIPT_BLOCK = re.compile(r"<script\b[\s\S]*?</script>", re.I)
_STYLE_BLOCK = re.compile(r"<style\b[\s\S]*?</style>", re.I)
_CONTENT_EL = re.compile(r"<(?:p|h[1-6]|article|section|li|td|a|pre)\b", re.I)
_SCRIPT_TAG = re.compile(r"<script\b", re.I)
_TAG = re.compile(r"<[^>]+>")


def _looks_like_data(html: str) -> bool:
    s = html.strip()
    if not s:
        return False
    if s[0] in "{[":
        return True
    if s[:10].lower().startswith(("<html", "<!")):
        if re.search(r"<body[^>]*>\s*<pre[^>]*>\s*[{\[]", s[:500], re.I):
            return True
        return False
    return s[0] == "<"


def _structural(html: str) -> BlockVerdict:
    n = len(html)
    if n > _STRUCTURAL_MAX or _looks_like_data(html):
        return BlockVerdict(False)
    if not _BODY_RE.search(html):
        return BlockVerdict(True, "structural", f"no <body> ({n}b)", ("no_body",))
    body_match = re.search(r"<body\b[^>]*>([\s\S]*)</body>", html, re.I)
    body = body_match.group(1) if body_match else html
    stripped = _STYLE_BLOCK.sub("", _SCRIPT_BLOCK.sub("", body))
    visible = _TAG.sub("", stripped).strip()
    signals = []
    if len(visible) < 50:
        signals.append("minimal_text")
    content_n = len(_CONTENT_EL.findall(html))
    if content_n == 0:
        signals.append("no_content_elements")
    script_n = len(_SCRIPT_TAG.findall(html))
    if script_n > 0 and content_n == 0 and len(visible) < 100:
        signals.append("script_heavy_shell")
    if len(signals) >= 2 or (len(signals) == 1 and n < 5_000):
        return BlockVerdict(
            True, "structural",
            f"structural: {','.join(signals)} ({n}b, {len(visible)}v)",
            tuple(signals),
        )
    return BlockVerdict(False)


def detect(
    html: str,
    status_code: Optional[int] = None,
    headers: Optional[dict] = None,
) -> BlockVerdict:
    """Classify an HTTP response as blocked or not, with a typed verdict.

    `headers` is accepted for symmetry and future use (WAF headers); current
    implementation derives the verdict from status + body only.
    """
    html = html or ""
    n = len(html)

    if status_code == 429:
        return BlockVerdict(True, "rate_limited", "HTTP 429 Too Many Requests")

    snippet = html[:15_000]
    for pat, klass, reason in _TIER1:
        if pat.search(snippet):
            return BlockVerdict(True, klass, reason, (klass,))

    if n > 15_000:
        deep = _STYLE_BLOCK.sub("", _SCRIPT_BLOCK.sub("", html[:500_000]))[:30_000]
        for pat, klass, reason in _TIER1:
            if pat.search(deep):
                return BlockVerdict(True, klass, reason, (klass,))

    if status_code in (403, 503) and not _looks_like_data(html):
        if n < _EMPTY_THRESHOLD:
            return BlockVerdict(
                True, "empty",
                f"HTTP {status_code} near-empty ({n}b)",
                (f"http_{status_code}", "empty"),
            )
        check = snippet
        if n > _TIER2_MAX:
            check = _STYLE_BLOCK.sub("", _SCRIPT_BLOCK.sub("", html[:500_000]))[:30_000]
        for pat, klass, reason in _TIER2:
            if pat.search(check):
                return BlockVerdict(
                    True, klass, f"{reason} (HTTP {status_code}, {n}b)",
                    (f"http_{status_code}", klass),
                )
        return BlockVerdict(
            True, "generic", f"HTTP {status_code} with HTML ({n}b)",
            (f"http_{status_code}",),
        )

    if status_code and status_code >= 400 and n < _TIER2_MAX:
        for pat, klass, reason in _TIER2:
            if pat.search(snippet):
                return BlockVerdict(
                    True, klass, f"{reason} (HTTP {status_code}, {n}b)",
                    (f"http_{status_code}", klass),
                )

    if status_code == 200:
        stripped = html.strip()
        if len(stripped) < _EMPTY_THRESHOLD and not _looks_like_data(html):
            return BlockVerdict(
                True, "empty",
                f"HTTP 200 near-empty ({len(stripped)}b)",
                ("http_200", "empty"),
            )

    return _structural(html)


def is_blocked(*args, **kwargs) -> tuple[bool, str]:
    """Legacy-shape helper matching the signature of crawl4ai's `is_blocked`."""
    v = detect(*args, **kwargs)
    return v.blocked, v.reason
