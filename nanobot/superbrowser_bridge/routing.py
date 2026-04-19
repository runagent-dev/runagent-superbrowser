"""
Routing logic for the orchestrator — pure Python, no nanobot deps.

Moved out of `orchestrator_tools.py` so the classifier and preference
helpers can be unit-tested without a full nanobot install.

Public surface:
    _classify_task(instructions, url) -> {approach, reason, confidence}
    _rewrite_for_search(instructions, url) -> str
    _record_routing_outcome(domain, approach, success) -> None
    _preferred_approach(domain) -> {approach, reason, confidence} | None
    _domain_from_url(url) -> str
    _learnings_path(domain), _captcha_learnings_path(domain), _routing_path(domain)
"""

from __future__ import annotations

import json as _json
import os
import re as _re
from pathlib import Path
from urllib.parse import urlparse

# Same layout as before — the orchestrator workspace lives one level up.
_BASE = Path(__file__).resolve().parent.parent
LEARNINGS_DIR = str(_BASE / "workspace_orchestrator" / "learnings")


# --- Path helpers ---------------------------------------------------------

def _domain_from_url(url: str) -> str:
    """Extract domain for learnings filename."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        return parsed.hostname or "unknown"
    except Exception:
        return "unknown"


def _learnings_path(domain: str) -> str:
    os.makedirs(LEARNINGS_DIR, exist_ok=True)
    safe = domain.replace("/", "_").replace(":", "_")
    return os.path.join(LEARNINGS_DIR, f"{safe}.md")


def _captcha_learnings_path(domain: str) -> str:
    os.makedirs(LEARNINGS_DIR, exist_ok=True)
    safe = domain.replace("/", "_").replace(":", "_")
    return os.path.join(LEARNINGS_DIR, f"{safe}.captcha.json")


def _routing_path(domain: str) -> str:
    os.makedirs(LEARNINGS_DIR, exist_ok=True)
    safe = domain.replace("/", "_").replace(":", "_")
    return os.path.join(LEARNINGS_DIR, f"{safe}.routing.json")


# --- Classifier -----------------------------------------------------------

# Verbs that imply the agent must *act* on a page (click, fill, submit).
# Paired with a specific URL, these strongly indicate browser delegation.
_ACTION_VERBS = _re.compile(
    r"\b(book|buy|purchase|order|register|sign\s*in|sign\s*up|log\s*in|login|"
    r"fill|submit|upload|post|delete|remove|add\s+to\s+cart|checkout|"
    r"schedule|reserve|cancel|confirm|apply|enroll|"
    r"send\s+(?:a\s+)?(?:message|email|dm))\b",
    _re.IGNORECASE,
)

# Verbs that imply aggregating values across many items — the classic
# "search and compute" pattern that ChatGPT handles via web search.
_AGGREGATION_VERBS = _re.compile(
    r"\b(average|avg|mean|median|total|sum|compare|cheapest|most\s+expensive|"
    r"top[-\s]*\d+|top[-\s]*rated|best\s+rated|how\s+much|what.*price|"
    r"list\s+(?:all\s+|the\s+)?(?:prices|listings|items|products|results))\b",
    _re.IGNORECASE,
)

# Factual-lookup patterns — one answer from a text source.
_FACTUAL_LOOKUP = _re.compile(
    r"\b(who\s+(?:is|was|won|invented|founded)|what\s+(?:is|was|year)|"
    r"when\s+(?:is|was|did)|where\s+(?:is|was)|why\s+(?:is|did)|"
    r"summari[sz]e|explain|define|describe\s+(?:the|what))\b",
    _re.IGNORECASE,
)

# Visual-only phrasing that genuinely needs pixels.
_VISUAL_ONLY = _re.compile(
    r"\b(screenshot|take\s+a\s+picture|looks?\s+like|visually|pixel|"
    r"chart|graph|map\s+view|layout|design\s+of|appearance\s+of)\b",
    _re.IGNORECASE,
)

# Plural context — aggregation verbs are more trustworthy when paired with
# an explicitly plural target.
_PLURAL_CONTEXT = _re.compile(
    r"\b(listings|products|items|prices|reviews|articles|results|options|"
    r"restaurants|flights|hotels|deals|offers|posts|comments|entries)\b",
    _re.IGNORECASE,
)

# Transactional / live-inventory queries. These need a browser because
# search engines don't have fresh pricing or availability for booking
# sites — the answer literally doesn't exist in a snippet.
_TRANSACTIONAL_PATTERNS = _re.compile(
    r"\b(hotel|flight|room|booking|reservation|airfare|fare|"
    r"tickets?|availability|check\s*(?:in|out)|stay|night|seats?|"
    r"in\s+stock|out\s+of\s+stock|delivery|shipping|rental)s?\b",
    _re.IGNORECASE,
)

# Date indicators — presence of a specific date/date-range strongly
# suggests the user wants fresh live data, not an article summary.
_DATE_INDICATORS = _re.compile(
    r"\b("
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}"
    r"|next\s+(?:week|weekend|month|friday|saturday|sunday|monday|tuesday|wednesday|thursday)"
    r"|this\s+(?:week|weekend|month)"
    r"|tomorrow|tonight"
    r"|for\s+\d+\s+(?:night|day|week|month)s?"
    r"|from\s+[^\s]+\s+to\s+[^\s]+"
    r")\b",
    _re.IGNORECASE,
)

# Known travel/shopping/booking brands that always require browser
# interaction even when users mention them without a full URL. The typo
# variants are deliberate — users type "gozayan" and "gozayaan" equally.
_BRAND_TO_URL: dict[str, str] = {
    # travel — Bangladesh / South Asia
    "gozayaan": "https://www.gozayaan.com",
    "gozayan": "https://www.gozayaan.com",
    "shohoz": "https://www.shohoz.com",
    "novoair": "https://www.flynovoair.com",
    # travel — global
    "expedia": "https://www.expedia.com",
    "kayak": "https://www.kayak.com",
    "priceline": "https://www.priceline.com",
    "orbitz": "https://www.orbitz.com",
    "booking.com": "https://www.booking.com",
    "booking": "https://www.booking.com",
    "agoda": "https://www.agoda.com",
    "airbnb": "https://www.airbnb.com",
    "hotels.com": "https://www.hotels.com",
    "trivago": "https://www.trivago.com",
    "skyscanner": "https://www.skyscanner.com",
    "google flights": "https://www.google.com/travel/flights",
    # shopping / marketplaces
    "amazon": "https://www.amazon.com",
    "ebay": "https://www.ebay.com",
    "mercari": "https://www.mercari.com",
    "etsy": "https://www.etsy.com",
    "walmart": "https://www.walmart.com",
    "target": "https://www.target.com",
    "bestbuy": "https://www.bestbuy.com",
    "best buy": "https://www.bestbuy.com",
    # real estate
    "zillow": "https://www.zillow.com",
    "redfin": "https://www.redfin.com",
    "apartments.com": "https://www.apartments.com",
    "realtor.com": "https://www.realtor.com",
    # food / rides
    "opentable": "https://www.opentable.com",
    "doordash": "https://www.doordash.com",
    "grubhub": "https://www.grubhub.com",
    "uber eats": "https://www.ubereats.com",
    "instacart": "https://www.instacart.com",
    "lyft": "https://www.lyft.com",
    # events
    "ticketmaster": "https://www.ticketmaster.com",
    "stubhub": "https://www.stubhub.com",
    "seatgeek": "https://seatgeek.com",
}

_BRAND_PATTERN = _re.compile(
    r"\b(" + "|".join(_re.escape(b) for b in sorted(_BRAND_TO_URL, key=len, reverse=True)) + r")\b",
    _re.IGNORECASE,
)


# Explicit target-site verbs — "go to X", "visit X", "open X", "check X".
# These are direct user commands to point at a specific site; X is the
# target even when it's a name we've never heard of.
_EXPLICIT_TARGET_VERBS = _re.compile(
    r"\b(?:go\s+to|goto|visit|open|navigate\s+to|browse|check\s+(?:out\s+)?|"
    r"look\s+(?:this\s+|it\s+)?up\s+on|search\s+(?:on|in)|pull\s+up)\s+"
    r"([a-zA-Z][a-zA-Z0-9.-]{1,40})",
    _re.IGNORECASE,
)

# Prepositional brand — "on Amazon", "at Walmart", "from Gozayaan".
# Requires the target to start with a capital letter OR end in a TLD so
# we don't match sentence fragments like "on time" or "at all".
_PREP_CAPITALIZED_TARGET = _re.compile(
    r"\b(?:on|at|from|via)\s+"
    r"([A-Z][A-Za-z0-9]+(?:\.[A-Za-z]{2,5})?(?:\s+[A-Z][A-Za-z0-9]+)?)"
    r"\b"
)

# Bare domain — "zalora.com.bd", "booking.com", "example.shop".
# Broader than a strict URL regex: we let the escalation path prepend
# https:// so users can type just the domain.
_BARE_DOMAIN = _re.compile(
    r"\b([a-z][a-z0-9-]{1,30}\.(?:com|net|io|co|org|app|shop|store|ai|dev|"
    r"me|uk|bd|in|pk|lk|au|ca|de|fr|jp|it|es|nl|se|no|br|mx|sg|kr)"
    r"(?:\.[a-z]{2,4})?)\b",
    _re.IGNORECASE,
)

# Common English words that frequently follow prepositions — NOT brands.
# Without this skip-list, "Review on Sunday" matches the prep-target rule.
_PREP_SKIP_WORDS = {
    "sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "time", "hand", "all", "average", "that", "this", "these", "those",
    "a", "an", "the", "some", "any", "my", "your", "his", "her",
}


def _extract_browser_target(text: str) -> str | None:
    """Find a target URL for a browser delegation from free text.

    Priority cascade:
      1. Explicit `http(s)://` URL  — use verbatim.
      2. Known brand (_BRAND_TO_URL) — direct site URL, fastest path.
      3. Bare domain (word.tld)     — prepend scheme.
      4. "go to X" / "visit X"      — unknown brand; return Google search URL
                                      so the browser worker can find the
                                      real site via its first click.
      5. Prep target "on X" (Capitalized) — same; Google search launching pad.

    Returning a google.com/search URL for unknown brands lets the orchestrator
    delegate cleanly (the browser worker's existing research-prompt knows how
    to pick the correct result and click through).
    """
    if not text:
        return None

    # 1. Explicit URL.
    url_m = _re.search(r"https?://[^\s)]+", text)
    if url_m:
        return url_m.group(0).rstrip(".,;)")

    # 2. Known brand.
    brand_m = _BRAND_PATTERN.search(text)
    if brand_m:
        key = brand_m.group(1).lower()
        known = _BRAND_TO_URL.get(key)
        if known:
            return known

    # 3. Bare domain.
    dom_m = _BARE_DOMAIN.search(text)
    if dom_m:
        d = dom_m.group(1).lower()
        return f"https://{d}" if d.startswith("www.") else f"https://www.{d}"

    # 4. Explicit target command — "go to X", "visit X".
    cmd_m = _EXPLICIT_TARGET_VERBS.search(text)
    if cmd_m:
        target = cmd_m.group(1).strip(".,;)")
        if target and target.lower() not in _PREP_SKIP_WORDS:
            # Unknown brand → use Google search URL so the browser worker
            # can land on the right domain via its own first click.
            from urllib.parse import quote_plus
            return f"https://www.google.com/search?q={quote_plus(target)}"

    # 5. Prepositional capitalized target — "on Amazon", "from Gozayaan".
    prep_m = _PREP_CAPITALIZED_TARGET.search(text)
    if prep_m:
        target = prep_m.group(1).strip()
        if target and target.lower() not in _PREP_SKIP_WORDS:
            from urllib.parse import quote_plus
            return f"https://www.google.com/search?q={quote_plus(target)}"

    return None


def _classify_task(instructions: str, url: str | None = None) -> dict:
    """Deterministic routing classifier.

    Returns {approach, reason, confidence}. No LLM call. Checked BEFORE
    worker spawn so the orchestrator can be nudged if it picked the wrong
    tool. Per-domain learned preferences (Layer 4) override the rule
    cascade when they exist with enough signal.
    """
    text = (instructions or "").strip()
    domain = _domain_from_url(url) if url else ""

    # Rule 6 (runs first as override): learned preference from past outcomes.
    if domain:
        pref = _preferred_approach(domain)
        if pref:
            return {
                "approach": pref["approach"],
                "reason": f"learned preference for {domain}: {pref['reason']}",
                "confidence": pref["confidence"],
            }

    # Rule 1: specific URL + action verb → browser.
    if url and _ACTION_VERBS.search(text):
        return {
            "approach": "browser",
            "reason": "action verb on a specific URL (needs to interact)",
            "confidence": 0.9,
        }

    # Rule 1.5: transactional query with a date range → browser.
    # "hotel in Paris April 15-17", "flight from NYC to SFO next weekend",
    # "room for 2 nights" — search engines don't have fresh pricing or
    # availability for booking-class sites; the data lives behind a form.
    if _TRANSACTIONAL_PATTERNS.search(text) and _DATE_INDICATORS.search(text):
        return {
            "approach": "browser",
            "reason": (
                "transactional query with specific dates — live pricing/availability "
                "requires the booking site's own form; search snippets won't have it"
            ),
            "confidence": 0.88,
        }

    # Rule 2: aggregation verbs.
    # Runs BEFORE the bare-brand rule so cross-site comparisons
    # ("compare Dyson V15 across amazon, bestbuy, target") stay on the
    # search path — snippets reach all three sites at once whereas a
    # single browser session only hits one.
    agg_match = _AGGREGATION_VERBS.search(text)
    if agg_match:
        has_plural = bool(_PLURAL_CONTEXT.search(text))
        return {
            "approach": "search",
            "reason": (
                f"aggregation verb '{agg_match.group(0)}' detected"
                + (" with plural context" if has_plural else "")
                + " — snippets usually contain enough data"
            ),
            "confidence": 0.85 if has_plural else 0.75,
        }

    # Rule 3: factual lookup patterns.
    fact_match = _FACTUAL_LOOKUP.search(text)
    if fact_match:
        return {
            "approach": "search",
            "reason": f"factual lookup pattern '{fact_match.group(0)}' — answer is in text",
            "confidence": 0.8,
        }

    # Rule 4: visual-only phrasing.
    vis_match = _VISUAL_ONLY.search(text)
    if vis_match:
        return {
            "approach": "browser",
            "reason": f"visual-only keyword '{vis_match.group(0)}' — needs pixels",
            "confidence": 0.8,
        }

    # Rule 4.5: known transactional brand mentioned without scheme.
    # "Check the Dyson V15 on amazon", "find a 3-bedroom airbnb in Austin".
    # Runs AFTER aggregation so cross-site comparisons don't get forced
    # into browser. We only promote single-brand mentions to browser and
    # include target_url so the caller has a concrete destination.
    if not url:
        brand_target = _extract_browser_target(text)
        if brand_target:
            return {
                "approach": "browser",
                "reason": f"mentioned a transactional brand → must use {brand_target}",
                "confidence": 0.8,
                "target_url": brand_target,
            }

    # Rule 5: "Find X on site.com" with no interaction verbs.
    if url and "find" in text.lower() and not _ACTION_VERBS.search(text):
        return {
            "approach": "hybrid",
            "reason": "find-on-site phrasing — try search first, browser only if search is insufficient",
            "confidence": 0.65,
        }

    # Rule 7 (fallback): URL given → browser, else → search.
    if url:
        return {
            "approach": "browser",
            "reason": "specific URL given and no aggregation/factual hint — default to browser",
            "confidence": 0.55,
        }
    return {
        "approach": "search",
        "reason": "no specific URL — open-ended question, start with search",
        "confidence": 0.6,
    }


# --- Blocked-content detector (Layer 5) -----------------------------------

_BLOCK_MARKERS = [
    "just a moment",           # Cloudflare challenge
    "enable javascript",
    "please turn on javascript",
    "verify you are human",
    "i'm not a robot",
    "access denied",
    "403 forbidden",
    "429 too many",
    "cf-ray",                  # Cloudflare header echoed into HTML
    "cf-browser-verification",
    "captcha",
    "checking your browser",
    "_cf_chl_",
    "ddos protection",
    "security check",
]


def _looks_blocked(content: str) -> tuple[bool, str]:
    """Heuristic: does this HTTP response look like a bot-block stub?

    Returns (blocked, reason). Preferred path is the typed detector in
    `superbrowser_bridge.antibot.bot_detect` (Akamai/CF/DataDome/
    PerimeterX/Kasada-aware). If that module fails to import (minimal
    installs), fall back to the legacy marker/visible-text check.

    Callers that need the typed verdict (with block_class, etc.) should
    use `looks_blocked_typed` below.
    """
    try:
        from superbrowser_bridge.antibot.bot_detect import detect
        v = detect(content or "")
        if v.blocked:
            return True, v.reason or v.klass
        return False, ""
    except Exception:
        pass
    # Legacy path.
    if not content:
        return True, "empty_response"
    text = content.strip()
    if len(text) < 500:
        return True, f"too_short:{len(text)}"
    lower = text.lower()
    for marker in _BLOCK_MARKERS:
        if marker in lower:
            return True, f"marker:{marker}"
    stripped = _re.sub(
        r"<script[\s\S]*?</script>|<style[\s\S]*?</style>",
        "", text, flags=_re.IGNORECASE,
    )
    stripped = _re.sub(r"<[^>]+>", " ", stripped)
    visible = _re.sub(r"\s+", " ", stripped).strip()
    if len(visible) < 200:
        return True, f"no_visible_text:{len(visible)}"
    return False, ""


def looks_blocked_typed(content: str, status_code: int | None = None) -> dict:
    """Return a typed block verdict: {blocked, klass, reason}.

    Callers in the orchestrator use this to choose which tier to try
    next. `klass` is one of akamai/cloudflare/perimeterx/datadome/
    imperva/sucuri/kasada/generic/empty/rate_limited/structural/''.
    """
    try:
        from superbrowser_bridge.antibot.bot_detect import detect
        v = detect(content or "", status_code)
        return {"blocked": v.blocked, "klass": v.klass, "reason": v.reason}
    except Exception:
        blocked, reason = _looks_blocked(content)
        return {"blocked": blocked, "klass": "generic" if blocked else "", "reason": reason}


def _rewrite_for_search(instructions: str, url: str | None) -> str:
    """Strip interaction verbs from a browser-task prompt so it reads as a
    research question. Used by the captcha fallback so the search worker
    answers the data question without trying to act.
    """
    text = (instructions or "").strip()
    text = _re.sub(
        r"\b(open|navigate to|go to|click|fill|submit|scroll to|browse)\b\s*",
        "",
        text,
        flags=_re.IGNORECASE,
    )
    text = _re.sub(r"\s+", " ", text).strip()
    if url and url not in text:
        domain = _domain_from_url(url)
        if domain and domain != "unknown":
            text = f"{text} (on {domain})"
    return text


# --- Routing preference (Layer 4) -----------------------------------------

def _record_routing_outcome(
    domain: str,
    approach: str,
    success: bool,
    used_rendered: bool = False,
    *,
    tier: int | None = None,
    block_class: str = "",
) -> None:
    """Increment per-domain counters after a delegation finishes.

    `used_rendered` flags search runs that needed to fall back to the
    stealth-browser render path. A domain where search always needs
    rendering is essentially a browser task in disguise.

    When `tier` is set, also update the antibot tier ledger:
      tier_outcomes: {"<tier>": "success"|"fail:<class>"}
      lowest_successful_tier: <int>
    """
    if not domain or domain == "unknown":
        return
    from datetime import datetime, timezone
    path = _routing_path(domain)
    data: dict = {
        "domain": domain,
        "browser_success": 0, "browser_fail": 0,
        "search_success": 0, "search_fail": 0,
        "search_needed_render": 0,
        "tier_outcomes": {},
        "lowest_successful_tier": None,
        "block_class": "",
        "last_updated": None,
    }
    if os.path.exists(path):
        try:
            with open(path) as f:
                loaded = _json.load(f)
            if isinstance(loaded, dict):
                data.update(loaded)
        except (ValueError, OSError):
            pass
    # Legacy counters (kept so _preferred_approach continues to work).
    key = f"{approach}_{'success' if success else 'fail'}"
    if key in data:
        data[key] = int(data.get(key, 0)) + 1
    if used_rendered:
        data["search_needed_render"] = int(data.get("search_needed_render", 0)) + 1
    # Tier-aware ledger.
    if tier is not None:
        outcomes = data.get("tier_outcomes") or {}
        if not isinstance(outcomes, dict):
            outcomes = {}
        outcomes[str(tier)] = (
            "success" if success
            else (f"fail:{block_class}" if block_class else "fail")
        )
        data["tier_outcomes"] = outcomes
        if success:
            cur = data.get("lowest_successful_tier")
            if cur is None or int(tier) < int(cur):
                data["lowest_successful_tier"] = int(tier)
        if block_class:
            data["block_class"] = block_class
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(path, "w") as f:
            _json.dump(data, f, indent=2)
    except OSError:
        pass


def choose_starting_tier(domain: str) -> int:
    """Return the lowest tier known to succeed on this domain, else 0.

    Used by the orchestrator to skip tiers that have reliably failed in
    the past and jump straight to the cheapest working one.
    """
    if not domain or domain == "unknown":
        return 0
    path = _routing_path(domain)
    if not os.path.exists(path):
        return 0
    try:
        with open(path) as f:
            data = _json.load(f)
    except (ValueError, OSError):
        return 0
    if not isinstance(data, dict):
        return 0
    lst = data.get("lowest_successful_tier")
    if isinstance(lst, int) and 0 <= lst <= 5:
        return lst
    return 0


def _preferred_approach(domain: str) -> dict | None:
    """Return {approach, reason, confidence} if past outcomes show a clear
    winner on this domain. Requires ≥3 attempts per side with ≥30% delta.
    """
    path = _routing_path(domain)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = _json.load(f)
    except (ValueError, OSError):
        return None
    bs, bf = int(data.get("browser_success", 0)), int(data.get("browser_fail", 0))
    ss, sf = int(data.get("search_success", 0)), int(data.get("search_fail", 0))
    b_total, s_total = bs + bf, ss + sf
    if b_total < 3 or s_total < 3:
        if b_total == 0 and s_total >= 3 and ss >= sf:
            return {
                "approach": "search",
                "reason": f"search {ss}/{s_total} successful, browser untried",
                "confidence": 0.7,
            }
        if s_total == 0 and b_total >= 3 and bs >= bf:
            return {
                "approach": "browser",
                "reason": f"browser {bs}/{b_total} successful, search untried",
                "confidence": 0.7,
            }
        return None
    b_rate = bs / b_total
    s_rate = ss / s_total
    delta = s_rate - b_rate
    # If every "successful" search run needed the rendered fallback, the
    # search path is effectively paying browser cost every time. Flip the
    # preference to browser so the orchestrator stops pretending search
    # is cheap on this domain.
    needed_render = int(data.get("search_needed_render", 0))
    if ss >= 3 and needed_render >= ss:
        return {
            "approach": "browser",
            "reason": (
                f"search succeeded {ss}/{s_total} times but needed rendered fallback "
                f"on {needed_render}/{ss} — effectively a browser task"
            ),
            "confidence": 0.8,
        }
    if delta >= 0.3:
        return {
            "approach": "search",
            "reason": f"search {ss}/{s_total} vs browser {bs}/{b_total}",
            "confidence": min(0.95, 0.7 + delta),
        }
    if delta <= -0.3:
        return {
            "approach": "browser",
            "reason": f"browser {bs}/{b_total} vs search {ss}/{s_total}",
            "confidence": min(0.95, 0.7 - delta),
        }
    return None
