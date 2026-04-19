"""Python-side reader/writer for the shared bot-protection cookie jar.

Mirrors the on-disk format used by `src/browser/captcha/cookie-jar.ts` so
the TypeScript Puppeteer layer (Tier 1) and this Python layer (Tier 2/3)
share clearance cookies across tiers.

File layout: `~/.superbrowser/cookie-jar/<hostname>.json` containing
`{scope: {userAgent, captureUrl, capturedAt (ms epoch), cookies: [...]}}`.
Opt-in via SUPERBROWSER_COOKIE_JAR=1.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Iterable

_HARD_TTL_MS = 7 * 24 * 60 * 60 * 1000

# Whitelist must stay in lockstep with cookie-jar.ts:40-57.
_PROTECTION_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"^cf_clearance$",
        r"^__cf_bm$",
        r"^_hcaptcha",
        r"^h-captcha",
        r"^recaptcha",
        r"^datadome$",
        r"^incap_ses_",
        r"^visid_incap_",
        r"^AWSALB",
        r"^AWSALBCORS",
        r"^bm_sv$",
        r"^bm_sz$",
        r"^ak_bmsc$",
        r"^_abck$",
        r"^reese84$",
        r"^px-?",
    )
]


def _enabled() -> bool:
    return os.environ.get("SUPERBROWSER_COOKIE_JAR") == "1"


def _jar_dir() -> Path:
    override = os.environ.get("SUPERBROWSER_COOKIE_JAR_PATH")
    if override:
        return Path(override)
    return Path.home() / ".superbrowser" / "cookie-jar"


def _scope() -> str:
    return os.environ.get("SUPERBROWSER_TASK_ID") or "global"


def _safe_host(hostname: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "_", hostname, flags=re.I)


def _file_for(hostname: str) -> Path:
    return _jar_dir() / f"{_safe_host(hostname)}.json"


def is_protection_cookie(name: str) -> bool:
    return any(p.search(name) for p in _PROTECTION_PATTERNS)


def read_entry(hostname: str) -> dict | None:
    """Return the persisted entry for the current scope, or None."""
    if not hostname:
        return None
    f = _file_for(hostname)
    try:
        if not f.exists():
            return None
        data = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(_scope())
    if not isinstance(entry, dict):
        return None
    captured = entry.get("capturedAt", 0)
    if (time.time() * 1000) - captured > _HARD_TTL_MS:
        return None
    return entry


def load_cookies(hostname: str, *, current_ua: str | None = None) -> dict[str, str]:
    """Return a {name: value} dict of live bot-protection cookies.

    Skips restore if the saved UA differs from `current_ua` — mirroring
    the TS side. Returns empty dict when disabled, expired, or mismatched.
    """
    if not _enabled():
        return {}
    entry = read_entry(hostname)
    if not entry:
        return {}
    if current_ua and entry.get("userAgent") and entry["userAgent"] != current_ua:
        return {}
    now_sec = time.time()
    out: dict[str, str] = {}
    for c in entry.get("cookies", []) or []:
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0 and exp < now_sec:
            continue
        out[name] = value
    return out


def save_cookies(
    hostname: str,
    cookies: Iterable[dict],
    *,
    user_agent: str = "",
    capture_url: str = "",
) -> int:
    """Persist matching bot-protection cookies.

    `cookies` is an iterable of cookie dicts with at least `name` and `value`
    and optional `domain`, `path`, `expires`, `httpOnly`, `secure`, `sameSite`.
    Returns the count saved (0 if disabled or nothing matched).
    """
    if not _enabled() or not hostname:
        return 0
    kept: list[dict] = []
    for c in cookies:
        name = (c or {}).get("name")
        if not name or not is_protection_cookie(name):
            continue
        kept.append({
            "name": name,
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "expires": c.get("expires", -1),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", True)),
            "sameSite": c.get("sameSite", None),
        })
    if not kept:
        return 0
    f = _file_for(hostname)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    try:
        data = json.loads(f.read_text()) if f.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data[_scope()] = {
        "userAgent": user_agent,
        "captureUrl": capture_url,
        "capturedAt": int(time.time() * 1000),
        "cookies": kept,
    }
    try:
        f.write_text(json.dumps(data, indent=2))
    except OSError:
        return 0
    return len(kept)


def has_fresh(hostname: str) -> bool:
    return read_entry(hostname) is not None
