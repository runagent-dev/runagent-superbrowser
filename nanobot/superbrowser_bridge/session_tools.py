"""
Low-level session-based browser tools for nanobot.

State is encapsulated in BrowserSessionState — not module globals.
This allows multiple Nanobot instances (e.g., orchestrator + browser worker)
to have isolated state in the same process.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import os
import base64
from datetime import datetime
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

SUPERBROWSER_URL = "http://localhost:3100"
SCREENSHOT_DIR = os.environ.get("SUPERBROWSER_SCREENSHOT_DIR", "/tmp/superbrowser/screenshots")


def _auth_headers() -> dict[str, str]:
    """Bearer header injected on every TS-server request when token is set.

    The TS server (src/server/auth.ts:tokenAuth) gates `/session/:id/script` and
    `/function` behind TOKEN; without this header those endpoints 403, which
    historically broke the deterministic-script escape hatch on hard sites.
    """
    tok = os.environ.get("SUPERBROWSER_TOKEN") or os.environ.get("TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


# --- Tier 3 (patchright) HTTP-shaped shim -----------------------------------
#
# Sessions whose IDs start with `t3-` are served by the in-process
# `T3SessionManager` instead of the TS server. Intercepting here means all
# 21 existing tools work unchanged — they still call _request_with_backoff;
# this helper just peels off t3-routed URLs and returns a response-shaped
# object the tool code can call .json() / .status_code / .text on.

class _T3Response:
    """Minimal httpx.Response stand-in for t3 dispatches."""

    def __init__(self, payload: Any, status_code: int = 200, content: bytes = b""):
        self._payload = payload
        self.status_code = status_code
        self.content = content or (payload if isinstance(payload, bytes) else b"")
        self.headers: dict[str, str] = {}
        self.text = ""
        if isinstance(payload, bytes):
            # Binary response (screenshot). Advertise as JPEG so callers
            # that key off content-type treat it correctly.
            self.headers["content-type"] = "image/jpeg"
        elif isinstance(payload, str):
            self.text = payload
            self.headers["content-type"] = "text/plain"
        elif isinstance(payload, (dict, list)):
            try:
                self.text = json.dumps(payload)
                self.headers["content-type"] = "application/json"
            except Exception:
                self.text = ""

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise httpx.HTTPStatusError(
                f"t3 {self.status_code}", request=None, response=None,  # type: ignore[arg-type]
            )


def _is_t3_url(url: str) -> bool:
    return "/session/t3-" in url


async def _t3_dispatch_from_http(
    method: str, url: str, *, json_body: dict[str, Any] | None,
) -> _T3Response:
    """Parse the t3-routed URL + body and call T3SessionManager."""
    from superbrowser_bridge.antibot import interactive_session as _t3

    # Split path. Expected forms:
    #   /session/t3-<uuid>             -> DELETE (close)
    #   /session/t3-<uuid>/<verb>      -> GET or POST
    path = url.split(SUPERBROWSER_URL, 1)[-1].split("?", 1)[0]
    parts = [p for p in path.split("/") if p]
    # parts: ["session", "t3-<uuid>", "<verb>?"]
    if len(parts) < 2 or not parts[1].startswith("t3-"):
        return _T3Response({"error": "bad t3 url"}, status_code=400)
    sid = parts[1]
    verb = parts[2] if len(parts) >= 3 else None
    body = dict(json_body or {})

    mgr = _t3.default()
    try:
        if verb is None:
            # DELETE /session/<sid>
            if method.upper() == "DELETE":
                res = await mgr.close(sid)
                return _T3Response(res)
            return _T3Response({"error": "no verb"}, status_code=400)

        if verb == "navigate":
            url_to = body.get("url", "")
            data = await mgr.navigate(sid, url_to, timeout_s=body.get("timeout_s", 45.0))
            return _T3Response(data)

        if verb == "state":
            data = await mgr.state(sid, use_vision=bool(body.get("vision", False)))
            return _T3Response(data)

        if verb == "screenshot":
            png = await mgr.screenshot(sid)
            return _T3Response(png, content=png)

        if verb == "markdown":
            md = await mgr.get_markdown(sid)
            return _T3Response({"content": md})

        if verb == "click":
            if "bbox" in body or ("x" in body and "y" in body):
                x = float(body.get("x", body.get("bbox", {}).get("x0", 0)))
                y = float(body.get("y", body.get("bbox", {}).get("y0", 0)))
                bbox = body.get("bbox")
                expected_label = body.get("expected_label") or body.get("label")
                strategy = body.get("strategy") or "primary"
                data = await mgr.click_at(
                    sid, x, y, bbox=bbox,
                    strategy=str(strategy).lower(),
                    expected_label=(
                        str(expected_label).strip()
                        if expected_label else None
                    ),
                )
            else:
                data = await mgr.click(sid, int(body["index"]))
            return _T3Response(data)

        if verb == "type":
            data = await mgr.type(
                sid,
                int(body["index"]),
                body.get("text", ""),
                clear=bool(body.get("clear", True)),
            )
            return _T3Response(data)

        if verb == "type-at" or verb == "type_at":
            data = await mgr.type_at(
                sid,
                float(body.get("x", 0)),
                float(body.get("y", 0)),
                body.get("text", ""),
                clear=bool(body.get("clear", True)),
                target_label=str(body.get("label", "")),
            )
            return _T3Response(data)

        if verb == "fix-text-at" or verb == "fix_text_at":
            data = await mgr.fix_text_at(
                sid,
                float(body.get("x", 0)),
                float(body.get("y", 0)),
                body.get("text", ""),
                target_label=str(body.get("label", "")),
            )
            return _T3Response(data)

        if verb == "keys":
            # BrowserKeysTool sends `keys` as either a string ("Enter",
            # "ArrowDown", or a chord like "Control+A") OR as a list of
            # such strings. NEVER pass a bare string through `list()` —
            # that splits it into individual characters and presses each
            # one, turning `browser_keys("Enter")` into typing the
            # letters E-n-t-e-r into the focused input.
            raw_keys = body.get("keys", [])
            if isinstance(raw_keys, str):
                keys_list = [raw_keys]
            elif isinstance(raw_keys, (list, tuple)):
                keys_list = [str(k) for k in raw_keys]
            else:
                keys_list = []
            data = await mgr.keys(sid, keys_list)
            return _T3Response(data)

        if verb == "scroll":
            data = await mgr.scroll(
                sid,
                direction=body.get("direction"),
                percent=body.get("percent"),
            )
            return _T3Response(data)

        if verb == "drag":
            data = await mgr.drag(
                sid,
                float(body.get("startX", 0)), float(body.get("startY", 0)),
                float(body.get("endX", 0)), float(body.get("endY", 0)),
                steps=int(body.get("steps", 20)),
            )
            return _T3Response(data)

        if verb == "select":
            data = await mgr.select(sid, int(body["index"]), body.get("value", ""))
            return _T3Response(data)

        if verb == "evaluate":
            data = await mgr.evaluate(sid, body.get("script", ""))
            return _T3Response({"result": data})

        if verb == "script":
            data = await mgr.run_script(sid, body.get("code", ""))
            return _T3Response(data)

        if verb == "wait-for" or verb == "wait_for":
            data = await mgr.wait_for(
                sid,
                selector=body.get("selector"),
                timeout_s=float(body.get("timeout", 10.0)),
            )
            return _T3Response(data)

        # Captcha verbs — map URL shapes like /captcha/detect, /captcha/solve,
        # /captcha/screenshot to the antibot.captcha module.
        if verb == "captcha":
            sub = parts[3] if len(parts) >= 4 else ""
            from superbrowser_bridge.antibot import captcha as _cap
            if sub == "detect":
                info = await _cap.detect(mgr, sid)
                return _T3Response({
                    "captcha": {
                        "present": info.present,
                        "type": info.type,
                        "site_key": info.site_key,
                        "widget_bbox": info.widget_bbox,
                        "widget_selector": info.widget_selector,
                        "frame_url": info.frame_url,
                        "notes": info.notes,
                    },
                })
            if sub == "screenshot":
                info = await _cap.detect(mgr, sid)
                data = await _cap.widget_screenshot(mgr, sid, info)
                return _T3Response(data)
            if sub == "solve":
                info = await _cap.detect(mgr, sid)
                if not info.present:
                    return _T3Response({
                        "solved": True, "method": "none",
                        "note": "no captcha detected at solve time",
                    })
                method = (body.get("method") or "auto").lower()
                if method in ("auto", "token") and info.type in (
                    "recaptcha-v2", "hcaptcha", "turnstile",
                ):
                    res = await _cap.solve_token(mgr, sid, info)
                    if res.get("solved"):
                        return _T3Response(res)
                if method in ("auto", "slider") and info.type == "slider":
                    res = await _cap.solve_slider(mgr, sid, info)
                    if res.get("solved") or method == "slider":
                        return _T3Response(res)
                if (
                    method in ("auto", "cf_wait")
                    and info.type == "cf_interstitial"
                ):
                    # Whole-page CF interstitial — wait for auto-pass.
                    res = await _cap.solve_cf_interstitial(mgr, sid, info)
                    if res.get("solved") or method == "cf_wait":
                        return _T3Response(res)
                if method in ("auto", "vision"):
                    res = await _cap.solve_vision(mgr, sid, info)
                    return _T3Response(res)
                return _T3Response({
                    "solved": False, "method": method,
                    "error": f"no applicable strategy for {info.type}",
                })

        # Verbs that don't yet have a t3 equivalent (dialog, human-input).
        return _T3Response(
            {"error": f"t3 verb '{verb}' not yet implemented"},
            status_code=501,
        )
    except KeyError as exc:
        return _T3Response({"error": f"session {exc} not found"}, status_code=404)
    except Exception as exc:
        # Log the full traceback to both stdout and a dedicated log file
        # so operators can diagnose T3 crashes without having to grep
        # through asyncio noise. The file path is intentionally stable
        # so repeated failures accumulate for post-mortem inspection.
        import traceback as _tb
        tb_str = _tb.format_exc()
        _err_msg = (
            f"[t3 dispatch] verb={verb!r} sid={sid!r} "
            f"{type(exc).__name__}: {exc}"
        )
        print(_err_msg)
        print(tb_str)
        try:
            log_path = os.environ.get("T3_ERROR_LOG") or "/tmp/superbrowser/t3_errors.log"
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a") as _lf:
                from datetime import datetime as _dt
                _lf.write(
                    f"\n--- {_dt.utcnow().isoformat()} ---\n"
                    f"{_err_msg}\n"
                    f"body: {json.dumps(body, default=str)[:500]}\n"
                    f"{tb_str}\n"
                )
        except Exception:
            pass
        return _T3Response(
            {
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "verb": verb,
                "traceback_head": tb_str.splitlines()[-3:] if tb_str else [],
            },
            status_code=500,
        )


def _detect_playwright_pseudo(selector: str) -> str | None:
    """Reject selectors that use Playwright-specific pseudo-classes the
    Puppeteer-backed bridge cannot evaluate.

    `:has-text(...)` / `:text(...)` / `:contains(...)` / `:visible` /
    `:hidden` / `:nth-match(...)` look like CSS but are Playwright
    extensions. document.querySelector silently returns null on them, so
    the click reports "no element found" with no hint that the SELECTOR
    syntax was the problem. Catching this pre-dispatch saves the brain's
    iteration budget AND points at correct alternatives.

    `>>` is Playwright's selector-chaining operator (`button >> text=Foo`).
    Same issue.

    Native CSS `:has(...)` is allowed (Chromium 105+). We only reject
    the Playwright extensions.
    """
    if not selector or not isinstance(selector, str):
        return None
    s = selector
    sl = s.lower()
    bad: list[tuple[str, str]] = []
    for pseudo, hint in (
        (":has-text(", "Use [aria-label*='X'] OR browser_click_at(V_n)."),
        (":text(", "Use [aria-label*='X'] OR browser_click_at(V_n)."),
        (":contains(", "Use [aria-label*='X'] OR browser_click_at(V_n)."),
        (":visible", "Visibility filter is implicit; drop it."),
        (":hidden", "Hidden elements aren't clickable; pick a visible target."),
        (":nth-match(", "Use :nth-of-type(N) (standard CSS) instead."),
    ):
        if pseudo in sl:
            bad.append((pseudo, hint))
    if " >> " in s or s.strip().startswith(">>"):
        bad.append((
            ">>",
            "`>>` is Playwright's selector chain. Use a single CSS selector or "
            "browser_click_at(V_n).",
        ))
    if not bad:
        return None
    lines = ["[click_selector_failed:playwright_pseudo] Selector uses non-CSS extensions Puppeteer can't evaluate:"]
    for pseudo, hint in bad:
        lines.append(f"  - {pseudo!r}: {hint}")
    lines.append(
        "Recovery: take a fresh browser_screenshot then "
        "browser_click_at(vision_index=V_n), OR rewrite using only "
        "standard CSS (id, class, attribute selectors, :nth-of-type, "
        ":has() — but NOT :has-text/:text/:contains/:visible)."
    )
    return "\n".join(lines)


import re as _re_sel

# Strong discriminators: presence of any one of these in the selector
# is enough to mark it specific (no match-count pre-flight needed).
# These are inherently unique-anchoring on real sites: id, data-testid,
# name, for, aria-label.
_STRONG_DISCRIMINATOR_PATTERNS = [
    # Real id selectors: `#foo`, `div#foo`, ` #foo`, `>#foo`, etc.
    # Anchored to start, whitespace, or combinator — never inside quotes.
    _re_sel.compile(r"(?:^|[\s>+~,(])#[A-Za-z_][\w-]*"),
    _re_sel.compile(r"[A-Za-z_-]+#[A-Za-z_][\w-]*"),
    _re_sel.compile(r"\[id[*^$~|]?="),
    _re_sel.compile(r"\[data-testid"),
    _re_sel.compile(r"\[data-test"),
    _re_sel.compile(r"\[data-cy"),
    _re_sel.compile(r"\[name[*^$~|]?="),
    _re_sel.compile(r"\[for[*^$~|]?="),
    _re_sel.compile(r"\[aria-label[*^$~|]?="),
]

# Weak discriminators: nth-of-type / nth-child are POSITIONAL — they
# pick the Nth child relative to the immediate parent. They only make
# the whole selector specific when the parent is itself uniquely
# anchored. So `:nth-of-type(N)` ALONE is vague; `#foo li:nth-of-type(2)`
# is specific (parent has an id).
#
# Observed wineaccess pattern that bypassed the prior check:
# `a[role='button'][aria-expanded='false']:nth-of-type(2)` — has
# `:nth-of-type(` but no parent anchor, points at "the 2nd `<a>` among
# siblings matching the role/aria filters", which on a real DOM matches
# multiple unrelated accordion toggles.
_WEAK_DISCRIMINATOR_PATTERNS = [
    _re_sel.compile(
        r":nth-of-type\(|:nth-child\(|:nth-last-of-type\(|:nth-last-child\("
        r"|:first-of-type|:last-of-type|:first-child|:last-child"
        r"|:only-child|:only-of-type"
    ),
]


# Patterns indicating a script is exploring the DOM (read-only). When
# any of these appear AND no write op is detected, the eval is refused
# while vision is fresh — the brain should be reading bbox labels, not
# re-querying.
_EVAL_EXPLORATION_PATTERNS = _re_sel.compile(
    r"\bquerySelectorAll\b|\bquerySelector\b|"
    r"\bgetElementById\b|\bgetElementsByClassName\b|"
    r"\bgetElementsByTagName\b|\bgetElementsByName\b|"
    r"\bdocument\.body\.innerText\b|\bdocument\.body\.textContent\b"
)

# Write operations. If ANY of these appear in the script, exploration
# refusal is bypassed — even an exploration-shaped query is fine when
# it's part of a click-and-verify or value-set flow.
_EVAL_WRITE_PATTERNS = _re_sel.compile(
    r"\.click\(|\.focus\(|\.blur\(|"
    r"\.setAttribute\(|\.removeAttribute\(|"
    r"\.value\s*=|\.checked\s*=|\.selected\s*=|"
    r"\.dispatchEvent\(|"
    r"\.scrollTo\(|\.scrollIntoView\(|window\.scroll[BTL]"
)

# Probes that don't query selectors at all (just read browser-level
# state). Always allowed regardless of vision freshness.
_EVAL_PROBE_PATTERNS = _re_sel.compile(
    r"\bdocument\.readyState\b|\bdocument\.title\b|"
    r"\blocation\.(href|pathname|search|hostname|origin)\b|"
    r"\bwindow\.(innerWidth|innerHeight|scrollY|scrollX)\b|"
    r"\bdocument\.cookie\b|\bnavigator\.userAgent\b"
)


def _eval_looks_like_exploration(script: str) -> bool:
    """True when the script is read-only DOM exploration that the brain
    should be doing via vision bboxes instead. Excludes:
      • scripts containing any write op (.click, .value=, dispatchEvent, …)
      • scripts that only probe browser-level state (location, readyState)
      • short scripts (under 40 chars after strip — likely a tiny check)
    """
    if not script or not isinstance(script, str):
        return False
    s = script.strip()
    if len(s) < 40:
        return False
    if _EVAL_WRITE_PATTERNS.search(s):
        return False
    if not _EVAL_EXPLORATION_PATTERNS.search(s):
        return False
    # A pure browser-state probe is fine even if it incidentally uses
    # querySelector elsewhere (rare). Conservative: only skip if NO
    # exploration pattern appears outside the probe regions.
    if _EVAL_PROBE_PATTERNS.search(s) and not _EVAL_EXPLORATION_PATTERNS.search(s):
        return False
    return True


def _looks_vague_selector(selector: str) -> bool:
    """True when the selector has no STRONG discriminator. Weak-only
    discriminators (`:nth-of-type` etc.) without a strong-anchored
    parent are still treated as vague — they're positional and the
    parent context isn't pinned.

    Triggers a match-count pre-flight in BrowserClickSelectorTool — if
    querySelectorAll would match >1, refuse with [selector_too_vague].

    Specific (no pre-flight):
      "#submit"                            (strong: id)
      "[data-testid=cart]"                 (strong: data-testid)
      "label[for='oregon']"                (strong: for)
      "#accordion li:nth-of-type(3)"       (id parent + nth child)
      "button[name='go']"                  (strong: name)

    Vague (pre-flight + refuse if matches >1):
      "button"
      "a[role='button']"
      "a[aria-expanded='false'][href='#']"
      "[role=checkbox]"
      ":nth-of-type(2)"                                (weak alone)
      "a[role='button']:nth-of-type(2)"                (weak alone, no parent anchor)
    """
    if not selector or not isinstance(selector, str):
        return False
    has_strong = any(p.search(selector) for p in _STRONG_DISCRIMINATOR_PATTERNS)
    return not has_strong


# ── v8: vision-grounded clicks (target_label protocol) ──
#
# Forces the brain to NAME the V_n it's clicking before the click happens.
# Tools that take `vision_index` now require `target_label` — a string
# fuzzy-matched against the actual V_n label vision emitted. Mismatch
# refuses with the actual ranked V_n labels surfaced inline so the brain
# can pivot in the same turn. This is a protocol change, not a refusal
# layer: a click no longer means "execute action" — it means "execute
# action WITH my reading of the page attached, so the system can verify
# I actually read." Kill switch VISION_TARGET_LABEL_REQUIRED=0 reverts
# to v7 behavior (no validation).


def _format_bbox_label_list(vresp: Any, max_count: int = 8) -> str:
    """Compact `[V_n] 'label'` rendering for refusal recovery messages.

    Mirrors the ranked-order sort in vision_agent/schemas.py:as_brain_text
    (line 998) so V_n in the refusal matches V_n the brain saw in its
    most recent screenshot reply.
    """
    if vresp is None or not getattr(vresp, "bboxes", None):
        return "  (no vision response cached — call browser_screenshot first)"
    bboxes = vresp.bboxes

    def _rank_key(b: Any) -> tuple:
        # Match the rank function in vision_agent/schemas.py:981.
        role = getattr(b, "role_in_scene", None)
        role_rank = 0 if role == "blocker" else (1 if role == "target" else 2)
        return (
            role_rank,
            0 if getattr(b, "intent_relevant", False) else 1,
            0 if getattr(b, "clickable", True) else 1,
            -float(getattr(b, "confidence", 0.0) or 0.0),
        )

    try:
        ordered = sorted(bboxes, key=_rank_key)[:max_count]
    except Exception:
        ordered = list(bboxes)[:max_count]
    lines: list[str] = []
    for i, b in enumerate(ordered, start=1):
        label = (getattr(b, "label", "") or "").strip() or "(no label)"
        marker = ""
        if getattr(b, "intent_relevant", False):
            marker = "  ← matches intent"
        lines.append(f"  [V{i}] {label!r}{marker}")
    return "\n".join(lines) if lines else "  (vision returned 0 bboxes)"


def _validate_target_label(
    state: "BrowserSessionState",
    vision_index: int | None,
    target_label: str | None,
) -> str | None:
    """Verify the brain's `target_label` matches the actual V_n label.

    Returns a refusal string when the protocol is violated (vision_index
    is set, validation enabled, and either target_label is missing or
    mismatches). Returns None to allow the click.

    Behavior:
      • vision_index is None → no validation (raw (x, y) path).
      • VISION_TARGET_LABEL_REQUIRED=0 → no validation.
      • No vision response cached → no validation (brain hasn't taken a
        screenshot yet; nothing to verify against).
      • V_n out of range → no refusal here (handled by existing freshness
        / range gates in click_at).
      • bbox.label is empty → allow (vision didn't emit a label; can't
        verify mismatch).
      • target_label missing/empty → refuse with
        [click_at_target_label_required].
      • target_label matches via bidirectional substring (case-insensitive,
        whitespace-stripped) → allow.
      • target_label mismatches → refuse with [click_at_label_mismatch]
        and inline V_n label list.
    """
    if vision_index is None:
        return None
    if os.environ.get("VISION_TARGET_LABEL_REQUIRED", "1") == "0":
        return None
    vresp = getattr(state, "_last_vision_response", None)
    if vresp is None or not getattr(vresp, "bboxes", None):
        # Brain hasn't taken a screenshot in this session — let downstream
        # gates (no_vision, bad_vision_index) produce the canonical error.
        return None
    try:
        bbox = vresp.get_bbox(int(vision_index))
    except Exception:
        return None
    if bbox is None:
        # Out-of-range vision_index — let the existing
        # [click_at_failed:bad_vision_index] gate handle it.
        return None
    actual_label = (getattr(bbox, "label", "") or "").strip()
    target = (target_label or "").strip()
    if not target:
        # Brain didn't pass target_label — protocol violation.
        labels_block = _format_bbox_label_list(vresp)
        return (
            f"[click_at_target_label_required] You called the V_n path "
            f"(vision_index={vision_index}) without passing `target_label`. "
            f"The bridge requires you to NAME what V_{vision_index} is "
            f"before clicking, so the system can verify you actually read "
            f"the bbox label. This catches the V_1-reflex pattern where "
            f"the brain clicks whatever V_n it thinks should be right "
            f"without decoding what V_n actually represents.\n"
            f"  Recovery: re-call with `target_label='<the label vision "
            f"assigned>'`. Ranked V_n labels from the most recent "
            f"screenshot:\n{labels_block}\n"
            f"Override: VISION_TARGET_LABEL_REQUIRED=0."
        )
    if not actual_label:
        # Vision didn't emit a label — can't verify mismatch, allow.
        return None
    a = actual_label.lower()
    t = target.lower()
    if a in t or t in a:
        return None  # match
    # Mismatch — refuse with actual labels inline.
    labels_block = _format_bbox_label_list(vresp)
    return (
        f"[click_at_label_mismatch] V{vision_index} actual label is "
        f"{actual_label!r} — your target_label {target!r} does not "
        f"match. The bridge requires your reading of the page to align "
        f"with what vision saw. This catches the V_1-reflex where the "
        f"brain picks an index without decoding what's there.\n"
        f"  Ranked V_n labels (top 8) from the most recent screenshot:\n"
        f"{labels_block}\n"
        f"Pick the V_n whose label actually matches your intent and "
        f"re-call with the matching `target_label`. "
        f"Override: VISION_TARGET_LABEL_REQUIRED=0."
    )


# Arch v3 fix E — labels that designate a chevron/expand affordance
# rather than a primary toggle. Click position is shifted to the right
# edge when a bbox carries one of these labels.
_CHEVRON_LABEL_RE = __import__("re").compile(
    r"\b(?:expand[ \-]?sub[ \-]?options?"
    r"|expand sub[- ]?options?"
    r"|sub[- ]?regions?"
    r"|expand regions?"
    r"|toggle\s+(?:all|sub|expand)"
    r"|(?:^|\s)(?:expand|chevron|caret|disclosure)\b"
    r"|\(expand[^)]*\)"
    r"|\(chevron\)"
    r"|\(toggle[^)]*\))",
    __import__("re").IGNORECASE,
)


def _bbox_is_chevron_label(label: str) -> bool:
    """Return True when a bbox label designates an "expand / chevron /
    toggle-sub-options" affordance — used by click_at to shift the
    click target to the bbox's right edge."""
    if not label:
        return False
    return bool(_CHEVRON_LABEL_RE.search(label))


_SELECTOR_VAGUE_BARE = frozenset({
    "button", "a", "label", "summary", "input", "div", "span",
    "li", "tr", "td", "p", "h1", "h2", "h3", "h4", "section",
    "article", "header", "footer", "nav", "form",
    "[role='button']", '[role="button"]',
    "[role='link']", '[role="link"]',
    "[role='checkbox']", '[role="checkbox"]',
    "[type='button']", '[type="button"]',
})


def _selector_is_vague(selector: str) -> bool:
    """Heuristic: is the selector a "kitchen-sink" probe that needs a
    target_label to be safe to dispatch?

    Vague forms include:
      - bare tag selectors ("button", "summary", "a")
      - multi-fallback comma lists ("summary, button, [role='button']")
      - bare role/type attribute selectors ("[role='button']")
      - generic universal selectors

    Specific (NOT vague) forms include:
      - id selectors (#email, #accordion-region)
      - data-testid / data-test-id
      - explicit value attribute matches ([name='oregon'])
      - selectors with text content matchers (handled separately)
    """
    s = (selector or "").strip().lower()
    if not s:
        return True
    # Multi-fallback comma list with ≥2 alternatives → kitchen-sink probe.
    if s.count(",") >= 1:
        return True
    # Bare tag or role/type without further qualifiers.
    if s in _SELECTOR_VAGUE_BARE:
        return True
    # Universal selector, attribute-only.
    if s in {"*", "[role]", "[type]"}:
        return True
    return False


def _selector_is_specific(selector: str) -> bool:
    """Inverse: highly-specific selectors that name the target so well
    they don't need a `target_label`.

    Recognized:
      - id selectors (#x)
      - data-testid / data-test-id / data-cy / data-qa attributes
      - id= attribute matches
      - longer, multi-segment selectors with attribute matchers
    """
    s = (selector or "").strip().lower()
    if not s:
        return False
    if s.startswith("#") and " " not in s and "," not in s:
        return True
    for marker in (
        "[data-testid", "[data-test-id", "[data-cy", "[data-qa",
        "[id=", "[name=",
    ):
        if marker in s:
            return True
    # Long, single-target selectors with explicit attribute matchers
    # (e.g. "label[for*='oregon' i]") — likely intentional, not probes.
    if "," not in s and "[" in s and "=" in s and len(s) > 12:
        return True
    return False


def _vision_label_overlaps(target_label: str, bbox_label: str) -> bool:
    """Return True when target_label and bbox_label share a content token.

    Used by the vision-alignment check: tokenize both, drop stopwords,
    require ≥1 token of length ≥4 to overlap (case-insensitive).
    Stopwords like "button", "click", "filter" don't count alone — they
    appear on too many bboxes and would over-match.
    """
    if not target_label or not bbox_label:
        return False
    import re as _re
    stop = _SELECTOR_TARGET_STOPWORDS

    def _tokens(s: str) -> set[str]:
        toks = _re.split(r"[\s,.;:!?()\[\]{}\"'/\-_]+", s.lower())
        return {t for t in toks if len(t) >= 4 and t not in stop}

    target_toks = _tokens(target_label)
    bbox_toks = _tokens(bbox_label)
    return bool(target_toks & bbox_toks)


_SELECTOR_TARGET_STOPWORDS = frozenset({
    "button", "click", "link", "label", "filter", "filters", "input",
    "field", "checkbox", "radio", "option", "options", "menu", "open",
    "close", "expand", "collapse", "show", "hide", "select", "selected",
    "active", "with", "from", "into", "this", "that", "page",
    "view", "more", "load", "loading",
})


def _validate_selector_target_label(
    selector: str,
    target_label: str | None,
    state: "BrowserSessionState | None" = None,
) -> str | None:
    """Arch v3 fixes A + D — selector validation, structural + vision-aligned.

    Mirrors `_validate_target_label` for vision-grounded clicks. Two layers:

    Layer 1 (fix A — structural):
      - SELECTOR_TARGET_LABEL_REQUIRED=0 → no validation.
      - Specific selector (#id, [data-testid], etc.) → no requirement.
      - Vague selector + missing target_label → refuse.

    Layer 2 (fix D — vision alignment, post-trace):
      When `target_label` IS provided AND `state` has a fresh vision
      response with bboxes, verify at least one V_n label overlaps with
      `target_label`. Refuse otherwise — brain is asserting it sees
      an element vision didn't emit. This is the dominant hallucination
      mode on dynamic filter pages where the DOM-anchored selector
      `#region-united-states` doesn't actually match anything visible.
      Kill switch: SELECTOR_VISION_ALIGNMENT=0.
    """
    if os.environ.get("SELECTOR_TARGET_LABEL_REQUIRED", "1") == "0":
        return None
    sel = (selector or "").strip()
    target = (target_label or "").strip()
    # Layer 1 — structural shape check.
    if _selector_is_vague(sel):
        if not (target and len(target) >= 3):
            return (
                f"[click_selector_target_label_required] You called "
                f"browser_click_selector with a vague selector "
                f"({sel!r}) and no `target_label`. The bridge requires you to "
                f"NAME what visible text/aria you expect that selector to "
                f"match before clicking. This catches kitchen-sink selectors "
                f"like \"summary, button, [role='button']\" that match anything "
                f"on the page.\n"
                f"  Recovery options:\n"
                f"  1. Re-call with `target_label='<short label of the element "
                f"you expect to click — e.g. \"Oregon checkbox\", \"Apply "
                f"filters button\">'`.\n"
                f"  2. Replace the selector with a specific one: an #id, "
                f"[data-testid='...'], [name='...'], or a label/text-content "
                f"match (e.g. `label[for='region-oregon']`).\n"
                f"  3. If you don't know the exact element, browser_screenshot "
                f"first — vision will emit V_n bboxes and you can use "
                f"browser_click_at(vision_index=...) instead.\n"
                f"Override: SELECTOR_TARGET_LABEL_REQUIRED=0."
            )
        # vague + named target → fall through to layer 2
    elif _selector_is_specific(sel):
        # id/data-testid: selector self-names; layer 2 is still useful but
        # specific selectors are usually intentional. Allow without further
        # checks unless target_label was supplied (then layer 2 runs).
        if not target:
            return None
    # Layer 2 — vision alignment. Only runs when target_label is provided.
    if (
        os.environ.get("SELECTOR_VISION_ALIGNMENT", "1") == "0"
        or state is None
        or not target
    ):
        return None
    vresp = getattr(state, "_last_vision_response", None)
    if vresp is None:
        return None  # no vision yet — can't verify
    bboxes = list(getattr(vresp, "bboxes", None) or [])
    if not bboxes:
        return None  # empty vision pass; allow
    # Layer E (arch v3 fix L) — when target_label HAS a high-confidence
    # clickable V_n match, redirect to browser_click_at instead. On
    # dynamic filter pages a working V_n bbox is more reliable than the
    # selector hook (which rearranges between clicks). Vision saw it →
    # use the V_n path.
    prefer_vision = (
        os.environ.get("SELECTOR_PREFER_VISION", "1") != "0"
    )
    if prefer_vision:
        # Mirror VisionResponse.get_bbox ranking so the V_n index here
        # matches what the brain sees in as_brain_text.
        def _rank(b: Any) -> tuple[int, int, int, float]:
            role_rank = (
                0 if getattr(b, "role_in_scene", "") == "blocker"
                else (1 if getattr(b, "role_in_scene", "") == "target" else 2)
            )
            return (
                role_rank,
                0 if getattr(b, "intent_relevant", False) else 1,
                0 if getattr(b, "clickable", False) else 1,
                -float(getattr(b, "confidence", 0.0) or 0.0),
            )
        ordered = sorted(bboxes, key=_rank)
        for idx, b in enumerate(ordered, start=1):
            bbox_label = (getattr(b, "label", "") or "")
            if not _vision_label_overlaps(target, bbox_label):
                continue
            conf = float(getattr(b, "confidence", 0.0) or 0.0)
            clickable = bool(getattr(b, "clickable", False))
            if conf < 0.7 or not clickable:
                # Low-confidence or non-clickable V_n exists — selector
                # might still be the better path. Don't redirect; fall
                # through to "allow".
                continue
            sid = (
                getattr(state, "session_id", "") or "<session_id>"
            )
            return (
                f"[click_selector_redundant_with_v_n] You called "
                f"browser_click_selector(selector={sel!r}, "
                f"target_label={target!r}) but vision already emitted "
                f"V_{idx} with label '{bbox_label[:80]}' "
                f"(confidence={conf:.2f}, clickable=true). On dynamic "
                f"filter pages the DOM hook rearranges between clicks "
                f"— the V_n bbox path is more reliable.\n"
                f"REQUIRED: re-call as\n"
                f"  browser_click_at(session_id='{sid}', "
                f"vision_index={idx}, "
                f"target_label='{bbox_label[:80]}')\n"
                f"Override: SELECTOR_PREFER_VISION=0."
            )
    # Layer D (existing) — find ANY V_n whose label overlaps with target_label.
    for b in bboxes:
        bbox_label = (getattr(b, "label", "") or "")
        if _vision_label_overlaps(target, bbox_label):
            return None  # match found (low-conf / non-clickable; allow)
    # No V_n matches the brain's stated label — likely a hallucinated
    # selector pointing at a stale DOM hook. Refuse with the V_n list.
    labels_block = _format_bbox_label_list(vresp)
    return (
        f"[click_selector_label_unseen] You called "
        f"browser_click_selector(selector={sel!r}, target_label={target!r}) "
        f"but vision did NOT emit any V_n with that label in the most "
        f"recent screenshot. On dynamic filter pages the DOM rearranges "
        f"after each click; selectors that worked on the previous page "
        f"often hallucinate against the current DOM. If the element "
        f"genuinely exists, vision should see it.\n"
        f"  Recovery options:\n"
        f"  1. Take a fresh browser_screenshot — vision may now see the "
        f"target. Then click via browser_click_at(vision_index=V_n).\n"
        f"  2. If the element really isn't visible, scroll into view "
        f"first (browser_scroll_until or browser_scroll), then re-screenshot.\n"
        f"  3. browser_look_again with expected_labels=[<your target>] "
        f"forces a careful coverage pass over the current view.\n"
        f"  Ranked V_n labels (top 8) from the most recent screenshot:\n"
        f"{labels_block}\n"
        f"Override: SELECTOR_VISION_ALIGNMENT=0."
    )


_FILTER_LABEL_RE = __import__("re").compile(
    r"\b(filter|facet|amenity|amenities|pairing|pairings|region|regions|"
    r"price\s*range|price|sort|option|sub[- ]options?|chevron|caret|"
    r"category|categories|brand|color|size|rating|stars?)\b",
    __import__("re").IGNORECASE,
)


def _inventory_filters_redundant(
    state: "BrowserSessionState | None",
) -> str | None:
    """Arch v3 fix M — refuse browser_inventory_filters when vision has
    already surfaced enough filter-shaped bboxes.

    The inventory tool scrolls the filter panel server-side to inventory
    every checkbox/radio. On dynamic accordion sites that rearranges the
    DOM and invalidates the V_n indices vision just emitted. If vision
    already shows the filters as bboxes, the brain should click them
    directly via browser_click_at(V_n) — no scroll, no DOM thrash.

    Heuristic (cheap, no LLM):
      - count V_n bboxes whose role is checkbox/radio/input;
      - PLUS V_n's whose label matches `_FILTER_LABEL_RE`;
      - PLUS V_n's whose role_in_scene='target' AND label overlaps a
        TaskBrief constraint canonical_value (so brain-targeted filter
        chips count even if their role is "button").
      - threshold ≥ 4 → refuse with redirect.

    Kill switch: INVENTORY_FILTERS_VISION_GUARD=0.
    """
    if state is None:
        return None
    if os.environ.get("INVENTORY_FILTERS_VISION_GUARD", "1") == "0":
        return None
    vresp = getattr(state, "_last_vision_response", None)
    if vresp is None:
        return None
    bboxes = list(getattr(vresp, "bboxes", None) or [])
    if len(bboxes) < 4:
        return None  # tiny vision pass; allow inventory
    # Constraint-keyword set from the brief (if present).
    brief = getattr(state, "task_brief", None)
    constraint_kws: set[str] = set()
    if brief is not None:
        try:
            for c in brief.constraints:
                cv = (c.canonical_value or c.text or "").strip().lower()
                for tok in cv.replace("-", " ").replace("_", " ").split():
                    if len(tok) >= 4:
                        constraint_kws.add(tok)
        except Exception:
            pass
    filter_indices: list[int] = []
    seen_labels: list[str] = []
    # Mirror the V_n ranking from VisionResponse.as_brain_text so V_n
    # in the refusal text matches what the brain sees.
    def _rank(b: Any) -> tuple[int, int, int, float]:
        role_rank = (
            0 if getattr(b, "role_in_scene", "") == "blocker"
            else (1 if getattr(b, "role_in_scene", "") == "target" else 2)
        )
        return (
            role_rank,
            0 if getattr(b, "intent_relevant", False) else 1,
            0 if getattr(b, "clickable", False) else 1,
            -float(getattr(b, "confidence", 0.0) or 0.0),
        )
    ordered = sorted(bboxes, key=_rank)
    for idx, b in enumerate(ordered, start=1):
        role = (getattr(b, "role", "") or "").lower()
        label = (getattr(b, "label", "") or "")
        label_low = label.lower()
        is_filter_role = role in {"checkbox", "radio", "input"}
        is_filter_label = bool(_FILTER_LABEL_RE.search(label_low))
        is_constraint_target = (
            getattr(b, "role_in_scene", "") == "target"
            and any(kw in label_low for kw in constraint_kws)
        )
        if is_filter_role or is_filter_label or is_constraint_target:
            filter_indices.append(idx)
            seen_labels.append(label[:60])
    if len(filter_indices) < 4:
        return None
    # Build refusal with the matching V_n indices listed.
    sid = getattr(state, "session_id", "") or "<session_id>"
    sample = ", ".join(
        f"V_{i} '{lbl}'" for i, lbl in zip(filter_indices[:6], seen_labels[:6])
    )
    return (
        f"[inventory_filters_redundant] Vision already emitted "
        f"{len(filter_indices)} filter-shaped bboxes in the most recent "
        f"screenshot ({sample}). Calling browser_inventory_filters would "
        f"scroll the panel server-side, which on this site can rearrange "
        f"the DOM and invalidate the V_n indices vision just gave you "
        f"— the brain ends up clicking selectors that no longer match.\n"
        f"REQUIRED: click the matching V_n directly via "
        f"browser_click_at(session_id='{sid}', vision_index=...). If a "
        f"specific filter is BELOW the fold and not in the V_n list, "
        f"browser_scroll first, then browser_screenshot, then click_at.\n"
        f"Override: INVENTORY_FILTERS_VISION_GUARD=0."
    )


def _stale_dom_index_block(
    state: "BrowserSessionState",
    *,
    tool_name: str,
    target_disp: str,
) -> str | None:
    """Shared stale-index guard for any tool that takes a DOM `[N]` index
    or absolute coordinates. Returns a structured `[*_failed:stale_dom_index]`
    string when the brain has made at least `VISION_MAX_AGE_TURNS` mutating
    actions since the last screenshot; otherwise returns None.

    Why a single helper rather than per-tool blocks: keeping the message
    format identical across click/type/select/fix_text means the brain
    learns one recovery pattern (re-screenshot → use selector OR V_n)
    instead of memorizing each tool's variant.

    Arch v4 (Step 2): the env opt-outs `DOM_INDEX_STALE_GUARD` and
    `VISION_MAX_AGE_TURNS` are gone. The gate is mandatory and the
    threshold is fixed at 1 — exactly one mutating action per vision
    epoch. The brain MUST call browser_screenshot to refresh V_n
    indices before a second mutating tool call.
    """
    if state.actions_since_screenshot < 1:
        return None
    state.log_activity(
        f"{tool_name}({target_disp})(STALE_DOM_INDEX)",
        f"actions_since_screenshot={state.actions_since_screenshot}",
    )
    fail_tag = (
        "click_failed" if "click" in tool_name
        else "type_failed" if "type" in tool_name
        else f"{tool_name}_failed"
    )
    return (
        f"[{fail_tag}:stale_dom_index] You have made "
        f"{state.actions_since_screenshot} mutating action(s) since the "
        f"last screenshot. DOM `{target_disp}` no longer reliably refers "
        f"to the element you saw — the tree has shifted. Call "
        f"browser_screenshot first, then "
        f"browser_click_at(vision_index=V_n) using the fresh V_n indices "
        f"from the new screenshot. Do not chain index/coords-based "
        f"actions without a screenshot between."
    )


def _maybe_tighten_to_dom_rect(
    bbox: "Any",
    vis_x0: int,
    vis_y0: int,
    vis_x1: int,
    vis_y1: int,
    image_w: int,
    image_h: int,
    dpr: float,
) -> tuple[int, int, int, int] | None:
    """Arch v4 Phase J — return (x0,y0,x1,y1) tightened to the DOM
    element's bounding rect when bbox.dom_check carries a `rect`.

    Click center always stays within the original vision rect — the
    DOM rect is only used IF its center falls inside the vision rect
    (otherwise the bubble-up resolved a non-target ancestor, e.g. the
    whole row instead of the chevron icon, and tightening would land
    on the wrong element). In that case we return None and the
    original vision rect is used. CSS-pixel rect; no DPR division
    needed (DOM rects are CSS-pixel native).

    Returns None when:
      • dom_check is missing or has no rect
      • the DOM rect's center is outside the vision rect (mismatch)
      • the DOM rect is degenerate (zero or negative area)
    """
    dom_check = getattr(bbox, "dom_check", None)
    if not isinstance(dom_check, dict):
        return None
    rect = dom_check.get("rect")
    if not isinstance(rect, dict):
        return None
    try:
        dx0 = int(rect.get("x0"))
        dy0 = int(rect.get("y0"))
        dx1 = int(rect.get("x1"))
        dy1 = int(rect.get("y1"))
    except (TypeError, ValueError):
        return None
    if dx1 <= dx0 or dy1 <= dy0:
        return None
    # Sanity: DOM rect's center should fall inside the original vision
    # rect (with a small tolerance). Otherwise the bubble-up resolved
    # to a non-target ancestor and tightening would mis-click.
    cx = (dx0 + dx1) // 2
    cy = (dy0 + dy1) // 2
    pad = 4
    if not (
        (vis_x0 - pad) <= cx <= (vis_x1 + pad)
        and (vis_y0 - pad) <= cy <= (vis_y1 + pad)
    ):
        return None
    # Sanity: the DOM rect shouldn't be enormous compared to the vision
    # rect. If it is, the bubble-up grabbed a wrapping container — fall
    # back to the vision rect.
    vis_area = max(1, (vis_x1 - vis_x0) * (vis_y1 - vis_y0))
    dom_area = (dx1 - dx0) * (dy1 - dy0)
    if dom_area > vis_area * 4:
        return None
    return (dx0, dy0, dx1, dy1)


def _resolve_vision_index_by_label(
    state: "Any",
    vision_index: int,
    target_label: str,
    bboxes: list,
) -> tuple[int, str | None]:
    """Arch v4 Phase K — when the brain's `vision_index` doesn't point
    at a bbox whose label matches `target_label` but a different V_M
    does, return the remap. Makes `target_label` the source of truth
    and `vision_index` a tiebreaker hint, eliminating the
    `[click_at_label_mismatch]` wall-of-labels refusal that pushes
    the brain off `click_at` and into `eval` / `run_script`.

    Returns:
      (vision_index_to_use, optional_remap_note). Note is None when
      no remap was needed; populated when a remap fired so the caller
      can prepend it to the click result caption.

    Behavior:
      - Empty target_label / empty bboxes → no-op, returns
        (vision_index, None).
      - V_N's label substring-matches target_label → original is fine,
        no remap.
      - Otherwise call `_find_best_label_match` (Phase F fuzzy matcher:
        substring / Levenshtein ≤ 2 / token-overlap ≥ 0.6).
        - If best == vision_index or best <= 0: no remap. The caller
          will hit the existing label-mismatch refusal naturally.
        - If best != vision_index and within remap budget: remap.
      - Tiebreaker: when multiple V_n match equally, `_find_best_label_
        match` already returns the first one in iteration order. We
        don't override that ordering here — fuzzy matcher's "first
        substring match wins" preserves the bbox-list ranking which
        already encodes intent_relevant + clickable + confidence.
      - Honors CLICK_AT_AUTO_REMAP=0 (kill switch) and the per-session
        CLICK_AT_REMAP_MAX cap to prevent spirals on hallucinated
        labels.
    """
    # Arch v4 (Step 3 follow-up): the auto-remap is OFF by default.
    # Real-run traces showed it silently rewriting the brain's
    # vision_index to a fuzzy-label-matched V_M that turned out to be
    # the wrong target (e.g. a product-card heading instead of a
    # filter chip), producing the "lands on a single bottle instead
    # of filtering" failure mode. The existing `[click_at_label_mismatch]`
    # refusal at line 637 is the right behavior on label mismatch —
    # it forces the brain to re-screenshot and pick a V_n whose label
    # actually matches its declared target. Fuzzy override is too
    # aggressive.
    #
    # The CLICK_AT_AUTO_REMAP env var still exists as an opt-in for
    # users who want to A/B test, but it now defaults to "0" rather
    # than "1".
    if (
        not target_label
        or not bboxes
        or not isinstance(vision_index, int)
        or vision_index <= 0
    ):
        return vision_index, None
    if os.environ.get("CLICK_AT_AUTO_REMAP", "0") != "1":
        return vision_index, None
    try:
        max_remaps = int(os.environ.get("CLICK_AT_REMAP_MAX", "3"))
    except ValueError:
        max_remaps = 3
    if max_remaps <= 0:
        return vision_index, None
    used = int(getattr(state, "_click_at_remap_count", 0) or 0)
    if used >= max_remaps:
        return vision_index, None

    # V_N's current label — check if it already substring-matches.
    needle = target_label.strip().lower()
    if 0 < vision_index <= len(bboxes):
        cur_label = (getattr(bboxes[vision_index - 1], "label", "") or "").strip().lower()
        if needle and cur_label and (needle in cur_label or cur_label in needle):
            return vision_index, None  # original V_N is fine

    # Find the best-matching V_M via Phase F fuzzy matcher.
    best = _find_best_label_match(bboxes, target_label)
    if best <= 0 or best == vision_index:
        return vision_index, None  # caller falls through to existing refusal

    new_label = (getattr(bboxes[best - 1], "label", "") or "").strip()
    state._click_at_remap_count = used + 1
    note = (
        f"[click_at_remap V_{vision_index}→V_{best} "
        f"reason=\"target_label='{target_label[:60]}' matched V_{best} "
        f"label '{new_label[:60]}', not V_{vision_index}\"]"
    )
    return best, note


def _find_best_label_match(bboxes: list, target_label: str) -> int:
    """Arch v4 Move 3 — fuzzy-match `target_label` against the labels
    in a freshly-emitted bbox list. Returns the 1-based index of the
    best match, or -1 when no bbox is similar enough.

    Match rules (any one is enough):
      • exact lowercase substring match (target ⊆ label or label ⊆ target)
      • Levenshtein distance ≤ 2 on the lowercased labels
      • token overlap ≥ 0.6 of the smaller token set, after stripping
        separators and dropping tokens of len < 3 (mirrors
        merge_brief_progress._tokenize)

    The function does not break ties beyond "first match wins" — caller
    can iterate with a different filter if needed.
    """
    if not bboxes or not target_label:
        return -1
    needle = target_label.strip().lower()
    # "Wi-Fi" etc. — also try a separator-stripped form so the
    # substring path handles common label variants.
    needle_compact = needle
    for ch in "-_ /.;:,":
        needle_compact = needle_compact.replace(ch, "")
    needle_toks = _label_tokens(needle)
    best_idx = -1
    best_score = 0.0
    for i, b in enumerate(bboxes, start=1):
        label = (getattr(b, "label", "") or "").strip().lower()
        if not label:
            continue
        label_compact = label
        for ch in "-_ /.;:,":
            label_compact = label_compact.replace(ch, "")
        # Substring exact (raw OR compact). Either path is enough —
        # "Wi-Fi" → "wifi" matches "WiFi filter chip" → "wififilterchip".
        if needle and label and (needle in label or label in needle):
            return i
        if (
            needle_compact and label_compact
            and (needle_compact in label_compact or label_compact in needle_compact)
        ):
            return i
        # Levenshtein distance ≤ 2.
        try:
            if _levenshtein(needle, label) <= 2:
                return i
        except Exception:
            pass
        # Token overlap.
        toks = _label_tokens(label)
        if needle_toks and toks:
            overlap = len(needle_toks & toks)
            denom = max(1, min(len(needle_toks), len(toks)))
            score = overlap / denom
            if score >= 0.6 and score > best_score:
                best_score = score
                best_idx = i
    return best_idx


def _label_tokens(label: str) -> set[str]:
    """Tokenize a bbox label the same way TaskBrief tokenizes canonical
    values. Lowercase, strip separators, drop tokens of length < 3.
    """
    s = (label or "").lower()
    for ch in "-_,/.;:":
        s = s.replace(ch, " ")
    return {t for t in s.split() if len(t) >= 3}


def _levenshtein(a: str, b: str) -> int:
    """Tiny Levenshtein implementation for short strings (label
    matching). O(len(a) * len(b)) memory and time; fine for labels
    capped at ~80 chars."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    # DP row-by-row.
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(
                cur[j - 1] + 1,        # insert
                prev[j] + 1,           # delete
                prev[j - 1] + cost,    # substitute
            )
        prev = cur
    return prev[lb]


def _vision_alternatives_hint(
    state: "BrowserSessionState",
    *,
    exclude_index: int | None = None,
    limit: int = 3,
) -> str:
    """Build a short "try these instead" line from the last vision pass.

    Used on click refusals (low confidence, timeout, blocker, stale
    index) so the brain has concrete alternative targets instead of
    reflexively retrying a neighbour [index]. Returns the empty string
    when there's no usable vision data.
    """
    resp = getattr(state, "_last_vision_response", None)
    if resp is None:
        return ""
    freshness = getattr(resp, "screenshot_freshness", "fresh") or "fresh"
    if freshness == "stale":
        return ""
    bboxes = list(getattr(resp, "bboxes", []) or [])
    if not bboxes:
        return ""
    ranked = sorted(
        enumerate(bboxes, start=1),
        key=lambda pair: (
            0 if getattr(pair[1], "intent_relevant", False) else 1,
            0 if getattr(pair[1], "clickable", False) else 1,
            -float(getattr(pair[1], "confidence", 0.0) or 0.0),
        ),
    )
    picks: list[str] = []
    for i, b in ranked:
        if exclude_index is not None and i == exclude_index:
            continue
        label = (getattr(b, "label", "") or getattr(b, "role", "") or "").strip()
        conf = float(getattr(b, "confidence", 0.0) or 0.0)
        picks.append(f"V{i} (conf {conf:.2f} '{label[:30]}')")
        if len(picks) >= limit:
            break
    if not picks:
        return ""
    tag = "" if freshness == "fresh" else f" [vision {freshness}]"
    return "Higher-value targets from recent vision" + tag + ": " + ", ".join(picks) + "."


async def _push_vision_bboxes(
    session_id: str,
    resp: Any,
    *,
    url: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """POST denormalized bboxes to the SuperBrowser server so live
    viewers can flash them on the screencast overlay.

    Fire-and-forget; never raises into the caller. Brain-text indices
    (`V_n`) come from the same ranking `as_brain_text()` uses so the
    overlay's labels match what the brain sees.

    Extra payload fields (all optional):
      - `url`          — the URL the screenshot was captured on. The UI
                          uses this to drop bboxes whose URL no longer
                          matches the current screencast frame.
      - `freshness`    — `fresh|uncertain|stale` from the vision model.
                          The UI dims the overlay when != "fresh".
      - `latencyMs`    — vision-agent round-trip. Surfaces in the UI as
                          debug info.
    """
    if not session_id:
        return
    iw, ih = getattr(resp, "image_width", 0), getattr(resp, "image_height", 0)
    if iw <= 0 or ih <= 0:
        return
    # Mirror the rank order of as_brain_text() so the overlay's V_n
    # labels line up with what the brain sees in tool output.
    ordered = sorted(
        getattr(resp, "bboxes", []),
        key=lambda b: (
            0 if getattr(b, "intent_relevant", False) else 1,
            0 if getattr(b, "clickable", False) else 1,
            -getattr(b, "confidence", 0.0),
        ),
    )
    payload_bboxes: list[dict[str, Any]] = []
    for i, b in enumerate(ordered, start=1):
        try:
            x0, y0, x1, y1 = b.to_pixels(iw, ih)
        except Exception:
            continue
        payload_bboxes.append({
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "label": getattr(b, "label", "")[:40],
            "role": getattr(b, "role", "other"),
            "clickable": bool(getattr(b, "clickable", False)),
            "intent_relevant": bool(getattr(b, "intent_relevant", False)),
            "index": i,
        })
    freshness = getattr(resp, "screenshot_freshness", "fresh") or "fresh"
    payload: dict[str, Any] = {
        "bboxes": payload_bboxes,
        "imageWidth": iw,
        "imageHeight": ih,
        "url": url or "",
        "freshness": freshness,
    }
    if latency_ms is not None:
        payload["latencyMs"] = int(latency_ms)
    # For T3 sessions, fan out to the local Python event bus so the T3
    # viewer at :3101 can paint the same overlays T1 shows. The TS
    # server POST still runs (404s cleanly for t3-* session IDs) so
    # the non-T3 path stays byte-identical.
    if session_id.startswith("t3-"):
        try:
            from superbrowser_bridge.antibot import t3_event_bus as _bus
            _bus.default().emit_vision_bboxes(
                session_id, payload_bboxes, iw, ih,
                url=url or "",
                freshness=freshness,
                latency_ms=latency_ms,
            )
        except Exception:
            pass
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/vision-bboxes",
                json=payload,
            )
    except Exception:
        # Best-effort — overlay is debug visualization, not load-bearing.
        pass


async def _push_vision_pending(session_id: str) -> None:
    """Tell live viewers a vision pass is in flight. The UI renders a
    transient "vision updating…" indicator; without it the overlay
    silently lags the action by one Gemini round-trip.

    Fire-and-forget. Never raises into the caller.
    """
    if not session_id:
        return
    payload = {"dispatchedAt": int(time.time() * 1000)}
    if session_id.startswith("t3-"):
        try:
            from superbrowser_bridge.antibot import t3_event_bus as _bus
            _bus.default().emit_vision_pending(session_id)
        except Exception:
            pass
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/vision-pending",
                json=payload,
            )
    except Exception:
        pass


async def _decorate_bboxes_with_dom_check(
    session_id: str,
    resp: "Any",
    image_width: int,
    image_height: int,
) -> None:
    """Cross-check each bbox center against DOM truth and decorate.

    For every bbox in `resp`, compute the pixel center, ask the browser
    what's under that point, and if the DOM element disagrees with the
    vision label / role, attach a `dom_check` payload onto the bbox so
    `as_brain_text()` can render `[DOM_DISAGREE …]`. Only flags real
    disagreements — agreements leave `dom_check = None`.

    Heuristic for "disagree":
      * Vision claims an interactive role (button / link / input) but DOM
        bubble-up resolved to nothing interactive (ok=false), OR
      * Vision label has no token in common with DOM text AND vision role
        differs from DOM tag/role.

    Soft-fails: any exception leaves all bboxes un-decorated. The render
    path never relies on `dom_check` being populated.
    """
    bboxes = list(getattr(resp, "bboxes", None) or [])
    if not bboxes:
        return
    points: list[dict[str, int]] = []
    for b in bboxes:
        try:
            x0, y0, x1, y1 = b.to_pixels(image_width, image_height)
        except Exception:
            points.append({"x": 0, "y": 0})
            continue
        points.append({"x": int((x0 + x1) // 2), "y": int((y0 + y1) // 2)})
    try:
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/elements-at-points",
            json={"points": points},
            timeout=8.0,
        )
    except Exception:
        return
    if r.status_code >= 400:
        return
    try:
        results = (r.json() or {}).get("results") or []
    except Exception:
        return
    if not isinstance(results, list) or len(results) != len(bboxes):
        return

    def _tokens(s: str) -> set[str]:
        out: set[str] = set()
        for tok in (s or "").lower().replace("-", " ").split():
            tok = "".join(ch for ch in tok if ch.isalnum())
            if len(tok) >= 3:
                out.add(tok)
        return out

    interactive_roles = {"button", "link", "input", "checkbox", "radio",
                         "textbox", "combobox", "menuitem", "tab", "switch"}

    for b, dr in zip(bboxes, results):
        if not isinstance(dr, dict) or not dr.get("ok"):
            # Vision claimed something interactive at this point but DOM
            # found nothing — that's a disagreement worth surfacing.
            v_role = (getattr(b, "role", "") or "").lower()
            if v_role in interactive_roles:
                b.dom_check = {
                    "tag": "",
                    "role": "",
                    "text": "",
                    "disagree": True,
                }
            continue
        dom_tag = (dr.get("tag") or "").lower()
        dom_role = (dr.get("role") or "").lower()
        dom_text = (dr.get("text") or "")
        # Arch v4 Phase J — DOM rect from the resolved element. When
        # populated, click_at uses this pixel-exact rect instead of
        # the looser vision bbox so the click lands inside the actual
        # text/control area, not on padding.
        dom_rect = dr.get("rect") if isinstance(dr.get("rect"), dict) else None
        v_role = (getattr(b, "role", "") or "").lower()
        v_label = (getattr(b, "label", "") or "")

        # Token overlap between vision label and DOM text — strong agree
        # signal even when the literal strings differ.
        text_overlap = bool(_tokens(v_label) & _tokens(dom_text))
        # Role/tag agreement — many sites tag <button> with role=button
        # explicitly so accept either match path.
        role_agree = (
            v_role == dom_role
            or v_role == dom_tag
            or (v_role in interactive_roles and dom_role in interactive_roles)
            or (v_role in interactive_roles and dom_tag in {"a", "button", "input", "select", "textarea"})
        )
        # Phase J — populate dom_check with rect on AGREEMENT too, so
        # the click can tighten even when vision and DOM agree on
        # role/text. Set disagree=False in that case so as_brain_text
        # doesn't flag it. The render path's disagree-only filter
        # already handles the legacy case.
        if text_overlap or role_agree:
            if dom_rect is not None:
                b.dom_check = {
                    "tag": dom_tag,
                    "role": dom_role,
                    "text": dom_text[:60],
                    "disagree": False,
                    "rect": dom_rect,
                }
            continue
        payload = {
            "tag": dom_tag,
            "role": dom_role,
            "text": dom_text[:60],
            "disagree": True,
        }
        if dom_rect is not None:
            payload["rect"] = dom_rect
        b.dom_check = payload


def _read_image_dims(b64: str) -> tuple[int, int]:
    """Decode (width, height) from a base64-encoded screenshot.

    Used to denormalize Gemini's box_2d coords (in [0, 1000] space)
    against the actual screenshot dimensions before any click is
    dispatched. Returns (0, 0) if PIL isn't available or the bytes
    don't decode — the vision agent then falls back to showing
    normalized coords in brain text.
    """
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        return int(img.width), int(img.height)
    except Exception:
        return 0, 0


async def _request_with_backoff(
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> httpx.Response:
    """POST/GET with jittered backoff on transient errors.

    Retries on 429 (rate-limited) and 503 (service overloaded) with
    delays roughly [1s, 2s, 4s] + small jitter. Honors the server's
    Retry-After header when present.

    This exists because a single run of nanobot fires 200-500 tool calls
    against our TS server — without backoff, a brief burst hitting the
    per-IP rate limiter would surface as a hard 429 that the LLM mis-
    classifies as a permanent outage and refuses to retry.
    """
    # Intercept t3 session URLs — route to the in-process patchright manager
    # instead of the TS server.
    if _is_t3_url(url):
        return await _t3_dispatch_from_http(method, url, json_body=json)

    import random
    last_exc: Exception | None = None
    headers = _auth_headers()
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        for attempt in range(max_retries + 1):
            try:
                if method.upper() == "GET":
                    r = await client.get(url, params=params)
                elif method.upper() == "DELETE":
                    r = await client.delete(url)
                else:
                    r = await client.post(url, json=json)
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                last_exc = e
                if attempt == max_retries:
                    raise
                delay = (2 ** attempt) + random.uniform(0, 0.5)
                print(f"  [net retry {attempt + 1}/{max_retries}] {type(e).__name__}: waiting {delay:.1f}s")
                await asyncio.sleep(delay)
                continue

            # Retryable status codes. Honor Retry-After if present.
            if r.status_code in (429, 503):
                if attempt == max_retries:
                    return r  # caller sees the 429 after all retries
                retry_after = r.headers.get("Retry-After")
                try:
                    retry_after_s = float(retry_after) if retry_after else None
                except ValueError:
                    retry_after_s = None
                delay = retry_after_s if retry_after_s is not None else (2 ** attempt) + random.uniform(0, 0.5)
                # Cap at 10s — Retry-After from a confused server could otherwise block the run.
                delay = min(10.0, delay)
                print(f"  [429 retry {attempt + 1}/{max_retries}] waiting {delay:.1f}s")
                await asyncio.sleep(delay)
                continue

            return r
    # Unreachable: loop either returns or raises.
    if last_exc:
        raise last_exc
    raise RuntimeError("request retry loop exited without return")


async def _fetch_feedback_state() -> dict[str, Any]:
    """Read the TS-side FeedbackBus snapshot over HTTP.

    Non-fatal on any failure — returns {} so callers fall through to the
    normal dispatch path (caller stays the same when the signal is down).
    """
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/feedback")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


# --- Atomic field-correction JS (tier-agnostic) -------------------------------
# Runs inside /evaluate on either t1 (TS server) or t3 (patchright) to do the
# full probe-write-verify cycle in a single synchronous tick. No intermediate
# empty state where a framework re-render could race. Placeholders
# __TARGET_X__ / __TARGET_Y__ / __TARGET_TEXT__ get string-replaced by the
# Python caller with JSON-literal values.
_ATOMIC_FIX_TEXT_JS = """
(() => {
  const x = __TARGET_X__, y = __TARGET_Y__, target = __TARGET_TEXT__;
  const el = document.elementFromPoint(x, y);
  if (!el) return {ok: false, reason: 'no_element'};
  const tag = el.tagName.toLowerCase();
  const isInput = tag === 'input' || tag === 'textarea';
  const isEditable = !!el.isContentEditable;
  if (!isInput && !isEditable) {
    return {ok: false, reason: 'not_input', tag};
  }
  const attrLabel = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || '';
  const attrName = el.name || '';
  const attrAutocomplete = el.getAttribute('autocomplete') || '';
  const attrInputType = isInput ? (el.getAttribute('type') || 'text').toLowerCase() : '';
  if (isInput) {
    if (['file','checkbox','radio','hidden','submit','button',
         'image','reset','range','color'].includes(attrInputType)) {
      return {ok: false, reason: 'non_text_input', tag, input_type: attrInputType};
    }
  }
  const before = isInput ? (el.value || '') : (el.innerText || '');
  if (before === target) {
    return {ok: true, before, after: target, changed: false, tag,
            label: attrLabel, name: attrName, autocomplete: attrAutocomplete,
            input_type: attrInputType};
  }
  try { el.focus(); } catch (_) {}
  try {
    if (isInput) {
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype
                                       : HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && desc.set) {
        desc.set.call(el, target);
        el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: target}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      } else {
        el.value = target;
      }
    } else if (isEditable) {
      el.innerText = target;
      el.dispatchEvent(new InputEvent('input', {bubbles: true}));
    }
  } catch (e) {
    return {ok: false, reason: 'exception', error: String(e).slice(0, 120), before, tag};
  }
  const after = isInput ? (el.value || '') : (el.innerText || '');
  return {ok: after === target, before, after, changed: before !== after, tag,
          label: attrLabel, name: attrName, autocomplete: attrAutocomplete,
          input_type: attrInputType};
})()
"""


def _diff_text(a: str, b: str) -> str:
    """Human-readable diff summary for a → b text change."""
    if a == b:
        return "no change"
    p = 0
    while p < len(a) and p < len(b) and a[p] == b[p]:
        p += 1
    suf = 0
    while (suf < len(a) - p and suf < len(b) - p
           and a[len(a) - 1 - suf] == b[len(b) - 1 - suf]):
        suf += 1
    old_mid = a[p:len(a) - suf]
    new_mid = b[p:len(b) - suf]
    if not old_mid and new_mid:
        return f"inserted {new_mid!r} at position {p}"
    if old_mid and not new_mid:
        return f"removed {old_mid!r} at position {p}"
    return f"replaced {old_mid!r} with {new_mid!r} at position {p}"


def _schedule_vision_prefetch(
    state: "BrowserSessionState", session_id: str,
) -> "asyncio.Task[Any] | None":
    """Fire a background vision_agent.analyze() so the next
    `browser_screenshot` call finds cached bboxes instead of waiting 3-8s
    for Gemini.

    Returns the spawned task so callers can optionally wait for it with
    a budget via `_await_vision_prefetch`. Errors are swallowed inside
    `_run()`; the caller receives `None` when vision is disabled, the
    session is missing, or task creation failed.

    Called from the success path of mutating tools (click, type, scroll,
    navigate). Uses the same cache key as the sync path so the real
    screenshot call hits cache.
    """
    try:
        from vision_agent import (  # type: ignore[import-not-found]
            dom_hash_of,
            get_vision_agent,
            vision_agent_enabled,
        )
        try:
            from vision_agent import (  # type: ignore[import-not-found]
                dom_text_hash_of,
            )
        except ImportError:
            dom_text_hash_of = None  # type: ignore[assignment]
    except ImportError:
        return None
    if not vision_agent_enabled() or get_vision_agent is None:
        return None
    if not session_id:
        return None

    # Announce pending vision so the UI can show a "vision updating…"
    # indicator. Fire-and-forget; failure must not block the prefetch.
    try:
        asyncio.create_task(_push_vision_pending(session_id))
    except Exception:
        pass

    async def _run() -> "Any":
        try:
            r = await _request_with_backoff(
                "GET",
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true", "bounds": "true"},
                timeout=15.0,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            b64 = data.get("screenshot")
            if not b64:
                return None
            agent = get_vision_agent()
            img_w, img_h = _read_image_dims(b64)
            elements = data.get("elements", "")
            dh = dom_hash_of(elements) if dom_hash_of else ""
            # Phase 1.2: viewport-aware secondary cache-key signal so
            # the same page at different scroll positions doesn't reuse
            # bboxes captured for the previous viewport.
            dth = ""
            if dom_text_hash_of is not None:
                try:
                    dth = dom_text_hash_of(
                        elements,
                        scroll_info=data.get("scrollInfo"),
                    )
                except Exception:
                    dth = ""
            dispatched = time.monotonic()
            # DPR: when the viewport runs at deviceScaleFactor > 1 the
            # screenshot is physical-pixel sized. Pass it through so
            # click dispatch can divide to land in CSS pixel space.
            try:
                dpr_val = float(data.get("devicePixelRatio") or 1.0)
            except (TypeError, ValueError):
                dpr_val = 1.0
            resp = await agent.analyze(
                screenshot_b64=b64,
                intent=state._last_intent or "observe page",
                session_id=session_id,
                url=data.get("url", "") or state.current_url,
                dom_hash=dh,
                dom_text_hash=dth,
                previous_summary=state._last_vision_summary or None,
                image_width=img_w,
                image_height=img_h,
                task_instruction=state.task_instruction or None,
            )
            resp.with_image_dims(img_w, img_h, dpr=dpr_val)
            state._last_vision_response = resp
            state._last_vision_summary = resp.summary
            state._last_vision_ts = time.time()
            state._last_vision_url = (data.get("url", "") or state.current_url or "")
            state._last_dom_hash = dh or state._last_dom_hash
            state.vision_calls += 1
            # Push the fresh bboxes to live viewers immediately —
            # without this, overlay only updates on the next
            # screenshot tool call, so the user sees bboxes lag by
            # one full action cycle. Fire-and-forget, non-fatal.
            try:
                latency_ms = int((time.monotonic() - dispatched) * 1000)
                await _push_vision_bboxes(
                    session_id, resp,
                    url=state._last_vision_url,
                    latency_ms=latency_ms,
                )
            except Exception:
                pass
            return resp
        except Exception as exc:
            print(f"  [vision prefetch failed: {exc}]")
            return None

    try:
        new_task = asyncio.create_task(_run())
    except Exception:
        return None
    # Phase 1.1: store the task on state so the NEXT mutating tool call
    # can wait for it via ensure_vision_synced(). Cancel any prior
    # in-flight prefetch — only one is meaningful at a time, and a
    # never-awaited older task is just wasted Gemini latency. Best
    # effort; if cancellation is too late, the older task will write
    # into _last_vision_response then the newer task overwrites, so
    # correctness is preserved.
    prev = state._pending_vision_task
    if prev is not None and not prev.done():
        try:
            prev.cancel()
        except Exception:
            pass
    state._pending_vision_task = new_task
    return new_task


async def _append_fresh_vision(
    task: "asyncio.Task[Any] | None",
    result: str,
    *,
    budget_ms: int | None = None,
    expected_label: str | None = None,
    pre_url: str | None = None,
    pre_dom_hash: str | None = None,
    state: "BrowserSessionState | None" = None,
) -> str:
    """Wait for the prefetched vision pass (up to the budget) and
    append a one-line brain-facing hint to `result` when it arrives.

    The hint lets the planner reason on the post-action screen state
    in the SAME tool response rather than waiting for the next
    screenshot call. If the vision pass didn't finish in time, the
    task keeps running in the background (shielded) and the overlay
    will update on the next push.

    Phase 3.3: when `expected_label` + `pre_url` + `pre_dom_hash` are
    supplied (the click_at tool fills these in), compare the post-click
    vision pass against them. If the label is STILL visible AND the
    URL/DOM didn't change, the click missed — surface
    `[click_missed:label_still_visible]` so the brain stops assuming
    success after a no-op click on a `pointer-events:none` overlay or
    a covered element. This converts a class of silent failures into
    explicit signals.
    """
    resp = await _await_vision_prefetch(task, budget_ms=budget_ms)
    if resp is None:
        return result
    summary = (getattr(resp, "summary", "") or "").strip()
    note_parts: list[str] = []
    if summary:
        note_parts.append(summary[:240])
        freshness = getattr(resp, "screenshot_freshness", "fresh") or "fresh"
        if freshness != "fresh":
            note_parts[-1] = f"{note_parts[-1]} [freshness={freshness}]"
    # Phase 3.3 click-hit verification.
    if expected_label and state is not None:
        try:
            label_lower = expected_label.strip().lower()
            relevant = (getattr(resp, "relevant_text", "") or "").lower()
            current_url = (state.current_url or "")
            same_url = (
                pre_url is not None
                and pre_url == current_url
            )
            same_dom = (
                pre_dom_hash is not None
                and pre_dom_hash == (state._last_dom_hash or "")
            )
            if (
                label_lower
                and label_lower in relevant
                and same_url
                and same_dom
            ):
                # Record the cursor failure so the script-lockout gate
                # counts this as a tried-and-failed cursor strategy.
                try:
                    state.record_cursor_failure(
                        strategy="click_at",
                        target=expected_label[:80],
                        reason="label_still_visible (no URL/DOM delta)",
                    )
                except Exception:
                    pass
                miss_note = (
                    f"[click_missed:label_still_visible expected="
                    f"{expected_label[:40]!r}] The clicked target is "
                    f"still visible on the page and neither the URL "
                    f"nor DOM hash changed — the click likely landed "
                    f"on a covered or pointer-events:none surface. Re-"
                    f"observe vision (pick a fresh V_n) before trying "
                    f"again with a different strategy."
                )
                note_parts.append(miss_note)
        except Exception:
            pass
    if not note_parts:
        return result
    sep = "" if result.endswith("\n") else "\n"
    return f"{result}{sep}[vision] {' | '.join(note_parts)}"


async def _await_vision_required(
    task: "asyncio.Task[Any] | None",
    timeout_ms: int | None = None,
) -> "Any":
    """Phase 1.1 hard sync. Block until `task` resolves or `timeout_ms`
    elapses. Default timeout is VISION_HARD_SYNC_TIMEOUT_MS (8000ms).

    Unlike `_await_vision_prefetch`, this is intended to be called from
    the START of a mutating tool to guarantee fresh state — not from the
    END to opportunistically attach a hint. On timeout the task is left
    running (shielded), but the caller is responsible for surfacing
    that timeout to the brain so it can retry rather than dispatch on
    cached vision.
    """
    if task is None:
        return None
    if task.done():
        try:
            return task.result()
        except Exception:
            return None
    if timeout_ms is None:
        try:
            timeout_ms = int(
                os.environ.get("VISION_HARD_SYNC_TIMEOUT_MS") or "8000"
            )
        except ValueError:
            timeout_ms = 8000
    if timeout_ms <= 0:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.shield(task), timeout=timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None


async def _await_vision_prefetch(
    task: "asyncio.Task[Any] | None",
    budget_ms: int | None = None,
) -> "Any":
    """Wait up to `budget_ms` for a prefetch task to complete.

    Returns the VisionResponse when the task finishes in time, otherwise
    None. On timeout the task is left running (shielded) so the
    background cache write + UI push still happen. Budget defaults to
    VISION_AWAIT_BUDGET_MS env var (fallback 2000 ms); 0 disables the
    wait and returns immediately.
    """
    if task is None:
        return None
    if budget_ms is None:
        try:
            budget_ms = int(
                os.environ.get("VISION_AWAIT_BUDGET_MS") or "2000"
            )
        except ValueError:
            budget_ms = 2000
    if budget_ms <= 0:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.shield(task), timeout=budget_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None


async def _feedback_gate(tool_name: str) -> str | None:
    """Return a deferred-result string when another subsystem owns the
    browser right now (active captcha solve). None means `proceed`.

    Used at the top of mutating tools (click/type/scroll/navigate) to
    keep nanobot from racing the captcha solver — if the gate fires,
    nanobot gets an observation saying "captcha active, retry after 2s"
    and yields instead of firing a click that lands on a solved-then-
    reloaded page.
    """
    state = await _fetch_feedback_state()
    if state.get("captchaActive"):
        strategy = state.get("captchaStrategy") or "unknown"
        msg = (
            f"[feedback] {tool_name} deferred: captcha solve in progress "
            f"(strategy={strategy}). Retry after ~2000ms; do not issue "
            f"more actions until you see the captcha_done signal."
        )
        print(f"  {msg}")
        return msg
    return None

# After this many guard-refused browser_open calls in a single worker run, we
# stop being polite and abort the worker. The guard's text message is clearly
# not getting through to the LLM at this point and continuing would just
# drain the iteration budget on a no-op loop.
BLOCKED_BROWSER_OPEN_HARD_STOP = 3

def _maybe_no_effect_prefix(
    data: Any, tool_name: str, base_caption: str,
    *, session_state: "BrowserSessionState | None" = None,
) -> str:
    """Wrap a mutation-tool caption with a `[no_effect:...]` header when
    the TS bridge reports zero url/DOM/focus delta. The base caption is
    preserved so vision prefetch, cached bboxes and elements text still
    reach the brain — the prefix is what the brain AND the worker hook
    read as a hard failure signal.

    Also records the failure against the per-domain tactic registry via
    `routing.record_tactic_failure` and (on effect) decays any prior
    penalty via `routing.decay_tactic_success`. The penalty data is what
    the next worker's delegation prompt reads to pre-select a better
    tactic on sites that systematically reject a given tool.
    """
    had_effect, reason = _classify_effect(data, tool_name)
    # Tactic-penalty bookkeeping — resolve domain from session state.
    domain = ""
    if session_state is not None:
        try:
            from urllib.parse import urlparse
            url = session_state.current_url or ""
            if url:
                host = (urlparse(url).hostname or "").lower()
                domain = host[4:] if host.startswith("www.") else host
        except Exception:
            domain = ""
    try:
        from superbrowser_bridge.routing import (
            record_tactic_failure, decay_tactic_success,
        )
        if domain:
            if had_effect:
                decay_tactic_success(domain, tool_name)
            else:
                record_tactic_failure(domain, tool_name)
    except Exception:
        pass

    if had_effect:
        # Positive reinforcement — when a cursor tool successfully
        # moves the page state, tag it so the brain's next turn sees
        # "this tactic worked" and stays on the cursor track instead
        # of pivoting to scripts. Also resets the script-usage
        # counter so a cursor-success cleanly breaks any recent
        # script streak.
        if tool_name in _CURSOR_TOOL_NAMES:
            if session_state is not None:
                try:
                    session_state.consecutive_script_calls = 0
                except Exception:
                    pass
            return f"[cursor_success:{tool_name}] {base_caption}"
        return base_caption
    hint = (
        f"[no_effect:{tool_name}] {reason}. The tool dispatched but the "
        f"page didn't respond — no DOM mutation, no URL change, no focus "
        f"change. Do NOT retry the same tool with the same target; try "
        f"ONE OF (in this preference order): "
        f"(a) **browser_screenshot** first — the page may have changed "
        f"under you; re-observe and click the fresh [V_n]; "
        f"(b) **browser_semantic_click(target='<label>')** — atomic "
        f"fresh vision + dispatch, works across React apps; "
        f"(c) browser_click_selector(<css>) — pixel-exact if the target "
        f"has a stable CSS hook; "
        f"(d) browser_rewind_to_checkpoint if the page appears frozen. "
        f"Do NOT synthesize clicks via browser_run_script — JS clicks are "
        f"isTrusted=false and bot-detected; the sandbox will reject them."
    )
    return f"{hint}\n{base_caption}"


# Names of the "cursor-based" interaction tools — click, type, drag
# etc. Used by `_maybe_no_effect_prefix` to tag successful actions
# with `[cursor_success:...]` as positive reinforcement. Excludes
# scripts/eval (which don't move the cursor) and observation tools
# like screenshot/get_markdown.
_CURSOR_TOOL_NAMES = frozenset({
    "browser_click",
    "browser_click_at",
    "browser_click_selector",
    "browser_type",
    "browser_type_at",
    "browser_fix_text_at",
    "browser_keys",
    "browser_drag",
    "browser_drag_slider_until",
    "browser_select",
    "browser_semantic_click",
    "browser_semantic_type",
})


def _is_captcha_intent(intent: str) -> bool:
    """True when `intent`'s bucket is a captcha bucket — these intents
    switch the vision prompt into captcha-tile mode, so they should
    NEVER become sticky (would poison subsequent non-captcha tools).
    Falls back to a substring match when intent_bucket is unavailable."""
    if not intent:
        return False
    try:
        from vision_agent.prompts import intent_bucket as _bucket
        return _bucket(intent) in ("solve_captcha", "solve_captcha_step")
    except Exception:
        s = intent.lower()
        return "captcha" in s or "challenge" in s


def _last_vision_has_captcha_flag(state: "BrowserSessionState") -> bool:
    """True when the last vision response's flags.captcha_present is
    True. Used to decide whether a sticky captcha intent is still
    relevant — if the current page isn't flagged as a captcha, the
    intent is stale and should be dropped."""
    resp = getattr(state, "_last_vision_response", None)
    if resp is None:
        return False
    flags = getattr(resp, "flags", None)
    if flags is None:
        return False
    return bool(getattr(flags, "captcha_present", False))


def _maybe_script_usage_warning(state: "BrowserSessionState") -> str:
    """Return a `[script_warning] ...` string when the brain is
    over-using `browser_run_script` even though cursor alternatives
    are visible, else empty string.

    Trigger: 3+ consecutive script calls, AND the last vision pass
    emitted at least one clickable bbox. The warning lists the top 3
    labels so the brain has concrete semantic targets to reach for.
    """
    try:
        count = int(state.consecutive_script_calls or 0)
    except Exception:
        count = 0
    if count < 3:
        return ""
    resp = getattr(state, "_last_vision_response", None)
    bboxes = getattr(resp, "bboxes", None) if resp is not None else None
    if not bboxes:
        return ""
    labels: list[str] = []
    for b in bboxes[:20]:
        if not getattr(b, "clickable", False):
            continue
        lbl = (getattr(b, "label", "") or "").strip()
        if lbl and lbl not in labels:
            labels.append(lbl)
        if len(labels) >= 3:
            break
    if not labels:
        return ""
    rendered = ", ".join(f"'{lbl}'" for lbl in labels)
    return (
        f"[script_warning] {count} consecutive run_script calls. "
        f"Vision shows clickable bboxes ({rendered}, ...) — these can "
        f"be clicked atomically via `browser_semantic_click(target='<label>')` "
        f"without the WAF-block risk scripts carry. Reserve scripts "
        f"for actions no cursor tool can express."
    )


def _classify_effect(
    data: Any, tool_name: str,
) -> tuple[bool, str]:
    """Inspect a mutation tool's HTTP response for the TS `effect` field.

    Returns `(had_effect, no_effect_reason)`:
      * `had_effect=True, ""` when the TS bridge reports any of
        url_changed / mutation_delta > 0 / focused_changed.
      * `had_effect=False, <human_reason>` when all three are zero —
        the caller prefixes `[no_effect:<tool>] …` onto its return so
        the brain and the worker hook can distinguish "the tool fired
        but nothing happened" from a real success.
      * `had_effect=True, ""` when the `effect` field is missing —
        preserves legacy behavior against an older TS bridge that
        hasn't shipped the effect snapshot yet.

    Used by click / type / keys / drag / type_at / click_at / drag_slider
    at the moment they've got the HTTP response back but haven't built
    the brain-facing caption yet.
    """
    if not isinstance(data, dict):
        return True, ""
    effect = data.get("effect")
    if not isinstance(effect, dict):
        return True, ""  # TS side too old OR path didn't capture effect
    url_changed = bool(effect.get("url_changed"))
    try:
        mutation_delta = int(effect.get("mutation_delta") or 0)
    except (TypeError, ValueError):
        mutation_delta = 0
    focused_changed = bool(effect.get("focused_changed"))
    if url_changed or mutation_delta > 0 or focused_changed:
        return True, ""
    return False, (
        f"{tool_name}: url unchanged, DOM unchanged "
        f"(mutation_delta=0), focus unchanged"
    )


# After this many guard-refused browser_open calls in a single worker run, we
# stop being polite and abort the worker. The guard's text message is clearly
# not getting through to the LLM at this point and continuing would just
# drain the iteration budget on a no-op loop.



class WorkerMustExitError(RuntimeError):
    """Raised from a tool when the worker must terminate immediately.

    Bubbles up through nanobot's tool runner. Carries a reason string the
    orchestrator can surface to the user so the failure mode is observable
    (vs. a silent iteration drain).
    """


@dataclasses.dataclass
class PreplanLock:
    """Arch v4 Move 1 — declarative pre-action plan.

    The brain calls `browser_preplan(...)` to set this lock. The next
    state-change tool consumes it (sets `preplan_lock_consumed=True`).
    The freshness gate then refuses any further state-change until a
    fresh `browser_preplan` re-sets the lock with new declarations.

    Fields capture what the brain claims it's about to do so the
    refusal text can quote the prior intent ("Last preplan: focus=#2
    'WiFi', tool=click_at, target='Wi-Fi filter chip', expected='filter
    chip becomes checked'"). Verification (browser_verify_action +
    reconcile) then checks declared-vs-observed, closing the loop.
    """

    focus_constraint_idx: int = -1
    planned_tool: str = ""
    planned_target_label: str = ""
    planned_target_vision_index: int = -1
    expected_outcome: str = ""
    expected_postcondition: str = "dom_mutated"
    set_at_iter: int = -1


_CAPTCHA_KEYWORDS = (
    "captcha", "recaptcha", "hcaptcha", "turnstile", "cloudflare",
    "verify you are human", "prove you are not a robot", "slider puzzle",
    "click all images", "select all", "drag the", "i'm not a robot",
)
_HARD_DOMAINS = (
    "apartments.com", "zillow.com", "ticketmaster.com", "nytimes.com",
    "linkedin.com", "instagram.com", "facebook.com",
)
# Hash length used to dedupe screenshots when page content changes on same URL.
_CONTENT_HASH_LEN = 500


def _compute_screenshot_budget(
    task_instruction: str = "",
    target_url: str = "",
    is_research: bool = False,
) -> int:
    """Task-complexity-aware screenshot budget.

    Arch v3: base lifted 6 -> 12. The original cap was sized when vision
    was expensive; on Gemini Flash a screenshot costs cents. Multi-
    constraint queries reliably need ≥10 vision passes (filter panel
    open, filter applied, results refreshed, repeat per constraint),
    and the brain spent too much energy in v2 working around budget
    gating. +4 research, +10 captcha-suspect, +8 hard domains. Capped
    at 30 to prevent runaway cost.
    """
    budget = 12
    lower_task = (task_instruction or "").lower()
    lower_url = (target_url or "").lower()
    if is_research:
        budget += 4
    if any(kw in lower_task for kw in _CAPTCHA_KEYWORDS):
        budget += 10
    if any(dom in lower_url for dom in _HARD_DOMAINS):
        budget += 8
    return min(budget, 30)


class BrowserSessionState:
    """Per-instance state for browser session tools.

    Each Nanobot instance that registers browser tools gets its own state.
    This prevents multi-agent setups from sharing globals.
    """

    # Default budget when no task context is supplied. Use
    # configure_budget() to switch to complexity-aware allocation.
    # Arch v3: lifted 6 -> 12 (see _compute_screenshot_budget).
    DEFAULT_SCREENSHOT_BUDGET = 12
    CAPTCHA_MODE_ITERATIONS = 15
    # Arch v4 — session-wide click_at cap raised. The original v3 cap
    # of 3 fired at the 4th legitimate click in a multi-step flow
    # (e.g. Shop → White → Region → United States) and pushed the
    # brain to click_selector / run_script. Real loop detection now
    # lives in check_dead_click (same target + no DOM change refuses)
    # and the Phase D tool ladder ratchet. This cap is a last-resort
    # session-runaway guard, not a per-flow limiter.
    MAX_CLICK_AT = 60

    def __init__(self):
        self.max_screenshots = self.DEFAULT_SCREENSHOT_BUDGET
        self.screenshot_budget = self.max_screenshots
        self.vision_calls = 0
        self.text_calls = 0
        self.start_time = 0.0
        self.sessions_opened = 0
        self.activity_log: list[str] = []
        # Per-session (reset on each browser_open)
        self.step_counter = 0
        self.click_at_count = 0
        self.action_count = 0
        self.actions_since_screenshot = 0

        # Checkpoint & URL tracking
        self.task_id: str = ""
        self.checkpoints: list[dict] = []
        self.current_url: str = ""
        self.best_checkpoint_url: str = ""
        self.url_visit_counts: dict[str, int] = {}
        self.regression_count: int = 0
        # Dedupe key: (normalized_url, hash_of_content) — so a same URL with
        # changed content (e.g., after clicking "Load more") still allows a new
        # screenshot. Populated in mark_screenshot_taken().
        self.screenshotted_keys: set[tuple[str, str]] = set()
        self.last_screenshot_url: str = ""
        self.last_page_content_hash: str = ""
        self.step_history: list[dict] = []
        # Track consecutive click-type tool calls for loop detection
        self.consecutive_click_calls: int = 0
        # Arch v4.3 (Fix B): consecutive observation-only tool calls.
        # Increments on screenshot / get_markdown / eval(read-only) /
        # run_script(mutates=False) / wait_for / scroll_until-without-
        # match. Resets on any successful state-change tool. When
        # >= 3 AND TaskBrief has open constraints, browser_navigate
        # refuses with a "don't escape via navigate" message — the
        # observed failure mode is brain spamming reads then pivoting
        # to a hallucinated URL.
        self.consecutive_non_progress_obs: int = 0
        # Arch v4.4: counter that survives internal vision prefetches.
        # The dom_dirty_since_screenshot flag and actions_since_screenshot
        # counter are both cleared by `build_tool_result_blocks` whenever
        # ANY vision pass succeeds — including the post-click prefetch
        # that fires automatically after click_at. So they don't reflect
        # "the BRAIN has not yet seen a fresh screenshot since the last
        # mutation." This counter does: it increments on each mutating
        # tool's record_step and resets ONLY when BrowserScreenshotTool's
        # execute path completes a real brain-facing screenshot. Used by
        # should_allow_screenshot to bypass dedup after a mutation.
        self.mutations_since_brain_screenshot: int = 0
        # Arch v4.3 (final): hard session-level navigate counter. Any
        # browser_navigate call past cold-start increments this. The
        # in-task cap (default 1) refuses 2nd+ navigates when TaskBrief
        # has open constraints. Once the brain is on a page, it should
        # navigate by clicking visible links (V_n), not by URL
        # construction.
        self.session_navigate_count: int = 0
        # Hard guard against the brain re-clicking a target that produced
        # no DOM change. Cleared by `register_click_attempt` on a fresh
        # target; incremented when the same target re-fires AND the page
        # didn't change since the previous click. The guard refuses to
        # dispatch the (MAX_CONSECUTIVE_SAME_TARGET)th attempt — i.e.
        # one retry is allowed (some JS buttons genuinely need a second
        # click), but the third strike returns a structured error so the
        # brain is forced to switch tactic.
        self.last_click_target: str = ""
        self.last_click_dom_hash: str = ""
        self.consecutive_dead_clicks: int = 0
        self.MAX_CONSECUTIVE_SAME_TARGET = 3
        # Cross-index flail guard. consecutive_dead_clicks only catches
        # REPEATS of the same target. When the brain walks
        # [21]→[22]→[20] with every dispatch timing out, each looks like
        # a fresh target so that guard resets. Track HTTP timeouts
        # independently so two-in-a-row forces a re-screenshot.
        self.consecutive_click_timeouts: int = 0
        self.MAX_CONSECUTIVE_CLICK_TIMEOUTS = 2
        # Telemetry: how many times the TS-side snap-to-interactive
        # failed to find a clickable descendant inside the bbox we sent.
        # Incremented whenever a click response has snap.snapped=false.
        # Reset on every screenshot. Used to surface "vision bboxes are
        # habitually wrapping non-clickable containers" hints.
        self.snap_miss_count: int = 0
        # Active session ID (set by browser_open)
        self.session_id: str = ""
        # How many times has BrowserOpenTool had to refuse a redundant call?
        # The guard returns a stern message on the first few; if the LLM keeps
        # ignoring it past BLOCKED_BROWSER_OPEN_HARD_STOP, we raise to abort
        # the worker rather than silently drain its iteration budget.
        self.blocked_browser_open_count: int = 0

        # Captcha-mode: when a captcha is detected, relax the "no actions since
        # last screenshot" rule for CAPTCHA_MODE_ITERATIONS iterations. The
        # per-round counter `captcha_solve_round` is included in the dedup key
        # so each solve attempt gets its own screenshot allowance — preventing
        # the blanket bypass that used to let the worker screenshot the same
        # unchanged captcha 15 times.
        self.captcha_mode: bool = False
        self.captcha_mode_remaining: int = 0
        self.captcha_solve_round: int = 0

        # Cloudflare-interstitial navigation guard: set by BrowserNavigateTool
        # when the server reports block_class=cloudflare, cleared by a
        # successful browser_solve_captcha or navigation to a different URL.
        # While set, a repeat navigate to the same URL is refused with a
        # structured error telling the agent to solve first.
        self.last_nav_cf_blocked_url: str = ""
        self.nav_solve_called_since_block: bool = False

        # Per-index fingerprint cache. Populated on each /state fetch; read
        # by click/type tools to send `expected_fingerprint` along with the
        # request. Lets the TS side reject clicks that would land on a
        # different element than the LLM originally targeted (stale index).
        self.element_fingerprints: dict[int, str] = {}
        self.captcha_screenshots_used: int = 0
        # Hard cap on screenshots allowed within captcha mode (across rounds).
        # Kicks in only in captcha mode; the normal budget still caps overall.
        self.captcha_mode_screenshot_cap: int = 8

        # Network-layer block: set by browser_open/browser_navigate when the
        # target returns 4xx/5xx. Distinguishes "site blocks automated clients
        # before any page loads" from "page loaded but interaction failed" —
        # completely different failure classes needing different remediations
        # (IP/TLS/proxy vs. selector/timing/captcha).
        self.network_blocked: bool = False
        self.last_network_status: int | None = None

        # Human-in-the-loop handoff: when True, the TS server registers a
        # HumanInputManager for this session AND the captcha orchestrator
        # will fall back to human handoff after auto-strategies exhaust.
        # Orchestrator sets this before register_session_tools(). Default
        # False to preserve no-op behavior for workers that don't opt in.
        self.human_handoff_enabled: bool = False
        # Per-session budget — relayed to the TS server which enforces it.
        # 1 by default, overridable via SUPERBROWSER_MAX_HUMAN_HANDOFFS.
        self.human_handoff_budget: int = 1

        # Domain pinning: when set, BrowserNavigateTool rejects URLs
        # outside this domain (+ subdomains) and a small safe-list
        # (google.com, etc.). Prevents the worker LLM from hallucinating
        # to alternative sites when the target blocks it.
        self.pinned_domain: str = ""

        # Session alias: maps old T1 session IDs to new T3 session IDs
        # after mid-session escalation. Transparent to the LLM — it keeps
        # using the original session_id and we reroute internally.
        self._session_alias: dict[str, str] = {}

        # Vision preprocessor bookkeeping. Populated inside
        # build_tool_result_blocks so tools that don't pass an intent
        # explicitly inherit the last one used — useful when the brain
        # fires a chained sequence (navigate → click → verify) with the
        # same underlying intent.
        self._last_intent: str = ""
        self._last_dom_hash: str = ""
        self._last_vision_summary: str = ""
        # Task context stamped by configure_budget() when the orchestrator
        # spawns a browser worker — piped into the vision prompt so
        # Gemini knows WHAT the user is trying to do before it picks
        # which bboxes to emit.
        self.task_instruction: str = ""
        self.task_target_url: str = ""
        # Cached last VisionResponse so browser_click_at can resolve a
        # vision-index reference (e.g. bbox=V3) back to the original
        # bbox without re-running the vision pass. Reset whenever a new
        # screenshot triggers a fresh vision call.
        # Typed as the actual schema when available, falls back to Any
        # so the import stays lazy for environments without vision_agent.
        try:
            from nanobot.vision_agent.schemas import VisionResponse as _VR  # noqa: F401
            self._last_vision_response: Optional["_VR"] = None  # type: ignore[assignment]
        except Exception:
            self._last_vision_response: Any = None  # type: ignore[assignment]
        # Freshness bookkeeping for the cached vision response. Mutating
        # tools read these to decide whether to piggyback
        # `_last_vision_response.as_brain_text()` onto their text reply —
        # that's the fast path that keeps bboxes in front of the brain
        # without a browser_screenshot round trip.
        self._last_vision_ts: float = 0.0
        self._last_vision_url: str = ""
        # Vision epoch — a frozen snapshot of the vision response that
        # was LAST emitted to the brain as screenshot text. Tools that
        # resolve `vision_index` (click_at, type_at, fix_text_at,
        # drag_slider_until) read this FIRST, falling back to
        # `_last_vision_response` only when the epoch is None. Needed
        # because background vision prefetches overwrite
        # `_last_vision_response` between screenshot-text-emit and
        # click-dispatch — without the epoch, the brain's `V_n`
        # picked from the screenshot resolves against a RENUMBERED
        # prefetch response and lands on the wrong element. Advanced
        # only when `BrowserScreenshotTool` emits fresh vision text;
        # cleared on reset_per_session and on successful navigate.
        self._vision_epoch_id: int = 0
        self._vision_epoch_response: Any = None
        # URL the epoch was captured on. F5 — when `current_url` no
        # longer matches this, the epoch is stale (page implicitly
        # navigated via Enter / form submit / button click) and
        # `vision_for_target_resolution` falls back to the live
        # `_last_vision_response` so the next click resolves V_n
        # against the new page's bbox list, not the prior page's.
        self._vision_epoch_url: str = ""

        # Dead-type guard state. Tracks the last browser_type call so we can
        # reject a second identical type to the same index — the pattern
        # that produces "khulnakhulna, bangladesh" when the LLM misses an
        # autocomplete dropdown and re-types the full phrase.
        self.last_type_index: int = -1
        self.last_type_text: str = ""
        self.last_type_at: float = 0.0

        # Hierarchical perceive-plan-act state. Populated by the
        # screenshot tool after a vision pass; consumed by the click
        # ladder and the browser_plan_next_steps tool.
        #   _last_blockers       — DOM-derived blockers from ui_blockers.detect
        #   _last_action_queue   — ActionQueue from action_planner.plan
        #   _pending_postcondition — Postcondition dict the next click is
        #                            supposed to satisfy; verify_action
        #                            checks it after click_at returns.
        self._last_blockers: list = []  # list[BlockerInfo]
        self._last_action_queue: Any = None  # Optional[ActionQueue]
        self._pending_postcondition: Optional[dict] = None

        # Persistent multi-step task plan (task_plan.py). Populated once
        # per task when the brain calls browser_set_task_plan; consumed
        # by the worker_hook (renders checklist) and verify_action
        # (auto-checks active step's success_criteria after each click /
        # type / navigate). None for single-step or no-plan tasks.
        self.task_plan: Any = None  # Optional[task_plan.TaskPlan]

        # Arch v3 working memory — TaskBrief carries the full original
        # query, structured constraints with status, plan_of_attack, and
        # CoT trail. Survives session restart with full fidelity via
        # handoff_store. Built once at delegation time by
        # orchestrator_tools.delegate_browser_task; readers are
        # build_tool_result_blocks (renders), worker_hook (constraint
        # checklist), verify_action (auto-flips constraints), and the
        # browser_update_task_brief tool (brain-driven revisions).
        self.task_brief: Any = None  # Optional[task_brief.TaskBrief]

        # PageState history — last 5 PageState dicts captured from
        # vision passes. Used by handoff_store to preserve "what the
        # page looked like recently" across worker restart.
        self.vision_state_history: list[dict] = []
        self._VISION_STATE_HISTORY_CAP = 5

        # Failed-tactics ledger — short labels of approaches the brain
        # already tried and abandoned. Surfaced to the help_advisor and
        # to the next worker after restart so we don't re-suggest dead
        # paths.
        self.failed_tactics: list[str] = []
        self._FAILED_TACTICS_CAP = 20

        # Interaction ledger — last N (V_n, outcome) tuples from
        # click_at / type_at / verify_action. Helps the successor
        # worker avoid re-trying dead bboxes after restart.
        self.interaction_ledger: list[dict] = []
        self._INTERACTION_LEDGER_CAP = 20

        # Pre-action context for browser_verify_action. Captured by the
        # action dispatch path before each state-change tool runs;
        # cleared after verify completes.
        self.last_action_context: Any = None  # Optional[ActionContext]

        # Help-advisor budget — the first 3 browser_request_help calls
        # become non-terminal advisor calls; subsequent calls fall
        # through to the legacy "spawn successor worker" path.
        self.help_advisor_calls: int = 0
        self.HELP_ADVISOR_BUDGET = 3

        # Arch v3 fix G — track the most recent chevron click so the
        # next vision pass can detect "click landed but accordion did
        # not expand" and append a failed_tactic for the successor.
        # `_last_chevron_click_label` is set in click_at when the
        # right-edge shift fires; `_last_chevron_click_url` records the
        # URL at click time so the next vision pass can confirm we're
        # still on the same page.
        self._last_chevron_click_label: str = ""
        self._last_chevron_click_url: str = ""

        # URL-hallucination guard: a ring of href values observed in
        # recent state.elements / tool result snapshots. browser_navigate
        # uses it to reject same-domain URLs the brain dreamt up
        # (UUID-suffixed product slugs, made-up paths) that didn't
        # actually appear on any page we've seen. Bounded to keep
        # memory flat across long sessions.
        self.observed_anchor_urls: set[str] = set()
        self._OBSERVED_ANCHOR_CAP = 1024

        # Loop / stagnation detector. Lives on state so both
        # BrowserScreenshotTool (records each screenshot) and
        # BrowserWorkerHook (records each tool call + reads guidance)
        # can reach it. The hook keeps `self._loop` as an alias for
        # backward compat.
        from superbrowser_bridge.loop_detector import LoopDetector as _LD
        self.loop_detector = _LD()

        # Refusal-tool gate: True after any state-change tool returned
        # a non-success caption ([*_failed], [*_timeout], [click_silent],
        # [navigate_unverified], [VERIFY_MISS], [click_selector_failed]).
        # browser_screenshot clears it. browser_request_help and
        # browser_run_script(mutates=true) consult it via
        # must_screenshot_before_giving_up — refuse if the brain is
        # trying to escape without first looking at the page.
        self.last_failure_without_screenshot: bool = False
        self.last_failure_summary: str = ""

        # Arch v3 fix #5 — state-freshness gate. Set True by every
        # state-change tool (click/type/drag/navigate/run_script/eval).
        # Cleared by browser_screenshot / browser_state_check /
        # browser_look_again. Read by `must_screenshot_before_state_change`
        # at the top of each gated tool's execute(): when dirty, the
        # tool refuses with a redirect to browser_screenshot. Eliminates
        # the "click → script → script → URL guess" loop where the
        # brain reaches for JS instead of re-screenshotting after a click
        # rearranged the DOM.
        # Kill switch: STATE_FRESHNESS_GATE=0 disables the refusal.
        # Tracks the last mutating tool's name + a one-line outcome so
        # the refusal message can be specific.
        self.dom_dirty_since_screenshot: bool = False
        self.last_mutating_tool: str = ""
        self.last_mutating_summary: str = ""

        # Arch v4 Move 1 — preplan lock state. Forces a *vision → preplan
        # → action → verify* ritual: the brain physically cannot mutate
        # the page without first declaring (a) which constraint it's
        # attacking, (b) which tool, (c) what it expects to happen. The
        # preplan_lock holds that declaration; it's consumed by the next
        # state-change tool. Backoff prevents deadlocks: ≥3 consecutive
        # refusals auto-yield one free pass with [GATE_BACKOFF] guidance.
        # Kill switches: PREPLAN_GATE=0 disables; PREPLAN_BACKOFF=0 keeps
        # refusing instead of yielding.
        self.preplan_lock: PreplanLock | None = None
        self.preplan_lock_consumed: bool = False
        self.preplan_consecutive_refusals: int = 0
        self.preplan_backoff_just_fired: bool = False

        # Arch v4 Move 4 — per-target tool attempts ledger. Keyed by the
        # focus constraint's canonical_value when a preplan declares
        # focus_constraint_idx; falls back to a token-frozenset of the
        # target_label for unfocused calls (rare, e.g. dismissing a
        # blocker mid-task). Inner dict maps tool tier label →
        # attempt count. Cleared per-target when verify_action returns
        # verdict='succeeded' on that target. The preplan tool consults
        # this ledger to enforce the TOOL LADDER (click_at → selector
        # → run_script → navigate).
        self.tool_attempts: dict[str, dict[str, int]] = {}
        # Tracks the cold-start session signal for Tier-4 navigate
        # enforcement: True only before the first non-navigate state-
        # change fires. After any cursor/script/etc. tool runs we drop
        # the cold-start flag and same-domain navigates start ratcheting.
        self.is_cold_start: bool = True

        # Arch v4 Move 3 — set True while an internal click_at auto-
        # retry is in flight. The freshness + preplan gates skip their
        # double-consume checks under this flag because the retry is
        # logically covered by the brain's original preplan declaration.
        # The retry path resets this to False before returning.
        self._bbox_auto_retry_in_flight: bool = False

        # Arch v4 Phase K — counts how many times click_at auto-remapped
        # the brain's vision_index to a different V_n that matched the
        # target_label better. Capped at CLICK_AT_REMAP_MAX (env, default
        # 3 per session) so a hallucinated label can't spiral. Reset on
        # session restart (via __init__).
        self._click_at_remap_count: int = 0

        # v5: chain-of-thought trail. Optional `narration` param on
        # click_at / click_selector / type_at sets this; worker_hook
        # renders it on the NEXT turn as [last_intended: "..."] and
        # then clears it. Lets the brain compare its prior intent
        # against the actual outcome without code-side substring match.
        self._last_narration: str = ""

        # Phase 3.1: cursor-failure ledger. Each cursor-based interaction
        # tool that returns a failure caption records its strategy here
        # so BrowserRunScriptTool(mutates=true) can refuse to run until
        # at least 2 distinct cursor strategies have been tried and
        # failed. Eliminates the brain's reflex of "click failed → run
        # JS to click" which trips Cloudflare/Akamai isTrusted=false
        # detection. `cursor_failure_strategies` records DISTINCT
        # strategies for the lockout decision; `cursor_failure_records`
        # keeps the last few entries for the prompt-side hint.
        self.cursor_failure_strategies: set[str] = set()
        self.cursor_failure_records: list[dict[str, Any]] = []

        # Phase 2: per-form orchestration. None when no form_begin has
        # been called; populated with a FormFillSession instance while
        # the brain is filling a multi-field form. The worker hook
        # injects a remaining-fields checklist into every tool result
        # while this is set, and form_commit verifies field values
        # before allowing submit.
        self.form_session: Any = None  # Optional[FormFillSession]

        # Most recent filter manifest produced by browser_inventory_filters,
        # cached so browser_form_begin(inventory=true) can resolve user
        # labels to UI labels without a second scan. Shape:
        # {session_id, scope, options: [{label,kind,selector,group,selected}],
        #  captured_at: float}.
        self.last_filter_manifest: dict | None = None

        # Phase 1: hard sync gate. Tracks the most recent prefetch task
        # so the NEXT mutating tool can wait for it before acting on
        # potentially-stale state. Replaces the soft 2s budget that
        # otherwise lets the brain proceed on cached vision when the
        # prefetch hasn't landed.
        self._pending_vision_task: Optional["asyncio.Task[Any]"] = None
        # Wall-clock + brain-turn stamp captured each time the screenshot
        # tool freezes a new vision epoch. Used by the freshness gate to
        # reject clicks against an epoch that's older than
        # VISION_MAX_AGE_TURNS brain turns. Counts MUTATING tool calls
        # rather than wall time so a 30s "thinking" pause doesn't
        # invalidate vision but two intermediate actions do.
        self._vision_epoch_taken_at: float = 0.0
        self._vision_epoch_turn: int = 0
        # Brain turn counter. Incremented at the top of every mutating
        # tool (click/type/click_at/scroll/navigate). Read by the
        # freshness gate to compute epoch age in turns.
        self._brain_turn_counter: int = 0

    @property
    def backend(self) -> str:
        """Tier of the active session. `t3` for patchright (undetected
        Chromium), `t1` for Puppeteer via the TS server. Derived from
        session_id prefix.
        """
        return "t3" if self.session_id.startswith("t3-") else "t1"

    # --- budget configuration ---------------------------------------------

    def configure_budget(
        self,
        task_instruction: str = "",
        target_url: str = "",
        is_research: bool = False,
    ) -> int:
        """Set screenshot budget based on task complexity. Returns new budget."""
        self.max_screenshots = _compute_screenshot_budget(
            task_instruction=task_instruction,
            target_url=target_url,
            is_research=is_research,
        )
        self.screenshot_budget = self.max_screenshots
        # Capture the task context so the vision agent can reason about
        # what the agent is trying to do on this site when picking which
        # regions to bbox. "Book a flight on trip.com" → the vision agent
        # should prioritize departure / destination / date / search
        # button bboxes, not navbar noise.
        # Arch v3: full instruction is preserved (not truncated). The
        # brief carries this verbatim too; this field stays for the
        # legacy code paths that read state.task_instruction directly.
        self.task_instruction = task_instruction or ""
        self.task_target_url = target_url or ""
        return self.max_screenshots

    def enter_captcha_mode(self) -> None:
        """Relax screenshot limits for the next N iterations.

        Called when browser_detect_captcha returns a captcha. Captcha
        solving requires multiple screenshots per round (before drag,
        after drag, verify result) — normal budget would starve it.
        Resets per-round counters so a re-entry doesn't inherit stale
        dedup state from a previous challenge.
        """
        self.captcha_mode = True
        self.captcha_mode_remaining = self.CAPTCHA_MODE_ITERATIONS
        self.captcha_solve_round = 0
        self.captcha_screenshots_used = 0

    def tick_captcha_mode(self) -> None:
        """Decrement captcha_mode counter. Call once per agent iteration."""
        if not self.captcha_mode:
            return
        self.captcha_mode_remaining -= 1
        if self.captcha_mode_remaining <= 0:
            self.captcha_mode = False
            self.captcha_mode_remaining = 0

    def resolve_session_id(self, session_id: str) -> str:
        """Resolve a session ID through the alias chain (T1→T3 escalation)."""
        return self._session_alias.get(session_id, session_id)

    def reset_per_session(self):
        """Reset per-session counters. Budget is NOT reset."""
        self.step_counter = 0
        self.click_at_count = 0
        self.action_count = 0
        self.actions_since_screenshot = 0
        # Epoch from a prior session is meaningless for the new one.
        self._vision_epoch_response = None
        self._vision_epoch_id = 0
        self._vision_epoch_url = ""
        self._vision_epoch_taken_at = 0.0
        self._vision_epoch_turn = 0
        # Drop any in-flight prefetch from the previous session — the
        # task references the old session_id and would write into
        # _last_vision_response under a context the new session doesn't
        # care about.
        if self._pending_vision_task is not None and not self._pending_vision_task.done():
            try:
                self._pending_vision_task.cancel()
            except Exception:
                pass
        self._pending_vision_task = None
        self._brain_turn_counter = 0

    def freeze_vision_epoch(self) -> None:
        """Snapshot `_last_vision_response` as the current epoch.

        Called by `BrowserScreenshotTool` right after it emits the
        vision bbox text to the brain. Subsequent `browser_click_at(
        vision_index=V_n)` / `browser_type_at` calls resolve `V_n`
        against THIS snapshot, not against the live
        `_last_vision_response` (which a background prefetch may have
        overwritten between screenshot-text-emit and click-dispatch —
        that's the V_n drift bug).

        Also captures the URL so `vision_for_target_resolution` can
        invalidate the epoch when the page implicitly navigates
        (browser_keys(Enter), button-clicks-that-submit-a-form, etc.)
        — `state.current_url` will no longer match `_vision_epoch_url`
        and the epoch falls through to the live response.
        """
        self._vision_epoch_response = self._last_vision_response
        self._vision_epoch_url = self._last_vision_url or self.current_url or ""
        self._vision_epoch_id += 1
        # Phase 1.3: stamp the epoch with wall + turn counter so the
        # freshness gate can reject clicks against an epoch that's older
        # than VISION_MAX_AGE_TURNS mutating actions ago. Reset epoch_turn
        # to current counter — the brain just saw this screenshot, so
        # zero turns elapsed since the snapshot it's reasoning on.
        self._vision_epoch_taken_at = time.time()
        self._vision_epoch_turn = self._brain_turn_counter

    def vision_for_target_resolution(self) -> Any:
        """Return the vision response V-index readers (click_at /
        type_at / fix_text_at / the slider family) should resolve
        against. Prefers the frozen epoch; falls back to the live
        `_last_vision_response` when:
          - no epoch has been captured yet (first turn / mocked test);
          - the epoch's URL no longer matches `current_url` (the page
            implicitly navigated since the brain saw the screenshot —
            F5 fix; otherwise the brain's V_n picked from page A
            resolves against page A's bbox list while the click lands
            on page B, with bboxes that no longer apply).
        """
        if self._vision_epoch_response is not None:
            epoch_url = self._normalize_url(self._vision_epoch_url or "")
            current_url = self._normalize_url(self.current_url or "")
            if epoch_url and current_url and epoch_url != current_url:
                # Page changed. Epoch is stale. Live response is the
                # post-mutation prefetch and matches the current page.
                return self._last_vision_response
            return self._vision_epoch_response
        return self._last_vision_response

    def record_cursor_failure(
        self, *, strategy: str, target: str, reason: str,
    ) -> None:
        """Phase 3.1: log that a cursor-based interaction returned a
        non-success caption. Bounded ledger (last 12 entries) with a
        distinct-strategies set used by the script lockout.
        """
        if not strategy:
            return
        self.cursor_failure_strategies.add(strategy)
        self.cursor_failure_records.append({
            "strategy": strategy,
            "target": target[:120] if target else "",
            "reason": reason[:120] if reason else "",
            "turn": self._brain_turn_counter,
        })
        if len(self.cursor_failure_records) > 12:
            self.cursor_failure_records = self.cursor_failure_records[-12:]

    def cursor_lockout_summary(self) -> str:
        """Render the current cursor-failure ledger for prompt hints."""
        if not self.cursor_failure_records:
            return ""
        last = self.cursor_failure_records[-3:]
        rows = [
            f"  - {r['strategy']}({r['target']!r}): {r['reason']}"
            for r in last
        ]
        return "\n".join(rows)

    async def ensure_vision_synced(self, *, reason: str = "pre_action") -> "str | None":
        """Phase 1.1 hard sync gate. Block until the most recent vision
        prefetch lands. Returns None on success (caller proceeds), or a
        structured error string the caller should return as its tool
        result so the brain re-tries on a fresh state.

        Skipped entirely when VISION_HARD_SYNC=0 — preserves the legacy
        soft-budget behavior for rollback.

        Page-type-aware timeout: if VISION_HARD_SYNC_PAGE_TYPE_OVERRIDES
        is a JSON dict and the last vision response's page_type matches
        a key, that timeout (ms) is used instead of the global default.
        Useful for slow form/search pages where 8s isn't enough.

        Arch v4 (Step 2): the `VISION_HARD_SYNC=0` rollback toggle is
        gone — hard-sync is mandatory. Disabling it produced the
        "stale prefetch overwrites live epoch" hallucination that
        appeared after step ~5 in v3.
        """
        task = self._pending_vision_task
        if task is None or task.done():
            return None
        timeout_ms: int | None = None
        try:
            overrides_raw = os.environ.get("VISION_HARD_SYNC_PAGE_TYPE_OVERRIDES")
            if overrides_raw:
                overrides = json.loads(overrides_raw)
                last_resp = self._last_vision_response
                page_type = getattr(last_resp, "page_type", "") if last_resp else ""
                if page_type and page_type in overrides:
                    timeout_ms = int(overrides[page_type])
        except Exception:
            timeout_ms = None
        await _await_vision_required(task, timeout_ms=timeout_ms)
        if not task.done():
            return (
                f"[vision_unavailable:{reason}] Vision prefetch from the "
                f"previous action did not land in time. Re-issue the same "
                f"tool call — the prefetch is still running and will "
                f"complete shortly. Do NOT proceed on stale vision."
            )
        return None

    def init_if_needed(self):
        if self.start_time == 0.0:
            self.start_time = time.time()

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize URL for comparison (strip trailing slash, fragment)."""
        if not url:
            return ""
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, ""))

    def record_url(self, url: str) -> None:
        """Track a URL visit. Updates current_url and visit counts."""
        if not url:
            return
        norm = self._normalize_url(url)
        self.current_url = url
        self.url_visit_counts[norm] = self.url_visit_counts.get(norm, 0) + 1
        # Arch v3 fix #2: every URL change is a chance for the brief to
        # auto-flip filter/attribute/ordering constraints encoded in the
        # path or query string. Cheap (regex+token match), no LLM.
        try:
            brief = getattr(self, "task_brief", None)
            if brief is not None:
                from superbrowser_bridge.task_brief import reconcile_from_url
                reconcile_from_url(brief, url)
        except Exception:
            pass

    def record_checkpoint(self, url: str, title: str, action: str) -> None:
        """Record a progress checkpoint (successful meaningful step)."""
        self.checkpoints.append({
            "url": url, "title": title, "action": action,
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        if url and url != self.best_checkpoint_url:
            self.best_checkpoint_url = url

    def is_regression(self, url: str) -> bool:
        """Check if navigating to url is going backward."""
        if not url or not self.best_checkpoint_url:
            return False
        norm = self._normalize_url(url)
        best_norm = self._normalize_url(self.best_checkpoint_url)
        # Regression = revisiting an earlier URL when we've been deeper
        if norm == best_norm:
            return False
        return self.url_visit_counts.get(norm, 0) > 0

    def should_allow_screenshot(
        self,
        url: str,
        content_hash: str = "",
        intent: str = "",
    ) -> tuple[bool, str]:
        """Check if a screenshot should be allowed. Returns (allowed, reason).

        Captcha mode no longer blanket-bypasses dedup. Instead:
          - actions_since_screenshot check is relaxed (a captcha round might
            genuinely need multiple vision calls between tool actions)
          - captcha_solve_round is folded into the dedup key so each solve
            attempt gets its own allowance
          - a hard cap (captcha_mode_screenshot_cap) prevents runaway burn
            even if vision keeps failing

        Arch v3: explicit verify_action / state_check intent bypasses the
        actions_since_screenshot==0 rejection. Those tools are themselves
        observation hops — gating them on "did you click since last
        screenshot" forces the brain to act blindly.
        """
        if self.screenshot_budget <= 0:
            return False, "[Screenshot budget exhausted] Use browser_get_markdown or browser_eval instead."
        if self.captcha_mode:
            if self.captcha_screenshots_used >= self.captcha_mode_screenshot_cap:
                return False, (
                    f"[Captcha-mode screenshot cap hit ({self.captcha_mode_screenshot_cap}). "
                    "The vision-based solve isn't converging. Call browser_ask_user for human help, "
                    "or browser_request_help to hand off to a fresh tactic.]"
                )
            norm = self._normalize_url(url)
            key = (norm, f"cap-round-{self.captcha_solve_round}:{content_hash or ''}")
            if norm and key in self.screenshotted_keys:
                return False, (
                    "[Captcha screenshot already taken for this solve round with no change. "
                    "Call browser_solve_captcha or browser_click a tile — don't re-screenshot the same state.]"
                )
            return True, ""
        # Arch v4.5: ALL bypasses fire FIRST. The previous order put
        # the legacy `actions_since_screenshot == 0` gate above the
        # mutations bypass, so after a successful click_at (which
        # increments mutations_since_brain_screenshot but NOT
        # actions_since_screenshot — see BrowserClickAtTool), the gate
        # refused the screenshot with "[No actions since last screenshot]"
        # before the bypass could fire. The brain followed that hint
        # and pivoted to get_markdown / eval / done — observed in the
        # wineaccess real-run trace.
        #
        # `mutations_since_brain_screenshot` is the authoritative
        # signal: it survives internal vision prefetches (which clear
        # dom_dirty + actions_since_screenshot) and only resets when
        # BrowserScreenshotTool.execute completes a real brain-facing
        # screenshot. If it's > 0, the brain has acted but hasn't seen
        # the result yet — the screenshot must be allowed.
        if int(getattr(self, "mutations_since_brain_screenshot", 0) or 0) > 0:
            return True, ""
        # Arch v3: verify-action / state-check intents are explicit
        # observation hops — exempt from the actions_since_screenshot==0
        # rejection so the brain can verify state without first making a
        # dummy mutation.
        intent_lower = (intent or "").lower()
        is_verify_intent = (
            "verify_action" in intent_lower
            or "state_check" in intent_lower
            or "verify action" in intent_lower
            or "state check" in intent_lower
        )
        if self.actions_since_screenshot == 0 and not is_verify_intent:
            return False, "[No actions since last screenshot — reuse previous. Use browser_get_markdown to re-read content.]"
        norm = self._normalize_url(url)
        # Dedupe on (url, content_hash) — if content changed since last shot
        # at this URL, allow a fresh screenshot.
        key = (norm, content_hash or "")
        if norm and key in self.screenshotted_keys:
            return False, "[Screenshot already exists for this URL + content. Use browser_get_markdown or browser_eval to read page state instead.]"
        return True, ""

    def mark_screenshot_taken(self, url: str, content_hash: str = "") -> None:
        """Record that a screenshot was taken for (url, content_hash).

        In captcha mode the key includes the current solve round so each
        solve attempt gets its own dedup allowance.
        """
        norm = self._normalize_url(url)
        if not norm:
            return
        if self.captcha_mode:
            self.captcha_screenshots_used += 1
            self.screenshotted_keys.add(
                (norm, f"cap-round-{self.captcha_solve_round}:{content_hash or ''}")
            )
        else:
            self.screenshotted_keys.add((norm, content_hash or ""))
        self.last_screenshot_url = norm
        self.last_page_content_hash = content_hash or ""
        # Feed the loop detector's stale-screenshot streak counter and
        # clear the impasse-tool failure flag — the brain has now looked
        # at the page since whatever last failed.
        try:
            self.loop_detector.record_screenshot(norm, content_hash or "")
        except Exception:
            pass
        self.clear_action_failed()

    @staticmethod
    def hash_page_content(text: str, scroll_y: int | None = None) -> str:
        """Structural fingerprint of a page for screenshot dedup.

        Replaces the old "SHA1 of first 500 chars" scheme, which was both
        insensitive to real changes (below-fold content, lazy loads) and
        over-sensitive to benign re-renders (React class-hash churn).

        Input is the `clickableElementsToString()` payload returned by the
        TS server — one line per interactive element, format roughly:
          `[0]<button aria-label="Sign in">Sign in</button>`

        Fingerprint inputs:
          - count of interactive elements (line count)
          - tag-name histogram (button/input/a/…)
          - top-N aria-label / placeholder / name values, normalized
          - scroll-Y bucketed to 100px when supplied

        All inputs are concatenated into a deterministic canonical string,
        then SHA1-hashed and truncated. Bucketing scroll keeps tiny scroll
        jitters from invalidating dedup while still distinguishing real
        scroll positions.
        """
        if not text:
            return ""
        import hashlib
        import re

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        count = len(lines)

        # Tag histogram — parse `<tag` at the start of each element snippet.
        tag_counts: dict[str, int] = {}
        attr_pat = re.compile(r'(?:aria-label|placeholder|name)="([^"]+)"')
        attr_samples: list[str] = []
        for ln in lines[:40]:  # cap at 40 to keep bounded
            m = re.search(r"<([a-zA-Z][a-zA-Z0-9]*)", ln)
            if m:
                tag = m.group(1).lower()
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
            # Grab first meaningful attribute to anchor identity.
            a = attr_pat.search(ln)
            if a:
                val = a.group(1).strip().lower()
                # Normalize whitespace + drop digits (React IDs, counters).
                val = re.sub(r"\s+", " ", val)
                val = re.sub(r"\d+", "#", val)
                attr_samples.append(val[:40])

        hist = ",".join(f"{t}:{n}" for t, n in sorted(tag_counts.items()))
        # Use the first 20 normalized attributes — enough to differentiate
        # pages, few enough that a single added menu item doesn't flip the hash.
        attrs = "|".join(attr_samples[:20])
        scroll_bucket = ""
        if scroll_y is not None:
            scroll_bucket = f"s{int(scroll_y) // 100}"

        canonical = f"n={count}|h={hist}|a={attrs}|{scroll_bucket}"
        return hashlib.sha1(canonical.encode("utf-8", errors="ignore")).hexdigest()[:12]

    # Tools whose execution mutates the page DOM. Set the freshness
    # dirty flag in `record_step` so the next state-change tool gets
    # gated by `must_screenshot_before_state_change`.
    #
    # browser_navigate / browser_open are NOT included — they're
    # navigation-class tools that already return vision results in their
    # success path (clearing the flag implicitly), and they're often the
    # legitimate recovery move when the brain wants to start fresh.
    _MUTATING_TOOLS: frozenset = frozenset({
        "browser_click", "browser_click_at", "browser_click_selector",
        "browser_type", "browser_type_at", "browser_fix_text_at",
        "browser_keys", "browser_drag", "browser_drag_selectors",
        "browser_drag_path", "browser_drag_slider_until",
        "browser_set_slider", "browser_set_slider_at",
        "browser_select", "browser_select_option",
        "browser_run_script", "browser_eval",
        "browser_solve_captcha",
    })

    def record_step(self, tool_name: str, args_summary: str, result_summary: str) -> None:
        """Record a step in the structured step history."""
        self.step_history.append({
            "tool": tool_name,
            "args": args_summary,
            "result": result_summary[:200],
            "url": self.current_url,
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        # Arch v3 fix #5: tools in _MUTATING_TOOLS dirty the freshness
        # flag automatically — single chokepoint, no per-tool wiring.
        if tool_name in self._MUTATING_TOOLS:
            self.dom_dirty_since_screenshot = True
            self.last_mutating_tool = tool_name
            self.last_mutating_summary = (result_summary or "")[:160]
            # Arch v4.4: also bump the brain-screenshot mutation counter.
            # Unlike dom_dirty (which gets cleared by internal vision
            # prefetches), this only resets on explicit browser_screenshot.
            # Skip if the tool refused (we detect refusal markers in the
            # result) so failed clicks don't trigger dedup-bypass.
            _refused_marker = bool(
                result_summary
                and any(
                    marker in result_summary
                    for marker in (
                        "BLOCKED:", "_failed:", "_refused",
                        "_unverified", "stale_dom_index",
                    )
                )
            )
            if not _refused_marker:
                self.mutations_since_brain_screenshot += 1
        # Arch v4.3 (Fix B): observation-spam counter. Increments on
        # observation tools, resets on mutating tools that yielded a
        # real result (not refusals / errors). Read by BrowserNavigateTool
        # to refuse "escape via navigate after observation spam."
        _OBSERVATION_TOOLS = (
            "browser_screenshot", "browser_get_markdown",
            "browser_eval", "browser_run_script",
            "browser_wait_for", "browser_state_check",
            "browser_look_again", "browser_image_region",
            "browser_get_rect", "browser_form_status",
            "browser_detect_captcha", "browser_captcha_screenshot",
            "browser_dialog",
        )
        if tool_name in _OBSERVATION_TOOLS:
            # scroll_until that DID find the target counts as progress;
            # tracked separately below since it's listed as mutating.
            self.consecutive_non_progress_obs += 1
        elif tool_name in self._MUTATING_TOOLS:
            # Refusals / errors should NOT reset the counter — the brain
            # didn't actually advance state. We detect by sniffing the
            # result for refusal markers.
            _refused = bool(
                result_summary
                and any(
                    marker in result_summary
                    for marker in (
                        "BLOCKED:", "_failed:", "_refused",
                        "_unverified", "stale_dom_index",
                    )
                )
            )
            if not _refused:
                self.consecutive_non_progress_obs = 0
            # Arch v4 Move 4: drop cold-start once any non-navigate
            # mutating tool fires. Tier-4 navigate ratcheting kicks in
            # from this point. Cold-start navigates (browser_open,
            # the very first browser_navigate of a session) bypass the
            # ladder.
            if tool_name not in {"browser_open", "browser_navigate"}:
                self.is_cold_start = False
            # Arch v4 Move 4: bookkeep the attempt against the current
            # preplan_lock's target so ladder ratchet logic has data.
            try:
                self._record_tool_attempt(tool_name, result_summary)
            except Exception:
                pass

    # ── Arch v4 Move 4 — tool ladder ratchet helpers ─────────────
    # Tier label maps. We intentionally keep a small fixed set so the
    # ledger stays interpretable. Sub-tools (e.g. type_at, drag_at) are
    # rolled up into Tier 1 / Tier 2 by family.
    _TIER1_TOOLS = frozenset({
        "browser_click_at", "browser_type_at", "browser_fix_text_at",
        "browser_set_slider_at", "browser_drag", "browser_drag_path",
        "browser_keys", "browser_scroll", "browser_scroll_until",
    })
    _TIER2_TOOLS = frozenset({
        "browser_click_selector", "browser_type",
        "browser_select", "browser_select_option",
        "browser_set_slider", "browser_drag_selectors",
        "browser_drag_slider_until",
    })
    _TIER3_TOOLS = frozenset({"browser_run_script", "browser_eval"})
    _TIER4_TOOLS = frozenset({"browser_navigate"})

    def _tier_for_tool(self, tool_name: str) -> int:
        if tool_name in self._TIER1_TOOLS:
            return 1
        if tool_name in self._TIER2_TOOLS:
            return 2
        if tool_name in self._TIER3_TOOLS:
            return 3
        if tool_name in self._TIER4_TOOLS:
            return 4
        return 0  # uncategorized — observation tools, etc.

    def _ledger_key_for_lock(
        self, lock: "PreplanLock | None", fallback_label: str = "",
    ) -> str:
        """Derive the per-target ledger key. Prefer the focus
        constraint's canonical_value (stable identity), fall back to a
        canonical-token fingerprint of the target label so vision
        label drift ('Wi-Fi' vs 'WiFi') doesn't fragment the ledger.
        """
        brief = getattr(self, "task_brief", None)
        if lock is not None and brief is not None:
            idx = lock.focus_constraint_idx
            if 0 <= idx < len(brief.constraints):
                cv = (brief.constraints[idx].canonical_value or "").strip().lower()
                if cv:
                    return f"constraint:{cv}"
        label_src = (
            (lock.planned_target_label if lock is not None else "")
            or fallback_label
            or ""
        ).lower()
        if not label_src:
            return "label:<unscoped>"
        # Lowercase + strip separators + drop tokens <3 chars. Mirrors
        # task_brief.merge_brief_progress._tokenize, so vision label
        # drift collapses to one ledger key.
        for ch in "-_,/.;:":
            label_src = label_src.replace(ch, " ")
        toks = sorted(t for t in label_src.split() if len(t) >= 3)
        if not toks:
            return "label:<unscoped>"
        return "label:" + "+".join(toks)

    def _record_tool_attempt(
        self, tool_name: str, result_summary: str,
    ) -> None:
        """Bookkeep an attempt on the per-target ledger. Called from
        record_step for every mutating tool. Tier 0 (observation,
        navigate to a different domain) is skipped — only ladder tiers
        contribute.
        """
        tier = self._tier_for_tool(tool_name)
        if tier == 0:
            return
        lock = self.preplan_lock
        key = self._ledger_key_for_lock(lock)
        rec = self.tool_attempts.setdefault(key, {})
        rec[f"tier{tier}"] = rec.get(f"tier{tier}", 0) + 1
        # Track failures separately so the ratchet only escalates after
        # genuine failure (not after a successful click that we count
        # as Tier 1 attempted). A failing result_summary contains one
        # of the canonical failure markers; otherwise treat as success.
        result_str = str(result_summary or "")
        is_fail = any(
            m in result_str for m in (
                "_failed", "_timeout", "click_silent",
                "VERIFY_MISS", "selector_ambiguous", "BLOCKED:",
                "NETWORK_BLOCKED", "navigate_unverified",
            )
        )
        if is_fail:
            rec[f"tier{tier}_failed"] = rec.get(f"tier{tier}_failed", 0) + 1

    def clear_tool_attempts_for_lock(
        self, lock: "PreplanLock | None" = None,
    ) -> None:
        """Reset the ledger for a specific target — called when
        verify_action returns verdict='succeeded' so subsequent attacks
        on a *different* target start fresh on Tier 1.
        """
        key = self._ledger_key_for_lock(lock or self.preplan_lock)
        if key in self.tool_attempts:
            del self.tool_attempts[key]

    def mark_action_failed(self, summary: str) -> None:
        """Set the failure flag consumed by the impasse-tool refusal.

        Called by every state-change tool that returns a non-success
        caption ([*_failed], [*_timeout], [click_silent],
        [navigate_unverified], [VERIFY_MISS]). browser_screenshot clears
        the flag. While set, browser_request_help and
        browser_run_script(mutates=true) refuse — the brain MUST take
        another screenshot before giving up.
        """
        self.last_failure_without_screenshot = True
        self.last_failure_summary = (summary or "")[:160]

    def clear_action_failed(self) -> None:
        """Clear the failure flag — called by browser_screenshot."""
        self.last_failure_without_screenshot = False
        self.last_failure_summary = ""

    def mark_dom_dirty(self, tool_name: str, summary: str = "") -> None:
        """Record that a mutating tool just ran. Sets the freshness flag
        consumed by `must_screenshot_before_state_change`. Cleared by
        browser_screenshot / browser_state_check / browser_look_again.
        """
        self.dom_dirty_since_screenshot = True
        self.last_mutating_tool = (tool_name or "")[:48]
        self.last_mutating_summary = (summary or "")[:160]

    def clear_dom_dirty(self) -> None:
        """Clear the freshness flag — called by observation tools."""
        self.dom_dirty_since_screenshot = False
        self.last_mutating_tool = ""
        self.last_mutating_summary = ""

    def must_screenshot_before_state_change(self, tool_name: str) -> str | None:
        """Arch v3 fix #5 — refuses a state-change tool when the prior
        action was also state-changing and no screenshot ran in between.
        Returns the refusal text on block, None on allow.

        The brain's dominant failure mode is: click → DOM rearranges →
        brain reaches for `browser_run_script` / `browser_eval` /
        `browser_click_selector` to "guess" the new DOM, instead of
        re-screenshotting. Forcing a screenshot first is what makes V_n
        indices fresh and keeps the click ladder usable.

        Exempt tools (always allowed):
          - browser_screenshot, browser_state_check, browser_look_again,
            browser_get_markdown, browser_image_region — observations
          - browser_navigate — sometimes the recovery move
          - browser_close, browser_request_help, browser_ask_user — exits
          - browser_set_task_plan / browser_plan_* / browser_update_task_brief
            — pure state updates on the worker, no DOM impact

        Kill switch: STATE_FRESHNESS_GATE=0 (default 1).
        """
        if os.environ.get("STATE_FRESHNESS_GATE", "1") == "0":
            return None
        # Arch v4 Move 3 — internal auto-retry is covered by the
        # outer call's gate clearance. Skip both gates here.
        if getattr(self, "_bbox_auto_retry_in_flight", False):
            return None
        # The list of GATED tools. Anything mutating the page goes here.
        gated = {
            "browser_click", "browser_click_at", "browser_click_selector",
            "browser_type", "browser_type_at", "browser_fix_text_at",
            "browser_keys", "browser_drag", "browser_drag_selectors",
            "browser_drag_path", "browser_drag_slider_until",
            "browser_set_slider", "browser_set_slider_at",
            "browser_select", "browser_select_option",
            "browser_run_script", "browser_eval",
            "browser_solve_captcha",
        }
        if tool_name not in gated:
            return None
        # Arch v3 fix C — gate-pass marks dirty IMMEDIATELY. Same-turn
        # parallel mutating batches (Anthropic API can return multiple
        # tool_use blocks per turn) used to slip past the gate because
        # `record_step` (which sets dirty) ran at the END of execute().
        # By dirtying on gate-pass we guarantee the second parallel call's
        # gate check sees dirty=True even if the first call hasn't
        # finished record_step yet. Idempotent with record_step's set.
        if not self.dom_dirty_since_screenshot:
            self.dom_dirty_since_screenshot = True
            self.last_mutating_tool = tool_name
            self.last_mutating_summary = "in_flight"
            # Arch v4 Move 1: screenshot-freshness clear; now enforce
            # preplan-lock. Returns None to allow, refusal text to block.
            return self._check_preplan_lock(tool_name)
        prev_tool = self.last_mutating_tool or "(prior action)"
        prev_outcome = self.last_mutating_summary or ""
        outcome_bit = f" Outcome: {prev_outcome[:120]}" if prev_outcome else ""
        sid = self.session_id or "<session_id>"
        return (
            f"[refused: state is dirty — screenshot before next action]\n"
            f"You just ran {prev_tool} which mutated the page; the V_n "
            f"indices and DOM you have are STALE. Calling {tool_name} now "
            f"would either click a moved/missing element or guess via JS "
            f"against a DOM you can't see.{outcome_bit}\n"
            f"REQUIRED next call — one of:\n"
            f"  - browser_screenshot(session_id='{sid}') — full vision pass, "
            f"new V_n indices, fresh bbox geometry.\n"
            f"  - browser_state_check(session_id='{sid}', expected='...') — "
            f"cheap state-only pass; use when you only need to confirm "
            f"the prior action took effect.\n"
            f"After a fresh vision pass the gate clears and you can call "
            f"{tool_name} again with fresh references. DO NOT call "
            f"browser_run_script or browser_eval to work around this — "
            f"that's the failure mode this gate exists to prevent. "
            f"browser_get_markdown is fine for reading text but does NOT "
            f"clear this gate (no vision = no fresh V_n)."
        )

    def _check_preplan_lock(self, tool_name: str) -> str | None:
        """Arch v4 Move 1 — refuses a state-change tool when no fresh
        preplan has been declared since the last consumption. Layered
        on top of must_screenshot_before_state_change: the screenshot-
        freshness gate runs first, then this one.

        Side effects:
          - On allow: marks `preplan_lock_consumed = True` so the next
            state-change requires a fresh preplan.
          - On consecutive refusals: increments `preplan_consecutive_
            refusals`. At ≥3, sets `preplan_backoff_just_fired=True`,
            resets the counter, and ALLOWS the call (so the brain can
            unstick). worker_hook surfaces [GATE_BACKOFF n=1] to the
            brain. Disabled by PREPLAN_BACKOFF=0.
          - On allow: resets `preplan_consecutive_refusals` to 0.
        Arch v4.2: gate is now OFF by default (PREPLAN_GATE=0).
        BrowserPreplanTool is no longer registered in the default tool
        surface, so requiring it here would refuse every state change.
        Re-enable with PREPLAN_GATE=1 only when running in legacy v4
        mode (REGISTER_LEGACY_V4_TOOLS=1).
        """
        if os.environ.get("PREPLAN_GATE", "0") != "1":
            return None
        # No lock OR lock already consumed: refuse (or backoff).
        lock = self.preplan_lock
        if lock is None or self.preplan_lock_consumed:
            backoff_active = (
                os.environ.get("PREPLAN_BACKOFF", "1") != "0"
                and self.preplan_consecutive_refusals >= 3
            )
            if backoff_active:
                # Yield: allow this call, reset counter, signal hook.
                self.preplan_consecutive_refusals = 0
                self.preplan_backoff_just_fired = True
                # The lock stays None / consumed, but consume-mark is
                # idempotent — let it through. Marking consumed here
                # would be a no-op since it's already consumed/missing.
                return None
            self.preplan_consecutive_refusals += 1
            sid = self.session_id or "<session_id>"
            if lock is None:
                last_quote = (
                    "(no prior preplan on this session)"
                )
            else:
                last_quote = (
                    f"focus=#{lock.focus_constraint_idx + 1}, "
                    f"tool={lock.planned_tool!r}, "
                    f"target={lock.planned_target_label!r}, "
                    f"expected={lock.expected_outcome!r}"
                )
            return (
                f"[refused: preplan_lock consumed]\n"
                f"Calling {tool_name} requires a fresh browser_preplan. "
                f"Last preplan: {last_quote}.\n"
                f"Before the next action, declare which TaskBrief "
                f"constraint you're attacking, which tool, and what "
                f"should happen. Call:\n"
                f"  browser_preplan(session_id='{sid}', "
                f"focus_constraint_idx=<int>, planned_tool='click_at', "
                f"planned_target_label='<V_n label>', "
                f"planned_target_vision_index=<V_n>, "
                f"expected_outcome='<one-sentence>', "
                f"expected_postcondition='bbox_state_change'|'dom_mutated'|"
                f"'url_changed')\n"
                f"This forces vision→preplan→action→verify discipline "
                f"so multi-constraint queries don't drift. Override "
                f"with PREPLAN_GATE=0 if you genuinely need to bypass."
            )
        # Lock is fresh: allow + consume.
        self.preplan_consecutive_refusals = 0
        self.preplan_lock_consumed = True
        return None

    def must_screenshot_before_giving_up(self) -> str | None:
        """Refusal message for browser_request_help /
        browser_run_script(mutates=true) when the brain is trying to
        escape the screenshot→click loop without first looking at the
        page. Returns None when the call is allowed.

        Three exit conditions allow the impasse tools through:
          1. No active task plan (single-step task — no loop to enforce).
          2. Active step is already `unsatisfiable` (the brain has
             genuinely earned the give-up via 2 verify_action misses).
          3. Loop detector reports ≥3 stale screenshots in a row (the
             page is genuinely stuck; not a brain choice).
        Otherwise: refuse if `last_failure_without_screenshot` is set.
        """
        if os.environ.get("LOOP_REFUSAL_GUARD", "1") == "0":
            return None
        plan = self.task_plan
        if plan is None:
            return None
        active = None
        try:
            active = plan.peek_active()
        except Exception:
            return None
        if active is None:
            return None
        if active.status == "unsatisfiable":
            return None
        try:
            stale = int(getattr(self.loop_detector, "stale_screenshot_count", 0))
        except Exception:
            stale = 0
        if stale >= 3:
            return None
        if not self.last_failure_without_screenshot:
            return None
        return (
            f"[refused: take a screenshot first] The active task-plan step "
            f"({active.name!r}) is still in_progress and the last action "
            f"failed ({self.last_failure_summary[:80]}). Call "
            f"browser_screenshot(session_id='{self.session_id}') to see "
            f"what actually happened on the page, then pick a different "
            f"V_n or selector. Calling this impasse tool without first "
            f"looking at the page is the failure mode this guard exists "
            f"to prevent. If the page is genuinely stuck, take 3 "
            f"screenshots in a row that produce identical state and the "
            f"refusal will lift. Override: LOOP_REFUSAL_GUARD=0."
        )

    def hydrate_from_handoff(self, h: Any) -> None:
        """Restore state from a `handoff_store.WorkerHandoff` snapshot.

        Called when `delegate_browser_task` is invoked with a non-empty
        `resume_from_task_id` and the prior worker saved state. Restores
        the browser session_id, URL, task_plan, ledgers, and observed
        anchors so the new worker continues from where the previous one
        gave up instead of opening a fresh browser.
        """
        if h is None:
            return
        self.session_id = getattr(h, "session_id", "") or self.session_id
        self.current_url = getattr(h, "current_url", "") or self.current_url
        self.pinned_domain = getattr(h, "pinned_domain", "") or self.pinned_domain
        self.task_instruction = (
            getattr(h, "task_instruction", "") or self.task_instruction
        )
        self.task_target_url = (
            getattr(h, "task_target_url", "") or self.task_target_url
        )
        if getattr(h, "task_plan", None) is not None:
            self.task_plan = h.task_plan
        # Arch v3 — restore TaskBrief, vision history, failed tactics,
        # interaction ledger so the successor worker isn't blind.
        if getattr(h, "task_brief", None) is not None:
            self.task_brief = h.task_brief
        vsh = getattr(h, "vision_state_history", None)
        if isinstance(vsh, list):
            self.vision_state_history = list(vsh)[-self._VISION_STATE_HISTORY_CAP:]
        ft = getattr(h, "failed_tactics", None)
        if isinstance(ft, list):
            self.failed_tactics = list(ft)[-self._FAILED_TACTICS_CAP:]
        il = getattr(h, "interaction_ledger", None)
        if isinstance(il, list):
            self.interaction_ledger = list(il)[-self._INTERACTION_LEDGER_CAP:]
        self.cursor_failure_strategies = set(
            getattr(h, "cursor_failure_strategies", set()) or set()
        )
        self.cursor_failure_records = list(
            getattr(h, "cursor_failure_records", []) or []
        )
        self.observed_anchor_urls = set(
            getattr(h, "observed_anchor_urls", set()) or set()
        )
        manifest = getattr(h, "last_filter_manifest", None)
        if manifest is not None:
            self.last_filter_manifest = manifest
        # Mark the session as "already opened" so the BrowserOpenTool
        # idempotency guard refuses a redundant open and steers the
        # brain to browser_screenshot instead.
        if self.session_id:
            self.sessions_opened = max(self.sessions_opened, 1)

    def _task_plan_step_prefix(self) -> str:
        """Return a one-line `[step i/N → "name"]` prefix for the active
        TaskPlan step, or empty string when no plan is set.

        Used by build_text_only and build_tool_result_blocks to prepend
        the canonical plan-cursor reminder at the TOP of every tool
        reply. The full plan checklist is still rendered post-iteration
        by worker_hook; this prefix is the single-line cursor at the
        moment of decision. Kill switch STEP_PREFIX_IN_CAPTION=0.
        """
        if os.environ.get("STEP_PREFIX_IN_CAPTION", "1") == "0":
            return ""
        plan = getattr(self, "task_plan", None)
        if plan is None:
            return ""
        try:
            active_idx = plan.active_index()
            if active_idx is None:
                return f"[step done {len(plan.steps)} of {len(plan.steps)}]"
            active = plan.steps[active_idx]
            # Use " of " instead of "/" so the prefix can't pollute
            # filesystem path generation when the caption is hashed
            # into a screenshot filename label downstream.
            return (
                f"[step {active_idx + 1} of {len(plan.steps)} → "
                f"{active.name!r}]"
            )
        except Exception:
            return ""

    def harvest_anchor_urls(self, blob: str) -> int:
        """Extract `href="..."` URLs from a tool result blob and add them
        to `observed_anchor_urls`. Used by the URL-hallucination guard
        in browser_navigate to refuse same-domain URLs the brain made
        up. Returns the number of new URLs added (for tests).
        """
        if not blob or not isinstance(blob, str):
            return 0
        import re as _re
        added = 0
        for m in _re.finditer(r'href=[\'"]([^\'"\s<>]+)[\'"]', blob):
            href = m.group(1)
            if not href or href.startswith(("javascript:", "mailto:", "#")):
                continue
            if href in self.observed_anchor_urls:
                continue
            self.observed_anchor_urls.add(href)
            added += 1
        # Also extract bare URLs from common element listings like
        # `[12]<a href=https://...>` or `link: https://...` so we
        # capture data even when the format isn't standard HTML.
        for m in _re.finditer(r'https?://[^\s\'"<>\)]+', blob):
            url = m.group(0).rstrip(".,);")
            if url in self.observed_anchor_urls:
                continue
            self.observed_anchor_urls.add(url)
            added += 1
        # Bound the set to avoid unbounded growth on long sessions.
        if len(self.observed_anchor_urls) > self._OBSERVED_ANCHOR_CAP:
            # Drop arbitrary half — order is fine since we only need
            # "recent enough"; the next tool result will repopulate.
            extra = len(self.observed_anchor_urls) - self._OBSERVED_ANCHOR_CAP // 2
            for _ in range(extra):
                self.observed_anchor_urls.pop()
        return added

    async def check_active_task_step(
        self, session_id: str, *, pre_url: str = "",
    ) -> str:
        """Verify the active TaskPlan step's success_criteria.

        Called by state-change tools (click_at, click_selector, navigate,
        type_at) right before returning their result. Surfaces one of:
          • [subgoal_advanced: <name>] — criterion satisfied; step
            marked done; the next step (if any) becomes active.
          • [subgoal_not_satisfied: <criterion> attempt N/M] — criterion
            not met yet; brain should keep trying or replan.
          • [subgoal_unsatisfiable: <name> after M attempts] — step
            flipped to unsatisfiable; brain MUST call browser_plan_skip_step
            or browser_plan_replan or done(success=False) before next click.
        Returns "" when there's no active plan or no active step.
        """
        plan = self.task_plan
        if plan is None:
            return ""
        step = plan.peek_active()
        if step is None:
            return ""
        # Don't check criteria for terminal extraction steps that have
        # no observable side effect; the brain reports completion via
        # the message tool.
        if step.delegate is not None and step.delegate.kind == "extraction":
            return ""
        # Fast path for URL-shape criteria — answer locally from
        # state.current_url instead of round-tripping through
        # backend.state(sid). Cheaper, and works in unit tests / when
        # the network state probe is briefly down. Only the actual
        # verification is local; the result is still routed through
        # mark_attempt for consistent advance / unsatisfiable handling.
        kind = step.success_criteria.kind
        if kind == "url_changed":
            cur = (self.current_url or "").strip()
            before = (pre_url or "").strip()
            verified = bool(cur) and bool(before) and cur != before
            reason = "url_changed" if verified else "url_same"
            from superbrowser_bridge.task_plan import MAX_STEP_ATTEMPTS
            return _format_subgoal_note(self, step, verified, reason, MAX_STEP_ATTEMPTS)
        if kind == "url_matches":
            cur = (self.current_url or "").strip()
            pat = str(step.success_criteria.payload.get("pattern")
                      or step.success_criteria.payload.get("substring") or "")
            if not pat:
                return "\n[subgoal_check_skipped: url_matches has no pattern]"
            verified = pat in cur
            reason = "url_matches" if verified else "url_mismatch"
            from superbrowser_bridge.task_plan import MAX_STEP_ATTEMPTS
            return _format_subgoal_note(self, step, verified, reason, MAX_STEP_ATTEMPTS)
        try:
            from superbrowser_bridge.tier_evaluate import get_backend as _get_backend
            from superbrowser_bridge.verify_action import verify_after, PreState
        except Exception as exc:
            return f"\n[subgoal_check_skipped: import_failed:{exc!s:.40s}]"
        try:
            backend = _get_backend(session_id)
            vr = await verify_after(
                backend, session_id,
                step.success_criteria.to_dict(),
                pre_state=PreState(url=pre_url or self.current_url or ""),
                state=self,
            )
        except Exception as exc:
            # Probe error is non-blocking; let the brain continue.
            return f"\n[subgoal_check_skipped: probe_error:{exc!s:.60s}]"
        # Failed probes (network down, missing pre-state, fail-open
        # exception path) shouldn't count toward the 2-strike attempt
        # budget — that would flip steps to unsatisfiable on transient
        # errors. Only count attempts when the probe actually ran.
        _probe_failed_reasons = (
            "error:", "state_fetch_failed", "captcha_probe_failed",
            "blocker_probe_failed", "no_pre_url", "no_pre_hash",
        )
        reason_str = vr.reason or ""
        if not vr.verified and any(
            reason_str.startswith(p) for p in _probe_failed_reasons
        ):
            return f"\n[subgoal_check_skipped: {reason_str[:80]}]"
        # Arch v3: when the postcondition links to a TaskBrief constraint
        # via constraint_id, flip that constraint to satisfied on success.
        # Free path — runs after the verify_after probe already succeeded.
        try:
            cid = step.success_criteria.payload.get("constraint_id") or ""
            cid = cid or getattr(step.success_criteria, "constraint_id", "") or ""
            if (
                vr.verified
                and cid
                and getattr(self, "task_brief", None) is not None
            ):
                idx = self.task_brief.find_constraint_by_canonical(cid)
                if idx >= 0:
                    self.task_brief.mark_constraint(
                        idx,
                        "satisfied",
                        f"verify_after({reason_str[:60]})",
                        self.current_url or "",
                    )
        except Exception:
            pass
        from superbrowser_bridge.task_plan import MAX_STEP_ATTEMPTS
        return _format_subgoal_note(self, step, vr.verified, reason_str, MAX_STEP_ATTEMPTS)

    def check_dead_click(self, click_target: str) -> str | None:
        """Pre-flight check before dispatching a click.

        Counts how many times this exact click target has been fired in
        a row without the page DOM changing in between. Once the count
        would reach `MAX_CONSECUTIVE_SAME_TARGET`, refuse with a
        structured error so the brain is forced to pick a different
        target. Different target OR a DOM change resets the count.

        DOM change is detected via `_last_dom_hash` (set by every state
        fetch).

        Returns a structured error string when blocking, or None when
        the click is allowed to proceed.
        """
        same_target = (click_target == self.last_click_target)
        same_dom = (
            bool(self._last_dom_hash)
            and self._last_dom_hash == self.last_click_dom_hash
        )
        if same_target and same_dom:
            # Nth consecutive dead attempt at the same target.
            self.consecutive_dead_clicks += 1
        else:
            # Fresh attempt at this target (different target OR page moved).
            self.consecutive_dead_clicks = 1
        if self.consecutive_dead_clicks >= self.MAX_CONSECUTIVE_SAME_TARGET:
            # Reset so the brain picking a new target next round clears
            # the strike count cleanly.
            self.consecutive_dead_clicks = 0
            self.last_click_target = ""
            return (
                f"[dead_click_blocked] {click_target} has been clicked "
                f"{self.MAX_CONSECUTIVE_SAME_TARGET} times in a row with "
                "no DOM change. The previous clicks did not move the "
                "page. Switch tactic: call browser_screenshot to "
                "re-observe, pick a different V_n (try a sibling "
                "control or a different role — submit button vs. the "
                "input), or browser_wait_for content you expect to "
                "appear. Do NOT retry this exact target, and do NOT "
                "synthesize clicks via browser_run_script — JS clicks "
                "are isTrusted=false and bot-detected."
            )
        return None

    def register_click_attempt(self, click_target: str) -> None:
        """Stamp the current click target + DOM hash so the next call to
        `check_dead_click` can compare against them."""
        self.last_click_target = click_target
        self.last_click_dom_hash = self._last_dom_hash

    def advance_observation_token(self, source: str = "") -> None:
        """No-op shim retained so kept tools (click_selector,
        rewind_to_checkpoint, scroll_until, drag_slider_until) that
        were ported forward from the validator era can still call it
        without blowing up. The token machinery was part of the
        deleted validator subsystem; in the reverted architecture the
        in-tool freshness/blocker/confidence gates in click_at do
        the same job."""
        pass

    def get_last_checkpoint(self) -> dict | None:
        """Return the most recent checkpoint."""
        return self.checkpoints[-1] if self.checkpoints else None

    def export_step_history(self) -> str:
        """Export structured step history and checkpoint to disk.

        Writes TWO formats:
          - step_history.md  — human-readable markdown log
          - step_history.json — structured data the orchestrator parses
            to build domain-keyed captcha learnings and to inject prior
            context into subsequent tasks.
        """
        lines = ["## Step History"]
        for i, step in enumerate(self.step_history, 1):
            lines.append(f"{i}. [{step['time']}] {step['tool']}({step['args']}) → {step['result']}")
            if step.get("url"):
                lines.append(f"   URL: {step['url']}")

        if self.checkpoints:
            lines.append("\n## Checkpoints (progress markers)")
            for cp in self.checkpoints:
                lines.append(f"- [{cp['time']}] {cp['action']} → {cp['url']}")

        lines.append(f"\n## Best checkpoint URL: {self.best_checkpoint_url or 'none'}")
        lines.append(f"## Regressions detected: {self.regression_count}")

        content = "\n".join(lines)

        # Write to task-specific directory
        task_dir = f"/tmp/superbrowser/{self.task_id}" if self.task_id else "/tmp/superbrowser"
        os.makedirs(task_dir, exist_ok=True)
        step_path = os.path.join(task_dir, "step_history.md")
        with open(step_path, "w") as f:
            f.write(content)
        print(f"  [step history saved: {step_path}]")

        # Structured JSON export for orchestrator consumption.
        import json as _json_export
        structured = {
            "task_id": self.task_id,
            "sessions_opened": self.sessions_opened,
            "current_url": self.current_url,
            "best_checkpoint_url": self.best_checkpoint_url,
            "regression_count": self.regression_count,
            "checkpoints": self.checkpoints,
            "vision_calls": self.vision_calls,
            "text_calls": self.text_calls,
            "max_screenshots": self.max_screenshots,
            "screenshots_used": self.max_screenshots - self.screenshot_budget,
            "steps": self.step_history,
            "activity_log": self.activity_log,
        }
        json_path = os.path.join(task_dir, "step_history.json")
        try:
            with open(json_path, "w") as f:
                _json_export.dump(structured, f, indent=2, default=str)
            print(f"  [structured history saved: {json_path}]")
        except Exception as exc:  # pragma: no cover - best-effort persistence
            print(f"  [structured history save failed: {exc}]")

        # Save checkpoint as JSON for re-delegation
        if self.best_checkpoint_url:
            import json as _json
            checkpoint_data = {
                "url": self.best_checkpoint_url,
                "title": self.checkpoints[-1].get("title", "") if self.checkpoints else "",
                "regressions": self.regression_count,
            }
            cp_path = os.path.join(task_dir, "checkpoint.json")
            with open(cp_path, "w") as f:
                _json.dump(checkpoint_data, f)
            print(f"  [checkpoint saved: {cp_path}]")

        return content

    def log_activity(self, action: str, result: str = "ok"):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {action}"
        if result != "ok":
            entry += f" → {result}"
        self.activity_log.append(entry)
        if len(self.activity_log) > 30:
            self.activity_log.pop(0)

    def get_activity_summary(self) -> str:
        if not self.activity_log:
            return ""
        lines = "\n".join(self.activity_log[-15:])
        return (
            f"\n--- Previous activity (DO NOT repeat failed approaches) ---\n"
            f"{lines}\n"
            f"--- Screenshots remaining: {self.screenshot_budget}/{self.max_screenshots} | Sessions opened: {self.sessions_opened} ---"
        )

    def print_summary(self):
        elapsed = time.time() - self.start_time if self.start_time else 0
        used = self.max_screenshots - self.screenshot_budget
        print(f"\n  [Session Summary]")
        print(f"  Duration: {elapsed:.1f}s | Sessions: {self.sessions_opened}")
        print(f"  Vision calls: {self.vision_calls} | Text calls: {self.text_calls} | Screenshots: {used}/{self.max_screenshots}")
        est = self.vision_calls * 0.03 + self.text_calls * 0.002
        print(f"  Estimated cost: ~${est:.3f}")

    def export_activity_log(self) -> str:
        """Export structured activity log to disk for the orchestrator to read."""
        elapsed = time.time() - self.start_time if self.start_time else 0
        used = self.max_screenshots - self.screenshot_budget

        lines = [
            f"## Browser Worker Activity",
            f"Duration: {elapsed:.1f}s | Screenshots: {used}/{self.max_screenshots} | Tool calls: {self.vision_calls + self.text_calls}",
            "",
            "### Actions",
        ]
        lines.extend(self.activity_log)
        content = "\n".join(lines)

        # Write to disk so orchestrator/subagent can read it
        activity_path = "/tmp/superbrowser/last_activity.md"
        os.makedirs(os.path.dirname(activity_path), exist_ok=True)
        with open(activity_path, "w") as f:
            f.write(content)
        print(f"  [activity log saved: {activity_path}]")
        return content

    def save_screenshot(self, b64: str, label: str = "") -> str:
        self.step_counter += 1
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        fn = f"{self.step_counter:03d}-{label}.jpg" if label else f"{self.step_counter:03d}.jpg"
        path = os.path.join(SCREENSHOT_DIR, fn)
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"  [screenshot saved: {path}]")
        return path

    async def build_tool_result_blocks(
        self,
        b64: str,
        caption: str,
        *,
        intent: str | None = None,
        url: str | None = None,
        elements: str | None = None,
        elements_with_bounds: list[dict] | None = None,
        device_pixel_ratio: float = 1.0,
        coverage_mode: bool = False,
        expected_labels: list[str] | None = None,
        force_fresh: bool = False,
    ) -> list[dict] | str:
        """Async dispatch between the vision-preprocessor path and the
        legacy image-blocks path.

        When `VISION_ENABLED=1` the screenshot is sent to the dedicated
        vision agent (cheap model) and the brain only sees its textual
        summary + bboxes + flags. Otherwise we fall through to the
        legacy `build_image_blocks` that embeds the JPEG directly.

        `intent` and `url` are optional hints used solely by the vision
        path. `elements` is the DOM element listing — hashed as a cache
        key so a re-screenshot on the same URL with identical DOM hits
        the cache.
        """
        # v5: prepend the active TaskPlan step cursor as the first line
        # of the caption so the brain sees the holistic state BEFORE
        # the screenshot/vision content. Skipped on un-planned tasks.
        _step_prefix = self._task_plan_step_prefix()
        if _step_prefix:
            caption = f"{_step_prefix}\n{caption}" if caption else _step_prefix
        # Arch v3: prepend the TaskBrief in COMPACT form on regular tool
        # results. Full-form rendering happens after the vision pass
        # below where it sits next to the [STATE] block. Compact mode
        # adds ~80 tokens per tool reply — cheap insurance against the
        # brain forgetting filters/constraints mid-task.
        _brief = getattr(self, "task_brief", None)
        if _brief is not None and os.environ.get("BRIEF_IN_CAPTION", "1") != "0":
            try:
                _brief_line = _brief.to_brain_text(compact=True)
            except Exception:
                _brief_line = ""
            if _brief_line:
                caption = (
                    f"{_brief_line}\n{caption}" if caption else _brief_line
                )
        # Lazy import: keeps the vision package optional at import time
        # so a broken VISION_API_KEY doesn't blow up sessions that never
        # enable the feature.
        try:
            from vision_agent import (
                dom_hash_of,
                get_vision_agent,
                vision_agent_enabled,
            )
            try:
                from vision_agent import dom_text_hash_of
            except ImportError:
                dom_text_hash_of = None  # type: ignore[assignment]
        except ImportError:
            vision_agent_enabled = lambda: False  # type: ignore[assignment]
            get_vision_agent = None  # type: ignore[assignment]
            dom_hash_of = None  # type: ignore[assignment]
            dom_text_hash_of = None  # type: ignore[assignment]

        if vision_agent_enabled() and get_vision_agent is not None:
            dh = dom_hash_of(elements) if dom_hash_of else ""
            if dh:
                self._last_dom_hash = dh
            # Phase 1.2: viewport-aware secondary key — left empty here
            # because build_tool_result_blocks doesn't receive scroll
            # info. The prefetch path in _schedule_vision_prefetch
            # populates it from the live /state response. Empty string
            # falls through to legacy 5-tuple-equivalent caching, which
            # is correct (just less granular than the prefetch path).
            dth = ""
            if intent:
                self._last_intent = intent
            effective_intent = intent or self._last_intent or "observe page"
            effective_url = url or self.current_url
            try:
                agent = get_vision_agent()
                # Read screenshot dims off the bytes once — Gemini emits
                # box_2d in [0, 1000] space; downstream click dispatch
                # needs the source pixel dims to convert back to viewport
                # coordinates accurately.
                img_w, img_h = _read_image_dims(b64)
                # Arch v3 fix #3: derive the highest-priority unverified
                # TaskBrief constraint and pass it as active_constraint so
                # vision biases V_n ranking toward elements that advance
                # this specific constraint right now.
                _active_constraint: dict | None = None
                _brief = getattr(self, "task_brief", None)
                if _brief is not None:
                    try:
                        for _c in _brief.constraints:
                            if _c.status == "unverified":
                                _active_constraint = _c.to_dict()
                                break
                    except Exception:
                        _active_constraint = None
                resp = await agent.analyze(
                    screenshot_b64=b64,
                    intent=effective_intent,
                    session_id=self.session_id,
                    url=effective_url,
                    dom_hash=dh or self._last_dom_hash,
                    dom_text_hash=dth,
                    previous_summary=self._last_vision_summary or None,
                    image_width=img_w,
                    image_height=img_h,
                    task_instruction=self.task_instruction or None,
                    coverage_mode=coverage_mode,
                    expected_labels=expected_labels,
                    force_fresh=force_fresh,
                    active_constraint=_active_constraint,
                )
                self._last_vision_summary = resp.summary
                self._last_vision_response = resp
                self._last_vision_ts = time.time()
                self._last_vision_url = effective_url or self.current_url or ""
                self.vision_calls += 1
                self.actions_since_screenshot = 0
                # Arch v3 fix #5: a fresh vision pass clears the
                # freshness dirty flag — brain has seen the post-mutation
                # state, so the next state-change tool is allowed.
                self.clear_dom_dirty()
                # Freeze this response as the current epoch. The brain
                # is about to see `as_brain_text()` output — subsequent
                # V_n references MUST resolve to this snapshot, not to
                # whatever background prefetch writes into
                # `_last_vision_response` before the brain's next turn.
                self.freeze_vision_epoch()
                label = (
                    (caption or "").split("\n")[0][:30]
                    .replace(" ", "-")
                    .replace("/", "_")
                    .replace("\\", "_")
                )
                # Still save the raw screenshot locally for debugging —
                # doesn't leave the box, doesn't reach the brain.
                self.save_screenshot(b64, label)
                # Fire-and-forget push of detected bboxes to live viewers.
                # Lets the user see what Gemini "saw" (full set, color-
                # coded by role) for ~1.5s before the next click. Failure
                # is non-fatal — vision still works, just no overlay.
                try:
                    asyncio.create_task(_push_vision_bboxes(
                        self.session_id, resp,
                        url=self._last_vision_url,
                    ))
                except Exception as exc:
                    print(f"  [vision-overlay push failed: {exc}]")

                # DOM cross-check pass — for each bbox center, ask the
                # browser what's actually under that point. When vision's
                # label disagrees with DOM truth, the bbox gets a
                # dom_check payload that the render path surfaces as
                # [DOM_DISAGREE …] so the brain can avoid the bad target.
                # Gated by env so a misbehaving endpoint can be killed
                # without redeploy. Soft-fails: cross-check exceptions
                # never break the render path.
                if (
                    os.environ.get("VISION_DOM_CROSSCHECK", "1") != "0"
                    and resp.bboxes
                    and img_w > 0
                    and img_h > 0
                ):
                    try:
                        await _decorate_bboxes_with_dom_check(
                            self.session_id, resp, img_w, img_h,
                        )
                    except Exception as exc:
                        print(f"  [dom-crosscheck: skipped — {exc}]")

                # Hierarchical planner pass — DOM-side blocker scan +
                # action sequencing. Now runs for both t1 and t3 via
                # tier_evaluate.get_backend, which routes evaluate /
                # state through the right transport per session_id.
                # Kill switch: ACTION_PLANNER_T1=0 disables on t1 only;
                # ACTION_PLANNER_AUTO=0 disables everywhere. Soft-fails:
                # any exception falls back to the vision-only caption.
                plan_text = ""
                _planner_on = (
                    os.environ.get("ACTION_PLANNER_AUTO", "1") != "0"
                    and (
                        self.session_id.startswith("t3-")
                        or os.environ.get("ACTION_PLANNER_T1", "1") != "0"
                    )
                )
                if _planner_on:
                    try:
                        from superbrowser_bridge.tier_evaluate import get_backend as _get_backend
                        from superbrowser_bridge.antibot.ui_blockers import detect as _detect_blockers
                        from superbrowser_bridge.action_planner import plan as _plan_actions
                        backend = _get_backend(self.session_id)
                        blockers = await _detect_blockers(backend, self.session_id)
                        self._last_blockers = blockers
                        queue = _plan_actions(
                            vresp=resp,
                            blockers=blockers,
                            task_instruction=self.task_instruction or "",
                            url=effective_url or "",
                            recent_steps=self.step_history[-8:] if self.step_history else [],
                        )
                        self._last_action_queue = queue
                        plan_text = queue.to_brain_text()
                    except Exception as exc:
                        print(f"  [action-planner: skipped — {exc}]")

                try:
                    _max_age = int(os.environ.get("VISION_MAX_AGE_TURNS") or "1")
                except ValueError:
                    _max_age = 1
                brain_text = resp.as_brain_text(
                    current_turn=self._brain_turn_counter,
                    epoch_turn=self._vision_epoch_turn,
                    max_age_turns=_max_age,
                )
                if plan_text:
                    brain_text = f"{brain_text}\n\n{plan_text}"
                # Arch v3: reconcile brief constraints from page_state
                # and append the FULL brief to the brain text. Empty
                # PageState (legacy provider response) → no-op.
                # Arch v3 fix G — chevron-click verdict check. When the
                # previous click_at was a chevron-labeled bbox AND we're
                # on the same URL AND the page_state.last_action_verdict
                # indicates failure, the chevron click silently missed.
                # Append a failed_tactic so the successor worker (and the
                # advisor) know to try a different chevron strategy.
                try:
                    chevron_lbl = self._last_chevron_click_label
                    chevron_url = self._last_chevron_click_url
                    if chevron_lbl and chevron_url == (self.current_url or ""):
                        ps = getattr(resp, "page_state", None)
                        verdict_obj = (
                            getattr(ps, "last_action_verdict", None) if ps else None
                        )
                        verdict = (
                            getattr(verdict_obj, "verdict", "") if verdict_obj else ""
                        )
                        if verdict in ("failed", "uncertain"):
                            tactic = (
                                f"chevron_click_no_expansion: V_n labeled "
                                f"'{chevron_lbl[:80]}' was clicked at right-edge "
                                f"but accordion did not expand. Try "
                                f"browser_look_again with expected_labels=["
                                f"'{chevron_lbl[:60]}'] for a tighter chevron "
                                f"bbox, then browser_click_at on the new V_n. "
                                f"If the chevron is genuinely a non-button div, "
                                f"try clicking the parent row label instead."
                            )
                            if tactic not in self.failed_tactics:
                                self.failed_tactics.append(tactic)
                                if len(self.failed_tactics) > self._FAILED_TACTICS_CAP:
                                    self.failed_tactics = self.failed_tactics[
                                        -self._FAILED_TACTICS_CAP :
                                    ]
                    # One-shot: clear after this vision pass either way.
                    self._last_chevron_click_label = ""
                    self._last_chevron_click_url = ""
                except Exception:
                    pass
                _brief = getattr(self, "task_brief", None)
                if _brief is not None:
                    try:
                        from superbrowser_bridge.task_brief import (
                            reconcile_from_page_state,
                            reconcile_from_url,
                            reconcile_negative_constraints,
                        )
                        # Arch v4 (Step 6): track transitions across all
                        # reconcilers so we can log sub-goal closures and
                        # surface the focus advance for diagnostics. The
                        # actual sub-goal compression — completed_log
                        # append, stuck_counter reset, focus_id advance —
                        # is already handled inside TaskBrief.mark_constraint.
                        _flips_pre = 0
                        # Arch v3 fix #2: URL-based reconciliation runs
                        # FIRST. Many filter-state changes are encoded in
                        # the URL (e.g. /regions/oregon/, ?make=ford) and
                        # vision often can't see them until results render.
                        _flips_pre += reconcile_from_url(_brief, self.current_url) or 0
                        ps = getattr(resp, "page_state", None)
                        if ps is not None:
                            _flips_pre += reconcile_from_page_state(
                                _brief, ps, current_url=self.current_url,
                            ) or 0
                            _flips_pre += reconcile_negative_constraints(
                                _brief, ps, current_url=self.current_url,
                            ) or 0
                        if _flips_pre > 0:
                            print(
                                f"  [task_brief] sub-goal compression: "
                                f"{_flips_pre} constraint(s) closed; "
                                f"focus_id={_brief.focus_id or '-'}; "
                                f"completed_log={len(_brief.completed_log)}"
                            )
                        else:
                            # Arch v4 (Step 7): no transition this pass —
                            # bump the stuck counter. Once it crosses the
                            # threshold (default 8) and we still have
                            # redecompose budget (≤ MAX_REDECOMPOSE), call
                            # the LLM-backed redecompose() to regenerate
                            # the still-open tail of the checklist.
                            _stuck = _brief.bump_stuck()
                            if (
                                _stuck >= 8
                                and _brief.redecompose_count < _brief.MAX_REDECOMPOSE
                            ):
                                try:
                                    from superbrowser_bridge.task_brief import (
                                        redecompose,
                                    )
                                    from urllib.parse import urlsplit
                                    _path = ""
                                    try:
                                        _path = (
                                            urlsplit(self.current_url).path or ""
                                        )[:240]
                                    except Exception:
                                        _path = ""
                                    n_new = await redecompose(
                                        _brief, current_url_path=_path,
                                    )
                                    if n_new > 0:
                                        print(
                                            f"  [task_brief] redecompose: "
                                            f"{n_new} new tail item(s); "
                                            f"redecompose_count="
                                            f"{_brief.redecompose_count}"
                                        )
                                except Exception as exc:
                                    print(
                                        f"  [task_brief] redecompose skipped — {exc}"
                                    )
                        if ps is not None:
                            # Track recent PageState snapshots for restart
                            # fidelity. model_dump() is best-effort.
                            try:
                                ps_dict = ps.model_dump()
                            except Exception:
                                ps_dict = None
                            if ps_dict:
                                self.vision_state_history.append(ps_dict)
                                if len(self.vision_state_history) > self._VISION_STATE_HISTORY_CAP:
                                    self.vision_state_history = (
                                        self.vision_state_history[-self._VISION_STATE_HISTORY_CAP:]
                                    )
                        brief_text = _brief.to_brain_text(compact=False)
                        if brief_text:
                            brain_text = f"{brain_text}\n\n{brief_text}"
                    except Exception as exc:
                        print(f"  [task_brief render: skipped — {exc}]")
                text = f"{caption}\n\n{brain_text}" if caption else brain_text
                return [{"type": "text", "text": text}]
            except Exception as exc:
                # Never let a vision-layer failure break a tool result —
                # fall through to the legacy image path.
                print(f"  [vision-agent: falling back to image blocks — {exc}]")

        return self.build_image_blocks(
            b64,
            caption,
            elements_with_bounds=elements_with_bounds,
            device_pixel_ratio=device_pixel_ratio,
        )

    def build_image_blocks(
        self,
        b64: str,
        caption: str,
        elements_with_bounds: list[dict] | None = None,
        device_pixel_ratio: float = 1.0,
    ) -> list[dict]:
        """Build a vision-message-ready payload (text + image).

        If `elements_with_bounds` is provided, paint dashed bbox overlays +
        index labels on the screenshot so the LLM can ground on [index]
        instead of guessing pixel coordinates. Silently falls back to the
        raw screenshot if PIL is unavailable or overlay fails.
        """
        self.vision_calls += 1
        self.actions_since_screenshot = 0
        # Arch v3 fix #5: a fresh image-blocks pass also clears the
        # freshness dirty flag (the brain has now seen the page after
        # whatever mutation happened).
        self.clear_dom_dirty()
        label = caption.split("\n")[0][:30].replace(" ", "-").replace("/", "_")

        final_b64 = b64
        if elements_with_bounds:
            try:
                from superbrowser_bridge.highlights import build_highlighted_screenshot
                final_b64 = build_highlighted_screenshot(
                    b64, elements_with_bounds, device_pixel_ratio,
                )
            except Exception:
                final_b64 = b64

        # Clamp the final payload to ≤ 2MB / ≤1568px side. Runs AFTER the
        # highlight overlay pass so overlay bloat can't tip a 1.8MB raw
        # screenshot over Gemini's 1.5MB-ish reject threshold.
        try:
            from superbrowser_bridge.image_safety import sanitize_image_b64
            final_b64 = sanitize_image_b64(final_b64)
        except Exception as e:
            print(f"  [image-safety: sanitize failed, sending raw: {e}]")

        self.save_screenshot(final_b64, label)
        return [
            {"type": "text", "text": caption},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{final_b64}"}},
        ]

    def build_text_only(self, data: dict, prefix: str = "") -> str:
        self.action_count += 1
        self.text_calls += 1
        self.actions_since_screenshot += 1
        # Phase 1.2: pick up implicit navigations. The TS bridge reports
        # the live URL on every action response; if the page navigated
        # without us calling browser_navigate (form submit, JS redirect,
        # history.pushState), record it here so the freshness logic can
        # invalidate the vision epoch.
        prev_url = self.current_url or ""
        actual_url = data.get("url") or ""
        url_changed_tag = ""
        if actual_url and actual_url != prev_url:
            self.record_url(actual_url)
            # Arch v4.1 (Fix 3): on click-triggered (implicit) navigation,
            # clear the vision epoch the same way BrowserNavigateTool does
            # for explicit navs. Otherwise the next click_at would resolve
            # against pre-navigation V_n indices that no longer exist on
            # the new page — the "page gone but bbox is clicked" failure.
            self._vision_epoch_response = None
            self._vision_epoch_url = ""
            # Arch v4.1 (Fix 3): bust the per-session vision cache so a
            # SPA navigation that lands on a similar DOM hash can't get
            # a cache hit serving pre-mutation bboxes. bust_session is
            # async; we fire-and-forget through the running event loop
            # if there is one (build_text_only is called from within
            # async tool handlers so a loop is available in practice).
            try:
                from vision_agent.client import get_vision_agent
                _vagent = get_vision_agent()
                _sid = self.session_id or ""
                if _sid:
                    try:
                        _loop = asyncio.get_running_loop()
                        _loop.create_task(_vagent._cache.bust_session(_sid))
                    except RuntimeError:
                        # No running loop — best-effort sync drop.
                        _store = getattr(_vagent._cache, "_store", None)
                        if isinstance(_store, dict):
                            for k in list(_store.keys()):
                                if isinstance(k, tuple) and k and k[0] == _sid:
                                    _store.pop(k, None)
            except Exception:
                pass
            # Arch v4.1 (Fix 3): surface the URL change to the brain so
            # it knows its V_n list is stale. Freshness gate already
            # blocks chained clicks; this is the explicit signal.
            try:
                from urllib.parse import urlsplit
                _prev_p = (urlsplit(prev_url).path or "/")[:60]
                _now_p = (urlsplit(actual_url).path or "/")[:60]
                url_changed_tag = (
                    f"[URL_CHANGED prev={_prev_p!r} now={_now_p!r} "
                    f"V_n indices from prior screenshot are now stale; "
                    f"call browser_screenshot before any click_at]"
                )
            except Exception:
                url_changed_tag = (
                    "[URL_CHANGED V_n indices from prior screenshot are stale]"
                )
        # Arch v4.1 (Fix 1): pin the brief and focus_line at the very top
        # of every action tool result. Closes the "query context
        # evaporates" gap — without this, click_at/type_at/scroll/eval
        # results contain ZERO reference to TaskBrief and the brain
        # forgets the original 12-condition query mid-session.
        brief_lines: list[str] = []
        _brief = getattr(self, "task_brief", None)
        if _brief is not None and os.environ.get("BRIEF_IN_CAPTION", "1") != "0":
            try:
                _b = _brief.to_brain_text(compact=True)
                if _b:
                    brief_lines.append(_b)
                _f = _brief.focus_line()
                if _f:
                    brief_lines.append(_f)
            except Exception:
                brief_lines = []
        # v5: prepend the active TaskPlan step as the FIRST line of every
        # state-change tool reply so the brain sees the cursor BEFORE
        # the action result, not after (worker_hook still renders the
        # full plan as guidance for the NEXT turn). This is the "always
        # visible holistic state" the user asked for. Skipped on
        # un-planned tasks. Kill switch STEP_PREFIX_IN_CAPTION=0.
        step_prefix = self._task_plan_step_prefix()
        parts = [step_prefix, prefix] if step_prefix else [prefix]
        if data.get("url"):
            parts.append(f"Page: {data['url']}")
        if data.get("title"):
            parts.append(f"Title: {data['title']}")
        result_line = " | ".join(p for p in parts if p)
        # Brief lines + url-change tag prepend BEFORE the action result.
        head = "\n".join(brief_lines)
        if url_changed_tag:
            head = f"{head}\n{url_changed_tag}" if head else url_changed_tag
        result = f"{head}\n{result_line}" if head else result_line
        # Arch v4 (Step 3): the full DOM element listing is no longer
        # injected into the brain prompt — the brain navigates by V_n
        # bboxes from vision, not by DOM index. The DOM crosscheck at
        # click time still runs server-side. Kept harvest_anchor_urls
        # below where applicable so the URL-hallucination guard still
        # sees legit links from the rendered page.
        if data.get("consoleErrors"):
            result += f"\nConsole errors: {data['consoleErrors']}"
        if data.get("pendingDialogs"):
            result += f"\nPending dialogs: {data['pendingDialogs']}"
        # Arch v4 (Step 3 follow-up): the cached-vision piggyback is
        # gone. After a mutating tool, the V_n indices the brain saw on
        # the prior screenshot are by definition stale — page just
        # mutated. Re-attaching them onto the click/type result fed the
        # brain a list it would interpret as "current," leading to the
        # "clicking wrong bbox even though bbox agent is giving all
        # bboxes" failure mode. The brain MUST take a fresh screenshot
        # to get a fresh V_n list (Step 2 freshness gate enforces this
        # for the next mutating tool anyway).
        if self.action_count >= 5:
            result += (
                "\n\n[HINT: Keep using browser_click_at(vision_index=V_n) / "
                "browser_type_at for every interaction — each fires a "
                "real CDP mouse event with humanized motion, which "
                "avoids bot-detection. Do NOT batch steps into "
                "browser_run_script; JS clicks are isTrusted=false and "
                "frequently rejected by WAF-protected sites.]"
            )
        return result

    # How old a cached vision response can be before we stop piggybacking
    # it onto mutating-tool replies. Short enough that the brain doesn't
    # click stale bboxes; long enough to cover a rapid click-scroll-click
    # sequence where no fresh vision has landed yet.
    FRESH_VISION_SECONDS = 10.0

    def _fresh_vision_text(self, tool_url: str) -> str:
        """Return cached vision's brain_text when safe to attach, else "".

        Safe means: we have a cached VisionResponse, its URL matches the
        URL this tool response is reporting (so the brain doesn't mistake
        pre-navigation bboxes for post-navigation state), and it's young
        enough (FRESH_VISION_SECONDS) to still reflect the page.

        Rendering is deliberately cheap — `as_brain_text()` is pure
        Python string formatting, no I/O.
        """
        resp = self._last_vision_response
        if resp is None:
            return ""
        if (time.time() - self._last_vision_ts) > self.FRESH_VISION_SECONDS:
            return ""
        # URL match — normalize to just scheme+host+path (ignore query
        # churn that doesn't meaningfully change the page).
        def _strip_query(u: str) -> str:
            if not u:
                return ""
            return u.split("?", 1)[0].split("#", 1)[0]
        if tool_url and _strip_query(tool_url) != _strip_query(self._last_vision_url):
            return ""
        try:
            try:
                _max_age = int(os.environ.get("VISION_MAX_AGE_TURNS") or "1")
            except ValueError:
                _max_age = 1
            cached_text = resp.as_brain_text(
                current_turn=self._brain_turn_counter,
                epoch_turn=self._vision_epoch_turn,
                max_age_turns=_max_age,
            )
            return "[CACHED VISION — bboxes still valid; use vision_index=V_n to click]\n" + cached_text
        except Exception:
            return ""


async def _fetch_elements(session_id: str, state: "BrowserSessionState | None" = None) -> str:
    """Fetch current interactive elements without vision (cheap, no screenshot).

    This is the key BrowserOS pattern: every action gets a fresh element snapshot
    so the agent always knows what's on the page without wasting a screenshot.

    If `state` is passed, we ALSO update `state.element_fingerprints` with
    the fresh per-index fingerprint map. Click/type tools then send the
    cached fingerprint as `expected_fingerprint` so the TS side can reject
    stale-index clicks (DOM shifted between state-fetch and click).
    """
    try:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            params={"vision": "false"},
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        if state is not None:
            fps = data.get("fingerprints") or {}
            if isinstance(fps, dict):
                # JSON keys come back as strings; coerce to int for direct index lookup.
                state.element_fingerprints = {int(k): v for k, v in fps.items() if isinstance(v, str)}
            # Phase 1.2: propagate the URL the TS bridge actually sees.
            # Form submits / history.pushState / JS redirects don't go
            # through browser_navigate so state.current_url would otherwise
            # stay stuck on the URL we last typed into navigate. Updating
            # here lets `vision_for_target_resolution` correctly invalidate
            # the epoch when the page changed under us.
            actual_url = data.get("url") or ""
            if actual_url and actual_url != state.current_url:
                state.record_url(actual_url)
        return data.get("elements", "")
    except Exception:
        return ""


def _build_network_block_message(
    status_code: int, url: str, block_class: str = "",
) -> str:
    """Structured message when a page returns 4xx/5xx — tells the worker to
    stop immediately rather than trying interactions on a blocked shell.

    This is distinct from CAPTCHA: CAPTCHA returns 200 + a challenge page.
    A 403/429/503 means the bot-detection edge refused to serve content at
    all, so no amount of clicking will help. The right move is to exit the
    worker via done(success=False) so the orchestrator can route to the
    search worker or escalate (proxy, TLS fingerprinting, etc.).

    EXCEPTION: Cloudflare Managed Challenge masquerades as a 403 "Just a
    moment" page — the challenge can often auto-pass given enough time +
    humanized interaction. When `block_class='cloudflare'`, route the
    agent to `browser_solve_captcha(method='auto')` first; only fall
    back to `done(success=False)` if the solver can't clear it.
    """
    if (block_class or "").lower() == "cloudflare":
        return (
            f"\n\n[CF_INTERSTITIAL status={status_code} url={url} "
            f"block_class=cloudflare]\n"
            f"Cloudflare Managed Challenge ('Just a moment...') detected. "
            f"This is NOT a permanent block — CF is scoring the session "
            f"and may auto-clear given more time + humanization.\n"
            f"ACTION: call browser_solve_captcha(method='auto') — the "
            f"dedicated CF waiter (up to 60s of humanized polling) is "
            f"wired to handle this. If the solver returns solved=false "
            f"with block_class=cloudflare, THEN call "
            f"done(success=False, final_answer='CF_INTERSTITIAL_STUCK: {url}') "
            f"so the orchestrator can escalate to residential proxy / "
            f"headful mode / search."
        )
    reason_hint = {
        401: "Authentication required — this page needs a logged-in session.",
        403: "Forbidden — site's bot detection refused at the network layer. No page interaction will help.",
        429: "Rate-limited — the site throttled our requests. Different IP may help.",
        451: "Blocked for legal reasons (geographic restriction likely).",
        503: "Service unavailable — could be bot detection (Cloudflare/Akamai) or real outage.",
    }.get(status_code, "Server returned an error status — page content is not usable.")
    return (
        f"\n\n[NETWORK_BLOCKED status={status_code} url={url}]\n"
        f"{reason_hint}\n"
        f"ACTION: do NOT attempt further interactions. Call "
        f"done(success=False, final_answer='NETWORK_BLOCKED: HTTP {status_code} at {url}') "
        f"so the orchestrator can escalate (try a different approach, search worker, or request proxy)."
    )


def _build_not_found_message(url: str) -> str:
    """Recoverable message for HTTP 404 — distinct from NETWORK_BLOCKED.

    Arch v3 fix: a 404 on a single navigation usually means the brain
    guessed a URL that doesn't exist. The right action is to navigate
    elsewhere on the SAME session, not to bail. The earlier template
    used the NETWORK_BLOCKED wording, which the brain echoed into its
    final_answer; the orchestrator then detected the substring and
    spawned a fresh worker — burning the live session for nothing.
    """
    return (
        f"\n\n[PAGE_NOT_FOUND status=404 url={url}]\n"
        f"The URL doesn't exist on this site. This is a navigation "
        f"error, NOT a site-level block — DO NOT call done(success=False) "
        f"and DO NOT classify this as a network block in your final_answer.\n"
        f"ACTION: stay on this session. Either:\n"
        f"  - browser_navigate back to a known-good URL (e.g. the "
        f"site root or the listing page you came from), or\n"
        f"  - browser_screenshot to see what's currently rendered, then "
        f"pick a different link from the visible UI rather than "
        f"guessing another deep URL."
    )


def _format_subgoal_note(
    state: "BrowserSessionState",
    step: Any,
    verified: bool,
    reason: str,
    max_attempts: int,
) -> str:
    """Compose the [subgoal_*] note that follows a state-change tool's
    caption when a TaskPlan is active. Mutates the step (mark_attempt +
    plan-cursor advance on success). Both the URL fast-path and the
    verify_after slow-path in `BrowserSessionState.check_active_task_step`
    funnel through here so the brain sees a uniform format.
    """
    step.mark_attempt(verified, reason=reason or "")
    if step.status == "satisfied":
        # Advance the cursor so the next tool sees the new active.
        next_step = state.task_plan.active_step() if state.task_plan else None
        tail = (
            f" Next: {next_step.name!r}" if next_step else " Plan complete."
        )
        return f"\n[subgoal_advanced: {step.name!r}]{tail}"
    if step.status == "unsatisfiable":
        return (
            f"\n[subgoal_unsatisfiable: {step.name!r} after "
            f"{step.attempts} attempts — last reason: "
            f"{step.last_failure_reason[:80]}] "
            f"Call browser_plan_skip_step / browser_plan_replan / "
            f"done(success=False) before the next click."
        )
    return (
        f"\n[subgoal_not_satisfied: {step.success_criteria.kind} "
        f"reason={reason} attempt {step.attempts}/{max_attempts}]"
    )


def _format_state(data: dict, state: "BrowserSessionState | None" = None) -> str:
    parts: list[str] = []
    # Leading structured marker that survives tool-result truncation. Even
    # when maxToolResultChars slices the trailing base64 image apart, these
    # first ~120 characters stay intact, so the worker's LLM can always see
    # "the tool succeeded; a session is open" and won't fire a redundant
    # browser_open.
    session_id = data.get("sessionId") or (state.session_id if state else "")
    url = data.get("url") or ""
    title = (data.get("title") or "").replace('"', "'")[:80]
    step = state.step_counter if state else 0
    if session_id or url:
        parts.append(
            f'[SESSION_STATE session_id={session_id or "?"} '
            f'url={url or "?"} title="{title}" step={step}]'
        )
    if data.get("url"):
        parts.append(f"URL: {data['url']}")
    if data.get("title"):
        parts.append(f"Title: {data['title']}")
    if data.get("scrollInfo"):
        si = data["scrollInfo"]
        parts.append(f"Scroll: {si.get('scrollY', 0)}/{si.get('scrollHeight', 0)} (viewport: {si.get('viewportHeight', 0)})")
    # Arch v4 (Step 3): full DOM element listing is no longer dumped
    # into the screenshot caption. The brain navigates by V_n bboxes
    # from vision; DOM crosscheck at click time still runs server-side.
    # Anchor harvest stays — the URL-hallucination guard still needs
    # to see legit hrefs from the rendered page.
    if data.get("elements") and state is not None:
        try:
            state.harvest_anchor_urls(data["elements"])
        except Exception:
            pass
    if data.get("consoleErrors"):
        parts.append(f"\nConsole errors: {data['consoleErrors']}")
    if data.get("pendingDialogs"):
        parts.append(f"\nPending dialogs: {data['pendingDialogs']}")
    return "\n".join(parts)


# ── Tool classes — each holds a reference to shared BrowserSessionState ──

@tool_parameters(
    tool_parameters_schema(
        url=StringSchema("URL to open (optional)", nullable=True),
        region=StringSchema("Region code for geo-restricted sites (e.g., 'bd', 'in')", nullable=True),
        proxy=StringSchema("Direct proxy URL (e.g., 'socks5://proxy:1080')", nullable=True),
        intent=StringSchema(
            "Optional hint describing what you want from the vision agent "
            "(e.g. 'check if login is required', 'find search box'). "
            "Only used when VISION_ENABLED=1.",
            nullable=True,
        ),
        tier=StringSchema(
            "Which anti-bot tier to open the session on. "
            "'auto' (default) reads per-domain learnings and picks t1 or t3. "
            "'t1' forces the TS Puppeteer backend. "
            "'t3' forces the in-process patchright (undetected Chromium) "
            "backend — required for Akamai/DataDome/PerimeterX targets.",
            enum=("auto", "t1", "t3"),
            nullable=True,
        ),
        required=[],
    )
)
class BrowserOpenTool(Tool):
    name = "browser_open"
    description = (
        "Open a new browser session. Returns a screenshot and interactive elements. "
        "For geo-restricted sites, pass region='bd' (Bangladesh), 'in' (India), etc. "
        "Pass tier='t3' for hardened anti-bot sites (Akamai/DataDome/PerimeterX); "
        "'auto' reads the learning system."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def _open_session_on_tier(
        self,
        tier_name: str,
        *,
        url: str | None,
        region: str | None,
        proxy: str | None,
        max_stealth: bool = False,
    ) -> Any:
        """Open a session on the given tier. Returns the raw data dict on
        success, or a plain string when the tier-open itself fails (rate
        limit, T3 launch exception). String returns bubble up unchanged
        so the caller can surface them to the agent.

        `max_stealth=True` is forwarded to the T3 manager and forces the
        heaviest fingerprint config (persistent profile + headful + auto-
        Xvfb). Used by the T1-failure escalation path to maximize the
        chance of getting through where the default T3 config still
        trips. Ignored for tier_name="t1" (TS-side stack has no
        equivalent knob).
        """
        if tier_name == "t3":
            from superbrowser_bridge.antibot import interactive_session as _t3mgr
            try:
                return await _t3mgr.default().open(
                    url,
                    task_id=self.s.task_id,
                    timeout_s=45.0,
                    max_stealth=max_stealth,
                )
            except Exception as exc:
                return (
                    f"[t3_open_failed] Could not open Tier-3 undetected "
                    f"Chromium session: {type(exc).__name__}: {str(exc)[:200]}"
                )
        # Default: T1 via the TS server.
        payload: dict[str, Any] = {}
        if url:
            payload["url"] = url
        if region:
            payload["region"] = region
        if proxy:
            payload["proxy"] = proxy
        if self.s.human_handoff_enabled:
            payload["enableHumanHandoff"] = True
            payload["humanHandoffBudget"] = self.s.human_handoff_budget

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/create",
            json=payload,
            timeout=45.0,
        )
        if r.status_code == 429:
            return (
                "[transient_rate_limit] Browser session service is busy "
                "(HTTP 429 after retries). This is a temporary rate limit, "
                "NOT a permanent outage. Wait ~30 seconds and call "
                "browser_open again. Do not switch to a different strategy."
            )
        r.raise_for_status()
        return r.json()

    async def execute(self, url: str | None = None, region: str | None = None, proxy: str | None = None, intent: str | None = None, tier: str | None = None, **kw: Any) -> Any:
        self.s.init_if_needed()

        # --- Tier selection ---------------------------------------------
        # 'auto' reads per-domain learnings; explicit 't1'/'t3' forces it.
        chosen_tier = (tier or "auto").lower()
        if chosen_tier == "auto":
            try:
                from urllib.parse import urlparse as _urlparse
                from superbrowser_bridge.routing import choose_starting_tier
                host = _urlparse(url or "").hostname or ""
                learned = choose_starting_tier(host) if host else 0
                # Tier ≥ 3 → open directly on t3. Tier 4 isn't interactive;
                # we still open on t3 and let the vision loop decide.
                chosen_tier = "t3" if learned >= 3 else "t1"
            except Exception:
                chosen_tier = "t1"

        # --- Idempotency guard ------------------------------------------
        # Two paths reach this tool with a live session already:
        #   1. The worker's LLM is in an amnesia loop (truncated/stripped
        #      screenshots → can't tell browser_open already ran) and is
        #      firing it again for the same URL.
        #   2. The orchestrator pre-seeded self.s.session_id from a
        #      resumption artifact (orchestrator_tools.py resumption path)
        #      and the worker's LLM ignored the "DO NOT call browser_open"
        #      instruction in the prompt.
        # In both cases creating a second real session is the bug — it
        # overwrites session_id with a throwaway and discards any progress.
        # Return a plain-string message (no image blocks, so truncation
        # can't mangle it) pointing the LLM at the right next tool.
        if self.s.session_id:
            # Arch v3: never raise WorkerMustExitError here. Killing the
            # worker over a tool-name confusion was the single biggest
            # cause of mid-task session restarts in arch v2. Counter is
            # kept for telemetry only; behavior below is idempotent.
            self.s.blocked_browser_open_count += 1
            same_url = (
                not url
                or self.s._normalize_url(url) == self.s._normalize_url(self.s.current_url)
            )
            print(
                f"\n>> browser_open BLOCKED (session already active: "
                f"{self.s.session_id}) — refusal #{self.s.blocked_browser_open_count}"
            )
            if same_url:
                return (
                    f"[SESSION_ALREADY_OPEN session_id={self.s.session_id} "
                    f"url={self.s.current_url}]\n"
                    f"A browser session is already active on this URL. "
                    f"DO NOT call browser_open again — it would discard your "
                    f"current page.\n"
                    f"Use one of these instead:\n"
                    f"  - browser_screenshot(session_id=\"{self.s.session_id}\") "
                    f"to see the current view\n"
                    f"  - browser_get_markdown(session_id=\"{self.s.session_id}\") "
                    f"to read the page text\n"
                    f"  - browser_click / browser_type to interact\n"
                    f"  - browser_navigate(session_id=\"{self.s.session_id}\", "
                    f"url=\"...\") to switch URLs on the same session"
                )
            # Arch v3: when a url is supplied AND it's different from the
            # active session's URL, transparently route to browser_navigate
            # on the existing session. This fixes the most common form of
            # the confusion ("LLM picked browser_open when it meant
            # browser_navigate") without losing progress. The brain still
            # sees a one-line note explaining what happened so it learns
            # the correct tool for next time.
            try:
                navigate_tool = BrowserNavigateTool(self.s)
                nav_result = await navigate_tool.execute(
                    session_id=self.s.session_id,
                    url=url,
                    intent=intent,
                )
            except Exception as exc:
                return (
                    f"[BROWSER_OPEN_AUTOROUTE_FAILED session_id={self.s.session_id}]\n"
                    f"You called browser_open with url={url!r} while a session "
                    f"was already active. I tried to auto-route to "
                    f"browser_navigate but it failed: "
                    f"{type(exc).__name__}: {str(exc)[:200]}\n"
                    f"Call browser_navigate(session_id=\"{self.s.session_id}\", "
                    f"url=\"{url}\") explicitly."
                )
            note = (
                f"[BROWSER_OPEN_AUTOROUTED session_id={self.s.session_id}]\n"
                f"You called browser_open with url={url!r} while session "
                f"{self.s.session_id} was already active; I auto-routed to "
                f"browser_navigate on the existing session. Use "
                f"browser_navigate directly next time.\n"
            )
            # If the navigate result is a list of content blocks (e.g.
            # screenshot variant), prepend the note as a text block.
            if isinstance(nav_result, list):
                return [{"type": "text", "text": note}] + nav_result
            if isinstance(nav_result, str):
                return note + nav_result
            return note + str(nav_result)

        self.s.reset_per_session()
        self.s.sessions_opened += 1

        print(f"\n>> browser_open(url={url}, region={region}, tier={chosen_tier}) [session #{self.s.sessions_opened}, screenshots left: {self.s.screenshot_budget}]")

        # Escalate=True when the caller wants T1→T3 auto-recovery. Kept
        # behind a flag so an explicit `tier='t1'` request from the agent
        # is honored without surprise upgrades.
        allow_escalation = (tier or "auto").lower() == "auto"
        data = await self._open_session_on_tier(
            chosen_tier, url=url, region=region, proxy=proxy,
        )
        if isinstance(data, str):
            # `_open_session_on_tier` returns a string on hard failure
            # (transient rate limit, t3 launch failure). Surface it.
            return data

        # --- T1 retry on soft failures -----------------------------------
        # A single 401/403/429/503 from T1 is almost never strong enough
        # evidence that the site needs the heavyweight T3 stack: it can be
        # a fingerprint flicker, a rate-limit hiccup, or a one-off WAF
        # challenge. Retry T1 once on a fresh session before paying the
        # cost of patchright + residential proxy. 502 is upstream-broken
        # — neither a retry nor T3 will help, so skip retry on 502 and
        # fall through to the escalation block (which itself handles 502).
        T1_SOFT_RETRY_CODES = (401, 403, 429, 503)
        status_code = data.get("statusCode") if isinstance(data, dict) else None
        if (
            allow_escalation
            and chosen_tier == "t1"
            and isinstance(status_code, int)
            and status_code in T1_SOFT_RETRY_CODES
        ):
            print(
                f"  [T1 retry after HTTP {status_code}] first attempt "
                f"flagged; retrying T1 with a fresh session before "
                f"escalating..."
            )
            t1_sid = (data or {}).get("sessionId", "")
            if t1_sid:
                try:
                    await _request_with_backoff(
                        "DELETE",
                        f"{SUPERBROWSER_URL}/session/{t1_sid}",
                        timeout=10.0,
                    )
                except Exception:
                    pass
            # Brief backoff so the second attempt is not back-to-back
            # against the same edge. 750ms is enough to clear most
            # short-lived WAF rate-limit windows.
            await asyncio.sleep(0.75)
            retry_data = await self._open_session_on_tier(
                "t1", url=url, region=region, proxy=proxy,
            )
            if isinstance(retry_data, str):
                # Tier-open itself failed (rate limit / launch error).
                # Surface the message — same contract as the first try.
                return retry_data
            data = retry_data
            status_code = data.get("statusCode") if isinstance(data, dict) else None

        # --- T1 → T3 auto-escalation -------------------------------------
        # When the Tier-1 Puppeteer path hits a hard anti-bot block
        # (401/403/429/502/503) before any content loads, the Tier-3
        # patchright + stealth stack is the right next hop. Close the
        # doomed T1 session, record the block so choose_starting_tier
        # prefers T3 next time, and re-open on T3 within this tool call
        # — the caller sees one consistent result regardless of which
        # tier actually served it. Only reached if the T1 retry above
        # also failed (or the original status was 502, which we don't
        # bother retrying).
        if (
            allow_escalation
            and chosen_tier == "t1"
            and isinstance(status_code, int)
            and status_code in (401, 403, 429, 502, 503)
        ):
            print(
                f"  [T1→T3 auto-escalation] HTTP {status_code} on T1 "
                f"after retry; retrying with patchright (T3)..."
            )
            # Clean up the blocked T1 session.
            t1_sid = (data or {}).get("sessionId", "")
            if t1_sid:
                try:
                    await _request_with_backoff(
                        "DELETE",
                        f"{SUPERBROWSER_URL}/session/{t1_sid}",
                        timeout=10.0,
                    )
                except Exception:
                    pass
            # Record the T1 block so next task on this domain starts on T3.
            # Deferred to here (post-retry) so a single transient 403 does
            # not poison the routing ledger.
            try:
                from urllib.parse import urlparse as _up
                from superbrowser_bridge.routing import _record_routing_outcome
                host = (_up(url or "").hostname or "").lower()
                if host:
                    block_class = (
                        "rate_limit" if status_code in (429, 503)
                        else "antibot_403"
                    )
                    _record_routing_outcome(
                        host, approach="browser", success=False,
                        tier=1, block_class=block_class,
                    )
            except Exception:
                pass
            # Re-open on T3 with MAX stealth (persistent profile +
            # headful + auto-Xvfb). The lighter T3 default is fine when
            # the agent picks tier="t3" up-front, but on escalation we've
            # already burned two T1 attempts on this domain — pay the
            # extra launch cost for the heaviest fingerprint we can ship
            # rather than risk a third stuck attempt.
            chosen_tier = "t3"
            data = await self._open_session_on_tier(
                "t3", url=url, region=region, proxy=proxy,
                max_stealth=True,
            )
            if isinstance(data, str):
                return data

        actual_url = data.get("url", url or "")
        self.s.session_id = data.get("sessionId", "")
        self.s.log_activity(f"browser_open({url or 'blank'})", f"session={data.get('sessionId', '?')}")
        self.s.record_url(actual_url)
        self.s.record_checkpoint(actual_url, data.get("title", ""), f"browser_open({url or 'blank'})")
        self.s.record_step("browser_open", url or "blank", f"session={data.get('sessionId', '?')}")
        self.s.consecutive_click_calls = 0

        # If human handoff is enabled, print the view URL to stdout so the
        # user can pre-open it in their browser. The view page polls the
        # /human-input endpoint and will show a banner the instant the
        # agent needs help, so having it open beforehand eliminates the
        # race where the agent blocks for 5 min before the user notices.
        #
        # For t3 sessions, the live viewer is served by the Python-side
        # aiohttp server (default :3101), NOT the TS server (:3100). The
        # browser_open call starts it on demand so the URL is live when
        # the user clicks it.
        if self.s.human_handoff_enabled and self.s.session_id:
            if chosen_tier == "t3":
                try:
                    from superbrowser_bridge.antibot import t3_viewer as _v
                    await _v.ensure_started()
                    view_url = _v.view_url(self.s.session_id)
                except Exception as exc:
                    print(f"  [t3 viewer failed to start: {exc}]")
                    view_url = ""
            else:
                public_host = os.environ.get(
                    "SUPERBROWSER_PUBLIC_HOST", SUPERBROWSER_URL.rstrip("/"),
                )
                view_url = f"{public_host}/session/{self.s.session_id}/view"
            if view_url:
                print(
                    f"\n>> [HUMAN HANDOFF ENABLED] Open this URL in your browser "
                    f"and keep it open:\n>>   {view_url}\n>> "
                    f"If the agent needs help, you'll see a banner there."
                )

        caption = _format_state(data, self.s)
        caption = f"Session: {data['sessionId']}\n{caption}"

        # Network-layer block detection (4xx/5xx). Fast-fails before the worker
        # wastes iterations on an unresponsive page. 404 is treated as fatal
        # here (wrong URL) but not a network block per se.
        #
        # Special-case Cloudflare: a 403 with `block_class=cloudflare` is
        # usually the Managed Challenge interstitial, not a permanent
        # refusal. Arm the nav-guard (so duplicate navigate without solve
        # is refused) and emit a caption that routes to browser_solve_captcha.
        status_code = data.get("statusCode")
        _block_class = (
            str(data.get("block_class") or data.get("blockClass") or "")
            .lower()
        )
        if isinstance(status_code, int):
            self.s.last_network_status = status_code
            if status_code >= 400 and status_code != 404:
                self.s.network_blocked = True
                caption += _build_network_block_message(
                    status_code, actual_url, block_class=_block_class,
                )
                if _block_class == "cloudflare":
                    self.s.last_nav_cf_blocked_url = self.s._normalize_url(actual_url)
                    self.s.nav_solve_called_since_block = False
                    self.s.record_step(
                        "browser_open", url or "blank",
                        f"CF_INTERSTITIAL status={status_code}",
                    )
                else:
                    self.s.record_step("browser_open", url or "blank", f"NETWORK_BLOCKED status={status_code}")
                return caption
            elif status_code == 404:
                # Arch v3: 404 is recoverable — do NOT route through the
                # NETWORK_BLOCKED template (which tells the brain to bail).
                caption += _build_not_found_message(actual_url)
                self.s.record_step(
                    "browser_open", url or "blank", f"HTTP 404 at {actual_url}",
                )
                return caption

        # Surface captcha detection from the server
        if data.get("captchaDetected"):
            ct = data["captchaDetected"]["type"]
            caption += (
                f"\n\n[CAPTCHA DETECTED: {ct}] "
                f"Call browser_solve_captcha(session_id='{data['sessionId']}', method='auto') to solve it."
            )

        # Show previous activity so agent knows what was already tried
        if self.s.sessions_opened > 1:
            activity = self.s.get_activity_summary()
            if activity:
                caption += activity

        if data.get("screenshot") and self.s.screenshot_budget > 0:
            self.s.screenshot_budget -= 1
            if actual_url:
                self.s.mark_screenshot_taken(
                    actual_url,
                    self.s.hash_page_content(data.get("elements", "") or data.get("title", "")),
                )
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent=intent or "observe opened page",
                url=actual_url,
                elements=data.get("elements"),
            )
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID from browser_open"),
        url=StringSchema("URL to navigate to"),
        intent=StringSchema(
            "Optional hint for the vision agent (e.g. 'verify navigation "
            "succeeded', 'find sign-up button'). Only used when "
            "VISION_ENABLED=1.",
            nullable=True,
        ),
        required=["session_id", "url"],
    )
)
class BrowserNavigateTool(Tool):
    name = "browser_navigate"
    description = "Navigate to a URL in an open browser session."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, url: str, intent: str | None = None, **kw: Any) -> Any:
        session_id = self.s.resolve_session_id(session_id)
        print(f"\n>> browser_navigate({url})")
        # Arch v4.3 (final): hard session-level navigate cap. Once the
        # brain has navigated past the initial cold-start, additional
        # navigates inside the task are almost always hallucinations
        # (constructed URLs that 404) or escape attempts (when stuck
        # on a filter UI). The brain should be using browser_click_at
        # on V_n bboxes for in-page navigation. Cap defaults to 1
        # in-task navigate (in addition to the implicit cold-start
        # done by browser_open). Override with NAVIGATE_SESSION_CAP.
        try:
            _nav_cap = int(os.environ.get("NAVIGATE_SESSION_CAP", "1"))
        except ValueError:
            _nav_cap = 1
        if _nav_cap > 0:
            _has_open_for_nav = False
            try:
                _b = getattr(self.s, "task_brief", None)
                if _b is not None and getattr(_b, "constraints", None):
                    _has_open_for_nav = any(
                        getattr(c, "status", "unverified") == "unverified"
                        for c in _b.constraints
                    )
            except Exception:
                _has_open_for_nav = False
            _used = int(getattr(self.s, "session_navigate_count", 0) or 0)
            if _has_open_for_nav and _used >= _nav_cap:
                self.s.record_step(
                    "browser_navigate", url,
                    f"BLOCKED: navigate_session_cap "
                    f"(used={_used}, cap={_nav_cap})",
                )
                print(
                    f"   [NAV_GUARD_FIRED] navigate_session_cap "
                    f"used={_used}/{_nav_cap} — refused {url}"
                )
                return (
                    f"[navigate_session_cap_exceeded used={_used}/{_nav_cap}] "
                    f"You've already navigated {_used} time(s) in this "
                    f"session and the [CHECKLIST] still has open "
                    f"constraints. Additional in-task navigates are "
                    f"refused — they are almost always hallucinated "
                    f"URLs or escape attempts. The brain should use "
                    f"browser_click_at on visible V_n bboxes for "
                    f"in-page transitions.\n"
                    f"  What to do instead:\n"
                    f"  1. browser_screenshot to refresh V_n.\n"
                    f"  2. browser_click_at on the V_n that matches "
                    f"your current focus (look for filter chips, "
                    f"section headers to expand, links inside the "
                    f"sidebar).\n"
                    f"  3. If you genuinely cannot make progress, "
                    f"call done(success=False, final_answer='honest "
                    f"reason') — the orchestrator decides whether "
                    f"to retry from a different angle.\n"
                    f"  Override: NAVIGATE_SESSION_CAP={_nav_cap+1} "
                    f"or higher."
                )
        # Arch v4.3 (Fix B): refuse navigate when the brain has been
        # spamming observation tools without a successful state-change.
        # Real-trace pattern: brain takes 3-5 screenshots / evals /
        # wait_fors in a row, gets stuck, then escapes by navigating
        # to a hallucinated URL. Block that escape valve.
        try:
            _threshold = int(os.environ.get("NAV_AFTER_OBS_THRESHOLD", "3"))
        except ValueError:
            _threshold = 3
        if _threshold > 0:
            _has_open = False
            try:
                _b = getattr(self.s, "task_brief", None)
                if _b is not None and getattr(_b, "constraints", None):
                    _has_open = any(
                        getattr(c, "status", "unverified") == "unverified"
                        for c in _b.constraints
                    )
            except Exception:
                _has_open = False
            _obs = int(getattr(self.s, "consecutive_non_progress_obs", 0) or 0)
            # Allow the FIRST navigate of the task (cold-start before
            # any state change happened) by checking that we already
            # have a current_url AND an open brief.
            _is_cold_start = not (self.s.current_url or "").strip()
            if _has_open and _obs >= _threshold and not _is_cold_start:
                self.s.record_step(
                    "browser_navigate", url,
                    f"BLOCKED: navigate_after_obs_spam (obs={_obs}, "
                    f"threshold={_threshold})",
                )
                print(
                    f"   [NAV_GUARD_FIRED] navigate_after_obs_spam "
                    f"obs={_obs}/{_threshold} — refused {url}"
                )
                return (
                    f"[navigate_after_obs_spam: {_obs} consecutive "
                    f"observation tool calls without a state-change] "
                    f"You've spent {_obs} turns reading the page "
                    f"without clicking anything that advanced the "
                    f"checklist. Don't escape by navigating to a new "
                    f"URL — this is the failure mode where the brain "
                    f"invents a URL when stuck, lands on a 404 / "
                    f"redirect, and burns budget.\n"
                    f"  What to do instead:\n"
                    f"  1. browser_screenshot — get fresh V_n bboxes.\n"
                    f"  2. Pick the V_n whose label matches your "
                    f"current focus (see [FOCUS] in the caption) and "
                    f"call browser_click_at(vision_index=V_n, "
                    f"target_label='...'). If multiple V_n look like "
                    f"matches, pick the one in the filter sidebar / "
                    f"the one closest to text describing your focus.\n"
                    f"  3. If genuinely stuck on this constraint, "
                    f"call done(success=False, final_answer='honest "
                    f"reason') — that's the explicit failure path.\n"
                    f"  Override (only for cold-start nav recovery): "
                    f"NAV_AFTER_OBS_THRESHOLD=0."
                )
        # Arch v3 fix (post-trace): refuse anchor-only navigations
        # ("/path#fragment" or just "#fragment"). Anchor URLs scroll the
        # page to the named element WITHOUT triggering a real page load,
        # so they bypass the freshness gate's clear path while still
        # rearranging visible state. Brain often uses them as a
        # roundabout "scroll to filter" — the right tool is browser_scroll
        # or browser_scroll_until.
        if os.environ.get("REFUSE_ANCHOR_NAVIGATE", "1") != "0":
            from urllib.parse import urlsplit as _urlsplit
            try:
                target_parts = _urlsplit(url)
                current_parts = _urlsplit(self.s.current_url or "")
                same_origin_path = (
                    bool(target_parts.fragment)
                    and (
                        # Pure fragment: "#region"
                        (not target_parts.scheme and not target_parts.netloc and not target_parts.path)
                        # OR same-origin same-path with only fragment differing
                        or (
                            target_parts.scheme == current_parts.scheme
                            and target_parts.netloc == current_parts.netloc
                            and (target_parts.path or "/") == (current_parts.path or "/")
                            and target_parts.query == current_parts.query
                        )
                    )
                )
                if same_origin_path:
                    sid = self.s.session_id or "<session_id>"
                    return (
                        f"[browser_navigate_anchor_refused url={url!r}]\n"
                        f"This is an anchor-only navigation (URL fragment "
                        f"{target_parts.fragment!r}) on the current page. "
                        f"Anchor URLs scroll the browser to the named "
                        f"element WITHOUT a real page load — they don't "
                        f"trigger the freshness gate's clear path and "
                        f"often confuse downstream V_n geometry.\n"
                        f"  Recovery options:\n"
                        f"  1. browser_scroll_until(session_id='{sid}', "
                        f"target_text='<text>') — scrolls until the named "
                        f"text is visible.\n"
                        f"  2. browser_click_at(V_n) — if the target is "
                        f"already in the viewport, just click it.\n"
                        f"Override: REFUSE_ANCHOR_NAVIGATE=0."
                    )
            except Exception:
                pass
        gate = await _feedback_gate("browser_navigate")
        if gate:
            return gate
        # Capture pre-nav URL for the per-step subgoal verification at
        # the bottom of this function (task_plan.py).
        _pre_url_for_subgoal = self.s.current_url or ""

        # --- Domain-pinning guard -----------------------------------------
        # When pinned_domain is set, only allow navigation to the target
        # domain (+ subdomains) and a small safe-list. Prevents the worker
        # LLM from visiting alternative sites when the target blocks it.
        if self.s.pinned_domain:
            from urllib.parse import urlparse as _urlparse
            # Safe-list = OAuth + CDN only. google.com stays on the list
            # (OAuth flow needs `accounts.google.com`, `accounts.youtube.com`,
            # etc.) but SEARCH paths on it are blocked below — observed
            # 2026-04-19: LLM would pivot to google.com/search whenever
            # the real target was slow, turning every task into a Google
            # scrape that 429'd and poisoned the session.
            _SAFE_DOMAINS = ("google.com", "googleapis.com", "gstatic.com", "google.co")
            try:
                _parsed = _urlparse(url)
                _target_host = (_parsed.hostname or "").lower().replace("www.", "")
                _target_path = _parsed.path or ""
                _target_query = _parsed.query or ""
            except Exception:
                _target_host = ""
                _target_path = ""
                _target_query = ""
            _pinned = self.s.pinned_domain
            _is_pinned = _target_host == _pinned or _target_host.endswith("." + _pinned)
            _is_safe = any(
                _target_host == sd or _target_host.endswith("." + sd)
                for sd in _SAFE_DOMAINS
            )
            # Block Google Search as an escape hatch — `google.com/search`,
            # `google.com/?q=`, `google.com/images`, etc. The LLM must stay
            # on the pinned domain even when it's frustrated.
            _is_google = _target_host == "google.com" or _target_host.endswith(".google.com") or _target_host.endswith(".google.co")
            _looks_like_search = _is_google and (
                _target_path.startswith("/search")
                or _target_path.startswith("/images")
                or _target_path.startswith("/maps")
                or "q=" in _target_query
            )
            if _target_host and (not (_is_pinned or _is_safe) or _looks_like_search):
                reason = "search_escape" if _looks_like_search else "outside_pin"
                self.s.record_step("browser_navigate", url, f"BLOCKED: {reason} (pinned={_pinned})")
                print(f"   [DOMAIN_PINNED] blocked navigation to {_target_host}{_target_path} ({reason}, pinned={_pinned})")
                return (
                    f"[DOMAIN_PINNED] Navigation to {url} is BLOCKED. "
                    f"You MUST stay on {_pinned} (and its subdomains). "
                    f"Do NOT pivot to Google Search or other sites when the "
                    f"target is slow or annoying — fix the problem on "
                    f"{_pinned} itself. If {_pinned} is hard-blocked, call "
                    f"browser_escalate (to Tier 3) or browser_solve_captcha "
                    f"or browser_ask_user, or report failure via "
                    f"done(success=False)."
                )

        # --- URL-hallucination guard --------------------------------------
        # Same-domain navigations to URLs that contain a UUID-looking
        # segment (8+ hex chars with dashes) AND were never observed
        # in any element listing get refused. This blocks the failure
        # mode where the brain abandons the filter UI and dreams up a
        # product slug like
        # `/catalog/2022-chefs-table-pinot-noir-willamette-valley_3f41d384-c690-4bbb-b493-b22fa16c87e3/`
        # that 404s and burns the screenshot budget. Conservative on
        # purpose: legitimate non-UUID paths pass through.
        if (
            self.s.pinned_domain
            and os.environ.get("NAVIGATE_HALLUCINATION_GUARD", "1") != "0"
        ):
            try:
                from urllib.parse import urlparse as _urlparse2
                _p = _urlparse2(url)
                _host = (_p.hostname or "").lower().replace("www.", "")
                _is_pin = _host == self.s.pinned_domain or _host.endswith(
                    "." + self.s.pinned_domain
                )
                if _is_pin:
                    import re as _re_uuid
                    # Match a UUID v4-ish slug or any hex run ≥ 12 chars in
                    # the path. The wineaccess hallucination matched the
                    # full UUID; shorter hash-like ids are also a strong
                    # signal of a slug the brain didn't observe.
                    _has_uuid = bool(_re_uuid.search(
                        r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                        r"|[0-9a-f]{16,})",
                        (_p.path or "").lower(),
                    ))
                    if _has_uuid and url not in self.s.observed_anchor_urls:
                        # Also tolerate the URL appearing as a substring
                        # within a longer observed href (some sites
                        # rewrite URLs across renders).
                        _seen_substr = any(
                            url in seen or seen in url
                            for seen in self.s.observed_anchor_urls
                            if isinstance(seen, str)
                        )
                        if not _seen_substr:
                            self.s.record_step(
                                "browser_navigate", url,
                                "BLOCKED: navigate_unverified (UUID-like path "
                                "not seen in any observed link)",
                            )
                            print(
                                f"   [NAV_GUARD_FIRED] navigate_unverified "
                                f"(UUID-like path not observed) — refused "
                                f"{url}"
                            )
                            return (
                                f"[navigate_unverified] {url} contains a UUID-like "
                                f"segment that was NEVER observed in any element "
                                f"listing on this session. Refusing to navigate — "
                                f"the brain may be hallucinating a product slug.\n"
                                f"  Recovery: call browser_screenshot to see the "
                                f"current page, then browser_click_at(vision_index=V_n) "
                                f"on a real link from the bboxes. Set "
                                f"NAVIGATE_HALLUCINATION_GUARD=0 to bypass."
                            )

                    # Arch v4.2: also refuse SAME-DOMAIN path-segment
                    # navigations that don't appear (even as substring)
                    # in any observed anchor href, when:
                    #   - The brain is mid-task (TaskBrief has open
                    #     constraints), AND
                    #   - We are NOT on the homepage / hub
                    #     (current_url has a non-trivial path), AND
                    #   - The target path differs by ≥ 1 segment from
                    #     the current path (i.e. the brain is guessing
                    #     a deeper/alternate path).
                    # Real trace pattern: brain expanded "Region" filter
                    # accordion, scrolled, didn't find Oregon
                    # immediately, then NAVIGATED to
                    # /store/white-wine/oregon/ — a constructed path
                    # the brain assumed exists. When the path is right
                    # (sometimes), this works; when it's wrong, the
                    # brain lands on a 404 or redirect and the task
                    # cascade begins. Forcing the brain back to UI
                    # click is robustly safer.
                    _has_open = False
                    try:
                        _b = getattr(self.s, "task_brief", None)
                        if _b is not None and getattr(_b, "constraints", None):
                            _has_open = any(
                                getattr(c, "status", "unverified") == "unverified"
                                for c in _b.constraints
                            )
                    except Exception:
                        _has_open = False
                    _cur = self.s.current_url or ""
                    try:
                        _cur_p = _urlparse2(_cur)
                        _cur_path = (_cur_p.path or "/").rstrip("/")
                    except Exception:
                        _cur_path = ""
                    _new_path = (_p.path or "/").rstrip("/")
                    _same_domain = _is_pin
                    # Arch v4.3 (final): the previous gate required
                    # `_cur_path != ""`. That broke the homepage case:
                    # path "/" rstrips to "" so the guard never fired
                    # when the brain was on the entry URL — exactly
                    # the moment most hallucinated navigates happen.
                    # Now: any new-path that differs from current
                    # qualifies, including `/collections/white-wine`
                    # from `/`.
                    _path_seg_change = (_new_path != _cur_path)
                    # Arch v4.3 Fix A: tighten _seen_anywhere — the
                    # previous version did `seen in url` substring
                    # check, which leaked. After
                    # `browser_open(https://www.wineaccess.com/)` the
                    # homepage URL is a substring of EVERY same-domain
                    # URL — so the guard never fired on path
                    # hallucinations. Now we check (a) exact full-URL
                    # match, (b) exact PATH match against the set of
                    # observed paths, (c) same-path-as-current, and
                    # (d) prefix `url.startswith(seen)` for tracking
                    # suffix tolerance. The reverse `seen in url` is
                    # gone.
                    _observed_paths: set[str] = set()
                    for _seen in self.s.observed_anchor_urls:
                        if not isinstance(_seen, str):
                            continue
                        try:
                            _sp = (_urlparse2(_seen).path or "/").rstrip("/")
                            _observed_paths.add(_sp)
                        except Exception:
                            continue
                    _seen_anywhere = (
                        url in self.s.observed_anchor_urls
                        or _new_path in _observed_paths
                        or _new_path == _cur_path
                        or any(
                            url.startswith(_seen)
                            for _seen in self.s.observed_anchor_urls
                            if isinstance(_seen, str)
                            and len(_seen) > 0
                            # Avoid a 1-char prefix matching everything;
                            # require the observed prefix to be at least
                            # half the candidate URL length so domain-
                            # only roots ("https://www.wineaccess.com/")
                            # don't match deep paths.
                            and len(_seen) > len(url) // 2
                        )
                    )
                    if (
                        _has_open
                        and _same_domain
                        and _path_seg_change
                        and not _seen_anywhere
                        and os.environ.get(
                            "NAVIGATE_PATH_HALLUCINATION_GUARD", "1"
                        ) != "0"
                    ):
                        self.s.record_step(
                            "browser_navigate", url,
                            f"BLOCKED: navigate_path_unverified "
                            f"(path {_new_path!r} not observed in any "
                            f"link; mid-task)",
                        )
                        print(
                            f"   [NAV_GUARD_FIRED] navigate_path_unverified "
                            f"path={_new_path!r} (not in {len(_observed_paths)} "
                            f"observed paths; cur={_cur_path!r}) — refused "
                            f"{url}"
                        )
                        return (
                            f"[navigate_path_unverified path={_new_path!r}] "
                            f"You're navigating to a same-domain path "
                            f"the brain has NEVER seen as an anchor "
                            f"href on this session, while the "
                            f"[CHECKLIST] still has open constraints. "
                            f"This is the classic 'guess a deeper URL' "
                            f"failure mode — most sites' URL schemes "
                            f"are not what the brain assumes (e.g. "
                            f"`/white-wine/oregon/` may not exist; the "
                            f"real path may be `/store/search/` with a "
                            f"region filter applied via the sidebar). "
                            f"Result is usually a 404 or redirect that "
                            f"burns budget.\n"
                            f"  Recovery:\n"
                            f"  1. browser_screenshot — get fresh V_n.\n"
                            f"  2. browser_click_at(V_n) on the visible "
                            f"link / filter chip / accordion that "
                            f"advances your current focus. If the "
                            f"target is in a collapsed accordion, "
                            f"click the section header first.\n"
                            f"  Override: NAVIGATE_PATH_HALLUCINATION_GUARD=0."
                        )
            except Exception as exc:
                # Guard must never break a legitimate navigate.
                print(f"   [navigate_hallucination_guard skipped: {exc}]")

        # --- v6: URL filter-hack guard ---------------------------------
        # When a TaskPlan is active AND the target URL has filter-shaped
        # query params (food_pairings=, region_slug=, max_price=, type=,
        # ordering=, category__in=, etc.), refuse — the brain should be
        # applying these via the filter UI (browser_inventory_filters +
        # browser_click_selector), not URL-hacking around it. The
        # observed wineaccess pattern was: brain inventories filters,
        # learns the URL params, then constructs
        # `?food_pairings=fish,sweets&max_price=40&...` directly,
        # bypassing the visual loop. Also bypasses subgoal verification
        # and locks the brain into URL-construction guesses on sites
        # whose filter params don't match. Kill switch
        # URL_FILTER_HACK_REFUSAL=0.
        # Arch v4.2: condition switched from `task_plan is not None` to
        # `task_brief has unverified constraints`. With v4.1 we removed
        # the task_plan tools, so the old condition was always False
        # and this guard never fired — the brain could happily
        # construct
        # `?category__in=white-wine&region_slug=oregon,washington,...`
        # URLs (observed in real wineaccess trace, lands on broken /
        # mis-filtered pages and burns the screenshot budget).
        _has_open_constraints = False
        try:
            _b = getattr(self.s, "task_brief", None)
            if _b is not None and getattr(_b, "constraints", None):
                _has_open_constraints = any(
                    getattr(c, "status", "unverified") == "unverified"
                    for c in _b.constraints
                )
        except Exception:
            _has_open_constraints = False
        if (
            (self.s.task_plan is not None or _has_open_constraints)
            and os.environ.get("URL_FILTER_HACK_REFUSAL", "1") != "0"
        ):
            try:
                from urllib.parse import urlparse as _urlparse3, parse_qs as _parse_qs
                _p3 = _urlparse3(url)
                _qs = _parse_qs(_p3.query) if _p3.query else {}
                # Filter-shaped param names. Conservative list — common
                # patterns across e-commerce / catalog sites. Doesn't
                # include generic q=, page=, sort= (legitimate without
                # filter intent).
                _FILTER_PARAM_HINTS = (
                    "food_pairings", "food_pairing", "region", "region_slug",
                    "regions", "category", "category__in", "categories",
                    "type", "types", "max_price", "min_price", "price",
                    "price_range", "ordering", "facet", "facets",
                    "filter", "filters", "amenities", "amenity",
                    "brand", "brands", "tag", "tags", "varietal",
                )
                _matched_params = [
                    k for k in _qs.keys()
                    if any(
                        hint == k or k.startswith(hint + "[") or k.endswith("[]")
                        for hint in _FILTER_PARAM_HINTS
                    )
                    or any(hint in k.lower() for hint in _FILTER_PARAM_HINTS)
                ]
                # Refuse only when ≥2 filter-shaped params present —
                # single-param navigation (e.g. just ?ordering=) is
                # a legitimate sort and not the URL-hack pattern.
                # Arch v4.2: also refuse when ANY one filter param has
                # ≥2 comma-separated values — that pattern is almost
                # always a brain hallucination (no UI lets you pick
                # 4 regions with one click). Real trace value:
                # `region_slug=willamette-valley,washington,united-states,oregon`
                _multi_value = any(
                    "," in v
                    for vs in _qs.values()
                    for v in vs
                    if isinstance(v, str)
                )
                if len(_matched_params) >= 2 or (_matched_params and _multi_value):
                    why = (
                        "open TaskBrief constraints"
                        if _has_open_constraints
                        else "TaskPlan active"
                    )
                    self.s.record_step(
                        "browser_navigate", url,
                        f"BLOCKED: url_filter_hack ({len(_matched_params)} "
                        f"filter params; multi_value={_multi_value}) — {why}",
                    )
                    print(
                        f"   [NAV_GUARD_FIRED] url_filter_hack "
                        f"params={_matched_params[:4]} multi_value={_multi_value} "
                        f"({why}) — refused {url}"
                    )
                    return (
                        f"[navigate_filter_hack_refused: {len(_matched_params)} "
                        f"filter-shaped query params: "
                        f"{', '.join(_matched_params[:6])}"
                        + (", multi-value filter detected"
                           if _multi_value else "")
                        + f"] You have unverified constraints and you're "
                        f"trying to URL-hack the filters instead of "
                        f"applying them via the UI. This bypasses the "
                        f"reconcile-on-page-state logic, breaks on sites "
                        f"whose param names / value formats you guessed "
                        f"wrong (typical result: a 404 or empty results "
                        f"page), and skips the visual loop the brain is "
                        f"supposed to use.\n"
                        f"  Recovery:\n"
                        f"  1. browser_screenshot — see the current filter "
                        f"panel. Vision will surface filter chips as V_n.\n"
                        f"  2. browser_click_at(vision_index=V_n) on each "
                        f"filter chip ONE AT A TIME. Constraints in "
                        f"`[CHECKLIST]` flip to `done` as the URL gains "
                        f"each filter value (reconcile_from_url).\n"
                        f"  3. If a filter is in a collapsed sidebar "
                        f"section, click the section header (its V_n) to "
                        f"expand it, THEN click the chip inside. Don't "
                        f"navigate-by-URL.\n"
                        f"  Single-param navigation (e.g. just ?ordering=) "
                        f"still works — this guard fires only on ≥2 "
                        f"filter params or any multi-value param. "
                        f"Override: URL_FILTER_HACK_REFUSAL=0."
                    )
            except Exception as exc:
                print(f"   [url_filter_hack_guard skipped: {exc}]")

        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0
        # Arch v4.3 (final): bump the session-level navigate counter
        # on every successful dispatch. Read by the cap guard at the
        # top of execute() to refuse 2nd+ in-task navigates.
        self.s.session_navigate_count += 1

        # CF-interstitial nav guard: if the last navigate to THIS URL was
        # Cloudflare-blocked and nothing has been done to resolve it, a
        # fresh page.goto will just re-trigger the same interstitial and
        # burn budget. Tell the agent to call browser_solve_captcha first.
        _norm_target = self.s._normalize_url(url)
        if (
            self.s.last_nav_cf_blocked_url
            and _norm_target == self.s.last_nav_cf_blocked_url
            and not self.s.nav_solve_called_since_block
        ):
            self.s.record_step(
                "browser_navigate", url,
                "BLOCKED: last navigate to this URL hit CF interstitial; "
                "call browser_solve_captcha first",
            )
            return (
                f"[CF_INTERSTITIAL_PENDING] The last navigate to {url} "
                f"landed on a Cloudflare Managed Challenge "
                f"('Performing security verification'). Re-navigating "
                f"before solving will just re-trigger the same challenge. "
                f"Call browser_solve_captcha(session_id='{session_id}', "
                f"method='auto') to wait for the interstitial to auto-"
                f"clear, THEN retry this navigate. If the solver also "
                f"fails, call browser_ask_user to hand off to a human."
            )

        # Detect regression before navigating
        regression = self.s.is_regression(url)
        if regression:
            self.s.regression_count += 1

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/navigate",
            json={"url": url},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()

        actual_url = data.get("url", url)
        self.s.log_activity(f"navigate({url})", f"title={data.get('title', '?')}")
        self.s.record_url(actual_url)
        # Drop the prior epoch — it belongs to the old page. The next
        # click will fall back to `_last_vision_response` (blank or
        # post-nav prefetch) via `vision_for_target_resolution`, and
        # the very next `browser_screenshot` re-freezes the epoch.
        self.s._vision_epoch_response = None

        # Set/clear the CF nav-guard based on what came back. `block_class`
        # is populated by interactive_session.py after the challenge wait
        # loop fails to clear. A navigate to any OTHER URL clears the
        # guard regardless — progress elsewhere means the stuck state is
        # gone.
        _block_class = (
            str(data.get("block_class") or data.get("blockClass") or "")
            .lower()
        )
        if _block_class == "cloudflare":
            self.s.last_nav_cf_blocked_url = self.s._normalize_url(actual_url)
            self.s.nav_solve_called_since_block = False
        elif _norm_target != self.s.last_nav_cf_blocked_url:
            # Navigated to a different URL that isn't CF-blocked — guard off.
            self.s.last_nav_cf_blocked_url = ""
            self.s.nav_solve_called_since_block = False

        # --- Chrome-error one-shot retry + T1→T3 escalation ---------------
        # T1 may hit chrome-error:// on deep links (ERR_HTTP2_PROTOCOL_ERROR
        # from Akamai/Imperva JA3 rejection). Retry once on the same
        # session; if that also fails and this is a T1 session, escalate
        # to T3 transparently.
        if (
            _block_class == "chrome_error"
            and os.environ.get("T1_NAV_CHROME_ERROR_RETRY", "1") != "0"
        ):
            _chrome_err = data.get("chrome_error_code", "")
            print(f"  [T1 chrome-error retry] {_chrome_err} — retrying once")
            await asyncio.sleep(1.0)
            try:
                _r2 = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/navigate",
                    json={"url": url},
                    timeout=30.0,
                )
                _r2.raise_for_status()
                data = _r2.json()
                actual_url = data.get("url", url)
                self.s.record_url(actual_url)
                _block_class = (
                    str(data.get("block_class") or data.get("blockClass") or "")
                    .lower()
                )
            except Exception as _exc:
                print(f"  [T1 chrome-error retry failed] {_exc}")

            # If retry still failed and session is T1, escalate to T3.
            if (
                _block_class == "chrome_error"
                and not session_id.startswith("t3-")
                and os.environ.get("T1_NAV_ESCALATION", "1") != "0"
            ):
                print(
                    f"  [T1→T3 navigate escalation] {_chrome_err} — "
                    f"opening T3 session for {url}"
                )
                _old_sid = session_id
                try:
                    from superbrowser_bridge.antibot import interactive_session as _t3mgr
                    _t3_data = await _t3mgr.default().open(
                        url,
                        task_id=self.s.task_id,
                        timeout_s=45.0,
                        max_stealth=True,
                    )
                    if isinstance(_t3_data, dict):
                        _new_sid = _t3_data.get("sessionId", "")
                        if _new_sid:
                            try:
                                await _request_with_backoff(
                                    "DELETE",
                                    f"{SUPERBROWSER_URL}/session/{_old_sid}",
                                    timeout=10.0,
                                )
                            except Exception:
                                pass
                            self.s._session_alias[_old_sid] = _new_sid
                            self.s.session_id = _new_sid
                            session_id = _new_sid
                            data = _t3_data
                            actual_url = data.get("url", url)
                            self.s.record_url(actual_url)
                            _block_class = (
                                str(data.get("block_class") or "").lower()
                            )
                            try:
                                from urllib.parse import urlparse as _up
                                from superbrowser_bridge.routing import _record_routing_outcome
                                _host = (_up(url or "").hostname or "").lower()
                                if _host:
                                    _record_routing_outcome(
                                        _host, approach="browser", success=False,
                                        tier=1, block_class=_chrome_err or "chrome_error",
                                    )
                            except Exception:
                                pass
                            print(
                                f"  [T1→T3 escalation OK] new session={_new_sid}"
                            )
                except Exception as _exc:
                    print(f"  [T1→T3 escalation failed] {_exc}")

        caption = _format_state(data, self.s)

        # Network-layer block detection — same logic as browser_open. Exit
        # early so the worker doesn't try to interact with a 403/429 shell.
        # CF interstitial gets the solve-captcha routing caption and the
        # nav-guard block set above.
        status_code = data.get("statusCode")
        if isinstance(status_code, int):
            self.s.last_network_status = status_code
            if status_code >= 400 and status_code != 404:
                self.s.network_blocked = True
                caption += _build_network_block_message(
                    status_code, actual_url, block_class=_block_class,
                )
                if _block_class == "cloudflare":
                    self.s.record_step(
                        "browser_navigate", url,
                        f"CF_INTERSTITIAL status={status_code}",
                    )
                else:
                    self.s.record_step(
                        "browser_navigate", url,
                        f"NETWORK_BLOCKED status={status_code}",
                    )
                return caption
            elif status_code == 404:
                # Arch v3: 404 is recoverable. Brain stays on the session
                # and navigates elsewhere instead of being told to bail.
                caption += _build_not_found_message(actual_url)
                self.s.record_step("browser_navigate", url, f"HTTP 404 at {actual_url}")
                return caption

        self.s.record_step("browser_navigate", url, f"title={data.get('title', '?')}")
        # Prefetch vision so the LLM's next browser_screenshot finds the
        # bboxes already cached.
        _schedule_vision_prefetch(self.s, session_id)

        if regression:
            caption += "\n[WARNING: You already visited this URL. Fix your approach on the CURRENT page instead of going backward. Do NOT restart from the beginning.]"

        # Surface captcha detection from the server
        if data.get("captchaDetected"):
            ct = data["captchaDetected"]["type"]
            caption += (
                f"\n\n[CAPTCHA DETECTED: {ct}] "
                f"Call browser_solve_captcha(session_id='{session_id}', method='auto') to solve it."
            )

        # Per-step subgoal verification — many TaskPlan steps are
        # navigation-shaped ("apply filter X" → URL gains a query
        # param), so check the active step's success_criteria here too.
        # Uses the in-memory pre_url captured before the navigation.
        subgoal_note = await self.s.check_active_task_step(
            session_id, pre_url=_pre_url_for_subgoal,
        )
        if subgoal_note:
            caption += subgoal_note
        if data.get("screenshot") and self.s.screenshot_budget > 0:
            self.s.screenshot_budget -= 1
            if actual_url:
                self.s.mark_screenshot_taken(
                    actual_url,
                    self.s.hash_page_content(data.get("elements", "") or data.get("title", "")),
                )
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent=intent or "verify navigation succeeded",
                url=actual_url,
                elements=data.get("elements"),
            )
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "Optional hint for the vision agent. Only used when "
            "VISION_ENABLED=1.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserScreenshotTool(Tool):
    name = "browser_screenshot"
    description = "Take a screenshot. COSTS MONEY. Use browser_get_markdown or browser_eval to verify instead."

    # Generic intent phrases that don't tell vision anything actionable.
    # When the brain's intent matches one of these, we replace it with
    # the active TaskPlan step's name (which is by definition specific).
    _GENERIC_INTENT_PATTERNS = (
        "ground", "see what", "look at", "observe", "inspect page",
        "verify", "check page", "current state", "what's on the page",
        "page content", "general observation",
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    def _enrich_intent_with_plan(self, intent: str | None) -> str | None:
        """Auto-inject the active focus constraint's text into the
        screenshot intent when the brain's intent is missing or generic.

        Arch v4.1 (Fix 2b): source switched from `task_plan.peek_active()`
        (deprecated, often stale) to `TaskBrief.constraints[focus_idx]`
        — the v4 single source of truth. The constraint's text is the
        brain's *current* sub-goal; vision biases bboxes toward elements
        that advance it, so V_1 lands on the right control instead of
        whatever was visually prominent.

        Returns the enriched intent (or the original if no brief / no
        focus / brain's intent is already specific).
        """
        brief = getattr(self.s, "task_brief", None)
        if brief is None:
            return intent
        try:
            focus_idx = int(getattr(brief, "current_focus_idx", -1))
        except Exception:
            return intent
        constraints = getattr(brief, "constraints", None) or []
        if not (0 <= focus_idx < len(constraints)):
            return intent
        c = constraints[focus_idx]
        # Compose a short focus phrase from canonical_value + kind.
        cv = (getattr(c, "canonical_value", "") or "").strip()
        text = (getattr(c, "text", "") or "").strip()
        kind = (getattr(c, "kind", "") or "").strip()
        focus_phrase = cv or text
        if not focus_phrase:
            return intent
        if kind:
            focus_phrase = f"{focus_phrase} ({kind})"
        # Empty intent → use focus phrase directly.
        clean_intent = (intent or "").strip()
        if not clean_intent:
            return f"advance constraint: {focus_phrase}"
        # Generic intent → replace.
        intent_lower = clean_intent.lower()
        is_generic = any(p in intent_lower for p in self._GENERIC_INTENT_PATTERNS)
        if is_generic:
            return f"advance constraint: {focus_phrase} ({clean_intent})"
        # Specific intent → keep, but append focus as context for vision.
        return f"{clean_intent} (focus: {focus_phrase})"

    async def execute(self, session_id: str, intent: str | None = None, **kw: Any) -> Any:
        session_id = self.s.resolve_session_id(session_id)
        # v7: auto-inject the active TaskPlan step's name into intent
        # when the brain's intent is missing or generic. Vision uses
        # `intent` to decide which bboxes are `intent_relevant=true`,
        # which in turn drives V_n ordering. A generic intent ("ground
        # before plan") leaves Gemini guessing — V_1 ends up being
        # whatever's visually prominent (often a header button) instead
        # of the actual task target. With the plan step injected
        # ("Apply Region=Oregon"), Gemini ranks the Region accordion /
        # Oregon checkbox as intent_relevant and V_1 becomes the right
        # click. Kill switch AUTO_INJECT_PLAN_INTENT=0.
        if os.environ.get("AUTO_INJECT_PLAN_INTENT", "1") != "0":
            intent = self._enrich_intent_with_plan(intent)
        # Peek current page content so dedup keys on (url, content_hash)
        # — a reload or DOM change produces a different hash and unblocks.
        peek_hash = ""
        try:
            peek_elements = await _fetch_elements(session_id, self.s)
            peek_hash = BrowserSessionState.hash_page_content(peek_elements)
        except Exception:
            pass

        allowed, reason = self.s.should_allow_screenshot(
            self.s.current_url, peek_hash, intent=intent or "",
        )
        if not allowed:
            self.s.log_activity("screenshot(BLOCKED)", reason[:60])
            return reason

        self.s.screenshot_budget -= 1
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            # bounds=true returns selectorEntries (with x/y/width/height) +
            # devicePixelRatio so we can draw bbox overlays before the
            # screenshot goes to the vision LLM.
            params={"vision": "true", "bounds": "true"},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()

        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.mark_screenshot_taken(
                actual_url,
                self.s.hash_page_content(data.get("elements", "")),
            )
        # Arch v4.4: reset the brain-screenshot mutation counter NOW that
        # the brain is about to see fresh page state. This is the only
        # place this counter resets — internal vision prefetches don't
        # touch it.
        self.s.mutations_since_brain_screenshot = 0
        self.s.log_activity(f"screenshot({actual_url[:50] if actual_url else '?'})")
        self.s.record_step("browser_screenshot", "", f"url={actual_url[:60] if actual_url else '?'}")
        caption = _format_state(data, self.s)
        caption += f"\n[Screenshots remaining: {self.s.screenshot_budget}]"
        if data.get("screenshot"):
            entries = data.get("selectorEntries") or []
            dpr = float(data.get("devicePixelRatio") or 1.0)
            # Rename tagName → tag for the overlay (both naming schemes work
            # but tag is the overlay's canonical key).
            overlay_elements = [
                {
                    "index": e.get("index"),
                    "tag": e.get("tagName") or e.get("tag"),
                    "role": e.get("role") or (e.get("attributes") or {}).get("role"),
                    "bounds": e.get("bounds"),
                }
                for e in entries
                if e.get("bounds") and e.get("index") is not None
            ]
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent=intent or "observe page",
                url=actual_url,
                elements=data.get("elements"),
                elements_with_bounds=overlay_elements,
                device_pixel_ratio=dpr,
            )
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "What you're trying to find this time. The careful pass uses "
            "this to bias bbox emphasis — be specific: 'find the WiFi "
            "filter checkbox', not 'look at the page'.",
            nullable=True,
        ),
        expected_labels=ArraySchema(
            description=(
                "Optional list of element labels you EXPECT to be on the "
                "screen but didn't see in the previous pass (e.g. "
                "['WiFi included', 'Cleaning service']). Threaded into "
                "the coverage prompt so Gemini specifically searches for "
                "them and emits a tight bbox if it can find them."
            ),
            items=StringSchema("Expected label"),
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserLookAgainTool(Tool):
    name = "browser_look_again"
    description = (
        "Take a fresh screenshot and run a CAREFUL vision pass on it: "
        "bypass the cache, lift the bbox cap to 60, disable page-type "
        "culling, and use the larger fallback model if VISION_FALLBACK_MODEL "
        "is configured. Optional `expected_labels` are threaded into the "
        "coverage prompt so vision specifically looks for them. "
        "Use this when: (1) the previous render showed [VISION_MAY_BE_STALE], "
        "(2) browser_click_at failed twice with stale_index, (3) an element "
        "you know exists isn't in the bbox list, or (4) confidence on the "
        "candidate target is below 0.6. Higher-cost than browser_screenshot "
        "(more tokens, possibly the larger model) — don't reach for it as a "
        "default."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        intent: str | None = None,
        expected_labels: list[str] | None = None,
        **kw: Any,
    ) -> Any:
        session_id = self.s.resolve_session_id(session_id)
        eff_intent = intent or "look again carefully — coverage pass"
        labels_disp = (
            f", expected={expected_labels[:5]}"
            if expected_labels else ""
        )
        print(f"\n>> browser_look_again({eff_intent!r}{labels_disp})")

        # Skip the budget-guarded fast path; this tool is meant for
        # recovery, so the brain pays one screenshot cost regardless of
        # the dedup state.
        self.s.screenshot_budget -= 1
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            params={"vision": "true", "bounds": "true"},
            timeout=20.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[look_again_failed] HTTP {r.status_code}: {err}"
        data = r.json()
        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.mark_screenshot_taken(
                actual_url,
                self.s.hash_page_content(data.get("elements", "")),
            )
        self.s.record_step(
            "browser_look_again",
            f"intent={eff_intent[:60]} labels={(expected_labels or [])[:3]}",
            f"url={actual_url[:60] if actual_url else '?'}",
        )
        caption = _format_state(data, self.s)
        caption += f"\n[Look-again: coverage_mode + force_fresh{' + tier-up' if os.environ.get('VISION_FALLBACK_MODEL') else ''}]"
        if not data.get("screenshot"):
            return caption
        entries = data.get("selectorEntries") or []
        dpr = float(data.get("devicePixelRatio") or 1.0)
        overlay_elements = [
            {
                "index": e.get("index"),
                "tag": e.get("tagName") or e.get("tag"),
                "role": e.get("role") or (e.get("attributes") or {}).get("role"),
                "bounds": e.get("bounds"),
            }
            for e in entries
            if e.get("bounds") and e.get("index") is not None
        ]
        return await self.s.build_tool_result_blocks(
            data["screenshot"],
            caption,
            intent=eff_intent,
            url=actual_url,
            elements=data.get("elements"),
            elements_with_bounds=overlay_elements,
            device_pixel_ratio=dpr,
            coverage_mode=True,
            expected_labels=expected_labels,
            force_fresh=True,
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index"),
        button=StringSchema("Mouse button: left, right, middle", nullable=True),
        required=["session_id", "index"],
    )
)
class BrowserClickTool(Tool):
    name = "browser_click"
    description = "Click an interactive element by its [index] number."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, button: str | None = None, **kw: Any) -> Any:
        gate = self.s.must_screenshot_before_state_change("browser_click")
        if gate:
            return gate
        session_id = self.s.resolve_session_id(session_id)
        print(f"\n>> browser_click([{index}])")
        # DOM-index click refusal: refuse whenever ANY vision response
        # exists for this session. The prior age-≤2-turns gate let
        # browser_click([N]) through after a few intermediate actions
        # (observed wineaccess Worker 2 step 13: clicked [26] when
        # vision was 4+ turns old). Loosening to "always refuse if
        # vision has bboxes" matches the prompt's AVOID-tier ranking
        # for this tool — the brain can always browser_screenshot to
        # refresh V_n. Kill switch CLICK_INDEX_REFUSAL=0.
        if os.environ.get("CLICK_INDEX_REFUSAL", "1") != "0":
            try:
                resp = self.s._last_vision_response
                bbox_count = len(getattr(resp, "bboxes", []) or []) if resp else 0
                if bbox_count > 0:
                    age_turns = max(
                        0,
                        self.s._brain_turn_counter - self.s._vision_epoch_turn,
                    )
                    age_note = (
                        f"{age_turns} turns old"
                        if age_turns > 0
                        else "fresh"
                    )
                    return (
                        f"[click_index_refused: vision has {bbox_count} "
                        f"bboxes ({age_note})] browser_click(index={index}) "
                        f"uses the volatile DOM-index path AND dispatches "
                        f"via el.click() (isTrusted=false; rejected by "
                        f"Cloudflare/Akamai). The latest vision pass "
                        f"labelled targets as [V_1]..[V_{bbox_count}]. "
                        f"Recovery:\n"
                        f"  1. If vision is older than this turn, "
                        f"browser_screenshot first to refresh.\n"
                        f"  2. Read the V_n LABELS in the screenshot reply "
                        f"— pick the V_n whose label matches your intent "
                        f"(NOT just V_1).\n"
                        f"  3. browser_click_at(vision_index=V_n) — "
                        f"humanized cursor, isTrusted=true, pixel-exact.\n"
                        f"  4. OR browser_click_selector(<#id-or-data-testid>) "
                        f"for a stable hook from browser_inventory_filters.\n"
                        f"Override: CLICK_INDEX_REFUSAL=0."
                    )
            except Exception:
                pass
        gate = await _feedback_gate("browser_click")
        if gate:
            return gate
        # Phase 1.1: hard sync gate. Wait for any in-flight vision
        # prefetch from the previous action before dispatching.
        sync_block = await self.s.ensure_vision_synced(reason="browser_click")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        # Cross-index flail guard. If the last two clicks timed out,
        # force a re-screenshot before dispatching another HTTP click —
        # the backend is hung (blocker, loader, nav in flight) and
        # walking [N±1] just wastes the iteration budget.
        if self.s.consecutive_click_timeouts >= self.s.MAX_CONSECUTIVE_CLICK_TIMEOUTS:
            alts = _vision_alternatives_hint(self.s, limit=3)
            self.s.log_activity(
                f"click([{index}])(LOOP_BLOCKED)",
                f"timeouts={self.s.consecutive_click_timeouts}",
            )
            return (
                f"[click_loop_detected] {self.s.consecutive_click_timeouts} "
                f"consecutive click timeouts. The page is likely blocked "
                f"(loader, modal, or a pending navigation). Call "
                f"browser_screenshot to refresh vision before any further "
                f"click."
                + (f"\n{alts}" if alts else "")
            )
        stale = _stale_dom_index_block(
            self.s, tool_name="browser_click", target_disp=f"[{index}]",
        )
        if stale:
            return stale
        target_key = f"click[{index}]"
        dead = self.s.check_dead_click(target_key)
        if dead:
            self.s.log_activity(f"click([{index}])(DEAD_CLICK_BLOCKED)", "")
            return dead
        self.s.register_click_attempt(target_key)
        self.s.consecutive_click_calls += 1
        payload: dict[str, Any] = {"index": index}
        if button:
            payload["button"] = button
        # Send the fingerprint the LLM was targeting. If the DOM shifted,
        # the TS side returns 409 + stale_index with a suggested new index.
        cached_fp = self.s.element_fingerprints.get(index)
        if cached_fp:
            payload["expected_fingerprint"] = cached_fp
        elif self.s.element_fingerprints:
            # The cache has entries, just not for this index — the brain
            # is addressing an index that wasn't in the last state
            # response. Almost always means stale. Surface fast instead
            # of letting the TS click fail obscurely.
            await _fetch_elements(session_id, self.s)
            if index not in self.s.element_fingerprints:
                return (
                    f"[click_failed:unknown_index] [{index}] is not in "
                    f"the current selectorMap (fingerprints={len(self.s.element_fingerprints)} "
                    f"indices). Re-read the elements list and pick a "
                    f"valid index, or use browser_click_at(V_n) with a "
                    f"vision bbox."
                )
            cached_fp = self.s.element_fingerprints.get(index)
            if cached_fp:
                payload["expected_fingerprint"] = cached_fp

        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/click",
                json=payload,
                timeout=30.0,
            )
            # 409 = stale-index guard fired. Surface the suggested
            # index (if any) so the LLM retargets instead of blindly
            # retrying or falling back to click_at coords.
            if r.status_code == 409:
                info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                stale_msg = info.get("error", "Stale index")
                suggested = info.get("suggested_index")
                current = info.get("current_element", "")
                hint = f" Try [{suggested}]." if suggested is not None else " Re-read elements list and pick again."
                result = f"[stale_index] {stale_msg} Current [{index}] is {current}.{hint}"
                self.s.log_activity(f"click([{index}])(STALE)", f"suggested={suggested}")
                await _fetch_elements(session_id, self.s)
                return result
            # 400 = structured TS-side failure (element not found,
            # not visible, disabled, etc.). Parse and return an
            # actionable message to the LLM.
            if r.status_code == 400:
                info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                reason = info.get("reason", "unknown")
                err = info.get("error", f"click [{index}] failed")
                alternatives = info.get("alternatives") or []
                await _fetch_elements(session_id, self.s)
                self.s.log_activity(f"click([{index}])({reason})", err[:60])
                alt_lines = "\n".join(f"  - {a}" for a in alternatives[:3]) if alternatives else ""
                fresh_hint = "\nElements have been re-read above — pick a current [index]."
                # Phase 3.1: cursor failure ledger.
                self.s.record_cursor_failure(
                    strategy="click",
                    target=f"[{index}]",
                    reason=f"{reason}: {err[:80]}",
                )
                return (
                    f"[click_failed:{reason}] {err}"
                    + (f"\nAlternatives:\n{alt_lines}" if alt_lines else "")
                    + fresh_hint
                )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            # Opaque 4xx/5xx (not 400/409). Usually network-layer.
            self.s.log_activity(f"click([{index}])(HTTP{e.response.status_code})", str(e)[:60])
            return (
                f"[click_failed:http_{e.response.status_code}] {e.response.text[:200] if e.response.text else str(e)[:200]}"
            )
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout) as e:
            # Click dispatched but the backend never responded — almost
            # always means the page is blocked (a pending navigation, a
            # loader still running, or an overlay intercepting events).
            # Count it so the flail guard above trips on the next call.
            self.s.consecutive_click_timeouts += 1
            self.s.log_activity(
                f"click([{index}])(TIMEOUT)",
                f"count={self.s.consecutive_click_timeouts}",
            )
            alts = _vision_alternatives_hint(
                self.s, exclude_index=None, limit=3,
            )
            return (
                f"[click_failed:timeout] The backend didn't respond to "
                f"click([{index}]) within the HTTP timeout. The page is "
                f"likely waiting on navigation or blocked by a loader. "
                f"Call browser_screenshot to re-vision before retrying."
                + (f"\n{alts}" if alts else "")
            )
        except Exception as e:
            # True transport error (connection refused, etc.). Server down.
            self.s.log_activity(f"click([{index}])(TRANSPORT)", str(e)[:60])
            return f"[click_failed:transport] {str(e)[:200]} — browser service unreachable. Retry in a few seconds."

        # Successful HTTP response — clear the timeout counter so the
        # flail guard doesn't trip on a future unrelated hiccup.
        self.s.consecutive_click_timeouts = 0
        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.record_url(actual_url)
        # Snap telemetry (P3.12).
        snap = data.get("snap") if isinstance(data, dict) else None
        if isinstance(snap, dict) and snap.get("snapped") is False:
            self.s.snap_miss_count += 1
        self.s.log_activity(f"click([{index}])", f"url={actual_url[:50] if actual_url else '?'}")
        self.s.record_step("browser_click", f"index={index}", f"url={actual_url[:60] if actual_url else '?'}")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, f"Clicked [{index}]"),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index (the V_n the vision agent "
                "labelled this element). When set, the server snaps to "
                "the interactive element inside that bbox — far more "
                "accurate than clicking a guessed (x,y)."
            ),
            nullable=True,
        ),
        x=NumberSchema(description="X coordinate (CSS pixel). Ignored when vision_index is set.", nullable=True),
        y=NumberSchema(description="Y coordinate (CSS pixel). Ignored when vision_index is set.", nullable=True),
        target_label=StringSchema(
            description=(
                "REQUIRED when vision_index is set. The label you read "
                "for V_n in the most recent screenshot reply (e.g. "
                "'Wine Facts', 'Oregon checkbox', 'Sign in button'). "
                "The bridge fuzzy-substring-matches this against the "
                "actual V_n label vision emitted; refuses with the real "
                "labels listed inline if they don't match. This catches "
                "the V_1-reflex pattern where the brain picks an index "
                "without decoding what V_n actually is. Ignored when "
                "using raw (x, y)."
            ),
            nullable=True,
        ),
        narration=StringSchema(
            description=(
                "Optional one-sentence narration of WHY you're clicking "
                "this V_n and what you expect to happen. Stored on state; "
                "rendered back in the next turn's guidance as "
                "`[last_intended: ...]` so you can compare your prior "
                "intent against the actual outcome. Never required."
            ),
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserClickAtTool(Tool):
    name = "browser_click_at"
    description = (
        "Click using a vision bbox (vision_index=V_n) or raw (x,y) "
        "coordinates. Prefer vision_index whenever the vision agent "
        "labelled the target — the server snaps to the actual interactive "
        "element inside the bbox, eliminating off-by-pixel misses."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        vision_index: int | None = None,
        x: float | None = None,
        y: float | None = None,
        target_label: str | None = None,
        narration: str | None = None,
        internal_retry: bool = False,
        **kw: Any,
    ) -> Any:
        # Arch v4.3 (Fix C): refuse coordinate-only clicks when V_n is
        # available. Real-trace pattern: brain failed to find a label
        # via scroll_until / wait_for, then blind-clicked at (247, 1092)
        # with no vision_index and no target_label — bypassing every
        # protective layer (label-mismatch refusal, freshness gate's
        # V_n staleness check, auto-remap). Coord clicks are valid only
        # when vision returned 0 bboxes (genuine fallback) or for
        # captcha/puzzle solvers with no TaskBrief.
        if (
            vision_index is None
            and not (target_label and target_label.strip())
            and x is not None
            and y is not None
            and not internal_retry
            and os.environ.get("REFUSE_BLIND_COORD_CLICK", "1") != "0"
        ):
            _vresp = getattr(self.s, "_last_vision_response", None)
            _bbox_count = 0
            if _vresp is not None:
                _bbox_count = len(getattr(_vresp, "bboxes", None) or [])
            _has_open = False
            try:
                _b = getattr(self.s, "task_brief", None)
                if _b is not None and getattr(_b, "constraints", None):
                    _has_open = any(
                        getattr(c, "status", "unverified") == "unverified"
                        for c in _b.constraints
                    )
            except Exception:
                _has_open = False
            if _bbox_count >= 1 and _has_open:
                self.s.record_step(
                    "browser_click_at", f"(x={x},y={y})",
                    f"BLOCKED: blind_coord_click (no vision_index, no "
                    f"target_label; {_bbox_count} V_n available)",
                )
                print(
                    f"   [CLICK_GUARD_FIRED] blind_coord_click "
                    f"x={x} y={y} bbox_count={_bbox_count} — refused"
                )
                return (
                    f"[click_at_blind_coords_refused] You called "
                    f"click_at(x={x}, y={y}) with no vision_index AND "
                    f"no target_label, while the most recent screenshot "
                    f"has {_bbox_count} V_n bboxes available and "
                    f"[CHECKLIST] still has open constraints. "
                    f"Coordinate-only clicks bypass label-match "
                    f"verification, the freshness gate's V_n staleness "
                    f"check, and the auto-remap. They are the failure "
                    f"mode the brain falls into when it can't find a "
                    f"V_n match.\n"
                    f"  Recovery:\n"
                    f"  1. browser_screenshot — get a fresh V_n list "
                    f"if you suspect the prior list is stale.\n"
                    f"  2. Pick the V_n whose label matches your "
                    f"intent (see [FOCUS] in the caption) and call "
                    f"browser_click_at(vision_index=V_n, "
                    f"target_label='...').\n"
                    f"  3. If the V_n you want is below the fold, "
                    f"browser_scroll_until(target_text='...') first, "
                    f"then screenshot, then V_n click.\n"
                    f"  Override (captcha / puzzle solvers): "
                    f"REFUSE_BLIND_COORD_CLICK=0."
                )
        # Arch v4 Move 3 — internal retries inherit the original call's
        # preplan_lock + freshness clearance. Set the flag here; the
        # gate skips its consume checks under it. Cleared at the end.
        if internal_retry:
            self.s._bbox_auto_retry_in_flight = True
        # Arch v3 fix #5: state-freshness gate. Refuse if the brain is
        # chaining a click without a screenshot since the last mutation.
        gate = self.s.must_screenshot_before_state_change("browser_click_at")
        if gate:
            return gate
        # v5: stash optional narration so worker_hook renders it back as
        # [last_intended: ...] on the next turn. Never required, never
        # refused. Pure scaffolding for chain-of-thought trail.
        if narration:
            self.s._last_narration = str(narration)[:240]
        # Arch v4 Phase K — auto-resolve vision_index by target_label.
        # When V_N's label doesn't substring-match target_label but
        # another V_M does (per Phase F's fuzzy matcher), silently
        # remap so the click executes against the right element. This
        # eliminates the [click_at_label_mismatch] wall-of-labels
        # refusal that pushes the brain off click_at and into eval /
        # run_script / navigate when vision shifts a few V_n positions
        # between actions. The remap note is prepended to the result
        # caption so the brain sees what happened.
        remap_note: str | None = None
        if vision_index is not None and target_label:
            try:
                _vresp_for_remap = getattr(
                    self.s, "_last_vision_response", None,
                )
                _bboxes_for_remap = list(
                    getattr(_vresp_for_remap, "bboxes", None) or []
                )
                if _bboxes_for_remap:
                    new_idx, _note = _resolve_vision_index_by_label(
                        self.s, int(vision_index), target_label,
                        _bboxes_for_remap,
                    )
                    if _note is not None:
                        vision_index = new_idx
                        remap_note = _note
            except Exception:
                # Helper must never break the click — fall through.
                pass
        # v8: vision-grounded click protocol. When vision_index is set,
        # the brain MUST pass target_label naming what V_n is. Bridge
        # validates against the actual label vision emitted; refuses on
        # mismatch with real labels listed inline. This catches the
        # V_1-reflex pattern where the brain picks an index without
        # decoding what V_n actually represents. Runs BEFORE the sync
        # gate / brain_turn increment so a refusal doesn't waste the
        # turn budget. Kill switch VISION_TARGET_LABEL_REQUIRED=0.
        # Phase K — skip when a remap fired: Phase F's matcher is more
        # permissive (Levenshtein / token overlap) than the substring
        # check here, and the remap already verified intent alignment.
        if remap_note is None:
            label_refuse = _validate_target_label(
                self.s, vision_index, target_label,
            )
            if label_refuse:
                return label_refuse
        # Phase 1.1: hard sync gate. Block until the in-flight vision
        # prefetch from the previous action lands — without this the
        # brain's V_n resolves against a frozen epoch but the freshness
        # gate has no fresh post-action vision to validate against.
        sync_block = await self.s.ensure_vision_synced(reason="browser_click_at")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        self.s.click_at_count += 1
        self.s.consecutive_click_calls += 1
        if self.s.click_at_count > self.s.MAX_CLICK_AT:
            return (
                f"[BLOCKED] browser_click_at used "
                f"{self.s.click_at_count} times in this session — "
                f"runaway click loop. Call browser_screenshot to re-"
                f"observe; if the page is stuck call "
                f"browser_rewind_to_checkpoint, browser_navigate, or "
                f"browser_request_help. Do NOT attempt "
                f"browser_run_script to click — JS clicks are "
                f"isTrusted=false and bot-detected."
            )

        # Build the target key BEFORE resolving the bbox, so the guard
        # fires on intent (vision_index=V3) not on resolved coords (which
        # could shift slightly between calls due to anti-aliasing).
        if vision_index is not None:
            target_key = f"click_at(V{int(vision_index)})"
        elif x is not None and y is not None:
            # Round to a 5px grid — micro-jitter shouldn't escape the guard.
            target_key = f"click_at({round(float(x)/5)*5},{round(float(y)/5)*5})"
        else:
            target_key = "click_at(?)"
        dead = self.s.check_dead_click(target_key)
        if dead:
            self.s.log_activity(f"click_at{target_key}(DEAD_CLICK_BLOCKED)", "")
            return dead
        self.s.register_click_attempt(target_key)

        payload: dict[str, Any]
        log_target: str
        if vision_index is not None:
            # Prefer the frozen epoch (what the brain SAW on its last
            # screenshot), fall back to the live response only when no
            # epoch is set yet (pre-first-screenshot path / tests).
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return (
                    "[click_at_failed:no_vision] No recent vision response "
                    "to resolve vision_index against. Re-fetch state to "
                    "trigger a fresh vision pass, or pass raw (x, y)."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                return (
                    f"[click_at_failed:bad_vision_index] V{vision_index} "
                    f"is out of range (only {len(resp.bboxes)} bboxes in "
                    "the last vision response)."
                )
            # Oversized-bbox guard. Vision sometimes emits a bbox covering
            # an entire hero/banner/CTA band instead of the specific
            # control inside it; clicks land at the geometric center of
            # that swath, hitting whitespace or the wrong element. box_2d
            # is normalized to [0, 1000], so the threshold math is
            # viewport-independent.
            #   - "banner shape": >40% width AND <8% height (very wide,
            #     thin band — almost always wrong target)
            #   - "huge area": area > 40% of viewport (whole sections,
            #     not a single control)
            # Recovery: browser_look_again with the intended label as
            # expected_labels — coverage mode usually emits tighter per-
            # element bboxes. Opt-out via VISION_BBOX_SIZE_GUARD=0.
            if os.environ.get("VISION_BBOX_SIZE_GUARD", "1") != "0":
                try:
                    ymin, xmin, ymax, xmax = bbox.box_2d
                    bb_w = max(0, int(xmax) - int(xmin))
                    bb_h = max(0, int(ymax) - int(ymin))
                    bb_area = bb_w * bb_h
                    is_banner = bb_w > 400 and bb_h < 80
                    is_huge_area = bb_area > 400_000  # >40% of 1000x1000
                    if is_banner or is_huge_area:
                        shape = "banner" if is_banner else "huge_area"
                        label = (getattr(bbox, "label", "") or "?")[:40]
                        return (
                            f"[click_at_failed:bbox_too_wide shape={shape} "
                            f"w={bb_w} h={bb_h} area={bb_area}] V{vision_index} "
                            f"({label!r}) covers a {shape}-shaped region "
                            f"too large to be a precise click target. "
                            f"Vision likely grouped multiple controls "
                            f"under one bbox.\n"
                            f"Recovery: call "
                            f"`browser_look_again(intent='find <X>', "
                            f"expected_labels=['<exact target label>'])` "
                            f"— coverage mode usually emits a tight bbox "
                            f"per element. If the target genuinely IS the "
                            f"whole banner (rare), set VISION_BBOX_SIZE_GUARD=0."
                        )
                except Exception:
                    pass  # Never let the guard's own bug block a click.
            # Freshness gate — refuse to click when the last vision pass
            # flagged the screenshot as stale or uncertain. The planner
            # should re-screenshot before committing a click on a frame
            # the model itself said it couldn't trust.
            freshness = getattr(resp, "screenshot_freshness", "fresh")
            if freshness != "fresh":
                self.s.record_cursor_failure(
                    strategy="click_at",
                    target=f"V{vision_index}",
                    reason=f"stale_vision freshness={freshness}",
                )
                alts = _vision_alternatives_hint(
                    self.s, exclude_index=int(vision_index), limit=3,
                )
                return (
                    f"[click_at_failed:stale_vision freshness={freshness}] "
                    "Vision flagged the last screenshot as not fresh "
                    "(URL/page mismatch or loading overlay). Call "
                    "browser_screenshot to refresh vision before clicking."
                    + (f"\n{alts}" if alts else "")
                )
            # Phase 1.3 turn-based age gate. Beyond
            # VISION_MAX_AGE_TURNS mutating actions since the last
            # screenshot, the V_n indices the brain captured no longer
            # reliably point at the elements they did when the
            # screenshot was taken. The brain MUST re-screenshot. Wall-
            # clock isn't a useful proxy because a long thinking pause
            # doesn't mutate the page; the right unit is "actions
            # taken between epoch and now". _brain_turn_counter was
            # bumped by ensure_vision_synced for THIS click already, so
            # subtract 1 to count actions BEFORE this one.
            try:
                max_age_turns = int(
                    os.environ.get("VISION_MAX_AGE_TURNS") or "1"
                )
            except ValueError:
                max_age_turns = 1
            if max_age_turns > 0:
                age_turns = max(
                    0,
                    self.s._brain_turn_counter - 1
                    - self.s._vision_epoch_turn,
                )
                # Strict `>=`: any mutating action between vision epoch
                # and now invalidates V_n. Matches the DOM stale-index
                # guard so the brain can't sneak through one path while
                # the other refuses. Loose `>` here previously let the
                # wineaccess pattern (click_selector → click_at(V_n) on
                # an oversized bbox) succeed past the guard.
                if age_turns >= max_age_turns:
                    alts = _vision_alternatives_hint(
                        self.s, exclude_index=int(vision_index), limit=3,
                    )
                    return (
                        f"[click_at_failed:epoch_too_old age_turns="
                        f"{age_turns} max={max_age_turns}] V"
                        f"{vision_index} resolves against a vision "
                        f"snapshot taken {age_turns} actions ago — the "
                        f"page state may have shifted. Call "
                        f"browser_screenshot to refresh the V_n "
                        f"indices before clicking."
                        + (f"\n{alts}" if alts else "")
                    )
            # Blocker gate — if the scene has an active blocker layer
            # (cookie banner, modal, consent dialog) and this bbox lives
            # in a different layer, refuse. The planner must dismiss
            # the blocker before acting on content beneath it.
            scene = getattr(resp, "scene", None)
            active_blocker = (
                getattr(scene, "active_blocker_layer_id", None)
                if scene is not None else None
            )
            if active_blocker:
                bbox_layer = getattr(bbox, "layer_id", None)
                if bbox_layer and bbox_layer != active_blocker:
                    # Find the dismiss hint from the blocker layer so
                    # the brain has a concrete target to click first.
                    dismiss_hint = ""
                    try:
                        for layer in (getattr(scene, "layers", []) or []):
                            if getattr(layer, "id", None) == active_blocker:
                                dismiss_hint = (
                                    getattr(layer, "dismiss_hint", "") or ""
                                )
                                break
                    except Exception:
                        dismiss_hint = ""
                    hint = f" Dismiss '{dismiss_hint}' first." if dismiss_hint else ""
                    return (
                        f"[click_at_failed:blocker_active layer={active_blocker}] "
                        f"A blocker layer ({active_blocker}) is on top of "
                        f"content, and V{vision_index} sits in a different "
                        f"layer ({bbox_layer}).{hint} Then re-screenshot."
                    )
            # Confidence gate — a low-confidence bbox is Gemini's way of
            # saying "I'm not sure this is really here". Clicking it
            # lands on the wrong target more often than not. Threshold
            # is tuned via VISION_MIN_CLICK_CONFIDENCE (default 0.45).
            try:
                min_conf = float(
                    os.environ.get("VISION_MIN_CLICK_CONFIDENCE") or "0.45"
                )
            except ValueError:
                min_conf = 0.45
            if getattr(bbox, "confidence", 0.5) < min_conf:
                alts = _vision_alternatives_hint(
                    self.s, exclude_index=int(vision_index), limit=3,
                )
                return (
                    f"[click_at_failed:low_confidence V{vision_index}] "
                    f"bbox confidence={bbox.confidence:.2f} < "
                    f"{min_conf:.2f}. Call browser_screenshot to re-run "
                    "vision, then retry with a higher-confidence target."
                    + (f"\n{alts}" if alts else "")
                )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return (
                    "[click_at_failed:no_image_dims] Last vision response "
                    "has no source image dimensions; cannot denormalize "
                    "box_2d. Re-fetch state."
                )
            # CDP/JS expects CSS pixels; on retina/HiDPI viewports the
            # screenshot is physical-pixel-sized so we divide by DPR.
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
            bbox_label = (getattr(bbox, "label", "") or "").strip()
            # Arch v4 Phase J — DOM-rect override. When vision crosscheck
            # populated bbox.dom_check with the resolved DOM element's
            # rect, prefer that rect over the vision-emitted one. DOM
            # rects are pixel-exact and tight-fit to the visible text;
            # vision rects can drift by 0.5–1% (box_2d quantization) and
            # often span a whole row when the actual control is just an
            # icon in one corner. When no dom_check rect is available,
            # fall back to the vision rect. Override:
            # BBOX_DOM_TIGHTEN=0 keeps the vision rect always.
            if os.environ.get("BBOX_DOM_TIGHTEN", "1") != "0":
                tightened = _maybe_tighten_to_dom_rect(
                    bbox, x0, y0, x1, y1, iw, ih, dpr_val,
                )
                if tightened is not None:
                    x0, y0, x1, y1 = tightened
            # Arch v4 Phase J — chevron-shift removed. Always click the
            # geometric center of the four bbox points. Per-site
            # observation showed the right-edge shift hit empty space
            # on accordions whose chevron sat at the row's left, OR
            # fired on labels like "Region (expand sub-options)" where
            # the vision row bbox was already tight enough that the
            # center click DID expand the accordion. The new auto-retry
            # + [CLICK_MISS_RETRY] nudge handles misses without needing
            # the shift heuristic. We still RECORD that this was a
            # chevron-labeled click so the next vision pass can detect
            # no-expansion and emit guidance.
            if _bbox_is_chevron_label(bbox_label):
                self.s._last_chevron_click_label = bbox_label[:120]
                self.s._last_chevron_click_url = self.s.current_url or ""
            payload = {"bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}
            # Carry the vision label into the click payload so the T3
            # backend can run a post-snap semantic match check. Empty
            # label → the check is skipped on the backend, which is
            # fine for raw-coord clicks further below.
            if bbox_label:
                payload["expected_label"] = bbox_label[:120]
                payload["label"] = bbox_label[:120]
            log_target = f"V{vision_index}({x0},{y0}→{x1},{y1})"
            print(
                f"\n>> browser_click_at(V{vision_index}) → "
                f"bbox=({x0},{y0},{x1},{y1})"
            )
        else:
            if x is None or y is None:
                return "[click_at_failed:bad_args] Provide either vision_index or both x and y."
            payload = {"x": float(x), "y": float(y)}
            log_target = f"({x},{y})"
            print(f"\n>> browser_click_at({x}, {y})")

        # Arch v4 Phase I — capture the DOM hash BEFORE the HTTP click
        # so we can detect "click landed but page didn't actually
        # change" cases (e.g. chevron-shifted clicks on accordions
        # that don't expand). The verify_action probe is sometimes
        # fooled by trivial DOM mutations (animation frames, timestamps);
        # comparing pre/post _last_dom_hash directly is a more robust
        # "click missed" signal that we use to widen the auto-retry
        # trigger below.
        _pre_click_dom_hash = self.s._last_dom_hash or ""
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/click",
            json=payload,
            timeout=30.0,
        )
        # 409 = reward-band reject. Historical data says this zone
        # doesn't respond to clicks on this host; surface the hint
        # so the LLM re-reads elements instead of trying another
        # nearby coord.
        if r.status_code == 409:
            info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            err = info.get("error") or "click_at rejected: low-reward zone"
            self.s.log_activity(f"click_at{log_target}(BAND_REJECT)", f"band={info.get('band')}")
            return f"[low_reward_band] {err}"
        r.raise_for_status()
        data = r.json()
        # Element-mismatch guard (P1.4). The T3 backend compared the
        # element at the click target to the vision label we sent and
        # decided they don't match. Don't dispatch — return an
        # observation so the brain can re-screenshot and pick again.
        if isinstance(data, dict) and data.get("error") == "element_mismatch":
            found = data.get("found", {}) or {}
            alts = _vision_alternatives_hint(
                self.s, exclude_index=vision_index, limit=3,
            )
            self.s.log_activity(
                f"click_at{log_target}(ELEM_MISMATCH)",
                f"found={found.get('tag','?')}",
            )
            return (
                f"[click_at_failed:element_mismatch] Vision said this "
                f"target was '{data.get('expected_label','')}' but the "
                f"element at ({data.get('coords', {}).get('x','?')},"
                f"{data.get('coords', {}).get('y','?')}) is "
                f"<{(found.get('tag') or '?').lower()} "
                f"role='{found.get('role','')}'> text='"
                f"{(found.get('text') or '')[:80]}'. Call "
                f"browser_screenshot to refresh vision."
                + (f"\n{alts}" if alts else "")
            )
        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.record_url(actual_url)
        snap = data.get("snap")  # {x, y, snapped: bool, target?: str}
        if snap:
            snap_note = (
                f" snapped→({snap.get('x')},{snap.get('y')}) {snap.get('target','')}".strip()
                if snap.get("snapped") else " (raw bbox center; no interactive element matched)"
            )
        else:
            snap_note = ""

        # Post-click verification — look up the postcondition the planner
        # attached to this target (by vision_index or by coord match)
        # and run it via verify_action. Runs for both t1 and t3 via
        # tier_evaluate.get_backend; gated by VERIFY_AFTER_CLICK
        # (default on) and VERIFY_AFTER_CLICK_T1 (default on, separate
        # kill switch for t1 rollout). A miss is reported in the
        # caption so the brain can decide to retry with a different
        # strategy or call browser_plan_next_steps.
        verify_note = ""
        _verify_on = (
            os.environ.get("VERIFY_AFTER_CLICK", "1") != "0"
            and (
                session_id.startswith("t3-")
                or os.environ.get("VERIFY_AFTER_CLICK_T1", "1") != "0"
            )
        )
        if _verify_on:
            postcond = self._lookup_postcondition(vision_index, x, y)
            if postcond is not None:
                try:
                    from superbrowser_bridge.tier_evaluate import get_backend as _get_backend
                    from superbrowser_bridge.verify_action import verify_after, PreState
                    mgr = _get_backend(session_id)
                    vr = await verify_after(
                        mgr, session_id, postcond,
                        pre_state=PreState(url=self.s.current_url or ""),
                        state=self.s,
                    )
                    if not vr.verified:
                        # Default postcondition (dom_mutated) failing means
                        # the click went out but NOTHING changed — page,
                        # DOM, URL all identical. Before bothering the
                        # brain, ESCALATE through the click ladder —
                        # many pages reject "primary" bezier clicks but
                        # respond to a direct `el.click()` (JS) dispatch
                        # or to keyboard Enter. Silent failure most
                        # often means the site's click handler has a
                        # guard our primary click tripped (0-dwell, CSS
                        # pointer-events masking, framework re-render).
                        is_silent_default = (
                            postcond.get("kind") == "dom_mutated"
                            and not getattr(
                                self.s._last_action_queue, "actions", None,
                            )
                        )
                        # The click ladder (alternate js/keyboard strategies)
                        # is t3-only because it dispatches via T3SessionManager
                        # primitives the TS server doesn't expose with a
                        # comparable strategy parameter. T1 silent clicks
                        # surface the [click_silent] note and let the brain
                        # pick the recovery move.
                        escalated = False
                        if is_silent_default and \
                                session_id.startswith("t3-") and \
                                os.environ.get("CLICK_LADDER_AUTO", "1") != "0" and \
                                payload.get("bbox"):
                            for alt_strategy in ("js", "keyboard"):
                                try:
                                    from superbrowser_bridge.antibot import (
                                        interactive_session as _t3mgr2,
                                    )
                                    mgr2 = _t3mgr2.default()
                                    alt_bbox = payload.get("bbox")
                                    alt_x = (alt_bbox["x0"] + alt_bbox["x1"]) / 2
                                    alt_y = (alt_bbox["y0"] + alt_bbox["y1"]) / 2
                                    alt_resp = await mgr2.click_at(
                                        session_id, alt_x, alt_y,
                                        bbox=alt_bbox,
                                        strategy=alt_strategy,
                                    )
                                    if not isinstance(alt_resp, dict) or \
                                            not alt_resp.get("success"):
                                        continue
                                    # Re-verify after the escalated strategy.
                                    vr2 = await verify_after(
                                        mgr, session_id, postcond,
                                        pre_state=PreState(
                                            url=self.s.current_url or "",
                                        ),
                                        state=self.s,
                                    )
                                    if vr2.verified:
                                        escalated = True
                                        verify_note = (
                                            f"\n[click_escalated strategy={alt_strategy}] "
                                            f"Primary click was silent; "
                                            f"{alt_strategy} strategy landed the "
                                            f"action."
                                        )
                                        break
                                except Exception as exc:
                                    print(
                                        f"  [click ladder ({alt_strategy}) "
                                        f"failed: {exc}]"
                                    )
                                    continue
                        if not escalated:
                            if is_silent_default:
                                verify_note = (
                                    f"\n[click_silent reason={vr.reason}] "
                                    f"Primary + escalated (js/keyboard) "
                                    f"clicks all landed no DOM change. "
                                    f"Target likely non-interactive, "
                                    f"covered by an overlay, or waiting "
                                    f"on an async load. Call "
                                    f"browser_screenshot to re-vision, "
                                    f"dismiss any active blocker, or try "
                                    f"a different target."
                                )
                            else:
                                verify_note = (
                                    f"\n[VERIFY_MISS kind={vr.kind} reason={vr.reason}] "
                                    f"The click dispatched but the expected effect "
                                    f"({postcond.get('kind')}) didn't land. Consider "
                                    f"browser_plan_next_steps to re-sequence, or try "
                                    f"a different target."
                                )
                    elif os.environ.get("VERIFY_DEBUG") == "1":
                        verify_note = f"\n[verify_ok kind={vr.kind}]"
                except Exception as exc:
                    print(f"  [verify_action: skipped — {exc}]")

        # Arch v4 Move 3 — bbox auto-retry. When verification reports
        # a silent miss or postcondition mismatch, AND the brain
        # supplied a target_label, AND we haven't already retried, the
        # system takes one fresh screenshot and re-clicks against the
        # new V_n bearing the same label. Recovers transient misses
        # (the common case on dense filter modals where the DOM
        # rearranges mid-render) without burning an iteration. One
        # retry only — second miss returns control to the brain.
        #
        # Phase I — widened trigger: also fire when the post-click
        # DOM hash equals the pre-click hash (a robust "click missed"
        # signal that catches false-positive verify_action verdicts on
        # chevron clicks that don't expand the accordion). We compare
        # _last_dom_hash AFTER the click finished — record_url and the
        # synchronous parts of the click handler may have updated it.
        # If it's still equal to _pre_click_dom_hash, the page didn't
        # change.
        retry_outcome_note = ""
        verify_signaled_miss = (
            "[click_silent]" in verify_note or "[VERIFY_MISS]" in verify_note
        )
        post_click_dom_hash = self.s._last_dom_hash or ""
        dom_unchanged = bool(
            _pre_click_dom_hash
            and post_click_dom_hash == _pre_click_dom_hash
        )
        if (
            not internal_retry
            and target_label
            and (verify_signaled_miss or dom_unchanged)
            and os.environ.get("BBOX_AUTO_RETRY", "1") != "0"
        ):
            try:
                retry_text = await self._attempt_bbox_auto_retry(
                    session_id=session_id,
                    original_vision_index=vision_index,
                    target_label=str(target_label),
                    narration=narration,
                )
                if retry_text is not None:
                    # _attempt_bbox_auto_retry returns the retry's full
                    # tool result text on success or [BBOX_AUTO_RETRY_NO_MATCH]
                    # on label-gone. Either way, return that as the
                    # primary result so the brain sees the recovery.
                    self.s._bbox_auto_retry_in_flight = False
                    return retry_text
            except Exception as exc:
                self.s._bbox_auto_retry_in_flight = False
                retry_outcome_note = (
                    f"\n[BBOX_AUTO_RETRY_ERROR {type(exc).__name__}: "
                    f"{str(exc)[:120]}]"
                )

        self.s.record_step(
            "browser_click_at",
            log_target,
            f"url={actual_url[:60] if actual_url else '?'}{snap_note}",
        )
        # Arch v4.5 secondary fix: bump actions_since_screenshot like
        # the Drag/Slider tools already do. Without this, click_at
        # leaves the counter at 0, which made the legacy
        # actions_since_screenshot==0 gate misfire on the next
        # screenshot. The mutations_since_brain_screenshot bypass now
        # covers this case too, but keeping both signals aligned avoids
        # downstream surprises in any other guard that reads this.
        self.s.actions_since_screenshot += 1
        # Phase 3.3 click-hit verification: capture pre-click signals
        # so the post-click vision pass can flag a no-op click that
        # left the labeled target still visible.
        _expected_label = ""
        if vision_index is not None:
            try:
                _expected_label = (
                    payload.get("expected_label")
                    or payload.get("label")
                    or ""
                )
            except Exception:
                _expected_label = ""
        _pre_url = self.s.current_url or ""
        _pre_dom_hash = self.s._last_dom_hash or ""
        # Per-step subgoal verification (task_plan.py). Soft-fail: if the
        # active step's success_criteria can't be probed, the returned
        # note describes the skip reason and the brain continues.
        subgoal_note = await self.s.check_active_task_step(
            session_id, pre_url=_pre_url,
        )
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        # Arch v4 Phase K — prepend the remap note so the brain sees
        # the V_N→V_M correction at the top of the result. Empty
        # string when no remap fired.
        remap_prefix = (remap_note + "\n") if remap_note else ""
        result = await _append_fresh_vision(
            _vision_task,
            remap_prefix
            + self.s.build_text_only(data, f"Clicked {log_target}{snap_note}")
            + verify_note + subgoal_note + retry_outcome_note,
            expected_label=_expected_label or None,
            pre_url=_pre_url,
            pre_dom_hash=_pre_dom_hash,
            state=self.s,
        )
        # Arch v4 Move 3 — clear retry-in-flight flag (idempotent;
        # already cleared on the success path above).
        self.s._bbox_auto_retry_in_flight = False
        return result

    async def _attempt_bbox_auto_retry(
        self,
        session_id: str,
        original_vision_index: int | None,
        target_label: str,
        narration: str | None = None,
    ) -> str | None:
        """Arch v4 Move 3 — take a fresh screenshot, fuzzy-match
        target_label against the new bboxes, re-issue browser_click_at
        on the new V_n with internal_retry=True. Returns:

          • The retry's full tool result text when a matching V_n was
            found AND clicked (whether or not that retry succeeded).
            The text is annotated with [BBOX_AUTO_RETRY n=1 V_a→V_b
            outcome=succeeded|failed].
          • A short "[BBOX_AUTO_RETRY_NO_MATCH ...]" caption when the
            target_label is no longer visible after the fresh
            screenshot (different remediation than "clicked but no
            change" — the brain should switch tactics, not retry).
          • None if BBOX_AUTO_RETRY_MAX is exhausted or the retry
            machinery itself can't run (fall through to the original
            failure return).

        Caller is responsible for resetting state._bbox_auto_retry_in_
        flight on return.
        """
        s = self.s
        # Cap retries: default 1.
        max_retries = int(os.environ.get("BBOX_AUTO_RETRY_MAX", "1"))
        if max_retries <= 0:
            return None
        attempt_n = getattr(s, "_bbox_auto_retry_attempts", 0)
        if attempt_n >= max_retries:
            return None
        s._bbox_auto_retry_attempts = attempt_n + 1

        # Take a fresh screenshot via direct API. We don't go through
        # BrowserScreenshotTool here because we want a deterministic
        # vision pass without the screenshot-budget throttling and
        # without firing the screenshot-record-step machinery (which
        # would muddy the brain's tool history with a tool the brain
        # didn't call).
        try:
            r = await _request_with_backoff(
                "GET",
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true", "bounds": "true"},
                timeout=15.0,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
        b64 = data.get("screenshot") or ""
        if not b64:
            return None
        # Run vision against the new screenshot to get fresh bboxes.
        # Mirrors the screenshot-tool path: get the process-wide
        # VisionAgent and call analyze(). When vision is disabled or
        # unavailable, fall through to None so the caller takes the
        # original failure path.
        try:
            from vision_agent import (  # type: ignore[import-not-found]
                get_vision_agent, vision_agent_enabled,
            )
            if not vision_agent_enabled():
                return None
            agent = get_vision_agent()
            if agent is None:
                return None
            vresp = await agent.analyze(
                screenshot_b64=b64,
                intent=f"locate {target_label!r} after click miss",
                session_id=session_id,
                url=data.get("url", "") or s.current_url,
                dom_hash="",
                dom_text_hash="",
                previous_summary=getattr(s, "_last_vision_summary", None),
                image_width=int(data.get("imageWidth") or 0) or None,
                image_height=int(data.get("imageHeight") or 0) or None,
                task_instruction=getattr(s, "task_instruction", None) or None,
            )
        except Exception:
            return None
        bboxes = list(getattr(vresp, "bboxes", None) or [])
        if not bboxes:
            return (
                f"[BBOX_AUTO_RETRY_NO_MATCH target={target_label!r}] "
                f"Took a fresh screenshot but vision returned no "
                f"bboxes. The page may be loading or empty — "
                f"try browser_screenshot manually before re-attempting."
            )
        new_idx_1based = _find_best_label_match(bboxes, target_label)
        if new_idx_1based < 0:
            labels_preview = ", ".join(
                f"V_{i+1}={(b.label or '?')!r}" for i, b in enumerate(bboxes[:8])
            )
            return (
                f"[BBOX_AUTO_RETRY_NO_MATCH target={target_label!r}] "
                f"Took a fresh screenshot but no bbox label fuzzy-"
                f"matches the original target. Vision sees: "
                f"{labels_preview}. The label may have changed text "
                f"or the element is gone — switch tactics rather than "
                f"retry on a stale label."
            )
        # Replace the cached vision response with the fresh one so
        # downstream V_n indices resolve correctly during retry.
        s._last_vision_response = vresp
        # Recursive call with internal_retry=True. Gates skip their
        # consume checks; postcondition + verify still run normally.
        retry_result = await self.execute(
            session_id=session_id,
            vision_index=new_idx_1based,
            target_label=target_label,
            narration=narration,
            internal_retry=True,
        )
        retry_text = str(retry_result)
        retry_succeeded = (
            "[click_silent]" not in retry_text
            and "[VERIFY_MISS]" not in retry_text
        )
        outcome = "succeeded" if retry_succeeded else "failed"
        v_orig = original_vision_index if original_vision_index is not None else "?"
        annotation = (
            f"\n[BBOX_AUTO_RETRY n={attempt_n + 1} "
            f"V_{v_orig}→V_{new_idx_1based} outcome={outcome}]"
        )
        return retry_text + annotation

    def _lookup_postcondition(
        self,
        vision_index: int | None,
        x: float | None,
        y: float | None,
    ) -> dict | None:
        """Match the current click against the top planned action and return
        its postcondition, or fall through to a weakest-possible
        default that only catches "click dispatched but page didn't
        change at all" (the canonical silent-miss signal).

        A planner match is: the click's vision_index equals the top
        action's target_vision_index, OR the click's (x, y) falls
        inside the top action's target bbox (± 10 px slack).

        The default (dom_mutated) runs when no planner postcondition
        applies. Set VERIFY_DEFAULT=0 to disable and preserve the old
        "no postcondition, no verification" behaviour.
        """
        queue = self.s._last_action_queue
        if queue is not None and getattr(queue, "actions", None):
            top = queue.actions[0]
            # vision_index match (preferred)
            if vision_index is not None and top.target_vision_index is not None:
                if int(vision_index) == int(top.target_vision_index):
                    return top.postcondition.to_dict()
            # coord match (fallback)
            if x is not None and y is not None and top.target_bbox_pixels:
                x0, y0, x1, y1 = top.target_bbox_pixels
                if (x0 - 10) <= float(x) <= (x1 + 10) and \
                        (y0 - 10) <= float(y) <= (y1 + 10):
                    return top.postcondition.to_dict()
        # Default: "did anything change?" — dom_mutated catches the
        # "click silently missed" case even when the planner didn't
        # attach an explicit postcondition.
        if os.environ.get("VERIFY_DEFAULT", "1") != "0":
            return {"kind": "dom_mutated"}
        return None


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index (the V_n the vision agent "
                "labelled this input). Preferred over (x, y) whenever "
                "the vision agent has pointed at the field."
            ),
            nullable=True,
        ),
        x=NumberSchema(
            description="X coordinate (CSS pixel). Ignored when vision_index is set.",
            nullable=True,
        ),
        y=NumberSchema(
            description="Y coordinate (CSS pixel). Ignored when vision_index is set.",
            nullable=True,
        ),
        text=StringSchema("Text to type into the field at that point."),
        clear=BooleanSchema(
            description=(
                "Clear the field's existing value before typing (default: true). "
                "Uses React/Vue-aware clear so controlled components replace "
                "properly instead of appending."
            ),
            default=True,
        ),
        target_label=StringSchema(
            description=(
                "REQUIRED when vision_index is set. The label you read for "
                "V_n in the most recent screenshot reply (e.g. 'Search box', "
                "'Email input'). The bridge fuzzy-substring-matches this "
                "against the actual V_n label vision emitted; refuses with "
                "the real labels listed inline if they don't match. Catches "
                "the V_1-reflex pattern. Ignored when using raw (x, y)."
            ),
            nullable=True,
        ),
        narration=StringSchema(
            description=(
                "Optional one-sentence narration of WHY you're typing here "
                "and what you expect to happen. Stored on state; rendered "
                "back in the next turn's guidance as `[last_intended: ...]` "
                "for chain-of-thought trail. Never required."
            ),
            nullable=True,
        ),
        required=["session_id", "text"],
    )
)
class BrowserTypeAtTool(Tool):
    """Type at a vision bbox (V_n) or (x, y) coordinate. The bbox analogue
    of `browser_type(index, text)`.

    Checks the field's current value before typing — three outcomes the
    LLM sees in the return:
      - `skip_match`: field already contains the target text; no change.
      - `cleared_and_typed`: field had different content, cleared + typed.
      - `typed_into_empty`: field was empty, typed directly.

    Prefer this over `browser_click_at(V_n)` + `browser_keys([...])`,
    which appends at the cursor and turns `old|` + typing `new` into
    `oldnew` instead of `new`.
    """

    name = "browser_type_at"
    description = (
        "Type text into the input at a vision bbox (vision_index=V_n) or "
        "(x, y) coords. Probes the field's current value first and clears "
        "it (React-safe) before typing. Replaces click_at + keys for "
        "bbox-targeted typing — no more concatenation bugs."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        text: str,
        vision_index: int | None = None,
        x: float | None = None,
        y: float | None = None,
        clear: bool = True,
        target_label: str | None = None,
        narration: str | None = None,
        **kw: Any,
    ) -> Any:
        # Arch v3 fix #5: state-freshness gate.
        gate = self.s.must_screenshot_before_state_change("browser_type_at")
        if gate:
            return gate
        # v5: stash optional narration for the next-turn `[last_intended]`
        # render in worker_hook. Never required, never refused.
        if narration:
            self.s._last_narration = str(narration)[:240]
        # v8: vision-grounded type protocol. Same gate as click_at —
        # when vision_index is set, brain MUST name the V_n label.
        # Catches reflexive type-into-V_1 without reading.
        label_refuse = _validate_target_label(self.s, vision_index, target_label)
        if label_refuse:
            return label_refuse
        # Phase 1.1: hard sync gate before mutation.
        sync_block = await self.s.ensure_vision_synced(reason="browser_type_at")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        if text is None:
            text = ""

        # Resolve target point: vision_index first, then (x, y).
        target_x: float
        target_y: float
        label: str
        if vision_index is not None:
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return (
                    "[type_at_failed:no_vision] No recent vision response "
                    "to resolve vision_index against. Take a screenshot "
                    "first, or pass raw (x, y)."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                return (
                    f"[type_at_failed:bad_vision_index] V{vision_index} "
                    f"is out of range (only {len(resp.bboxes)} bboxes in "
                    "the last vision response)."
                )
            # Phase 1.3 turn-based age gate (mirrors BrowserClickAtTool).
            try:
                _max_age = int(
                    os.environ.get("VISION_MAX_AGE_TURNS") or "1"
                )
            except ValueError:
                _max_age = 1
            if _max_age > 0:
                _age = max(
                    0,
                    self.s._brain_turn_counter - 1
                    - self.s._vision_epoch_turn,
                )
                if _age > _max_age:
                    return (
                        f"[type_at_failed:epoch_too_old age_turns={_age} "
                        f"max={_max_age}] V{vision_index} resolves "
                        f"against a vision snapshot taken {_age} actions "
                        f"ago. Call browser_screenshot to refresh before "
                        f"typing."
                    )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return (
                    "[type_at_failed:no_image_dims] Last vision response "
                    "has no source image dimensions; cannot denormalize "
                    "box_2d. Take a fresh screenshot."
                )
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
            target_x = (x0 + x1) / 2
            target_y = (y0 + y1) / 2
            label = f"V{vision_index}"
            print(f"\n>> browser_type_at(V{vision_index}, text={text[:30]!r})")
        elif x is not None and y is not None:
            target_x = float(x)
            target_y = float(y)
            label = f"({int(target_x)},{int(target_y)})"
            print(f"\n>> browser_type_at(({x},{y}), text={text[:30]!r})")
        else:
            return "[type_at_failed:bad_args] Provide either vision_index or both x and y."

        # Route through /evaluate (works on both t1 and t3) rather than
        # through a dedicated /type-at endpoint (t3-only). Mechanism is
        # identical to browser_fix_text_at: atomic probe → native-setter
        # write → dispatched input/change events → confirm-read.
        import json as _json
        atomic_js = _ATOMIC_FIX_TEXT_JS.replace(
            "__TARGET_X__", str(float(target_x))
        ).replace(
            "__TARGET_Y__", str(float(target_y))
        ).replace(
            "__TARGET_TEXT__", _json.dumps(text)
        )
        ev = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": atomic_js},
            timeout=30.0,
        )
        ev.raise_for_status()
        payload_body = ev.json()
        result = (
            payload_body.get("result") if isinstance(payload_body, dict) else None
        ) or {}
        if not isinstance(result, dict) or not result.get("ok"):
            reason = (result or {}).get("reason", "unknown") if isinstance(result, dict) else "bad_shape"
            return f"[type_at_failed:{reason}] at {label}. detail={result}"

        before = str(result.get("before", "") or "")
        after = str(result.get("after", "") or "")
        changed = bool(result.get("changed"))

        if not changed:
            caption = (
                f"Field at {label} already contained {text!r} — no typing "
                f"needed. Proceed to next action."
            )
        elif before:
            caption = (
                f'Typed "{text}" at {label} (replaced existing '
                f'{before!r}).'
            )
        else:
            caption = f'Typed "{text}" at {label}.'

        self.s.record_step(
            "browser_type_at",
            f"{label}, text={text[:30]!r}",
            "skip_match" if not changed else ("cleared_and_typed" if before else "typed_into_empty"),
        )
        synthetic_data = {
            "success": True,
            "before": before,
            "after": after,
            "changed": changed,
        }
        # Post-type semantic verification. Returns a caption suffix and
        # may have already corrected the field in place.
        if changed:
            from .type_verify import verify_and_correct
            field_meta = {
                "label": str(result.get("label", "") or ""),
                "name": str(result.get("name", "") or ""),
                "autocomplete": str(result.get("autocomplete", "") or ""),
                "input_type": str(result.get("input_type", "") or ""),
            }
            outcome = await verify_and_correct(
                self.s, session_id,
                target_x=target_x, target_y=target_y,
                typed_text=text, label=label,
                page_url=self.s.current_url,
                field_meta=field_meta,
            )
            if outcome.kind == "corrected" and outcome.corrected_to:
                synthetic_data["after"] = outcome.after or outcome.corrected_to
                synthetic_data["auto_corrected"] = True
                synthetic_data["corrected_to"] = outcome.corrected_to
            caption += outcome.caption_suffix
        # Phase 2.1: notify the active form_session that this field was
        # typed into. Promotes its FieldStatus to FILLED (or
        # AWAIT_AUTOCOMPLETE if declared with autocomplete=true at
        # form_begin). The worker hook reads the updated state on the
        # next iteration so the brain sees a refreshed checklist.
        if self.s.form_session is not None:
            try:
                if vision_index is not None:
                    self.s.form_session.mark_typed(
                        label_or_index=int(vision_index),
                        value_typed=text,
                        turn=self.s._brain_turn_counter,
                    )
                if label:
                    self.s.form_session.mark_typed(
                        label_or_index=label,
                        value_typed=text,
                        turn=self.s._brain_turn_counter,
                    )
            except Exception:
                pass
        # Per-step subgoal verification (task_plan.py). Type events
        # rarely change URL but commonly satisfy `text_visible` /
        # `focus_on_role` criteria, so checking is still useful.
        subgoal_note = await self.s.check_active_task_step(
            session_id, pre_url=self.s.current_url or "",
        )
        if subgoal_note:
            caption += subgoal_note
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(synthetic_data, caption),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index for the input to correct. "
                "Preferred over (x, y) when vision labelled the field."
            ),
            nullable=True,
        ),
        x=NumberSchema(description="X coord; used only when vision_index absent.", nullable=True),
        y=NumberSchema(description="Y coord; used only when vision_index absent.", nullable=True),
        text=StringSchema(
            "The EXACT final text the field should contain after the fix. "
            "This is the target state, not a diff or an instruction — give "
            "the corrected spelling / value verbatim."
        ),
        required=["session_id", "text"],
    )
)
class BrowserFixTextAtTool(Tool):
    """Set a text field to an exact target value in one atomic step.

    Human-like correction pathway: when you've noticed a typo or stale
    content ('dahka', 'old search', leftover default), call this with the
    CORRECT final text. The tool reads the current value, computes the
    minimal diff for logging, then writes the target with the React/Vue
    safe native-setter + input/change events — no intermediate empty
    state where a race could concatenate.

    Prefer this over click_at → clear → type_at when fixing a typo:
    surgical, single-call, deterministic.
    """

    name = "browser_fix_text_at"
    description = (
        "Atomically set an input / textarea / contenteditable to a target "
        "text value. Reads the current content, reports the diff, writes "
        "the correction in one step. Use this to fix typos or replace "
        "stale field values without multi-step click + clear + retype."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        text: str,
        vision_index: int | None = None,
        x: float | None = None,
        y: float | None = None,
        **kw: Any,
    ) -> Any:
        # Arch v3 fix #5: state-freshness gate.
        gate = self.s.must_screenshot_before_state_change("browser_fix_text_at")
        if gate:
            return gate
        if text is None:
            text = ""

        # Stale guard: raw (x, y) coords are equally volatile to DOM [N]
        # — both reference the page state from the LAST screenshot.
        # vision_index falls through to the V_n stale-age guard which
        # already exists below.
        if x is not None and y is not None and vision_index is None:
            stale = _stale_dom_index_block(
                self.s, tool_name="browser_fix_text_at",
                target_disp=f"({int(float(x))},{int(float(y))})",
            )
            if stale:
                return stale

        # Resolve target point.
        if vision_index is not None:
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return (
                    "[fix_text_at_failed:no_vision] No recent vision response "
                    "to resolve vision_index against. Take a screenshot first "
                    "or pass raw (x, y)."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                return (
                    f"[fix_text_at_failed:bad_vision_index] V{vision_index} "
                    f"out of range (only {len(resp.bboxes)} bboxes)."
                )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return "[fix_text_at_failed:no_image_dims] take a fresh screenshot."
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
            target_x = (x0 + x1) / 2
            target_y = (y0 + y1) / 2
            label = f"V{vision_index}"
        elif x is not None and y is not None:
            target_x = float(x)
            target_y = float(y)
            label = f"({int(target_x)},{int(target_y)})"
        else:
            return "[fix_text_at_failed:bad_args] Provide vision_index or (x, y)."

        print(f"\n>> browser_fix_text_at({label}, target={text[:40]!r})")

        # Run the whole probe-write-verify cycle inside ONE /evaluate
        # call. /evaluate works on both t1 (TS server) and t3 (patchright
        # intercept), whereas a dedicated /fix-text-at endpoint only
        # exists on t3. Doing the full op in a single evaluate is also
        # race-free: elementFromPoint → native setter → confirm-read all
        # happen within one synchronous JS tick.
        import json as _json
        atomic_js = _ATOMIC_FIX_TEXT_JS.replace(
            "__TARGET_X__", str(float(target_x))
        ).replace(
            "__TARGET_Y__", str(float(target_y))
        ).replace(
            "__TARGET_TEXT__", _json.dumps(text)
        )
        ev = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": atomic_js},
            timeout=20.0,
        )
        ev.raise_for_status()
        payload = ev.json()
        result = (
            payload.get("result") if isinstance(payload, dict) else None
        ) or {}
        if not isinstance(result, dict):
            return f"[fix_text_at_failed] unexpected evaluate shape: {type(result).__name__}"

        if not result.get("ok"):
            return (
                f"[fix_text_at_failed:{result.get('reason','unknown')}] at "
                f"{label}. detail={result}"
            )

        before = str(result.get("before", "") or "")
        after = str(result.get("after", "") or "")
        changed = bool(result.get("changed"))
        diff = _diff_text(before, after) if changed else "no change"

        if not changed:
            caption = (
                f"Field at {label} already contained {text!r} — no change "
                f"needed. Proceed."
            )
        else:
            caption = (
                f"Fixed {label}: {before!r} → {after!r}\n"
                f"Edit: {diff}"
            )

        self.s.record_step(
            "browser_fix_text_at",
            f"{label}, target={text[:30]!r}",
            diff,
        )
        # Wrap result in the same shape build_text_only expects.
        synthetic_data = {
            "success": True,
            "before": before,
            "after": after,
            "changed": changed,
            "diff": diff,
        }
        if changed:
            from .type_verify import verify_and_correct
            field_meta = {
                "label": str(result.get("label", "") or ""),
                "name": str(result.get("name", "") or ""),
                "autocomplete": str(result.get("autocomplete", "") or ""),
                "input_type": str(result.get("input_type", "") or ""),
            }
            outcome = await verify_and_correct(
                self.s, session_id,
                target_x=target_x, target_y=target_y,
                typed_text=text, label=label,
                page_url=self.s.current_url,
                field_meta=field_meta,
            )
            if outcome.kind == "corrected" and outcome.corrected_to:
                synthetic_data["after"] = outcome.after or outcome.corrected_to
                synthetic_data["auto_corrected"] = True
                synthetic_data["corrected_to"] = outcome.corrected_to
            caption += outcome.caption_suffix
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(synthetic_data, caption),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index"),
        text=StringSchema("Text to type"),
        clear=BooleanSchema(description="Clear field first (default: true)", default=True),
        required=["session_id", "index", "text"],
    )
)
class BrowserTypeTool(Tool):
    name = "browser_type"
    description = "Type text into an input field by its [index] number."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, text: str, clear: bool = True, **kw: Any) -> Any:
        # Arch v3 fix #5: state-freshness gate.
        fgate = self.s.must_screenshot_before_state_change("browser_type")
        if fgate:
            return fgate
        session_id = self.s.resolve_session_id(session_id)
        print(f'\n>> browser_type([{index}], "{text}")')
        gate = await _feedback_gate("browser_type")
        if gate:
            return gate
        # Phase 1.1: hard sync gate.
        sync_block = await self.s.ensure_vision_synced(reason="browser_type")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        # DOM-index stale guard — same volatility as browser_click([N]).
        # Without this, the wineaccess pattern triggers: a click_selector
        # changes the page, then browser_type([24]) writes into a field
        # that was index 24 on the OLD page, not the current one.
        stale = _stale_dom_index_block(
            self.s, tool_name="browser_type", target_disp=f"[{index}]",
        )
        if stale:
            return stale

        # --- Dead-type guard --------------------------------------------
        # The LLM's most destructive misread: type "khulna" → autocomplete
        # dropdown appears → LLM doesn't notice → retypes "khulna,
        # Bangladesh" → field now reads "khulnakhulna, Bangladesh". Catch
        # the second identical-ish type and force the LLM to inspect the
        # dropdown before retyping.
        now_ts = time.time()
        if (
            index == self.s.last_type_index
            and self.s.last_type_text
            and (now_ts - self.s.last_type_at) < 12.0
        ):
            last_lower = self.s.last_type_text.lower()
            cur_lower = text.lower()
            # Consider it a dead-type if: the new text starts with the old
            # text, OR the new text is a superset of the old (contains it),
            # OR it's exactly the same.
            duplicative = (
                cur_lower == last_lower
                or cur_lower.startswith(last_lower)
                or last_lower in cur_lower
            )
            if duplicative:
                self.s.record_step(
                    "browser_type",
                    f"index={index}, text={text[:30]!r}",
                    "DEAD_TYPE: refused (autocomplete likely)",
                )
                return (
                    f"[DEAD_TYPE_REJECTED] Refused to re-type into [{index}]. "
                    f"You already typed {self.s.last_type_text!r} into this "
                    f"field seconds ago. Typing again WILL concatenate "
                    f"(producing garbage like \"{self.s.last_type_text}{text}\"). "
                    f"An autocomplete dropdown probably appeared — take a "
                    f"browser_screenshot, then browser_click the right "
                    f"suggestion (or browser_keys ArrowDown+Enter). Only "
                    f"retype if you pass clear=true AND the field is empty."
                )

        self.s.consecutive_click_calls += 1  # type is also step-by-step
        payload: dict[str, Any] = {"index": index, "text": text, "clear": clear}
        cached_fp = self.s.element_fingerprints.get(index)
        if cached_fp:
            payload["expected_fingerprint"] = cached_fp
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/type",
            json=payload,
            timeout=30.0,
        )
        if r.status_code == 409:
            info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            suggested = info.get("suggested_index")
            current = info.get("current_element", "")
            hint = f" Try [{suggested}]." if suggested is not None else " Re-read elements list and pick again."
            await _fetch_elements(session_id, self.s)
            return f"[stale_index] Element [{index}] is now {current}.{hint}"
        # Same structured-400 handling as BrowserClickTool — avoid
        # surfacing raw 'Client error 400' which empties Gemini's
        # next turn.
        if r.status_code == 400:
            info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            reason = info.get("reason", "unknown")
            err = info.get("error", f"type [{index}] failed")
            alternatives = info.get("alternatives") or []
            await _fetch_elements(session_id, self.s)
            self.s.log_activity(f"type([{index}])({reason})", err[:60])
            alt_lines = "\n".join(f"  - {a}" for a in alternatives[:3]) if alternatives else ""
            return (
                f"[type_failed:{reason}] {err}"
                + (f"\nAlternatives:\n{alt_lines}" if alt_lines else "")
                + "\nElements have been re-read above — pick a current [index]."
            )
        r.raise_for_status()
        data = r.json()

        # Record last-type state so the dead-type guard fires next time.
        self.s.last_type_index = index
        self.s.last_type_text = text
        self.s.last_type_at = time.time()

        # --- Post-type autocomplete dropdown scan -----------------------
        # Probe the page for newly-appeared autocomplete suggestions. If
        # we find any, surface them inline so the LLM picks one instead
        # of re-typing the full phrase.
        suggestions: list[dict] = []
        try:
            scan_js = """
            (() => {
              const seen = new Set();
              const out = [];
              const selectors = [
                '[role="listbox"] [role="option"]',
                '[role="combobox"] + * li',
                '.autocomplete-suggestions li, .autocomplete li',
                'ul.suggestions li, .suggestions li',
                '.MuiAutocomplete-listbox li',
                '[aria-live] li',
                '.dropdown-menu.show li, .dropdown-menu[style*="display: block"] li',
                '.ui-autocomplete li',
                '[class*="autocomplete"][class*="option"]',
                '[class*="suggestion"] li, [class*="suggestions"] li',
              ];
              for (const sel of selectors) {
                document.querySelectorAll(sel).forEach(el => {
                  const r = el.getBoundingClientRect();
                  if (r.width < 30 || r.height < 10) return;
                  if (r.top > window.innerHeight * 1.5) return;
                  const txt = (el.innerText || el.textContent || '').trim();
                  if (!txt || txt.length > 120 || seen.has(txt)) return;
                  seen.add(txt);
                  out.push({
                    text: txt,
                    x: Math.round(r.left + r.width / 2),
                    y: Math.round(r.top + r.height / 2),
                  });
                });
              }
              return out.slice(0, 8);
            })();
            """
            sr = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": scan_js},
                timeout=5.0,
            )
            if sr.status_code == 200:
                body = sr.json()
                got = body.get("result") if isinstance(body, dict) else None
                if isinstance(got, list):
                    suggestions = [s for s in got if isinstance(s, dict) and s.get("text")]
        except Exception as exc:
            print(f"  [dropdown scan failed: {exc}]")

        self.s.record_step(
            "browser_type",
            f'index={index}, text="{text[:30]}"',
            f"ok ({len(suggestions)} suggestions)" if suggestions else "ok",
        )

        # Surface pre-type inspection info so the LLM knows whether we
        # actually changed the field. `pretype_action` is one of
        # `typed_into_empty` (field was empty), `cleared_and_typed`
        # (existing value replaced), or `skip_match` (field already
        # contained target text — no change).
        pre_action = data.get("pretype_action") if isinstance(data, dict) else None
        pre_value = data.get("pretype_value") if isinstance(data, dict) else None
        if pre_action == "skip_match":
            caption = (
                f'Field [{index}] already contained {text!r} — no typing '
                f'needed. Proceed to next action.'
            )
        elif pre_action == "cleared_and_typed":
            caption = (
                f'Typed "{text}" into [{index}] '
                f'(cleared existing {pre_value!r} first)'
            )
        else:
            caption = f'Typed "{text}" into [{index}]'
        if suggestions:
            caption += (
                f"\n\nAutocomplete suggestions visible ({len(suggestions)}):"
            )
            for i, s in enumerate(suggestions, start=1):
                caption += f"\n  {i}. {s['text']!r} → browser_click_at(x={s['x']}, y={s['y']})"
            caption += (
                "\nDO NOT browser_type again into this field — pick a "
                "suggestion above via browser_click_at or use browser_keys "
                "(ArrowDown + Enter) to select the first one."
            )

        # Post-type semantic verification (index-addressed variant).
        # Skip when the tool no-op'd (field already matched).
        if pre_action != "skip_match":
            from .type_verify import verify_and_correct_by_index
            outcome = await verify_and_correct_by_index(
                self.s, session_id,
                dom_index=index, typed_text=text,
                page_url=self.s.current_url,
                field_meta={},
            )
            if outcome.kind == "corrected" and outcome.corrected_to:
                if isinstance(data, dict):
                    data["auto_corrected"] = True
                    data["corrected_to"] = outcome.corrected_to
            caption += outcome.caption_suffix

        # Prefetch vision so next screenshot call finds bboxes cached.
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, caption),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        keys=StringSchema("Keys to send (e.g. Enter, ArrowDown, Tab)"),
        required=["session_id", "keys"],
    )
)
class BrowserKeysTool(Tool):
    name = "browser_keys"
    description = "Send keyboard keys or shortcuts."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, keys: str, **kw: Any) -> Any:
        gate = self.s.must_screenshot_before_state_change("browser_keys")
        if gate:
            return gate
        session_id = self.s.resolve_session_id(session_id)
        print(f"\n>> browser_keys({keys})")
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/keys",
            json={"keys": keys},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        # Fetch updated elements after key press (e.g., Enter may submit form)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, f"Sent keys: {keys}"),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        direction=StringSchema("Scroll direction: up or down", nullable=True),
        percent=NumberSchema(description="Scroll to exact percentage 0-100", nullable=True),
        required=["session_id"],
    )
)
class BrowserScrollTool(Tool):
    name = "browser_scroll"
    description = "Scroll the page up or down, or to a specific percentage."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, direction: str | None = None, percent: float | None = None, **kw: Any) -> Any:
        session_id = self.s.resolve_session_id(session_id)
        print(f"\n>> browser_scroll({direction or f'{percent}%'})")
        gate = await _feedback_gate("browser_scroll")
        if gate:
            return gate
        payload: dict[str, Any] = {}
        if percent is not None:
            payload["percent"] = percent
        else:
            payload["direction"] = direction or "down"
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/scroll",
            json=payload,
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        # Fetch updated elements after scroll (new elements may be visible)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        action = f"Scrolled to {percent}%" if percent is not None else f"Scrolled {direction or 'down'}"
        # v5: soft hint to use closed-loop browser_scroll_until when the
        # active TaskPlan step names a recognizable noun-phrase target.
        # No refusal — blind scroll is sometimes the right move (long
        # results page, no specific target text yet). Hint only.
        # Kill switch SCROLL_HINT=0.
        scroll_hint = ""
        if os.environ.get("SCROLL_HINT", "1") != "0":
            scroll_hint = self._scroll_target_hint()
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, action) + scroll_hint,
        )

    def _scroll_target_hint(self) -> str:
        """Inspect the active TaskPlan step's name for a recognizable
        noun-phrase target. If found, return a `[scroll_hint]` suffix
        suggesting `browser_scroll_until(target_text=...)` instead of
        blind scrolling. Returns empty string when no plan, no active
        step, or no proper-noun target visible in the step name.
        """
        plan = getattr(self.s, "task_plan", None)
        if plan is None:
            return ""
        try:
            active = plan.peek_active()
        except Exception:
            return ""
        if active is None:
            return ""
        name = (active.name or "").strip()
        if not name:
            return ""
        # Skip the leading verb pattern ("Apply ", "Open ", "Select ",
        # "Click ", "Find ", "Set ") so the regex sees the target.
        import re as _re_n
        body = _re_n.sub(
            r"^(?:apply|open|select|click|find|set|enable|expand|"
            r"check|tick|toggle|configure)\s+",
            "", name, flags=_re_n.IGNORECASE,
        )
        # For `field=value` / `field: value` patterns the VALUE is the
        # scroll target, not the field name. Step names like
        # "Apply Region=Oregon" should hint "Oregon", not "Region".
        # If the body matches `<word>=<word>` or `<word>: <word>`, use
        # only the value side for target extraction.
        eq_match = _re_n.search(
            r"[A-Za-z][\w-]*\s*[=:]\s*([A-Za-z][\w +&-]*)",
            body,
        )
        if eq_match:
            body = eq_match.group(1)
        # Pull the first proper-noun-ish token: a Capitalized word ≥3
        # chars, optionally joined by "+" / "&" / "and" with another.
        m = _re_n.search(
            r"\b[A-Z][A-Za-z][A-Za-z]+(?:\s*(?:\+|&|and)\s*[A-Z][A-Za-z][A-Za-z]+)?",
            body,
        )
        if not m:
            return ""
        target = m.group(0)
        # Strip joiners for a clean target_text suggestion.
        target_first = _re_n.split(r"\s*(?:\+|&|and)\s*", target)[0]
        return (
            f"\n[scroll_hint] Active step targets {target_first!r} — "
            f"prefer browser_scroll_until(target_text={target_first!r}) "
            f"for closed-loop scrolling that stops at the match."
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index of the select/dropdown"),
        value=StringSchema("Option value or visible text to select"),
        required=["session_id", "index", "value"],
    )
)
class BrowserSelectTool(Tool):
    name = "browser_select"
    description = "Select an option in a dropdown by value."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, value: str, **kw: Any) -> Any:
        # Arch v3 fix #5: state-freshness gate.
        gate = self.s.must_screenshot_before_state_change("browser_select")
        if gate:
            return gate
        session_id = self.s.resolve_session_id(session_id)
        stale = _stale_dom_index_block(
            self.s, tool_name="browser_select", target_disp=f"[{index}]",
        )
        if stale:
            return stale
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/select",
            json={"index": index, "value": value},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        # Fetch updated elements after selection (may trigger form changes)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        return self.s.build_text_only(data, f'Selected "{value}" in [{index}]')


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        label=StringSchema(
            "Human-readable label of the dropdown trigger (e.g. 'Brand', "
            "'Processor Brand', 'Year of Release'). Matched via accessible-name "
            "/ <label for=> / aria-labelledby / visible text."
        ),
        value=StringSchema(
            "Visible text or value of the option to pick (e.g. 'Dell', 'Intel', "
            "'2017'). Matching is exact-ci → startsWith → contains → fuzzy."
        ),
        fuzzy=BooleanSchema(
            description="Allow fuzzy match (Levenshtein ≥0.7). Default true.",
            default=True,
        ),
        timeout=IntegerSchema(
            description="Max ms to wait for listbox/options to render (default 4000).",
            nullable=True,
        ),
        extra_option_selectors=ArraySchema(
            description=(
                "Optional CSS selectors to add to the option-discovery list, "
                "for bespoke widgets that don't expose [role=option]."
            ),
            items=StringSchema(""),
            nullable=True,
        ),
        required=["session_id", "label", "value"],
    )
)
class BrowserSelectOptionTool(Tool):
    """Pick a dropdown option by *label* + *value*, hiding DOM-index churn.

    Use this for ANY dropdown/listbox/combobox — native <select>, ARIA
    combobox+listbox, Headless-UI Listbox, etc. You never pass an index or
    vision V-index, so re-renders between cascade steps don't matter.

    On ambiguity (no exact/fuzzy match) the tool returns the candidate list
    instead of guessing — retry with a corrected `value`. For ≥2 dependent
    dropdowns prefer `browser_form_plan` so progress is tracked structurally.
    """

    name = "browser_select_option"
    description = (
        "Pick a dropdown option by label+value. Works on native <select> "
        "AND custom listbox/combobox widgets. Returns {ok, picked_text, "
        "verified, candidates?} — on ambiguity, retry with one of the "
        "candidates instead of clicking blindly."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        label: str,
        value: str,
        fuzzy: bool = True,
        timeout: int | None = None,
        extra_option_selectors: list[str] | None = None,
        **kw: Any,
    ) -> str:
        # Arch v3 fix #5: state-freshness gate.
        gate = self.s.must_screenshot_before_state_change("browser_select_option")
        if gate:
            return gate
        print(f"\n>> browser_select_option(label={label!r}, value={value!r})")
        payload: dict[str, Any] = {"label": label, "value": value, "fuzzy": bool(fuzzy)}
        if timeout is not None:
            payload["timeout"] = int(timeout)
        if extra_option_selectors:
            payload["extra_option_selectors"] = list(extra_option_selectors)
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/select_option",
            json=payload,
            timeout=20.0,
        )
        try:
            r.raise_for_status()
        except Exception as e:
            self.s.record_step("browser_select_option", f"{label}={value}", f"HTTP error: {e}")
            return f"[select_option_http_error] {e}"
        data = r.json() or {}
        ok = bool(data.get("ok"))
        picked = data.get("picked_text") or value
        verified = bool(data.get("verified"))
        reason = data.get("reason")
        candidates = data.get("candidates") or []

        # Cursor strategy ledger — counts as a real cursor attempt for the
        # cursor-first lockout in browser_run_script.
        try:
            self.s.cursor_failure_strategies  # type: ignore[attr-defined]
        except Exception:
            pass
        else:
            if not ok and reason:
                self.s.cursor_failure_strategies.add(f"select_option:{reason}")

        if ok:
            note = f"Picked '{picked}' for '{label}'" + ("" if verified else " (verify pending)")
            print(f"   [select_option] ok -> {picked!r} (verified={verified})")
            self.s.log_activity(f"select_option({label})", picked[:40])
            self.s.record_step("browser_select_option", f"{label}={picked}", "ok")
            return self.s.build_text_only(data, note)

        # Ambiguity / failure path — surface candidates so the LLM corrects
        # the value rather than re-screenshotting and click-looping.
        cand_preview = (
            (" candidates=" + str([c[:30] for c in candidates[:6]]))
            if candidates else ""
        )
        print(f"   [select_option] FAIL reason={reason or '?'}{cand_preview}")

        msg_parts = [f"[select_option_failed] reason={reason or 'unknown'} label={label!r} value={value!r}"]
        if reason == "trigger_not_found":
            msg_parts.append(
                "The label was not found on this page. Two common causes:\n"
                "  (a) the cascading dropdown stage is over and the page "
                "transitioned to a results grid / model picker — in which "
                "case STOP using browser_form_plan / browser_select_option "
                "and instead browser_get_markdown to inspect the result list, "
                "then browser_click on the matching item.\n"
                "  (b) the label text in the page is different from what "
                "you passed — call browser_screenshot to read actual labels, "
                "then retry with the exact text."
            )
        if candidates:
            shown = ", ".join(repr(c) for c in candidates[:15])
            msg_parts.append(f"candidates: {shown}")
            msg_parts.append(
                "Retry browser_select_option with one of the candidates above. "
                "Do NOT fall back to raw clicking — DOM indices change after "
                "each pick; this tool re-anchors on the label."
            )
        elif reason and reason != "trigger_not_found":
            msg_parts.append(
                "No options were collected. The listbox may use a non-ARIA "
                "pattern — retry with a more specific label, or pass "
                "extra_option_selectors=[...] (e.g. ['li.option', '.dropdown-item'])."
            )
        self.s.log_activity(f"select_option({label}, FAIL)", (reason or "")[:60])
        self.s.record_step("browser_select_option", f"{label}={value}", f"FAIL:{reason or '?'}")
        return "\n".join(msg_parts)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "Short description of what this filter form is for "
            "(e.g. 'Best Buy laptop trade-in valuation')."
        ),
        fields=ArraySchema(
            description=(
                "Ordered list of dropdowns to fill. Each entry: "
                "{label, value, kind?}. Order is the cascade order — later "
                "fields are filled only after earlier ones succeed. Use "
                "the *visible label text* (e.g. 'Brand', 'Processor Brand') "
                "and the *visible option text* (e.g. 'Dell', 'Intel')."
            ),
            items=ObjectSchema(
                label=StringSchema("Visible label text of the dropdown"),
                value=StringSchema("Visible option text to pick"),
                kind=StringSchema(
                    "Optional: 'select' | 'cascade_select' (default).",
                    nullable=True,
                ),
                required=["label", "value"],
            ),
        ),
        per_step_timeout=IntegerSchema(
            description="Per-field listbox-render timeout (ms, default 4000).",
            nullable=True,
        ),
        stop_on_failure=BooleanSchema(
            description=(
                "If true (default), stop and return on first failed field "
                "with the candidate list. If false, continue past failures "
                "to fill what's possible."
            ),
            default=True,
        ),
        required=["session_id", "intent", "fields"],
    )
)
class BrowserFormPlanTool(Tool):
    """Plan + execute a cascading filter form in one tool call.

    The LLM declares the *whole* form once — Brand=Dell, Processor=Intel,
    RAM=8GB, Year=2017, etc. — and the runtime fills each field by
    label-anchored selection (browser_select_option), settling between
    steps so the next dropdown's options can populate. Removes the
    "stale V-index, regress, retry" loop on multi-step filter forms.

    Returns a structured progress string. On per-field failure (no match,
    listbox didn't render) the tool surfaces the candidate list so the
    LLM can retry with a corrected value.
    """

    name = "browser_form_plan"
    description = (
        "Fill a cascading filter form (≥2 dependent dropdowns) in one "
        "call. Pass an ordered list of {label, value} pairs. The runtime "
        "label-anchors each pick (no DOM index, no V-index) and settles "
        "between steps. Strongly preferred over manual click-loops on "
        "trade-in / search-filter / quote forms."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        intent: str,
        fields: list[dict[str, Any]],
        per_step_timeout: int | None = None,
        stop_on_failure: bool = True,
        **kw: Any,
    ) -> str:
        if not isinstance(fields, list) or not fields:
            return "[form_plan_failed] `fields` must be a non-empty list."
        # Defensive: coerce to plain dicts; reject malformed entries early.
        clean: list[dict[str, Any]] = []
        for i, f in enumerate(fields):
            if not isinstance(f, dict):
                return f"[form_plan_failed] fields[{i}] is not a dict."
            label = (f.get("label") or "").strip()
            value = (f.get("value") or "").strip()
            if not label or not value:
                return (
                    f"[form_plan_failed] fields[{i}] missing label or value: "
                    f"{f!r}"
                )
            clean.append({"label": label, "value": value, "kind": (f.get("kind") or "cascade_select")})

        try:
            from superbrowser_bridge.form_session import FormFillSession, FieldStatus
        except ImportError as exc:
            return f"[form_plan_failed:import] {exc}"

        sess = FormFillSession.begin_cascade(
            intent=intent, fields=clean,
            started_at_turn=self.s._brain_turn_counter,
        )
        self.s.form_session = sess

        print(f"\n>> browser_form_plan({len(clean)} fields)")
        progress: list[str] = [
            f"[form_plan] intent={intent!r} planning {len(clean)} fields"
        ]
        failures: list[str] = []

        # Best-effort: close any open dropdown / modal before we start.
        # If the previous tool call left a listbox/menu open, the next
        # trigger lookup will land inside the overlay rather than on the
        # field's own combobox button.
        try:
            await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/keys",
                json={"keys": "Escape"},
                timeout=5.0,
            )
            await asyncio.sleep(0.2)
        except Exception:
            pass

        for entry in clean:
            label = entry["label"]
            value = entry["value"]
            payload = {"label": label, "value": value, "fuzzy": True}
            if per_step_timeout is not None:
                payload["timeout"] = int(per_step_timeout)
            print(f"   [form_plan] -> {label}={value!r}")
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/select_option",
                json=payload,
                timeout=20.0,
            )
            try:
                r.raise_for_status()
            except Exception as e:
                failures.append(f"{label}={value} → HTTP error: {e}")
                if stop_on_failure:
                    break
                continue
            data = r.json() or {}
            ok = bool(data.get("ok"))
            picked = data.get("picked_text") or value
            reason = data.get("reason")
            candidates = data.get("candidates") or []

            if ok:
                sess.mark_picked(label, picked)
                progress.append(f"  [+] {label} = {picked!r}")
                print(f"   [form_plan]   ok -> picked {picked!r}")
                # Settle so dependent dropdown's options can populate
                # before the next iteration. 350ms covers most React
                # state-update + listbox-render flows; tune via per_step_timeout.
                await asyncio.sleep(0.35)
                # And close any lingering listbox before the next trigger lookup.
                try:
                    await _request_with_backoff(
                        "POST",
                        f"{SUPERBROWSER_URL}/session/{session_id}/keys",
                        json={"keys": "Escape"},
                        timeout=5.0,
                    )
                    await asyncio.sleep(0.15)
                except Exception:
                    pass
            else:
                cand_str = ""
                if candidates:
                    cand_str = (
                        " candidates=" + ", ".join(repr(c) for c in candidates[:10])
                    )
                msg = f"{label}={value} → {reason or 'no_match'}{cand_str}"
                failures.append(msg)
                progress.append(f"  [!] {msg}")
                print(f"   [form_plan]   FAIL -> {reason or '?'} {('cands=' + str(candidates[:6])) if candidates else ''}")
                if stop_on_failure:
                    break

        # Final progress summary
        progress.append("")
        progress.append(sess.cascade_progress())
        if failures and stop_on_failure:
            progress.append(
                "Stopped on first failure. Retry browser_form_plan with "
                "corrected values for the remaining fields, OR retry just "
                "the failed field with browser_select_option using one of "
                "the listed candidates."
            )
        elif failures:
            progress.append(
                f"Continued past {len(failures)} failure(s). Use "
                "browser_select_option to retry each."
            )

        self.s.log_activity(
            f"form_plan({intent[:30]})",
            f"verified {sum(1 for fs in sess.fields.values() if fs.status == FieldStatus.VERIFIED)}/{len(sess.fields)}",
        )
        return "\n".join(progress)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        script=StringSchema("JavaScript code to execute in the page"),
        required=["session_id", "script"],
    )
)
class BrowserEvalTool(Tool):
    name = "browser_eval"
    description = "Execute JavaScript in the browser page. FREE — no screenshot cost."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, script: str, **kw: Any) -> str:
        # Arch v3 fix #5: state-freshness gate. Eval after a click
        # without a screenshot in between is the dominant detour pattern.
        gate = self.s.must_screenshot_before_state_change("browser_eval")
        if gate:
            return gate
        session_id = self.s.resolve_session_id(session_id)
        # Eval-for-exploration rate limit: when vision is fresh AND the
        # script is read-only DOM exploration (querySelector, getElementBy,
        # innerText scrape), refuse — the brain should be picking V_n
        # from the bbox list instead of poking the DOM. The wineaccess
        # trace had 4 such evals in a row; each one was the brain trying
        # to find a label vision had already labelled. Allows: post-action
        # verifies that include write-ops or explicit non-querying probes
        # (readyState, location, etc.). Kill switch
        # EVAL_EXPLORATION_REFUSAL=0.
        if (
            os.environ.get("EVAL_EXPLORATION_REFUSAL", "1") != "0"
            and _eval_looks_like_exploration(script)
        ):
            try:
                resp = self.s._last_vision_response
                bbox_count = len(getattr(resp, "bboxes", []) or []) if resp else 0
                age_turns = max(
                    0,
                    self.s._brain_turn_counter - self.s._vision_epoch_turn,
                )
                if bbox_count > 0 and age_turns <= 2:
                    return (
                        f"[eval_for_exploration_refused: vision fresh "
                        f"({bbox_count} bboxes, {age_turns} turns old)] "
                        f"This script reads the DOM with "
                        f"querySelector/getElementBy/etc. and has no write "
                        f"operations — that's exploration, not action. "
                        f"Vision already labelled the page with [V_1].."
                        f"[V_{bbox_count}]; pick from those instead of "
                        f"re-querying the DOM. Recovery:\n"
                        f"  1. Read the bboxes in the most recent screenshot "
                        f"reply — labels include checkbox names, button text, "
                        f"link text.\n"
                        f"  2. browser_click_at(vision_index=V_n) on the "
                        f"target.\n"
                        f"  3. If the target genuinely isn't visible: "
                        f"browser_screenshot to refresh + scroll.\n"
                        f"Eval IS allowed for: post-action verification, "
                        f"reading numeric values (price, count), "
                        f"non-querying probes (location.href, "
                        f"document.readyState), and any script with write "
                        f"ops (.click(), setAttribute, .value=). Override: "
                        f"EVAL_EXPLORATION_REFUSAL=0."
                    )
            except Exception:
                pass
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0  # eval resets click loop tracking
        print(f"\n>> browser_eval({script[:60]}...)")
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": script},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
        result = data.get("result")
        result_str = json.dumps(result, indent=2, ensure_ascii=False)[:5000] if isinstance(result, (dict, list)) else str(result)[:5000]
        self.s.log_activity(f"eval({script[:40]}...)", result_str[:60])
        self.s.record_step("browser_eval", script[:60], result_str[:100])
        return result_str


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        script=StringSchema(
            "Puppeteer script body. Variables: page (Puppeteer Page), context, helpers (sleep, screenshot, log)."
        ),
        context=ObjectSchema(description="Optional context data", nullable=True),
        timeout=IntegerSchema(description="Script timeout in ms (default: 60000)", nullable=True),
        mutates=BooleanSchema(
            description=(
                "Set true when the script mutates the page (click, "
                "type, input.value=, dispatchEvent). Default false — "
                "the sandbox rejects those operations and returns a "
                "[blocked_op:…] error. Keep false for read-only "
                "inspection (readyState, innerText, aria-labels). Only "
                "flip true when no cursor tool can express the action; "
                "isTrusted=false JS clicks are bot-detected by WAFs."
            ),
            default=False,
        ),
        required=["session_id", "script"],
    )
)
class BrowserRunScriptTool(Tool):
    name = "browser_run_script"
    description = (
        "Execute a Puppeteer script with full page API access. "
        "READ-ONLY by default — pass mutates=true to allow click/type/"
        "dispatchEvent/value-setter operations (rare)."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self, session_id: str, script: str,
        context: dict | None = None,
        timeout: int | None = None,
        mutates: bool = False,
        **kw: Any,
    ) -> str:
        # Arch v3 fix #5: state-freshness gate. Run-script is the brain's
        # ultimate detour from the bbox path; refuse if the page mutated
        # without a screenshot since.
        gate = self.s.must_screenshot_before_state_change("browser_run_script")
        if gate:
            return gate
        print(f"\n>> browser_run_script({script[:80]}...)")
        # Refusal gate: same loop-escape protection as
        # browser_request_help. Mutating scripts are the brain's other
        # favourite escape hatch — when a click fails it pivots to
        # `browser_run_script(mutates=true)` instead of taking another
        # screenshot. Block until the brain has at least looked at the
        # page since the last failure. Only applies when mutates=true;
        # read-only data extraction is always allowed.
        if bool(mutates):
            refuse = self.s.must_screenshot_before_giving_up()
            if refuse:
                return refuse
        # v6: read-only browser_run_script for DOM exploration is the
        # last unguarded escape hatch from the visual loop. The wineaccess
        # trace had 8 such scripts in a row — the brain's pattern was:
        # eval refused → switch to run_script (read_only) → same
        # exploration. Mirrors v3 EVAL_EXPLORATION_REFUSAL on browser_eval
        # but for run_script. Allows: write-op scripts, short browser-
        # state probes, and any script when no vision exists yet.
        # Kill switch SCRIPT_EXPLORATION_REFUSAL=0.
        if (
            not bool(mutates)
            and os.environ.get("SCRIPT_EXPLORATION_REFUSAL", "1") != "0"
            and _eval_looks_like_exploration(script)
        ):
            try:
                resp = self.s._last_vision_response
                bbox_count = len(getattr(resp, "bboxes", []) or []) if resp else 0
                if bbox_count > 0:
                    return (
                        f"[run_script_for_exploration_refused: vision has "
                        f"{bbox_count} bboxes available] This script reads "
                        f"the DOM with querySelector/getElementBy/etc. "
                        f"and has no write operations — that's exploration, "
                        f"not action. browser_run_script(read-only) and "
                        f"browser_eval are both gated for this pattern; "
                        f"the visual loop is the right tool. Recovery:\n"
                        f"  1. Read the V_n LABELS in the most recent "
                        f"screenshot reply.\n"
                        f"  2. browser_click_at(vision_index=V_n) or "
                        f"browser_click_selector(<#id-or-data-testid>) on "
                        f"the target.\n"
                        f"  3. browser_inventory_filters for filter modals "
                        f"— returns stable selectors WITHOUT script.\n"
                        f"Read-only run_script IS allowed for: bulk data "
                        f"extraction once you've reached the result page, "
                        f"non-querying probes, and write-op flows. "
                        f"Override: SCRIPT_EXPLORATION_REFUSAL=0."
                    )
            except Exception:
                pass
        # Phase 3.1: cursor-first lockout. Read-only scripts always
        # allowed (data extraction). Mutating scripts require evidence
        # that the brain has tried — and failed — at least 2 distinct
        # cursor strategies in this session. This forces the cursor →
        # selector → script ladder rather than letting the brain
        # short-cut to JS clicks (isTrusted=false; tripped by every
        # bot-detection edge).
        if (
            bool(mutates)
            and os.environ.get("CURSOR_FIRST_LOCKOUT", "1") not in ("0", "false", "no")
        ):
            try:
                min_strategies = int(
                    os.environ.get("CURSOR_LOCKOUT_MIN_STRATEGIES") or "2"
                )
            except ValueError:
                min_strategies = 2
            distinct = len(self.s.cursor_failure_strategies)
            if distinct < min_strategies:
                ledger = self.s.cursor_lockout_summary()
                tried_str = (
                    ", ".join(sorted(self.s.cursor_failure_strategies))
                    or "(none)"
                )
                return (
                    "[run_script_blocked:cursor_path_untried] You haven't "
                    f"exhausted cursor strategies for this session "
                    f"({distinct}/{min_strategies} distinct strategies "
                    f"failed; tried={tried_str}).\n"
                    "Try in order BEFORE running mutating JS:\n"
                    "  1. browser_click_at(vision_index=V_n) on the "
                    "target's bbox.\n"
                    "  2. browser_click_selector(<stable-css>) if the "
                    "target has a hook.\n"
                    "  3. browser_type_at / browser_scroll_until.\n"
                    "Only when 2+ DIFFERENT strategies have failed with "
                    "concrete error captions can mutates=true scripts "
                    "run. JS clicks are isTrusted=false and routinely "
                    "rejected by Cloudflare / Akamai."
                    + (f"\nRecent cursor failures:\n{ledger}" if ledger else "")
                )
        self.s.consecutive_click_calls = 0  # script execution resets click loop tracking
        payload: dict[str, Any] = {"code": script, "mutates": bool(mutates)}
        if context:
            payload["context"] = context
        if timeout:
            payload["timeout"] = timeout

        client_timeout = max(120.0, (timeout or 60000) / 1000 + 10)
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/script",
            json=payload,
            timeout=client_timeout,
        )
        r.raise_for_status()
        data = r.json()

        self.s.actions_since_screenshot += 1

        if not data.get("success"):
            error = data.get("error", "Unknown error")
            self.s.log_activity("run_script(FAILED)", error[:100])
            self.s.record_step("browser_run_script", script[:60], f"FAILED: {error[:100]}")
            # L1 sandbox rejected a mutation. Rewrite the reply so the
            # brain gets a concrete cursor-tool recommendation instead
            # of a raw JS error string (which it has been misreading
            # as a server 403).
            blocked_op = data.get("blocked_op")
            if blocked_op or error.startswith("[blocked_op:"):
                return (
                    f"[script_mutation_blocked] {error} The script "
                    f"tried to mutate the page from a mutates=false "
                    f"run. Either (a) re-call with mutates=true IF "
                    f"you genuinely need JS orchestration — rare, "
                    f"and many sites reject isTrusted=false clicks "
                    f"anyway — or (b) switch to "
                    f"browser_click_at(vision_index=V_n) / "
                    f"browser_type_at / browser_click_selector which "
                    f"use humanized isTrusted=true events."
                )
            # Fetch current elements so agent can see what's on the page and fix the script
            elements = await _fetch_elements(session_id, self.s)
            tip = "\n[TIP: Fix the script and retry in this SAME session. Do NOT navigate back to the start.]"
            if elements:
                tip += f"\n\nCurrent interactive elements:\n{elements}"
            return f"Script error: {error}{tip}"

        parts = []
        result = data.get("result")
        if result is not None:
            if isinstance(result, (dict, list)):
                parts.append(f"Result: {json.dumps(result, indent=2, ensure_ascii=False)[:5000]}")
            else:
                parts.append(f"Result: {str(result)[:5000]}")

        logs = data.get("logs", [])
        if logs:
            parts.append("Logs:\n" + "\n".join(logs[:20]))

        duration = data.get("duration", 0)
        parts.append(f"Duration: {duration}ms")
        self.s.log_activity(f"run_script(ok, {duration}ms)", str(result)[:60] if result else "void")
        self.s.record_step("browser_run_script", script[:60], str(result)[:100] if result else "void")
        self.s.record_checkpoint(self.s.current_url, "", f"run_script(ok, {duration}ms)")

        # Arch v4 (Step 3): no DOM element dump after run_script. Brain
        # uses browser_screenshot if it needs to re-anchor on V_n bboxes.
        return "\n".join(parts)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        text=StringSchema("Text to wait for on the page", nullable=True),
        selector=StringSchema("CSS selector to wait for", nullable=True),
        timeout=IntegerSchema(description="Max wait time in seconds (default: 10)", nullable=True),
        required=["session_id"],
    )
)
class BrowserWaitForTool(Tool):
    name = "browser_wait_for"
    description = (
        "Wait for text or a CSS selector to appear on the page. "
        "Much better than blind helpers.sleep() — polls efficiently until the condition is met. "
        "Provide either 'text' or 'selector' (not both). FREE — no screenshot cost."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        text: str | None = None,
        selector: str | None = None,
        timeout: int | None = None,
        **kw: Any,
    ) -> str:
        if not text and not selector:
            return "Error: provide either 'text' or 'selector' parameter."

        timeout_s = timeout or 10
        label = f'text="{text}"' if text else f'selector="{selector}"'
        print(f"\n>> browser_wait_for({label}, timeout={timeout_s}s)")

        if text:
            script = f"""
                const deadline = Date.now() + {timeout_s * 1000};
                while (Date.now() < deadline) {{
                    if (document.body.innerText.includes({json.dumps(text)})) {{
                        return {{found: true, title: document.title, url: location.href}};
                    }}
                    await new Promise(r => setTimeout(r, 500));
                }}
                return {{found: false, title: document.title, url: location.href, bodyPreview: document.body.innerText.substring(0, 200)}};
            """
        else:
            script = f"""
                const deadline = Date.now() + {timeout_s * 1000};
                while (Date.now() < deadline) {{
                    if (document.querySelector({json.dumps(selector)})) {{
                        return {{found: true, title: document.title, url: location.href}};
                    }}
                    await new Promise(r => setTimeout(r, 500));
                }}
                return {{found: false, title: document.title, url: location.href, bodyPreview: document.body.innerText.substring(0, 200)}};
            """

        client_timeout = max(30.0, timeout_s + 10)
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/script",
            json={"code": script, "timeout": timeout_s * 1000 + 5000},
            timeout=client_timeout,
        )
        r.raise_for_status()
        data = r.json()

        if not data.get("success"):
            self.s.log_activity(f"wait_for({label})", f"script error: {data.get('error', '?')[:60]}")
            return f"Wait failed (script error): {data.get('error', 'unknown')}"

        result = data.get("result", {})
        if result.get("found"):
            self.s.log_activity(f"wait_for({label})", "found")
            # Arch v4 (Step 3): no DOM element dump after wait_for.
            # Brain calls browser_screenshot next to refresh V_n bboxes.
            response = f"Found! Page: {result.get('url', '?')} | Title: {result.get('title', '?')}"
            return response
        else:
            self.s.log_activity(f"wait_for({label})", f"timeout after {timeout_s}s")
            return (
                f"Not found after {timeout_s}s (selector/text did NOT match). "
                f"This is a RENDERING-SPEED or SELECTOR issue — NOT a network "
                f"block. DO NOT escalate to Tier 3.\n"
                f"Page: {result.get('url', '?')} | Title: {result.get('title', '?')}\n"
                f"Page preview: {result.get('bodyPreview', 'N/A')}\n"
                f"Next steps:\n"
                f"  - browser_screenshot to see the actual rendered state.\n"
                f"  - Retry browser_wait_for with a longer timeout (20-30s) "
                f"or a different selector (e.g. try 'form', 'button[type=submit]' "
                f"instead of generic 'input').\n"
                f"  - browser_run_script with `return document.body.innerText.length` "
                f"to confirm the page has actually rendered content."
            )


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserGetMarkdownTool(Tool):
    name = "browser_get_markdown"
    description = "Extract page content as markdown. FREE — no screenshot cost."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        session_id = self.s.resolve_session_id(session_id)
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/markdown",
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("content", "No content extracted")[:10000]


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        accept=BooleanSchema(description="Accept (true) or dismiss (false)"),
        text=StringSchema("Text for prompt dialogs", nullable=True),
        required=["session_id", "accept"],
    )
)
class BrowserDialogTool(Tool):
    name = "browser_dialog"
    description = "Accept or dismiss a pending JavaScript dialog."

    async def execute(self, session_id: str, accept: bool, text: str | None = None, **kw: Any) -> str:
        session_id = self.s.resolve_session_id(session_id)
        payload: dict[str, Any] = {"accept": accept}
        if text:
            payload["text"] = text
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/dialog",
            json=payload,
            timeout=10.0,
        )
        r.raise_for_status()
        return f"Dialog {'accepted' if accept else 'dismissed'}"


# ─── Phase 2: form-fill orchestration tools ──────────────────────────────


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "What this form does — e.g. 'apartment search filters', "
            "'flight booking', 'signup'. Used by the worker hook to "
            "phrase the per-turn checklist."
        ),
        fields=ArraySchema(
            description=(
                "Ordered list of fields to fill. Each entry is an object "
                "with `label` (human-readable name to match against vision "
                "bboxes), `value` (target text to type), and optional "
                "`autocomplete` (true if this field opens a suggestions "
                "overlay that must be picked from)."
            ),
            items=ObjectSchema(
                label=StringSchema("Field name shown to the user"),
                value=StringSchema("Value to type"),
                autocomplete=BooleanSchema(
                    description="Whether this field opens an autocomplete dropdown",
                    nullable=True,
                ),
                required=["label", "value"],
            ),
        ),
        submit_label=StringSchema(
            "Optional label of the submit button (e.g. 'Search'). "
            "If provided, browser_form_commit will look for it in vision.",
            nullable=True,
        ),
        required=["session_id", "intent", "fields"],
    )
)
class BrowserFormBeginTool(Tool):
    """Phase 2.1: open a form-fill session.

    Tracks pending/filled/verified state for each declared field. While
    a session is active the worker hook injects a remaining-fields
    checklist into every tool result, and browser_form_commit refuses
    to dispatch the submit click until every field's typed value is
    visible on the page.

    Use this on dense filter/booking/signup forms where the brain
    routinely loses track of fields below an autocomplete dropdown.
    """

    name = "browser_form_begin"
    description = (
        "Open a tracked form-fill session. After calling, fill each "
        "field with browser_type_at — the session tracks progress and "
        "warns when a field is missed. Conclude with browser_form_commit "
        "to verify all values stuck before submitting. "
        "For filter modals: pass `inventory=true` after running "
        "browser_inventory_filters first; the session will fuzzy-match "
        "your requested labels against the manifest (resolves user "
        "term 'WiFi' → UI label 'Wi-Fi included') and attach a stable "
        "selector to each field for browser_click_selector application."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        intent: str,
        fields: list[dict[str, Any]],
        submit_label: str | None = None,
        inventory: bool = False,
        **kw: Any,
    ) -> str:
        if os.environ.get("FORM_SESSION_ENABLED", "1") in ("0", "false", "no"):
            return (
                "[form_begin_disabled] FORM_SESSION_ENABLED=0 — fall "
                "back to ad-hoc filling. Track remaining fields yourself."
            )
        if not isinstance(fields, list) or not fields:
            return "[form_begin_failed] `fields` must be a non-empty list."
        try:
            from superbrowser_bridge.form_session import FormFillSession
        except ImportError as exc:
            return f"[form_begin_failed:import] {exc}"

        # inventory=true → fuzzy-resolve user labels against the most
        # recent browser_inventory_filters manifest. Solves the
        # "user said 'cleaning', UI says 'Cleaning service included'"
        # gap that wrecks form_commit verification.
        resolution_lines: list[str] = []
        if inventory:
            manifest = getattr(self.s, "last_filter_manifest", None)
            if not manifest or not manifest.get("options"):
                return (
                    "[form_begin_failed:no_manifest] inventory=true but no "
                    "filter manifest cached on this session. Call "
                    "browser_inventory_filters(session_id) FIRST to scan the "
                    "open dialog, then re-call form_begin with inventory=true."
                )
            options = manifest.get("options") or []
            resolved_fields: list[dict[str, Any]] = []
            unresolved: list[tuple[str, list[tuple[str, float]]]] = []
            for f in fields:
                user_label = (f.get("label") or "").strip()
                if not user_label:
                    continue
                top = _match_label_against_manifest(user_label, options, top_k=3)
                if not top:
                    unresolved.append((user_label, []))
                    continue
                best_label, best_score, best_opt = top[0]
                if best_score >= 0.75:
                    resolved_fields.append({
                        **f,
                        "_resolved_label": best_opt.get("label"),
                        "_resolved_selector": best_opt.get("selector"),
                        "_match_score": best_score,
                    })
                    resolution_lines.append(
                        f"  ✓ {user_label!r} → {best_opt.get('label')!r} "
                        f"(score={best_score:.2f}, selector={best_opt.get('selector')})"
                    )
                else:
                    unresolved.append((user_label, [(o.get("label", ""), s) for _, s, o in top]))
            if unresolved:
                lines = ["[form_begin_failed:unresolved] Some requested filters did not match the manifest:"]
                for user_label, candidates in unresolved:
                    if candidates:
                        cand_str = ", ".join(f"{lbl!r} ({s:.2f})" for lbl, s in candidates)
                        lines.append(f"  ? {user_label!r} → closest: {cand_str}")
                    else:
                        lines.append(f"  ? {user_label!r} → no candidates (manifest empty for this region)")
                lines.append(
                    "ACTION: re-call browser_form_begin with the closest matching label "
                    "from the manifest (or drop the field if the site doesn't offer it)."
                )
                return "\n".join(lines)
            fields = resolved_fields  # only if all resolved confidently

        sess = FormFillSession.begin(
            intent=intent,
            fields=fields,
            started_at_turn=self.s._brain_turn_counter,
            submit_label=submit_label,
        )
        # Propagate resolved metadata onto FieldState entries.
        if inventory:
            for f in fields:
                key = (f.get("label") or "").strip().lower()
                fs = sess.fields.get(key)
                if fs is None:
                    continue
                if f.get("_resolved_label"):
                    fs.resolved_label = f["_resolved_label"]
                if f.get("_resolved_selector"):
                    fs.resolved_selector = f["_resolved_selector"]
                if f.get("_match_score") is not None:
                    fs.match_score = float(f["_match_score"])
        self.s.form_session = sess
        labels = ", ".join(fs.label for fs in sess.fields.values())
        out: list[str] = [f"[form_begin] intent={intent!r} fields=[{labels}]"]
        if resolution_lines:
            out.append("Resolved against manifest:")
            out.extend(resolution_lines)
            out.append(
                "Apply each via browser_click_selector(<resolved selector>). "
                "Selectors from the manifest are stable across modal scrolls. "
                "Conclude with browser_form_commit to verify."
            )
        else:
            out.append(
                "Now: call browser_screenshot once to anchor every field's "
                "bbox, then for each field call browser_type_at(vision_index="
                "V_n, text=...). After typing into a field that opens "
                "autocomplete, click the matching suggestion (or press "
                "Escape) BEFORE moving on. When every field is filled, "
                "call browser_form_commit to verify."
            )
        return "\n".join(out)


def _match_label_against_manifest(
    user_label: str,
    options: list[dict],
    top_k: int = 3,
) -> list[tuple[str, float, dict]]:
    """Return [(matched_label, score 0..1, option_dict)] sorted desc.

    Uses difflib + token-overlap + substring bonus + a compact (no-space)
    pass to bridge punctuation gaps like "WiFi" ↔ "Wi-Fi included". No
    external deps. Tolerant of short user terms vs longer UI labels and
    common UI suffixes ("included", "available", "service", "on-site").
    """
    import difflib
    import re as _re

    _SUFFIX_RE = _re.compile(
        r"\b(included|available|service|services|on[- ]site|allowed|"
        r"only|optional|free)\b",
        _re.IGNORECASE,
    )

    def _norm(s: str) -> str:
        s = (s or "").lower()
        s = _re.sub(r"[^a-z0-9 ]+", " ", s)
        return _re.sub(r"\s+", " ", s).strip()

    def _strip_suffixes(s: str) -> str:
        return _re.sub(r"\s+", " ", _SUFFIX_RE.sub(" ", s)).strip()

    def _stem(tok: str) -> str:
        # Cheap singular/-ing trim to bridge "pets"↔"pet", "parking"↔"park".
        for suf in ("ing", "es", "s"):
            if len(tok) > len(suf) + 2 and tok.endswith(suf):
                return tok[: -len(suf)]
        return tok

    def _tokens(s: str) -> set[str]:
        return {_stem(t) for t in _norm(s).split() if t}

    def _compact(s: str) -> str:
        return _norm(s).replace(" ", "")

    nu = _norm(user_label)
    nu_stripped = _strip_suffixes(nu)
    cu = _compact(user_label)
    tu = _tokens(user_label)
    if not nu and not cu:
        return []
    scored: list[tuple[str, float, dict]] = []
    for opt in options:
        olabel = (opt.get("label") or "").strip()
        if not olabel:
            continue
        no = _norm(olabel)
        no_stripped = _strip_suffixes(no)
        co = _compact(olabel)
        to = _tokens(olabel)
        # 1. Sequence ratio on normalized strings.
        ratio = difflib.SequenceMatcher(None, nu, no).ratio()
        # 2. Sequence ratio on suffix-stripped strings (handles "Wi-Fi included").
        ratio_stripped = difflib.SequenceMatcher(None, nu_stripped, no_stripped).ratio()
        # 3. Sequence ratio on compact (no-space) strings — bridges "wifi"↔"wi fi".
        ratio_compact = difflib.SequenceMatcher(None, cu, co).ratio() if (cu and co) else 0.0
        # 4. Token-overlap Jaccard with stemming.
        jac = len(tu & to) / max(1, len(tu | to)) if (tu and to) else 0.0
        # 5. Substring containment: a strong signal for "wifi" inside "wifiincluded".
        contain = 0.0
        if (nu and no) and (nu in no or no in nu):
            contain = 0.90
        elif (cu and co) and (cu in co or co in cu):
            contain = 0.88
        score = max(
            ratio * 0.6 + jac * 0.4,
            ratio_stripped * 0.7 + jac * 0.3,
            ratio_compact * 0.65 + jac * 0.35,
            contain,
        )
        scored.append((olabel, round(score, 3), opt))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:top_k]


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserFormStatusTool(Tool):
    """Phase 2.1: report the current form-fill checklist."""

    name = "browser_form_status"
    description = (
        "Report status of the active form-fill session: which fields are "
        "still pending, which are filled, which need autocomplete picks. "
        "Cheap / no screenshot. Returns [no_form_session] if none active."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        session_id = self.s.resolve_session_id(session_id)
        sess = self.s.form_session
        if sess is None:
            return (
                "[no_form_session] No form-fill session active. Call "
                "browser_form_begin to start tracking a multi-field form."
            )
        return sess.remaining_checklist(max_lines=20)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        force=BooleanSchema(
            description=(
                "Skip the verify-all-fields check and close the session anyway. "
                "Use only when you intentionally want to submit a partial form."
            ),
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserFormCommitTool(Tool):
    """Phase 2.1: verify and close a form-fill session.

    Forces a fresh screenshot, then for each tracked field checks that
    the typed value appears in the page's relevant_text. Returns a
    structured pass/fail report — the brain decides whether to refill
    mismatched fields or submit.
    """

    name = "browser_form_commit"
    description = (
        "Verify every tracked field's typed value appears on screen, "
        "then close the form-fill session. Returns the per-field "
        "verdict so the brain can refill any mismatches before "
        "clicking submit."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        force: bool = False,
        **kw: Any,
    ) -> str:
        sess = self.s.form_session
        if sess is None:
            return (
                "[form_commit_failed:no_session] No form session is "
                "active. Call browser_form_begin first."
            )
        # Refresh vision so we verify against the latest screenshot.
        resp = self.s._last_vision_response
        text_hay = ""
        if resp is not None:
            text_hay = (getattr(resp, "relevant_text", "") or "").lower()
        for fs in sess.fields.values():
            if fs.status == FieldStatus.SKIPPED:
                continue
            target_lower = (fs.target_value or "").strip().lower()
            if not target_lower:
                continue
            if target_lower in text_hay:
                sess.mark_verified(fs.label, fs.target_value)
            else:
                # Don't overwrite VERIFIED set during typing flow.
                if fs.status not in (FieldStatus.VERIFIED,):
                    fs.status = FieldStatus.MISMATCH
        summary = sess.commit_summary()
        if not force and not sess.is_complete():
            return (
                f"[form_commit_incomplete] {summary}\n"
                f"{sess.remaining_checklist(max_lines=10)}\n"
                f"Refill the mismatched / pending fields, then call "
                f"browser_form_commit again. Use force=true ONLY if you "
                f"intentionally want to submit a partial form."
            )
        # Success — clear the session so the next form starts clean.
        result = (
            f"[form_commit_ok] {summary}\n"
            f"You may now click the submit button "
            + (f"('{sess.submit_label}') " if sess.submit_label else "")
            + "via browser_click_at(vision_index=V_n)."
        )
        self.s.form_session = None
        return result


# Re-export FieldStatus into module scope so the commit tool can refer to
# it without importing locally on every call.
try:
    from superbrowser_bridge.form_session import FieldStatus  # noqa: F401
except ImportError:
    class FieldStatus:  # type: ignore[no-redef]
        VERIFIED = "verified"
        SKIPPED = "skipped"
        MISMATCH = "mismatch"


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserPlanNextStepsTool(Tool):
    """Re-run the hierarchical action planner against the cached vision
    + blocker state without taking a fresh screenshot.

    Useful after a click that missed its postcondition — the brain wants
    an updated plan without burning screenshot budget. The planner itself
    is cached by scene fingerprint, so rapid re-plans on an unchanged
    scene return the same queue in near-zero time.

    Returns the ordered [PLAN] block as plain text. The worker LLM can
    read it and pick the top action to execute, or override.
    """

    name = "browser_plan_next_steps"
    description = (
        "Re-compute the planned action queue (dismiss blockers → main goal) "
        "from the most recent vision + DOM-blocker snapshot. No screenshot, "
        "no vision call — cheap. Call after a failed dismiss or when the "
        "scene may have changed and you want the planner's latest ranking "
        "before deciding the next tool."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        session_id = self.s.resolve_session_id(session_id)
        # Planner now runs for both t1 and t3 via tier_evaluate. Single
        # kill switch: ACTION_PLANNER_T1=0 disables on t1.
        if not session_id.startswith("t3-") and \
                os.environ.get("ACTION_PLANNER_T1", "1") == "0":
            return (
                "[plan_unavailable] Planner disabled on t1 "
                "(ACTION_PLANNER_T1=0). Call browser_screenshot to get "
                "a fresh vision + suggested_actions instead."
            )
        resp = self.s._last_vision_response
        if resp is None:
            return (
                "[plan_unavailable] No cached vision response. Run "
                "browser_screenshot first."
            )
        try:
            from superbrowser_bridge.tier_evaluate import get_backend as _get_backend
            from superbrowser_bridge.antibot.ui_blockers import detect as _detect_blockers
            from superbrowser_bridge.action_planner import plan as _plan_actions
            backend = _get_backend(session_id)
            blockers = await _detect_blockers(backend, session_id)
            self.s._last_blockers = blockers
            queue = _plan_actions(
                vresp=resp,
                blockers=blockers,
                task_instruction=self.s.task_instruction or "",
                url=self.s.current_url or "",
                recent_steps=self.s.step_history[-8:] if self.s.step_history else [],
            )
            self.s._last_action_queue = queue
            return queue.to_brain_text()
        except Exception as exc:
            return f"[plan_failed] {str(exc)[:200]}"


# ── Multi-step task plan (task_plan.py) ─────────────────────────────
# Three small tools that let the brain commit to a top-level checklist
# of sub-goals once at the start of a task, then advance through them
# deterministically. Verify_action auto-checks the active step's
# success_criteria after each click / type / navigate; worker_hook
# renders the cursor in every tool reply. See task_plan.py for the
# state-machine details.


_PLAN_STEP_ITEM = ObjectSchema(
    name=StringSchema(
        "Short imperative description of the sub-goal — e.g. "
        "'apply Region=Oregon filter', 'sort by critic score', "
        "'extract top 5 wines'. One concrete verifiable action."
    ),
    success_criteria=ObjectSchema(
        kind=StringSchema(
            "Postcondition kind: url_changed, url_matches, text_visible, "
            "text_hidden, bbox_disappeared, focus_on_role, dom_mutated, "
            "flag_cleared. Cannot be 'none' — every step must be "
            "observable so verify_action can advance the plan automatically."
        ),
        payload=ObjectSchema(
            description=(
                "Kind-specific payload. url_matches → {pattern: substring}; "
                "text_visible/text_hidden → {text: needle}; "
                "bbox_disappeared → {widget_px: [x0,y0,x1,y1]}; "
                "focus_on_role → {selector: css}; flag_cleared → "
                "{flag: 'captcha_present' | 'blocker_present'}; "
                "url_changed / dom_mutated need no payload."
            ),
            nullable=True,
        ),
        timeout_ms=IntegerSchema(
            description="How long to wait for the criterion (default 2500)",
            nullable=True,
        ),
        required=["kind"],
    ),
    delegate=ObjectSchema(
        description=(
            "Optional sub-machine that executes within this step. "
            "kind='form_session' → brain must call browser_form_begin "
            "with payload.fields before clicking. kind='navigation' → "
            "step is a single navigate. kind='extraction' → terminal "
            "read step. kind='manual' → no enforced sub-machine."
        ),
        kind=StringSchema(
            "form_session | navigation | extraction | manual"
        ),
        payload=ObjectSchema(
            description="Sub-machine-specific payload (e.g. {fields: [...]})",
            nullable=True,
        ),
        required=["kind"],
        nullable=True,
    ),
    required=["name", "success_criteria"],
)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        steps=ArraySchema(
            description=(
                "Ordered list of sub-goals. Each step has `name`, "
                "`success_criteria` (Postcondition: kind + payload), and "
                "optional `delegate` for sub-machines like form_session. "
                "Plan must have ≥2 steps; for single-step tasks just "
                "skip the planner entirely."
            ),
            items=_PLAN_STEP_ITEM,
        ),
        required=["session_id", "steps"],
    )
)
class BrowserSetTaskPlanTool(Tool):
    """Commit to a persistent multi-step task plan.

    Call ONCE at the start of any task with ≥3 distinct sub-goals
    (multi-filter searches, multi-step booking flows, sequential
    extractions). The plan is rendered into every subsequent tool
    reply with a cursor advancing through steps; verify_action
    auto-checks the active step's success_criteria after each
    state-change tool. See task_plan.py for lifecycle.
    """

    name = "browser_set_task_plan"
    description = (
        "Commit to a top-level task plan: ordered sub-goals each with "
        "an observable success_criteria. Call ONCE after your first "
        "screenshot if the task has ≥3 sub-goals. Validator rejects "
        "empty / single-step plans and steps whose criterion is 'none'. "
        "Use browser_plan_skip_step / browser_plan_replan to recover "
        "from an unsatisfiable step rather than silently moving on."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        steps: list[dict[str, Any]],
        **kw: Any,
    ) -> str:
        from superbrowser_bridge.task_plan import (
            make_plan,
            TaskPlanValidationError,
        )

        try:
            plan = make_plan(steps)
        except TaskPlanValidationError as exc:
            return f"[set_task_plan_failed:validation] {exc}"
        except Exception as exc:
            return f"[set_task_plan_failed] {type(exc).__name__}: {exc}"

        prior = self.s.task_plan
        self.s.task_plan = plan
        # Move the first step to in_progress immediately so the next
        # state-change tool's verify_action probe knows what to check.
        active = plan.active_step()
        verb = "Replaced" if prior is not None else "Set"
        active_line = (
            f"Active: {active.name}" if active else "All steps satisfied"
        )
        return (
            f"[task_plan_set] {verb} plan with {len(plan.steps)} steps. "
            f"{active_line}\n{plan.to_brain_text()}"
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        reason=StringSchema(
            "Why this step can't be satisfied (e.g. 'site has no Oregon "
            "filter; only US-region', 'price slider is broken'). Recorded "
            "for post-task analysis."
        ),
        required=["session_id", "reason"],
    )
)
class BrowserPlanSkipStepTool(Tool):
    """Mark the active TaskPlan step as unsatisfiable and advance.

    Use when the active step genuinely can't be satisfied — the filter
    doesn't exist on this site, a required control is missing, or a
    flow has changed. Don't use for transient failures (network blip,
    stale vision); the planner already auto-flips to unsatisfiable
    after MAX_STEP_ATTEMPTS verification misses.
    """

    name = "browser_plan_skip_step"
    description = (
        "Skip the active task-plan step (mark it unsatisfiable) and "
        "advance to the next. Use only when the step truly can't be "
        "completed, not for transient failures."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, reason: str, **kw: Any) -> str:
        if self.s.task_plan is None:
            return (
                "[plan_skip_failed:no_plan] No active task plan. "
                "Call browser_set_task_plan first or just continue."
            )
        skipped = self.s.task_plan.skip_active(reason or "explicitly_skipped")
        if skipped is None:
            return (
                "[plan_skip_noop] No active step to skip — plan is complete."
            )
        next_step = self.s.task_plan.active_step()
        next_line = (
            f"Next: {next_step.name}"
            if next_step
            else "All remaining steps resolved"
        )
        return (
            f"[plan_step_skipped] {skipped.name!r} marked unsatisfiable "
            f"(reason: {reason[:80]}). {next_line}\n"
            f"{self.s.task_plan.to_brain_text()}"
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        reason=StringSchema(
            "Why the previous plan needs to be rebuilt (e.g. 'site has "
            "different filter taxonomy than expected'). Recorded for audit."
        ),
        new_steps=ArraySchema(
            description=(
                "Replacement step list with the same shape as "
                "browser_set_task_plan.steps. Same validator applies."
            ),
            items=_PLAN_STEP_ITEM,
        ),
        required=["session_id", "reason", "new_steps"],
    )
)
class BrowserPlanReplanTool(Tool):
    """Replace the current TaskPlan with a new one.

    Use when on-page reality diverges from your initial plan (filters
    that don't exist, an unexpected modal flow, a redesigned page).
    The previous plan is dropped; lifecycle counters reset.
    """

    name = "browser_plan_replan"
    description = (
        "Replace the current task plan entirely. Use when on-page "
        "reality diverges from your initial plan. Validator behavior "
        "matches browser_set_task_plan."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        reason: str,
        new_steps: list[dict[str, Any]],
        **kw: Any,
    ) -> str:
        from superbrowser_bridge.task_plan import (
            make_plan,
            TaskPlanValidationError,
        )

        try:
            plan = make_plan(new_steps)
        except TaskPlanValidationError as exc:
            return f"[plan_replan_failed:validation] {exc}"
        except Exception as exc:
            return f"[plan_replan_failed] {type(exc).__name__}: {exc}"

        prior_summary = ""
        if self.s.task_plan is not None:
            done = sum(
                1 for s in self.s.task_plan.steps if s.status == "satisfied"
            )
            prior_summary = (
                f" (replaced previous plan, {done}/"
                f"{len(self.s.task_plan.steps)} satisfied)"
            )
        self.s.task_plan = plan
        active = plan.active_step()
        active_line = (
            f"Active: {active.name}" if active else "All steps satisfied"
        )
        return (
            f"[plan_replanned] reason={reason[:80]!r}{prior_summary}. "
            f"{active_line}\n{plan.to_brain_text()}"
        )


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserCloseTool(Tool):
    name = "browser_close"
    description = "Close the browser session and free resources. Always close when done."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, **kw: Any) -> str:
        session_id = self.s.resolve_session_id(session_id)
        # v7: refuse close when an active TaskPlan still has work AND
        # there's screenshot budget left to do it. The brain otherwise
        # closes after partial progress and reports "I could not
        # truthfully return any qualifying wine" — observed wineaccess
        # trace where it reached the Oregon catalog, sorted by score,
        # then closed before applying remaining filters or extracting
        # results. If the brain genuinely needs to bail, it should call
        # done(success=False) — that's the explicit failure path.
        # Kill switch CLOSE_GUARD=0.
        if os.environ.get("CLOSE_GUARD", "1") != "0":
            refuse = self._maybe_refuse_premature_close()
            if refuse:
                return refuse
        print(f"\n>> browser_close({session_id})")
        self.s.log_activity(f"close({session_id})")
        self.s.print_summary()
        self.s.export_activity_log()
        self.s.export_step_history()
        # Route through _request_with_backoff so t3 sessions get intercepted
        # and dispatched to the in-process patchright manager.
        r = await _request_with_backoff(
            "DELETE",
            f"{SUPERBROWSER_URL}/session/{session_id}",
            timeout=10.0,
        )
        r.raise_for_status()
        used = self.s.max_screenshots - self.s.screenshot_budget
        return f"Session closed. Vision: {self.s.vision_calls}, Text: {self.s.text_calls}, Screenshots: {used}/{self.s.max_screenshots}, Regressions: {self.s.regression_count}"

    def _maybe_refuse_premature_close(self) -> str | None:
        """Refuse browser_close when the TaskBrief checklist still has
        unverified constraints AND screenshot budget is left to work on
        them.

        Arch v4.1 (Fix 2c): the v4 single source of truth is
        TaskBrief.checklist (constraints with status). If the brief
        reports `not is_complete()` and budget > 2, refuse with the
        list of unverified constraints. The brain can call
        browser_update_task_brief to mark constraints `not_applicable`
        when they genuinely don't apply on this site, or call
        done(success=False) for the explicit failure path. Legacy
        TaskPlan check kept as a fallback for sessions where TaskBrief
        is missing.

        Returns refusal message or None to allow.
        """
        budget = max(0, self.s.screenshot_budget)
        # TaskBrief takes precedence (v4 source of truth).
        brief = getattr(self.s, "task_brief", None)
        if brief is not None:
            try:
                total, sat, fail = brief.counts()
                unverified = [
                    c for c in brief.constraints
                    if getattr(c, "status", "unverified") == "unverified"
                ]
            except Exception:
                unverified = []
                total = sat = fail = 0
            if unverified and total > 0 and budget > 2:
                names = ", ".join(
                    repr((c.canonical_value or c.text or f"#{i+1}"))
                    for i, c in enumerate(unverified[:5])
                )
                ellipsis = "…" if len(unverified) > 5 else ""
                return (
                    f"[close_guard_refused: {len(unverified)} of {total} "
                    f"TaskBrief constraints still unverified "
                    f"(satisfied={sat}"
                    + (f", failed={fail}" if fail else "")
                    + f"); {budget} screenshots remaining] "
                    f"You're closing the session with real budget left "
                    f"and pending constraints. Don't give up here.\n"
                    f"  Unverified: {names}{ellipsis}\n"
                    f"  Recovery — pick one:\n"
                    f"  1. browser_screenshot — take a fresh look; the "
                    f"active focus may be one click away.\n"
                    f"  2. browser_update_task_brief(constraint_idx=N, "
                    f"status='not_applicable', reason='…') — when a "
                    f"constraint genuinely doesn't apply on this site.\n"
                    f"  3. done(success=False, final_answer='honest reason') — "
                    f"the explicit failure path. After done() the "
                    f"orchestrator closes the session for you.\n"
                    f"Override: CLOSE_GUARD=0."
                )
        # Legacy fallback: TaskPlan check (sessions without a brief).
        plan = getattr(self.s, "task_plan", None)
        if plan is None:
            return None
        try:
            unsatisfied = [
                s for s in plan.steps
                if s.status in ("pending", "in_progress")
            ]
        except Exception:
            return None
        if not unsatisfied:
            return None
        if budget <= 2:
            return None
        return (
            f"[close_guard_refused: {len(unsatisfied)} of "
            f"{len(plan.steps)} TaskPlan steps still unsatisfied; "
            f"{budget} screenshots remaining] You're closing the session "
            f"with real budget left and pending work. Don't give up here.\n"
            f"  Unsatisfied steps: "
            f"{', '.join(repr(s.name) for s in unsatisfied[:5])}"
            f"{'…' if len(unsatisfied) > 5 else ''}\n"
            f"  Recovery: browser_screenshot to retake the page, or "
            f"done(success=False, final_answer='honest reason') for the "
            f"explicit failure path.\n"
            f"Override: CLOSE_GUARD=0."
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        startX=NumberSchema("Start X coordinate"),
        startY=NumberSchema("Start Y coordinate"),
        endX=NumberSchema("End X coordinate"),
        endY=NumberSchema("End Y coordinate"),
        steps=IntegerSchema("Number of intermediate steps (default 25, higher = smoother)", nullable=True),
        required=["session_id", "startX", "startY", "endX", "endY"],
    )
)
class BrowserDragTool(Tool):
    name = "browser_drag"
    description = "Drag from (startX, startY) to (endX, endY). Useful for slider CAPTCHAs and drag-to-verify puzzles."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, startX: float, startY: float, endX: float, endY: float, steps: int | None = None, **kw: Any) -> str:
        gate = self.s.must_screenshot_before_state_change("browser_drag")
        if gate:
            return gate
        session_id = self.s.resolve_session_id(session_id)
        print(f"\n>> browser_drag(({startX},{startY}) -> ({endX},{endY}))")
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        payload: dict[str, Any] = {
            "startX": startX, "startY": startY,
            "endX": endX, "endY": endY,
        }
        if steps is not None:
            payload["steps"] = steps

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/drag",
            json=payload,
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()

        self.s.record_step("browser_drag", f"({startX},{startY})->({endX},{endY})", data.get("url", ""))
        caption = f"Dragged from ({startX},{startY}) to ({endX},{endY})"
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return caption


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserDetectCaptchaTool(Tool):
    name = "browser_detect_captcha"
    description = "Check if the page has a captcha."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        session_id = self.s.resolve_session_id(session_id)
        # Route through _request_with_backoff so t3 sessions go to the
        # in-process patchright captcha detector, not the TS server.
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/captcha/detect",
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        captcha = data.get("captcha")
        if not captcha:
            return "No captcha detected."
        # Normalize across tiers: t3 returns {present: bool, type: ...};
        # TS may return a different shape where absence is represented as
        # falsy. Either way, bail if nothing is active.
        if isinstance(captcha, dict) and (
            captcha.get("present") is False
            or captcha.get("type") in ("none", None, "")
        ):
            return "No captcha detected."
        # Detecting a captcha triggers captcha_mode: screenshot dedup +
        # "no-actions-since-last-shot" rules are relaxed so the solver can
        # take repeated before/after screenshots.
        self.s.enter_captcha_mode()
        # Surface the live-view URL the moment a captcha is detected, not
        # only when handoff fires. Gives the LLM (and any UI piping tool
        # output to a human) a link to offer immediately. For t3 sessions
        # this is the Python viewer port; for t1 it's the TS server.
        view_url: str | None = None
        if self.s.backend == "t3":
            try:
                from superbrowser_bridge.antibot import t3_viewer as _v
                await _v.ensure_started()
                view_url = _v.view_url(session_id)
            except Exception:
                view_url = None
        else:
            view_url = data.get("viewUrl") or (
                f"{os.environ['SUPERBROWSER_PUBLIC_HOST'].rstrip('/')}"
                f"/session/{session_id}/view"
                if os.environ.get("SUPERBROWSER_PUBLIC_HOST")
                else None
            )
        lines = [
            f"Captcha detected: type={captcha['type']}, "
            f"siteKey={captcha.get('siteKey', 'N/A')} "
            f"(captcha_mode active for next {self.s.CAPTCHA_MODE_ITERATIONS} iterations)",
        ]
        if view_url:
            lines.append(
                f"Live view for human handoff: {view_url} "
                f"(open this URL if you decide to hand off to the user)"
            )
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserCaptchaScreenshotTool(Tool):
    name = "browser_captcha_screenshot"
    description = "Take a close-up screenshot of the captcha area."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> Any:
        session_id = self.s.resolve_session_id(session_id)
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/captcha/screenshot",
            timeout=10.0,
        )
        if r.status_code == 404:
            return "No captcha area found."
        r.raise_for_status()
        # t3 returns a dict with image_base64; t1 returns raw JPEG bytes.
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        if isinstance(body, dict) and body.get("image_base64"):
            b64 = body["image_base64"]
        else:
            b64 = base64.b64encode(r.content).decode()
        return await self.s.build_tool_result_blocks(
            b64,
            "Captcha area — analyze to solve",
            intent="solve captcha — locate widget + tiles + handles",
            url=self.s.current_url,
        )


# --- Iterative captcha loop -------------------------------------------------
# Shared between BrowserSolveCaptchaTool (Python path, method='vision') and
# antibot/captcha/solve_vision.py (T3 HTTP path, POST /captcha/solve). Both
# entry points end up here so a fix to the "click every tile blindly" bug
# cannot regress in one path while the other is left behind.

_SUBMIT_KEYWORDS = ("verify", "submit", "next", "continue", "check", "done", "i'm done")


def _first_actionable(resp: Any) -> Any:
    """Pick the next bbox worth acting on from a VisionResponse.

    Preference: unselected captcha_tile → slider_handle → verify/submit
    button → captcha_widget fallback. Returns None if the response has
    nothing usable. Purely local — no side effects.
    """
    bboxes = getattr(resp, "bboxes", None) or []
    tiles, handles, submits, widgets = [], [], [], []
    for b in bboxes:
        role = (getattr(b, "role", "") or "").lower()
        label = (getattr(b, "label", "") or "").lower()
        if role == "captcha_tile":
            tiles.append(b)
        elif role == "slider_handle":
            handles.append(b)
        elif role == "captcha_widget":
            widgets.append(b)
        elif role == "button" and any(kw in label for kw in _SUBMIT_KEYWORDS):
            submits.append(b)
    # Tiles first if any remain; submit only takes priority when tiles are
    # gone (i.e. grid challenge completed).
    if tiles:
        return tiles[0]
    if handles:
        return handles[0]
    if submits:
        return submits[0]
    if widgets:
        return widgets[0]
    return None


async def _solve_captcha_iterative(
    session_id: str,
    captcha_info: Any,
    vision_agent: Any,
    *,
    task_instruction: str = "",
    solve_round: int = 0,
    max_steps: int = 12,
) -> dict[str, Any]:
    """Per-step vision-driven captcha solver.

    Each iteration: screenshot → vision (returns one next action) → click
    or drag → poll structural hash until page changes (cap 600ms). Exits
    when vision reports captcha_present=false or when a safety guard
    (dead-action streak, max_steps, HTTP failure) fires.

    Returns a structured dict the orchestrator's captcha-learnings
    consumer understands — see orchestrator_tools._update_captcha_learnings.
    Never raises into the caller; failures collapse into solved=False +
    an `error` field.
    """
    actions: list[str] = []
    last_click_xy: tuple[int, int] | None = None
    # Trail of up to 6 recent click points, overlaid on the next
    # screenshot so the model can see where we've already interacted.
    cursor_trail: list[tuple[int, int]] = []
    same_action_streak = 0
    steps_taken = 0
    model_name: str | None = None
    provider_name: str | None = None

    # Surface any text prompt the native detector already captured.
    prompt_hint = ""
    if captcha_info is not None:
        for n in list(getattr(captcha_info, "notes", None) or []):
            if isinstance(n, str) and n.startswith("text_signal:"):
                prompt_hint = n.split(":", 1)[1]
                break

    for step in range(max_steps):
        steps_taken = step + 1
        # 1. Screenshot + page state.
        try:
            sr = await _request_with_backoff(
                "GET",
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true"},
                timeout=15.0,
            )
            sr.raise_for_status()
            state = sr.json()
        except Exception as exc:
            actions.append(f"step {step}: state fetch failed: {exc}")
            return {
                "solved": False, "method": "vision_iterative",
                "subMethod": "python_vision_agent",
                "steps": steps_taken, "actions": actions,
                "provider": provider_name, "model": model_name,
                "error": f"state_fetch:{exc}",
            }

        b64 = state.get("screenshot")
        if not b64:
            actions.append(f"step {step}: no screenshot in state payload")
            return {
                "solved": False, "method": "vision_iterative",
                "subMethod": "python_vision_agent",
                "steps": steps_taken, "actions": actions,
                "provider": provider_name, "model": model_name,
                "error": "no_screenshot",
            }
        current_url = state.get("url", "")
        elements_str = (
            state.get("clickableElementsToString") or state.get("elements") or ""
        )
        try:
            page_hash = BrowserSessionState.hash_page_content(elements_str)
        except Exception:
            page_hash = ""
        img_w, img_h = _read_image_dims(b64)

        # 2. Ask vision for the single next action.
        last_action_hint = (
            f"Previous click: ({last_click_xy[0]},{last_click_xy[1]})"
            if last_click_xy else "Previous click: none"
        )
        try:
            resp = await vision_agent.analyze(
                screenshot_b64=b64,
                # "captcha step" routes to the solve_captcha_step intent
                # bucket in prompts.intent_bucket — step-mode suppresses
                # SoM overlay and skips result caching.
                intent="solve captcha step — pick the single next action",
                session_id=session_id,
                url=current_url,
                dom_hash=f"cap-r{solve_round}-s{step}-{page_hash}",
                image_width=img_w,
                image_height=img_h,
                task_instruction=(
                    (task_instruction or "")
                    + ("\nCaptcha prompt: " + prompt_hint if prompt_hint else "")
                    + f"\n{last_action_hint}"
                    + "\nReturn next_action committing to ONE step. If every "
                    "matching tile appears selected/dismissed, pick "
                    "action_type=submit targeting the verify button. If the "
                    "captcha appears gone, pick action_type=done. Do NOT "
                    "target within 40 pixels of the previous click unless a "
                    "visibly new tile has rendered there."
                ),
                cursor_trail=cursor_trail if cursor_trail else None,
            )
        except Exception as exc:
            actions.append(f"step {step}: vision call failed: {exc}")
            return {
                "solved": False, "method": "vision_iterative",
                "subMethod": "python_vision_agent",
                "steps": steps_taken, "actions": actions,
                "provider": provider_name, "model": model_name,
                "error": f"vision_call:{exc}",
            }

        model_name = model_name or resp.model
        provider_name = provider_name or resp.provider

        # 3. Done if the captcha is gone (only trust this after step 0 —
        # the very first screenshot should see the captcha).
        if step > 0 and not resp.flags.captcha_present:
            actions.append(f"step {step}: captcha_present=false, exiting loop")
            break

        # 4. Prefer the structured next_action from step-mode. Fall back
        # to the bbox-preference picker for older vision responses that
        # don't fill next_action (e.g. providers that ignore the field,
        # or non-step intents calling the shim).
        na = getattr(resp, "next_action", None)
        forced_drag = False  # na says drag_slider — override role dispatch
        forced_type = False  # na says type_text — use target_input_bbox
        type_value = ""
        last_expect_change = "static"
        if na is not None:
            at = (getattr(na, "action_type", "") or "").lower()
            if at == "done":
                actions.append(f"step {step}: next_action=done, exiting loop")
                break
            if at == "stuck":
                actions.append(
                    f"step {step}: next_action=stuck "
                    f"(reason: {getattr(na, 'reasoning', '')[:120]})"
                )
                return {
                    "solved": False, "method": "vision_iterative",
                    "subMethod": "python_vision_agent",
                    "steps": steps_taken, "actions": actions,
                    "provider": provider_name, "model": model_name,
                    "error": "vision_stuck",
                }
            forced_drag = at == "drag_slider"
            forced_type = at == "type_text"
            last_expect_change = getattr(na, "expect_change", "static") or "static"
            if forced_type:
                # type_text targets the input field, not the image.
                target = (
                    getattr(na, "target_input_bbox", None)
                    or getattr(na, "target_bbox", None)
                )
                type_value = (getattr(na, "type_value", "") or "").strip()
                if not type_value:
                    actions.append(
                        f"step {step}: type_text without type_value — treating as stuck"
                    )
                    return {
                        "solved": False, "method": "vision_iterative",
                        "subMethod": "python_vision_agent",
                        "steps": steps_taken, "actions": actions,
                        "provider": provider_name, "model": model_name,
                        "error": "type_text_missing_value",
                    }
            else:
                target = getattr(na, "target_bbox", None)
        else:
            target = None

        if target is None:
            target = _first_actionable(resp)
        if target is None:
            if step == 0 and resp.flags.captcha_widget_bbox is not None:
                target = resp.flags.captcha_widget_bbox
            else:
                actions.append(f"step {step}: no actionable bbox returned")
                return {
                    "solved": False, "method": "vision_iterative",
                    "subMethod": "python_vision_agent",
                    "steps": steps_taken, "actions": actions,
                    "provider": provider_name, "model": model_name,
                    "error": "no_actionable_bbox",
                }

        try:
            cx, cy = target.center_pixels(img_w, img_h)
            x0, y0, x1, y1 = target.to_pixels(img_w, img_h)
        except Exception as exc:
            actions.append(f"step {step}: malformed bbox: {exc}")
            return {
                "solved": False, "method": "vision_iterative",
                "subMethod": "python_vision_agent",
                "steps": steps_taken, "actions": actions,
                "provider": provider_name, "model": model_name,
                "error": "malformed_bbox",
            }

        # 5. Dead-action guard. Two successive near-duplicates within 40px
        # and no site change → bail to the caller (who decides handoff).
        if last_click_xy and abs(cx - last_click_xy[0]) < 40 and abs(cy - last_click_xy[1]) < 40:
            same_action_streak += 1
            if same_action_streak >= 2:
                actions.append(
                    f"step {step}: third same-target attempt within 40px — bailing"
                )
                return {
                    "solved": False, "method": "vision_iterative",
                    "subMethod": "python_vision_agent",
                    "steps": steps_taken, "actions": actions,
                    "provider": provider_name, "model": model_name,
                    "error": "dead_action_streak",
                }
        else:
            same_action_streak = 0

        # 6. Dispatch. Explicit `drag_slider` / `type_text` from next_action
        # win; otherwise fall back to bbox role.
        role = (getattr(target, "role", "") or "").lower()
        if forced_type:
            try:
                tr = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/type-at",
                    json={
                        "x": cx, "y": cy,
                        "text": type_value,
                        "clear": True,
                    },
                    timeout=30.0,
                )
                if tr.status_code >= 400:
                    actions.append(
                        f"step {step}: type-at HTTP {tr.status_code}, ending loop"
                    )
                    break
                actions.append(
                    f"step {step}: type_text {type_value!r} @({cx},{cy})"
                )
            except Exception as exc:
                actions.append(f"step {step}: type-at dispatch exception: {exc}")
                break
        elif forced_drag or role == "slider_handle":
            if captcha_info is not None and getattr(captcha_info, "widget_bbox", None):
                wbb = captcha_info.widget_bbox
                ex = max(float(wbb[2]) - 12, cx + 50)
            elif resp.flags.captcha_widget_bbox is not None:
                _x0, _y0, _x1, _y1 = resp.flags.captcha_widget_bbox.to_pixels(
                    img_w, img_h,
                )
                ex = max(_x1 - 12, cx + 50)
            else:
                ex = cx + 250
            try:
                dr = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/drag",
                    json={
                        "startX": cx, "startY": cy,
                        "endX": ex, "endY": cy,
                        "steps": 30,
                    },
                    timeout=30.0,
                )
                if dr.status_code >= 400:
                    actions.append(
                        f"step {step}: drag HTTP {dr.status_code}, ending loop"
                    )
                    break
                actions.append(
                    f"step {step}: drag handle {getattr(target, 'label', '')!r} "
                    f"({cx},{cy})→({int(ex)},{cy})"
                )
            except Exception as exc:
                actions.append(f"step {step}: drag dispatch exception: {exc}")
                break
        else:
            try:
                cr = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/click",
                    json={
                        "x": cx, "y": cy,
                        "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
                    },
                    timeout=30.0,
                )
                if cr.status_code == 409:
                    actions.append(
                        f"step {step}: click@({cx},{cy}) refused "
                        "(low-reward band) — re-analyzing"
                    )
                    # Do NOT advance last_click_xy/streak; re-ask vision.
                    continue
                if cr.status_code >= 400:
                    actions.append(
                        f"step {step}: click HTTP {cr.status_code}, ending loop"
                    )
                    break
                actions.append(
                    f"step {step}: click {role or 'bbox'} "
                    f"{getattr(target, 'label', '')!r} @({cx},{cy})"
                )
            except Exception as exc:
                actions.append(f"step {step}: click dispatch exception: {exc}")
                break

        last_click_xy = (cx, cy)
        cursor_trail.append((cx, cy))
        if len(cursor_trail) > 6:
            cursor_trail.pop(0)

        # 7. Poll the structural hash until it changes, capped adaptively
        # based on vision's expect_change hint. Hash-poll is faster than
        # a blind sleep on static grids and safer on slow re-rendering
        # ones. page_nav gets a much longer ceiling because whole-page
        # transitions take ~1.5s.
        poll_cap = {
            "page_nav": 2.5,
            "widget_replace": 1.0,
            "new_tile": 0.6,
            "static": 0.3,
        }.get(last_expect_change, 0.6)
        change_deadline = time.monotonic() + poll_cap
        while time.monotonic() < change_deadline:
            await asyncio.sleep(0.08)
            try:
                pr = await _request_with_backoff(
                    "GET",
                    f"{SUPERBROWSER_URL}/session/{session_id}/state",
                    params={"vision": "false"},
                    timeout=5.0,
                )
                if pr.status_code != 200:
                    continue
                ps = pr.json()
                _elems = (
                    ps.get("clickableElementsToString")
                    or ps.get("elements") or ""
                )
                if not _elems:
                    continue
                new_hash = BrowserSessionState.hash_page_content(_elems)
                if new_hash and new_hash != page_hash:
                    break
            except Exception:
                pass

    # 8. Verify once.
    solved = False
    try:
        vr = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            params={"vision": "true"},
            timeout=15.0,
        )
        if vr.status_code == 200:
            verify_state = vr.json()
            verify_b64 = verify_state.get("screenshot")
            if verify_b64:
                vresp = await vision_agent.analyze(
                    screenshot_b64=verify_b64,
                    intent="verify captcha cleared",
                    session_id=session_id,
                    url=verify_state.get("url", ""),
                    dom_hash=f"cap-verify-r{solve_round}",
                )
                solved = not vresp.flags.captcha_present
    except Exception:
        solved = False

    return {
        "solved": solved,
        "method": "vision_iterative",
        "subMethod": "python_vision_agent",
        "steps": steps_taken,
        "actions": actions,
        "provider": provider_name,
        "model": model_name,
    }


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        method=StringSchema("Solving method: 'auto', 'token', 'ai_vision', 'grid'", nullable=True),
        provider=StringSchema("Captcha solver: '2captcha' or 'anticaptcha'", nullable=True),
        api_key=StringSchema("API key for solver service", nullable=True),
        required=["session_id"],
    )
)
class BrowserSolveCaptchaTool(Tool):
    name = "browser_solve_captcha"
    description = "Solve a detected captcha automatically."

    def __init__(self, state: BrowserSessionState):
        self.s = state
        # Per-session lock so concurrent solve calls on the same session
        # serialize around captcha_solve_round / captcha_screenshots_used.
        # The agent fires single-threaded, but retry loops or stray parallel
        # tool calls can race otherwise.
        self._session_locks: dict[str, asyncio.Lock] = {}

    async def execute(self, session_id: str, method: str | None = None, provider: str | None = None, api_key: str | None = None, **kw: Any) -> str:
        session_id = self.s.resolve_session_id(session_id)
        # Any solve attempt unblocks the CF nav-guard — whether or not
        # the solve succeeds, the agent has acknowledged the interstitial,
        # and re-navigating is no longer a blind loop.
        self.s.nav_solve_called_since_block = True

        # Vision-based solve path. Replaces the three deleted TS vision
        # strategies (recaptcha-ai-grid, slider-drag, generic-vision) with
        # a single call to our dedicated Python vision agent. When the
        # brain (or fast-to-human policy) picks method='vision' we run the
        # full solve loop here and return a structured result without ever
        # hitting the server's captcha/solve endpoint.
        if method == "vision":
            return await self._solve_via_vision(session_id)

        # Auto-route captcha types that the TS server registry has no
        # strategy for (text_captcha, generic) straight to the Python
        # vision loop. Without this, method='auto' on T1 would fall all
        # the way through to human_handoff for a classic distorted-word
        # captcha that vision can solve directly.
        #
        # cf_interstitial gets its own solver (wait-based, not vision):
        # the whole page IS the challenge, no clicking needed — CF's JS
        # just needs time + humanization to auto-pass.
        if method in (None, "auto"):
            try:
                dr = await _request_with_backoff(
                    "GET",
                    f"{SUPERBROWSER_URL}/session/{session_id}/captcha/detect",
                    timeout=10.0,
                )
                if dr.status_code == 200:
                    det_cap = (dr.json() or {}).get("captcha") or {}
                    det_type = (det_cap.get("type") or "").lower()
                    det_present = bool(det_cap.get("present"))
                    det_notes = det_cap.get("notes") or []
                    print(
                        f"  [auto-route] detect -> type={det_type!r} "
                        f"present={det_present} notes={det_notes}"
                    )
                    if det_type == "cf_interstitial":
                        return await self._solve_via_cf_wait(session_id)
                    if det_type in ("text_captcha", "text", "generic", "image"):
                        return await self._solve_via_vision(session_id)
                    if det_type in ("recaptcha-v2", "hcaptcha"):
                        # Cheap first attempt: let the vision loop click the
                        # widget checkbox. On a trusted-fingerprint session
                        # (patchright + good proxy) this alone often passes.
                        # Small step budget so we bail fast if the image-tile
                        # challenge appears — the token vendor / human handoff
                        # below handle that class better.
                        print(
                            f"  [vision checkbox] trying widget click "
                            f"(max_steps=3) for {det_type!r} before token vendor"
                        )
                        vr = await self._try_vision_solve_for_widget(
                            session_id, det_cap, max_steps=3,
                        )
                        if vr and vr.get("solved"):
                            self.s.captcha_mode = False
                            self.s.captcha_mode_remaining = 0
                            self.s.record_step(
                                "browser_solve_captcha",
                                "vision_checkbox",
                                f"solved=True via vision checkbox-click | "
                                f"{json.dumps(vr, default=str)[:300]}",
                            )
                            return (
                                f"Captcha SOLVED via vision checkbox-click "
                                f"({vr.get('steps', 0)} step(s))"
                                f"\n\nResult JSON:\n"
                                f"{json.dumps(vr, indent=2, default=str)}"
                            )
                        if vr is not None:
                            self.s.record_step(
                                "browser_solve_captcha",
                                "vision_checkbox",
                                f"solved=False via vision checkbox-click, "
                                f"falling through | "
                                f"{json.dumps(vr, default=str)[:300]}",
                            )
                else:
                    print(
                        f"  [auto-route] detect HTTP {dr.status_code} — "
                        f"falling through to TS solver"
                    )
            except Exception as exc:
                # Detection failure is non-fatal — fall through to the
                # standard TS solver path which runs its own detection.
                print(
                    f"  [auto-route] detect raised "
                    f"{type(exc).__name__}: {exc} — falling through"
                )

        payload: dict[str, Any] = {}
        if method:
            payload["method"] = method
        if provider:
            payload["provider"] = provider
        if api_key:
            payload["apiKey"] = api_key
        # Advance the solve round BEFORE the call so the next screenshot
        # (which the LLM will take to inspect the result) gets a fresh dedup
        # allowance distinct from the pre-solve shots.
        self.s.captcha_solve_round += 1
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/captcha/solve",
            json=payload,
            timeout=180.0,
        )
        r.raise_for_status()
        data = r.json()

        # Build a structured result the orchestrator can parse — keeps the
        # method + subMethod + vendor + trace so per-domain captcha learnings
        # can be written automatically. The LLM sees JSON; a human-readable
        # summary is injected at the top for quick scanning.
        summary: str
        if data.get("solved"):
            summary = (
                f"Captcha SOLVED via {data.get('method', '?')}"
                f"/{data.get('subMethod', '?')} in {data.get('durationMs', 0)}ms "
                f"({data.get('attempts', 1)} attempt(s))"
            )
        else:
            summary = f"Captcha NOT solved: {data.get('error', 'all methods failed')}"

        # Record the structured method info so orchestrator can learn from it.
        structured = {
            "solved": bool(data.get("solved")),
            "captchaType": data.get("captchaType") or data.get("captcha", {}).get("type"),
            "vendorDetected": data.get("vendorDetected"),
            "method": data.get("method"),
            "subMethod": data.get("subMethod"),
            "attempts": data.get("attempts"),
            "totalRounds": data.get("totalRounds"),
            "durationMs": data.get("durationMs"),
            "siteKey": data.get("siteKey"),
            "iframeUrl": data.get("iframeUrl"),
            "error": data.get("error"),
        }
        # Drop None values so the JSON stays compact.
        structured = {k: v for k, v in structured.items() if v is not None}

        self.s.record_step(
            "browser_solve_captcha",
            method or "auto",
            f"{summary} | {json.dumps(structured, default=str)[:300]}",
        )

        # The solve freed us from captcha — end captcha_mode so the normal
        # budget rules resume.
        if data.get("solved"):
            self.s.captcha_mode = False
            self.s.captcha_mode_remaining = 0
            return f"{summary}\n\nResult JSON:\n{json.dumps(structured, indent=2, default=str)}"

        # --- Auto-escalation to human handoff (deterministic) -------------
        # When auto-solve fails AND human handoff is enabled, immediately
        # POST to the human-input endpoint instead of returning to the LLM
        # and hoping it calls browser_ask_user. This removes the LLM
        # decision loop from the critical captcha→human path.
        if self.s.human_handoff_enabled and self.s.human_handoff_budget > 0:
            print(f"   [auto-escalation] captcha auto-solve failed, requesting human handoff")
            self.s.human_handoff_budget -= 1
            try:
                handoff_timeout = int(os.environ.get("SUPERBROWSER_HANDOFF_TIMEOUT_MS", "180000")) / 1000
                async with httpx.AsyncClient(timeout=handoff_timeout + 10) as hclient:
                    hr = await hclient.post(
                        f"{SUPERBROWSER_URL}/session/{session_id}/human-input/ask",
                        json={
                            "type": "captcha",
                            "message": (
                                "Auto-solve failed for captcha. Please open the live view URL "
                                "and click through the challenge — the agent will detect when "
                                "it clears and resume automatically."
                            ),
                        },
                    )
                    if hr.status_code == 200:
                        hdata = hr.json()
                        if hdata.get("cancelled") or hdata.get("timeout"):
                            human_result = "Human handoff timed out or was cancelled."
                        else:
                            human_result = "Human solved the captcha. Resuming task."
                            self.s.captcha_mode = False
                            self.s.captcha_mode_remaining = 0
                    else:
                        human_result = f"Human handoff request failed (HTTP {hr.status_code})."
            except Exception as exc:
                human_result = f"Human handoff request error: {exc}"

            self.s.record_step(
                "browser_solve_captcha",
                "auto_escalation",
                human_result,
            )
            return (
                f"{summary}\n\n"
                f"[AUTO-ESCALATION] {human_result}\n\n"
                f"Result JSON:\n{json.dumps(structured, indent=2, default=str)}"
            )

        return f"{summary}\n\nResult JSON:\n{json.dumps(structured, indent=2, default=str)}"

    async def _solve_via_cf_wait(self, session_id: str) -> str:
        """Solve a Cloudflare Managed Challenge by waiting for auto-pass.

        Routes to `/captcha/solve` with method='cf_wait' — the T3 HTTP
        dispatcher picks up `cf_interstitial` detect type and calls the
        dedicated humanized wait loop (`antibot.captcha.solve_cf`). On
        T1 the TS server's own CF handling runs.
        """
        print(f"  [cf_wait] entering solver for session={session_id}")
        self.s.captcha_solve_round += 1
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            try:
                r = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/captcha/solve",
                    json={"method": "cf_wait"},
                    timeout=120.0,
                )
                if r.status_code >= 400:
                    body_snippet = ""
                    try:
                        body_snippet = json.dumps(r.json(), default=str)[:400]
                    except Exception:
                        body_snippet = (getattr(r, "text", "") or "")[:400]
                    print(
                        f"  [cf_wait] dispatcher returned HTTP {r.status_code}: "
                        f"{body_snippet}"
                    )
                    data: dict[str, Any] = {
                        "solved": False, "method": "cf_wait",
                        "error": f"HTTP {r.status_code}",
                        "dispatcher_body": body_snippet,
                    }
                else:
                    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    if not isinstance(data, dict):
                        data = {"solved": False, "method": "cf_wait",
                                "error": "non-dict response"}
                    print(
                        f"  [cf_wait] result: solved={data.get('solved')} "
                        f"durationMs={data.get('durationMs')} "
                        f"iterations={data.get('iterations')} "
                        f"cookies={data.get('cookies_landed')}"
                    )
            except Exception as exc:
                import traceback as _tb
                print(
                    f"  [cf_wait] dispatch raised {type(exc).__name__}: {exc}"
                )
                print(_tb.format_exc())
                data = {
                    "solved": False, "method": "cf_wait",
                    "error": f"dispatch failed: {exc}",
                }

        self.s.record_step(
            "browser_solve_captcha",
            "cf_wait",
            f"solved={data.get('solved')} | "
            f"{json.dumps(data, default=str)[:300]}",
        )

        if data.get("solved"):
            self.s.captcha_mode = False
            self.s.captcha_mode_remaining = 0
            # Clear any nav-guard set by a prior failed navigate.
            self.s.last_nav_cf_blocked_url = ""
            return (
                f"Captcha SOLVED via cf_wait (CF interstitial auto-passed "
                f"in {data.get('durationMs', 0)}ms)"
                f"\n\nResult JSON:\n{json.dumps(data, indent=2, default=str)}"
            )
        return (
            f"Captcha NOT solved: CF interstitial did not auto-clear. "
            f"Consider residential proxy (PROXY_POOL_RESIDENTIAL) or "
            f"headful mode (SUPERBROWSER_ALLOW_HEADFUL=1 + xvfb), or "
            f"call browser_ask_user for human handoff."
            f"\n\nResult JSON:\n{json.dumps(data, indent=2, default=str)}"
        )

    async def _try_slider_dispatch(self, session_id: str) -> dict | None:
        """POST to /captcha/solve with method='slider' and return the parsed
        result, or None if the call itself errored.

        Used by `_solve_via_vision` to give sliders first chance at the
        dedicated bezier-drag solver before falling back to vision-driven
        drag. Keeps the solver boundary clean — we just forward and let
        the tier's native slider code (antibot.captcha.solve_slider on
        T3, src/browser/captcha equivalent on T1) do its thing.
        """
        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/captcha/solve",
                json={"method": "slider"},
                timeout=60.0,
            )
            if r.status_code >= 400:
                return None
            data = r.json()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return None

    async def _try_vision_solve_for_widget(
        self,
        session_id: str,
        det_cap: dict,
        *,
        max_steps: int,
    ) -> dict[str, Any] | None:
        """Attempt a vision+cursor solve for widget captchas (recaptcha-v2,
        hcaptcha) using the already-detected widget bbox. Returns the
        structured result dict from `_solve_captcha_iterative`, or None
        when vision is unavailable — the caller reads None as "skip vision,
        go straight to token vendor." Never raises.
        """
        try:
            from vision_agent import get_vision_agent, vision_agent_enabled
        except ImportError:
            return None
        if not vision_agent_enabled():
            return None

        from superbrowser_bridge.antibot.captcha.detect import CaptchaInfo
        captcha_info = CaptchaInfo(
            type=det_cap.get("type", "none"),
            present=bool(det_cap.get("present", True)),
            site_key=det_cap.get("site_key", "") or "",
            widget_selector=det_cap.get("widget_selector", "") or "",
            widget_bbox=det_cap.get("widget_bbox"),
            input_bbox=det_cap.get("input_bbox"),
            frame_url=det_cap.get("frame_url", "") or "",
            notes=list(det_cap.get("notes") or []),
        )

        self.s.captcha_solve_round += 1
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            agent = get_vision_agent()
            return await _solve_captcha_iterative(
                session_id,
                captcha_info,
                agent,
                task_instruction=self.s.task_instruction,
                solve_round=self.s.captcha_solve_round,
                max_steps=max_steps,
            )

    async def _solve_via_vision(self, session_id: str) -> str:
        """Python-side captcha solver — delegates to the iterative loop.

        Per-step flow lives in `_solve_captcha_iterative` so the T3 HTTP
        dispatch path (antibot/captcha/solve_vision.py shim) and this
        Python-tool path share one implementation. This method owns the
        session-state plumbing around the loop: enable check, per-session
        serialization, captcha_mode reset, and structured-result logging.
        """
        try:
            from vision_agent import get_vision_agent, vision_agent_enabled
        except ImportError:
            return (
                "Captcha NOT solved: vision_agent package not importable. "
                "Install / configure the vision agent or use method='auto'."
            )
        if not vision_agent_enabled():
            return (
                "Captcha NOT solved: method='vision' requires VISION_ENABLED=1. "
                "Set the env flag and a VISION_API_KEY, or use method='auto'."
            )

        self.s.captcha_solve_round += 1
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())

        async with lock:
            # Best-effort detect for dispatcher context (slider widgets need
            # the server-side pixel bbox to clamp drag endpoints). Failure
            # is non-fatal — the loop proceeds with what vision sees.
            captcha_info: Any = None
            try:
                dr = await _request_with_backoff(
                    "GET",
                    f"{SUPERBROWSER_URL}/session/{session_id}/captcha/detect",
                    timeout=10.0,
                )
                if dr.status_code == 200:
                    cap = (dr.json() or {}).get("captcha") or {}
                    from superbrowser_bridge.antibot.captcha.detect import CaptchaInfo
                    captcha_info = CaptchaInfo(
                        type=cap.get("type", "none"),
                        present=bool(cap.get("present", True)),
                        site_key=cap.get("site_key", "") or "",
                        widget_selector=cap.get("widget_selector", "") or "",
                        widget_bbox=cap.get("widget_bbox"),
                        input_bbox=cap.get("input_bbox"),
                        frame_url=cap.get("frame_url", "") or "",
                        notes=list(cap.get("notes") or []),
                    )
            except Exception:
                captcha_info = None

            # Slider pre-dispatch: the dedicated bezier-drag solver
            # (antibot.captcha.solve_slider on T3 / TS equivalent on T1)
            # has a motion profile tuned to defeat Geetest/Tencent/DataDome
            # fingerprinting. Vision-driven drag via our /drag endpoint
            # works, but the bezier solver is materially better at this
            # specific class. Route sliders to it first; fall through to
            # the iterative loop only if the dedicated solver returns
            # unsolved.
            if captcha_info is not None and captcha_info.type == "slider":
                slider_result = await self._try_slider_dispatch(session_id)
                if slider_result is not None and slider_result.get("solved"):
                    self.s.record_step(
                        "browser_solve_captcha",
                        "vision->slider",
                        f"solved=True via dedicated slider solver | "
                        f"{json.dumps(slider_result, default=str)[:280]}",
                    )
                    self.s.captcha_mode = False
                    self.s.captcha_mode_remaining = 0
                    return (
                        f"Captcha SOLVED via dedicated slider bezier-drag"
                        f"\n\nResult JSON:\n{json.dumps(slider_result, indent=2, default=str)}"
                    )
                # Dedicated solver failed — fall through to the iterative
                # loop so vision can try. Error paths end up reported by
                # the loop result.

            agent = get_vision_agent()
            result = await _solve_captcha_iterative(
                session_id,
                captcha_info,
                agent,
                task_instruction=self.s.task_instruction,
                solve_round=self.s.captcha_solve_round,
            )

        self.s.record_step(
            "browser_solve_captcha",
            "vision",
            f"solved={result.get('solved')} | "
            f"{json.dumps(result, default=str)[:300]}",
        )

        if result.get("solved"):
            self.s.captcha_mode = False
            self.s.captcha_mode_remaining = 0
            return (
                f"Captcha SOLVED via vision_iterative "
                f"({result.get('steps', 0)} step(s))"
                f"\n\nResult JSON:\n{json.dumps(result, indent=2, default=str)}"
            )
        return (
            f"Captcha NOT solved after {result.get('steps', 0)} step(s). "
            f"Per fast-to-human policy, call browser_ask_user next."
            f"\n\nResult JSON:\n{json.dumps(result, indent=2, default=str)}"
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        question=StringSchema("What to ask the user"),
        input_type=StringSchema("Type: credentials, captcha, confirmation, otp, text, choice", nullable=True),
        required=["session_id", "question"],
    )
)
class BrowserAskUserTool(Tool):
    name = "browser_ask_user"
    description = (
        "Ask the user a question and BLOCK until they respond "
        "(up to 5 minutes). Use for credentials, OTP, confirmation, or "
        "when you need a human decision. The user replies via the remote "
        "view UI at /session/<id>/view or any HTTP client. Returns the "
        "user's reply as a string; on timeout returns a sentinel message "
        "you can react to."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        question: str,
        input_type: str | None = None,
        **kw: Any,
    ) -> Any:
        # Tier-3 path: spin up the Python live viewer and return its URL
        # as a hint to the LLM. The actual blocking "wait for user" is
        # simpler on t3 — we poll for a state change on the captcha
        # widget and resume when it clears. For now, return the URL and a
        # short wait loop so the user has ~3 min to interact.
        if self.s.backend == "t3":
            try:
                from superbrowser_bridge.antibot import t3_viewer as _v
                from superbrowser_bridge.antibot import captcha as _cap
                from superbrowser_bridge.antibot import interactive_session as _t3mgr

                await _v.ensure_started()
                view = _v.view_url(session_id)
                print(f"\n[HUMAN HANDOFF — t3] Open {view} in your browser.")
                # Poll every 3s for up to 5 min for the captcha to clear.
                mgr = _t3mgr.default()
                import asyncio as _asyncio
                import time as _time
                deadline = _time.time() + 5 * 60
                cleared = False
                while _time.time() < deadline:
                    await _asyncio.sleep(3.0)
                    try:
                        info = await _cap.detect(mgr, session_id)
                    except Exception:
                        continue
                    if not info.present:
                        cleared = True
                        break
                if cleared:
                    return (
                        f"[human_handoff_cleared] Captcha / verification "
                        f"cleared via human at {view}. Resuming."
                    )
                return (
                    f"[human_handoff_timeout] No state change detected after "
                    f"5 min at {view}. You can call browser_ask_user again or "
                    f"proceed with done(success=False)."
                )
            except Exception as exc:
                print(f"[t3 human handoff error: {exc}]")
                return f"[browser_ask_user_t3_error: {exc}. Cannot proceed.]"

        # Map nanobot-side hint to the TS server's HumanInputType. Default
        # 'text' is the safest — it accepts free-form replies and the UI's
        # "Done" button also works against it.
        valid_types = {
            "credentials", "captcha", "confirmation", "otp", "card", "text", "choice",
        }
        ht = (input_type or "text").lower()
        if ht not in valid_types:
            ht = "text"

        # Capture a screenshot to include in the request payload so any UI
        # listener (not just the live-view poller) can show what page the
        # agent is stuck on. Best-effort.
        screenshot_b64 = None
        try:
            sr = await _request_with_backoff(
                "GET",
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true"},
                timeout=10.0,
            )
            sr.raise_for_status()
            sdata = sr.json()
            screenshot_b64 = sdata.get("screenshot") or None
        except Exception:
            screenshot_b64 = None

        # View URL for the user — the concrete surface where they interact.
        public_host = os.environ.get(
            "SUPERBROWSER_PUBLIC_HOST", SUPERBROWSER_URL.rstrip("/"),
        )
        view_url = f"{public_host}/session/{session_id}/view"
        message = (
            f"{question}\n\n"
            f"To respond: open {view_url} in your browser. "
            f"Either interact with the page (for captchas) or click the "
            f"'Done' button when finished."
        )

        # Five-minute timeout matches HumanInputManager's default; the TS
        # server holds the HTTP connection open until the user replies or
        # the timer fires, so client-side we just wait.
        timeout_ms = 5 * 60 * 1000
        self.s.record_step(
            "browser_ask_user",
            f"type={ht}",
            f"view_url={view_url}",
        )
        try:
            async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 10) as client:
                r = await client.post(
                    f"{SUPERBROWSER_URL}/session/{session_id}/human-input/ask",
                    json={
                        "type": ht,
                        "message": message,
                        "screenshot": screenshot_b64,
                        "timeout": timeout_ms,
                    },
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            return (
                f"[browser_ask_user error: {exc}. "
                f"User was not asked. Continue without their input "
                f"or call again.]"
            )

        if data.get("timedOut"):
            return (
                f"[User did not respond within {timeout_ms // 60000} minutes. "
                f"Proceed without their input or call done(success=False).]"
            )

        response = data.get("response") or {}
        if response.get("cancelled"):
            return "[User cancelled the request. Proceed accordingly.]"

        payload = response.get("data") or {}
        if not payload:
            return "[User responded but provided no data.]"

        # Flatten the reply dict into a short readable string for the model.
        parts = [f"{k}: {v}" for k, v in payload.items()]
        return f"[User replied] {' | '.join(parts)}"


# ── Resumption-handoff helpers ───────────────────────────────────────────
# When a worker exits (stuck, captcha-blocked, or after browser_request_help),
# we save enough tactical state that the NEXT worker can resume on the same
# live Puppeteer session with knowledge of what already failed — instead of
# spawning a fresh session from the home page.
#
# File: /tmp/superbrowser/resumption.json
# Expiry: 5 minutes (RESUMPTION_TTL_SEC). Past that, the Puppeteer session
# has likely been GC'd server-side so liveness is doubtful regardless.

RESUMPTION_PATH = "/tmp/superbrowser/resumption.json"
RESUMPTION_TTL_SEC = 300


def _extract_recent_failures(step_history: list[dict], limit: int = 5) -> list[dict]:
    """Pull the most recent tool steps that look like failures.

    With Priority 1 in place, click/type results include phrases like
    '(element_covered):' or '(stale_selector):' when the structured reason
    is set. We match on those plus generic error markers.
    """
    out: list[dict] = []
    markers = ("FAILED", "failed (", "error:", "Script error", "ERROR:", "NOT solved")
    for step in reversed(step_history):
        result = str(step.get("result") or "")
        if any(m in result for m in markers):
            out.append({
                "tool": step.get("tool", ""),
                "args": str(step.get("args", ""))[:160],
                "result_excerpt": result[:220],
                "url": step.get("url", ""),
                "time": step.get("time", ""),
            })
        if len(out) >= limit:
            break
    return list(reversed(out))


def save_resumption_artifact(
    state: "BrowserSessionState",
    domain: str,
    help_reason: str = "",
    help_failed_tactics: str = "",
) -> bool:
    """Write a resumption hint so the next delegation can pick up where we left off.

    Returns True if the artifact was written. Never raises.
    """
    try:
        if not state.session_id or not state.current_url:
            return False
        payload = {
            "session_id": state.session_id,
            "current_url": state.current_url,
            "best_checkpoint_url": state.best_checkpoint_url,
            "domain": domain,
            "task_id": state.task_id,
            "recent_failures": _extract_recent_failures(state.step_history),
            "help_reason": help_reason or "",
            "help_failed_tactics": help_failed_tactics or "",
            "written_at": time.time(),
        }
        os.makedirs(os.path.dirname(RESUMPTION_PATH), exist_ok=True)
        with open(RESUMPTION_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"  [resumption artifact saved: session={state.session_id} url={state.current_url}]")
        return True
    except OSError as exc:
        print(f"  [resumption save failed: {exc}]")
        return False


async def load_resumption_artifact(domain: str) -> dict | None:
    """Read and validate a resumption artifact for the given domain.

    Returns None if the artifact is missing, expired, from a different
    domain, or the referenced Puppeteer session is no longer alive
    on the TS server.
    """
    if not os.path.exists(RESUMPTION_PATH):
        return None
    try:
        with open(RESUMPTION_PATH) as f:
            payload = json.load(f)
    except (ValueError, OSError):
        return None

    age = time.time() - float(payload.get("written_at", 0) or 0)
    if age > RESUMPTION_TTL_SEC:
        try:
            os.remove(RESUMPTION_PATH)
        except OSError:
            pass
        return None
    if payload.get("domain") != domain:
        return None

    sid = payload.get("session_id")
    if not sid:
        return None

    # Cheap liveness probe — hit whichever backend owns this session.
    try:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{sid}/state",
            params={"vision": "false"},
            timeout=5.0,
        )
        if r.status_code != 200:
            try:
                os.remove(RESUMPTION_PATH)
            except OSError:
                pass
            return None
    except Exception:
        return None

    return payload


def clear_resumption_artifact() -> None:
    """Remove the resumption artifact (call when a new session successfully supersedes it)."""
    if os.path.exists(RESUMPTION_PATH):
        try:
            os.remove(RESUMPTION_PATH)
        except OSError:
            pass


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        claim=StringSchema(
            "The exact factual claim you're about to report. Include the value, unit, "
            "and what it refers to. E.g. 'Total price for 2 nights at Agoda Grand Sylhet "
            "May 3-5 is BDT 14,500' or '5-star hotel count in Sylhet on GoZayaan is 3'."
        ),
        required=["session_id", "claim"],
    )
)
class BrowserVerifyFactTool(Tool):
    """Visual sanity check before reporting an extracted value.

    Takes a fresh screenshot and frames a narrow verification question for
    the next model turn. The LLM must look at the actual page and say whether
    it supports the claim. Catches the common failure mode where an extraction
    script returned null/wrong-element and the model filled in a plausible
    number downstream.

    Intentionally bypasses normal dedup — verification screenshots are a
    deliberate, infrequent request and must see the current state.
    """

    name = "browser_verify_fact"
    description = (
        "Visually verify a factual claim against the current page before reporting it. "
        "Call this with the EXACT value you're about to return. Then look at the "
        "returned screenshot and answer honestly: does the page actually show this? "
        "If not, do NOT report the original value — go back and fix your extraction."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, claim: str, **kw: Any) -> Any:
        session_id = self.s.resolve_session_id(session_id)
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            params={"vision": "true"},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()

        self.s.record_step("browser_verify_fact", claim[:80], "screenshot taken for verification")

        caption = (
            f"[VERIFY CLAIM]\n"
            f"Claim under review: {claim}\n\n"
            f"Look at the screenshot below carefully. In your NEXT reply, respond ONLY "
            f"with a JSON object of the form:\n"
            f'  {{"supported": <bool>, "observed_value": "<what you actually see on the page, '
            f"verbatim, or null if absent>\", "
            f'"reason": "<one sentence explaining what you saw>"}}\n\n'
            f"Rules:\n"
            f"- supported=true only if the claim matches what's visible on the page exactly "
            f"(values, units, context). A crossed-out price is NOT the current price.\n"
            f"- If the page shows a DIFFERENT value than the claim, set supported=false "
            f"and put the real value in observed_value.\n"
            f"- If the page doesn't show enough to tell, set supported=false with a "
            f"'cannot verify' reason — do NOT rubber-stamp.\n"
            f"- After this verify, if supported=false, FIX your extraction and retry. "
            f"If supported=true, report the claim as your final answer."
        )

        if data.get("screenshot"):
            # Don't let verify-fact screenshots eat the captcha cap (they're
            # not for captcha) nor trigger normal dedup (verification must
            # see the live page state). Route through the same async
            # vision-preprocessor hook every other screenshot tool uses, so
            # the brain never sees the raw image when VISION_ENABLED=1.
            self.s.vision_calls += 1
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent="verify fact against page",
                url=data.get("url", self.s.current_url),
                elements=data.get("elements"),
            )
        # No screenshot available — still return the caption so the caller
        # can at least reason about the textual state.
        return caption + "\n\n[No screenshot available — verify against browser_get_markdown output.]"


@tool_parameters(
    tool_parameters_schema(
        reason=StringSchema(
            "Why you're stuck. Be specific: 'element_covered by cookie banner I can't dismiss', "
            "'captcha solve failed 3 times', 'selector index keeps shifting'."
        ),
        failed_tactics=StringSchema(
            "Comma-separated list of tactics you already tried. E.g., "
            "'click [5] twice, scroll-and-retry, switch to XPath selector'."
        ),
        required=["reason", "failed_tactics"],
    )
)
class BrowserRequestHelpTool(Tool):
    """Escape hatch: worker signals 'I'm stuck' with structured context.

    Writes a resumption artifact so the orchestrator can spin up a new
    worker that RESUMES on the same live Puppeteer session with a
    different tactic — instead of starting from scratch.

    The worker should call `done(success=False, final_answer=...)` on the
    next turn after calling this tool.
    """

    name = "browser_request_help"
    description = (
        "Call this when you're stuck and a fresh tactic is needed. "
        "Writes structured state so the orchestrator can delegate a "
        "SUCCESSOR worker that resumes on the SAME live browser session "
        "with knowledge of what failed. "
        "After calling this tool, call done(success=False) with a short "
        "explanation — do NOT keep trying the same tactics."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, reason: str, failed_tactics: str, **kw: Any) -> str:
        # Refusal gate: if a TaskPlan step is still in_progress and the
        # brain hasn't taken a screenshot since the last failed action,
        # refuse — force re-entry into the screenshot→click loop. The
        # user-named pattern: instead of "tool failed → take screenshot →
        # try different V_n" the brain reaches for request_help. This
        # blocks that until the brain has at least looked at the page.
        refuse = self.s.must_screenshot_before_giving_up()
        if refuse:
            return refuse

        # Track failed tactics on the session — they survive into
        # handoff and into the help-advisor's "do not suggest" list.
        try:
            for line in (failed_tactics or "").split("\n"):
                tactic = line.strip().lstrip("-• ").strip()
                if tactic and tactic not in self.s.failed_tactics:
                    self.s.failed_tactics.append(tactic)
            if len(self.s.failed_tactics) > self.s._FAILED_TACTICS_CAP:
                self.s.failed_tactics = self.s.failed_tactics[
                    -self.s._FAILED_TACTICS_CAP :
                ]
        except Exception:
            pass

        # Arch v3: first 3 calls become non-terminal advisor calls.
        # The advisor returns tactical advice as the tool result and the
        # worker keeps running in the same conversation. After 3 calls,
        # fall through to the legacy "save resumption artifact + spawn
        # successor" path.
        if (
            os.environ.get("HELP_ADVISOR_ENABLED", "1") != "0"
            and self.s.help_advisor_calls < self.s.HELP_ADVISOR_BUDGET
        ):
            self.s.help_advisor_calls += 1
            try:
                from superbrowser_bridge.help_advisor import advise_sync
            except Exception as exc:
                advise_sync = None  # type: ignore[assignment]
                print(f"[help_advisor import failed: {exc}]")
            if advise_sync is not None:
                # Build advisor payload from the live session.
                brief_dict = None
                brief = getattr(self.s, "task_brief", None)
                if brief is not None and hasattr(brief, "to_dict"):
                    try:
                        brief_dict = brief.to_dict()
                    except Exception:
                        brief_dict = None
                page_state_dict = None
                last_resp = getattr(self.s, "_last_vision_response", None)
                if last_resp is not None:
                    try:
                        ps = getattr(last_resp, "page_state", None)
                        if ps is not None and hasattr(ps, "model_dump"):
                            page_state_dict = ps.model_dump()
                    except Exception:
                        page_state_dict = None
                last_steps_raw = (
                    getattr(self.s, "step_history", None) or []
                )[-6:]
                last_steps = [
                    s for s in last_steps_raw if isinstance(s, dict)
                ]
                advice = advise_sync({
                    "brief": brief_dict,
                    "current_url": self.s.current_url,
                    "page_state": page_state_dict,
                    "last_steps": last_steps,
                    "failed_tactics": list(self.s.failed_tactics)[-12:],
                    "failed_bboxes": list(
                        getattr(self.s, "_failed_bboxes", []) or []
                    )[-8:],
                    "reason": reason,
                })
                self.s.record_step(
                    "browser_request_help",
                    reason[:80],
                    f"advised={self.s.help_advisor_calls}/"
                    f"{self.s.HELP_ADVISOR_BUDGET}",
                )
                budget_left = (
                    self.s.HELP_ADVISOR_BUDGET - self.s.help_advisor_calls
                )
                return (
                    f"[ADVISOR call {self.s.help_advisor_calls}/"
                    f"{self.s.HELP_ADVISOR_BUDGET}, {budget_left} left]\n"
                    f"{advice}\n\n"
                    f"This is in-session advice — keep working. After "
                    f"{self.s.HELP_ADVISOR_BUDGET} advisor calls, the next "
                    f"request_help will trigger a successor worker spawn."
                )

        # Legacy path: save resumption artifact + signal "successor needed".
        from superbrowser_bridge.routing import _domain_from_url
        domain = _domain_from_url(self.s.current_url) if self.s.current_url else ""
        saved = save_resumption_artifact(
            self.s, domain,
            help_reason=reason,
            help_failed_tactics=failed_tactics,
        )
        self.s.record_step(
            "browser_request_help",
            reason[:80],
            f"saved={saved} session={self.s.session_id}",
        )
        hint = (
            "[HELP REQUESTED] Resumption state saved. "
            "Now call done(success=False, final_answer='Need different tactic: ...') "
            "with a ≤30-word summary. "
            "The orchestrator will delegate a fresh worker that resumes on this "
            "same browser session with your failed tactics excluded."
        ) if saved else (
            "[HELP REQUEST NOT SAVED] session_id or current_url is empty — "
            "resumption artifact could not be written. Proceed with done(success=False) "
            "and explain the blocker in final_answer."
        )
        return hint


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID to escalate"),
        reason=StringSchema(
            "Short reason for escalation (logged into per-domain learnings).",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserEscalateTool(Tool):
    """Migrate a Tier-1 session to Tier-3 (undetected Chromium).

    Exports the current URL + cookies + localStorage + sessionStorage
    from the t1 session, closes it, opens a fresh t3 session with those
    pre-loaded, navigates back to the same URL. From the LLM's POV the
    session_id changes; all subsequent tool calls route transparently to
    the new backend.

    Typically fired by the worker hook when `network_blocked=True` or
    vision detects a captcha; can also be called explicitly when the LLM
    wants to pre-emptively route through undetected Chromium.
    """

    name = "browser_escalate"
    description = (
        "Escalate a Tier-1 session to Tier-3 (undetected Chromium for "
        "Akamai/DataDome/PerimeterX). Preserves URL + cookies + "
        "localStorage. Form state resets — re-fill any in-progress inputs. "
        "One-way within a task. Returns the new t3 session_id."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, reason: str | None = None, force: bool = False, **kw: Any) -> str:
        session_id = self.s.resolve_session_id(session_id)
        if self.s.backend == "t3":
            return (
                f"[already_t3] Session {session_id} is already on Tier 3 "
                f"(backend={self.s.backend}). No escalation needed."
            )
        if not self.s.session_id or self.s.session_id != session_id:
            return (
                f"[session_mismatch] Requested session_id={session_id}, "
                f"active session_id={self.s.session_id}. Not escalating."
            )

        # --- Validation: refuse to escalate a session that isn't blocked ---
        # Observed failure mode (2026-04-19): the LLM calls
        # `browser_escalate(reason="403 Forbidden")` when browser_wait_for
        # merely TIMED OUT on a slow-rendering SPA — no 403 ever occurred.
        # The escalation then tears down the t1 session, reopens on t3, and
        # the t3 session re-encounters the same slow page, chaining more
        # spurious guesses. Refuse escalation unless we have concrete
        # evidence the session is blocked.
        last_status = self.s.last_network_status
        has_evidence = (
            bool(self.s.network_blocked)
            or (last_status is not None and last_status >= 400 and last_status != 404)
        )
        # Also accept evidence from vision: if the last vision pass flagged
        # a captcha, escalation is justified.
        vresp = getattr(self.s, "_last_vision_response", None)
        vflags = getattr(vresp, "flags", None) if vresp is not None else None
        if vflags is not None and bool(getattr(vflags, "captcha_present", False)):
            has_evidence = True

        if not has_evidence and not force:
            self.s.record_step(
                "browser_escalate", session_id,
                f"REFUSED: no block evidence (status={last_status}, "
                f"network_blocked={self.s.network_blocked}, reason={reason!r})",
            )
            return (
                f"[escalate_rejected] Session {session_id} is NOT actually "
                f"blocked. network_blocked={self.s.network_blocked}, "
                f"last_status={last_status or 'OK'}, url={self.s.current_url}. "
                f"The reason you gave ({reason!r}) is not reflected in any "
                f"tool output. Common cause: a slow-rendering SPA timing out "
                f"browser_wait_for. DO NOT confabulate failure reasons.\n"
                f"Instead:\n"
                f"  - browser_screenshot to see the actual page state.\n"
                f"  - browser_wait_for with a longer timeout (e.g. 20-30s) "
                f"or a different selector that matches what actually renders.\n"
                f"  - browser_run_script to inspect document.readyState / "
                f"document.body.innerHTML.length.\n"
                f"If you truly observe HTTP 4xx/5xx, 'Access Denied', a "
                f"visible captcha widget, or bot-wall prose in a screenshot, "
                f"call this tool again with force=true."
            )

        # --- 1. Snapshot state from the t1 session ---------------------
        t1_url = self.s.current_url or ""
        cookies: list[dict] = []
        local_storage: dict[str, str] = {}
        session_storage: dict[str, str] = {}

        try:
            ev = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": (
                    "(() => ({"
                    "localStorage: Object.fromEntries(Object.entries(localStorage)),"
                    "sessionStorage: Object.fromEntries(Object.entries(sessionStorage)),"
                    "url: location.href,"
                    "}))()"
                )},
                timeout=15.0,
            )
            if ev.status_code == 200:
                data = ev.json()
                result = data.get("result") if isinstance(data, dict) else None
                if isinstance(result, dict):
                    local_storage = result.get("localStorage") or {}
                    session_storage = result.get("sessionStorage") or {}
                    if not t1_url:
                        t1_url = result.get("url") or ""
        except Exception as exc:
            print(f"  [escalate] localStorage export failed: {exc}")

        try:
            ck = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/script",
                json={"code": "return await page.cookies();"},
                timeout=15.0,
            )
            if ck.status_code == 200:
                payload = ck.json()
                if isinstance(payload, dict):
                    out = payload.get("output") or []
                    if isinstance(out, list):
                        cookies = [c for c in out if isinstance(c, dict)]
        except Exception as exc:
            print(f"  [escalate] cookie export failed: {exc}")

        # --- 2. Close the t1 session ------------------------------------
        try:
            await _request_with_backoff(
                "DELETE",
                f"{SUPERBROWSER_URL}/session/{session_id}",
                timeout=10.0,
            )
        except Exception as exc:
            print(f"  [escalate] t1 close failed (ignored): {exc}")

        # --- 3. Record tier-1 failure in the learning system ------------
        try:
            from urllib.parse import urlparse as _urlparse
            from superbrowser_bridge.routing import _record_routing_outcome
            host = _urlparse(t1_url).hostname or ""
            if host:
                _record_routing_outcome(
                    host, "browser", False, tier=1,
                    block_class="escalated:" + (reason or "unspecified"),
                )
        except Exception:
            pass

        # --- 4. Open a fresh t3 session with imported state -------------
        from superbrowser_bridge.antibot import interactive_session as _t3mgr
        try:
            import_state = {
                "cookies": cookies,
                "localStorage": local_storage,
                "sessionStorage": session_storage,
            }
            data = await _t3mgr.default().open(
                t1_url or None,
                task_id=self.s.task_id,
                import_state=import_state,
                timeout_s=45.0,
            )
        except Exception as exc:
            return (
                f"[escalate_failed] Tier-3 open failed: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            )

        new_sid = data.get("sessionId", "")
        self.s.session_id = new_sid
        self.s.network_blocked = False
        self.s.consecutive_click_calls = 0
        # Legacy idempotency guard sees a fresh session.
        self.s.blocked_browser_open_count = 0
        self.s.log_activity(f"escalate(t1->t3 reason={reason or '?'})", f"new_sid={new_sid}")
        self.s.record_step("browser_escalate", t1_url, f"reason={reason or 'unspecified'} new_sid={new_sid}")

        return (
            f"[escalated_to_t3] Session migrated to Tier 3 (undetected "
            f"Chromium). new_session_id={new_sid} url={data.get('url', t1_url)} "
            f"cookies_imported={len(cookies)} localStorage_keys={len(local_storage)} "
            f"reason={reason or 'unspecified'}\n"
            f"IMPORTANT: form inputs were reset during escalation. Re-fill any "
            f"in-progress form before submitting. All subsequent browser_* "
            f"tools use the new session_id transparently."
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        max_iterations=IntegerSchema(
            "Safety cap on scroll steps inside the dialog (default 12, max 30).",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserInventoryFiltersTool(Tool):
    name = "browser_inventory_filters"
    description = (
        "Scan-and-collect tool for filter/option dialogs. When a filter "
        "panel/dialog is open, this scrolls it top-to-bottom in one tool "
        "call and returns EVERY checkbox/radio/option/switch with its "
        "label, group heading, stable CSS selector, and current selected "
        "state. Restores the original scroll position before returning. "
        "Use this BEFORE applying multi-value filters (WiFi+cleaning, "
        "amenities, multi-pick facets) so the brain sees the full option "
        "inventory in one shot — no iterative click→screenshot→scroll "
        "loop. Falls back to scanning the document if no dialog is open. "
        "Cheap: server-side only, no vision/screenshot cost. Returned "
        "selectors are stable across modal scrolls (vision V_n indices "
        "are NOT) — pair with browser_click_selector for reliable apply."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        max_iterations: int | None = None,
        **kw: Any,
    ) -> Any:
        gate = await _feedback_gate("browser_inventory_filters")
        if gate:
            return gate
        # Arch v3 fix M — refuse when vision already surfaced enough
        # filter-shaped bboxes. Inventory's server-side scroll
        # invalidates V_n indices and confuses downstream clicks.
        redundant = _inventory_filters_redundant(self.s)
        if redundant:
            return redundant

        if not session_id:
            return "[inventory_filters_failed:no_session] Provide session_id."

        payload: dict[str, Any] = {}
        if max_iterations is not None:
            payload["maxIterations"] = int(max_iterations)

        print("\n>> browser_inventory_filters()")

        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/inventory-filters",
                json=payload,
                timeout=20.0,
            )
        except Exception as exc:
            return f"[inventory_filters_failed] request error: {exc}"

        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[inventory_filters_failed] HTTP {r.status_code}: {err}"

        data = r.json()
        options = data.get("options") or []
        # Hierarchy fields from the v5 expander sweep (page.ts:2105+).
        # When INVENTORY_HIERARCHY=0 we drop them to revert to flat rendering.
        if os.environ.get("INVENTORY_HIERARCHY", "1") == "0":
            expanders: list[dict] = []
            # Strip parent_label from each option so downstream form_session
            # fuzzy match doesn't see it either.
            for o in options:
                o.pop("parent_label", None)
        else:
            expanders = data.get("expanders") or []
        scope = data.get("scope") or "document"
        total = int(data.get("total") or 0)
        travel = int(data.get("scrollTravelPx") or 0)
        iters = int(data.get("iterations") or 0)

        # Cache the manifest on session state so form_begin(inventory=true)
        # can reuse it without a second scan, AND so the post-click expander
        # rescan in BrowserClickSelectorTool can detect when the brain
        # clicked an expander.
        try:
            self.s.last_filter_manifest = {
                "session_id": session_id,
                "scope": scope,
                "options": options,
                "expanders": expanders,
                "captured_at": time.time(),
            }
        except Exception:
            pass

        self.s.record_step(
            "browser_inventory_filters",
            f"scope={scope} total={total} expanders={len(expanders)} "
            f"travel={travel}px iters={iters}",
            self.s.current_url or "",
        )

        if total == 0 and not expanders:
            return (
                "[inventory_filters:empty] No checkbox/radio/option/switch "
                "controls found"
                f"{' inside any open dialog' if scope == 'document' else ''}"
                ". If you expected a filter panel here, the dialog may not "
                "be open yet — open it (click 'All Filters', 'Show filters', "
                "etc.) and call this tool again."
            )

        # Group options by their group heading for compact display.
        groups: dict[str, list[dict]] = {}
        for opt in options:
            g = (opt.get("group") or "").strip() or "(ungrouped)"
            groups.setdefault(g, []).append(opt)

        lines: list[str] = []
        lines.append(
            f"[inventory_filters_ok scope={scope} total={total} "
            f"groups={len(groups)} expanders={len(expanders)} "
            f"travel={travel}px iters={iters}]"
        )
        lines.append(
            "Stable CSS selectors below — prefer browser_click_selector(<sel>) "
            "for applying these. Selected=true means the option is already "
            "checked; toggle only if the task requires it."
        )

        # Hierarchical expanders (v5): show BEFORE the flat option groups
        # so the brain sees parent expanders are first-class citizens, not
        # just leaf checkboxes. The wineaccess "click US, get all 50
        # states" failure mode comes from treating "United States" as a
        # leaf when it's actually a parent that needs expanding.
        if expanders:
            lines.append(
                "\n## Collapsed groups (click expander to reveal children, "
                "then click the child option)"
            )
            for ex in expanders:
                state_marker = "▼" if ex.get("expanded") else "▶"
                ctrl = ex.get("controls_selector") or "?"
                cc = ex.get("child_count")
                cc_note = f" children≈{cc}" if isinstance(cc, int) and cc > 0 else ""
                lines.append(
                    f"  [{state_marker}] expander label={ex.get('label','')!r} "
                    f"selector={ex.get('selector','')} controls={ctrl}{cc_note}"
                )

        for gname, opts in groups.items():
            lines.append(f"\n## {gname}")
            for o in opts:
                sel_marker = "✓" if o.get("selected") else "·"
                parent = o.get("parent_label")
                parent_suffix = (
                    f"  (under {parent!r} — expand parent first if collapsed)"
                    if parent else ""
                )
                lines.append(
                    f"  [{sel_marker}] {o.get('kind','?'):<8} "
                    f"label={o.get('label','')!r:<40} "
                    f"selector={o.get('selector','')}{parent_suffix}"
                )

        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        target_text=StringSchema(
            "Text or regex of the element you want to scroll to. Substring "
            "match if it's not a valid regex. Optional if target_role given.",
            nullable=True,
        ),
        target_role=StringSchema(
            "ARIA role / tagName to filter on (e.g. 'button', 'h2'). "
            "Optional if target_text given.",
            nullable=True,
        ),
        direction=StringSchema(
            "'down' (default) or 'up'.",
            nullable=True,
        ),
        max_iterations=IntegerSchema(
            "Safety cap on scroll steps. Default 10, max 40.",
            nullable=True,
        ),
        step_ratio=NumberSchema(
            description="Fraction of viewport to scroll per step (0.1–1.0). Default 0.8.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserScrollUntilTool(Tool):
    name = "browser_scroll_until"
    description = (
        "Closed-loop scroll. Walks the page in `direction` until an "
        "element matching `target_text` (substring or regex) and/or "
        "`target_role` becomes visible, the page can't scroll further, "
        "or `max_iterations` elapses. Cheap — uses interactive-element "
        "polling between steps, no screenshot per iteration. Returns a "
        "structured outcome with `reason` ('matched' | 'page_end' | "
        "'page_start' | 'max_iterations') so the brain knows whether "
        "to act, retreat, or give up. Prefer this over browser_scroll "
        "when you know what you're scrolling toward — it stops at the "
        "right place AND tells you when content runs out, instead of "
        "blindly scrolling and re-screenshotting."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        target_text: str | None = None,
        target_role: str | None = None,
        direction: str | None = None,
        max_iterations: int | None = None,
        step_ratio: float | None = None,
        **kw: Any,
    ) -> Any:
        gate = await _feedback_gate("browser_scroll_until")
        if gate:
            return gate

        if not (target_text and target_text.strip()) and not (target_role and target_role.strip()):
            return (
                "[scroll_until_failed:no_target] Provide target_text or "
                "target_role. Substring match works for most cases — pass "
                "the visible text of the element you want to find."
            )

        payload: dict[str, Any] = {
            "direction": direction or "down",
        }
        if target_text and target_text.strip():
            payload["targetText"] = target_text.strip()
        if target_role and target_role.strip():
            payload["targetRole"] = target_role.strip()
        if max_iterations is not None:
            payload["maxIterations"] = int(max_iterations)
        if step_ratio is not None:
            payload["stepRatio"] = float(step_ratio)

        target_disp = target_text or f"role={target_role}"
        print(
            f"\n>> browser_scroll_until({target_disp!r}, dir={payload['direction']})"
        )

        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/scroll-until",
                json=payload,
                timeout=30.0,  # closed-loop can take 10 iterations × ~300ms
            )
        except Exception as exc:
            return f"[scroll_until_failed] request error: {exc}"

        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[scroll_until_failed] HTTP {r.status_code}: {err}"

        data = r.json()
        outcome = data.get("outcome") or {}
        reason = str(outcome.get("reason") or "unknown")
        iters = int(outcome.get("iterations") or 0)
        scrolled = int(outcome.get("scrolledPx") or 0)

        # Update scroll telemetry so the next vision pass sees a fresh
        # [SCROLL_STATE …] line including reached_bottom/reached_top hints
        # that came from this closed-loop call.
        _update_scroll_telemetry(
            self.s,
            data.get("scrollInfo"),
            payload["direction"],
            extra={
                "last_scroll_reason": reason,
                "reached_bottom": reason == "page_end",
                "reached_top": reason == "page_start",
            },
        )

        # Mirror the BrowserDragSliderUntilTool record convention so
        # step_history shows a clear summary line for downstream
        # loop-detection and task-graph signal evaluation.
        self.s.record_step(
            "browser_scroll_until",
            f"{target_disp!r} → {reason} in {iters} iters ({scrolled}px)",
            data.get("url", ""),
        )

        lines: list[str] = []
        if outcome.get("found"):
            matched = outcome.get("matchedText") or ""
            sel = outcome.get("matchedSelector") or ""
            lines.append(
                f"FOUND {target_disp!r} after {iters} iter(s), "
                f"scrolled {scrolled}px. matched={matched[:80]!r} "
                f"selector={sel}"
            )
        else:
            tag = (
                "page_end" if reason == "page_end"
                else "page_start" if reason == "page_start"
                else reason
            )
            lines.append(
                f"[scroll_until_failed:{tag}] target {target_disp!r} not "
                f"found after {iters} iter(s) ({scrolled}px). "
                f"reason={reason}."
            )
            if reason == "page_end":
                lines.append(
                    "  Page can't scroll further down. The target may be "
                    "above (try direction='up') or may live inside a "
                    "scrollable sidebar / panel that the WINDOW scroll "
                    "doesn't move. Recovery: take browser_screenshot — "
                    "vision will emit V_n bboxes for ALL visible filter "
                    "section headers; click the relevant header (its "
                    "V_n) to expand it, then click the chip inside. "
                    "DO NOT pivot to browser_navigate with a constructed "
                    "filter URL — that route lands on broken pages."
                )
            elif reason == "page_start":
                lines.append(
                    "  Already at top of page. Try direction='down' or "
                    "verify the target text/role is correct. The label "
                    "may be plural/singular different ('Food Pairings' "
                    "vs 'Food Pairing') — try a shorter substring."
                )
            elif reason == "max_iterations":
                lines.append(
                    "  Hit iteration cap. If you believe the target "
                    "exists further on, raise max_iterations (cap is "
                    "40) or use a more specific target_text. If the "
                    "target lives in a collapsed accordion, click the "
                    "section header first to expand it; scroll alone "
                    "won't reveal it."
                )

        if data.get("elements"):
            lines.append(str(data["elements"]))

        # Schedule a vision prefetch so the next browser_screenshot is
        # cached — same convention as the other scroll tools.
        self.s.advance_observation_token("scroll_until")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task, "\n".join(lines),
            state=self.s,
        )


def _update_scroll_telemetry(
    state: "BrowserSessionState",
    scroll_info: Any,
    direction: str | None,
    extra: dict | None = None,
) -> None:
    """Record post-scroll geometry on the session state.

    Read by `_format_state` (and the [SCROLL_STATE …] caption line in
    `build_text_only`) so vision can reason about whether more scrolling
    is plausible. Tolerant of missing scrollInfo — telemetry is best-
    effort and must not break the tool path.
    """
    try:
        if not isinstance(scroll_info, dict):
            scroll_info = {}
        scroll_y = int(scroll_info.get("scrollY") or 0)
        scroll_h = int(scroll_info.get("scrollHeight") or 0)
        vp_h = int(scroll_info.get("viewportHeight") or 0)
        # 12px of slack at the bottom catches off-by-one rounding without
        # falsely flagging "reached_bottom" mid-page.
        reached_bottom = scroll_h > 0 and (scroll_y + vp_h) >= (scroll_h - 12)
        reached_top = scroll_y <= 4
        prev = getattr(state, "scroll_telemetry", None) or {}
        history = list(prev.get("direction_history") or [])
        if direction:
            history.append(direction)
            history = history[-6:]
        tel = {
            "scrollY": scroll_y,
            "scrollHeight": scroll_h,
            "viewportHeight": vp_h,
            "direction_history": history,
            "reached_bottom": reached_bottom,
            "reached_top": reached_top,
        }
        if extra:
            tel.update(extra)
        state.scroll_telemetry = tel
    except Exception:
        # Telemetry is best-effort — never let it block the scroll tool.
        pass


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index of the select/dropdown"),
        value=StringSchema("Option value or visible text to select"),
        required=["session_id", "index", "value"],
    )
)

class BrowserRewindToCheckpointTool(Tool):
    """Bail from the current page state back to the last known-good URL.

    Session memory escape hatch: when the worker has exhausted local
    retries and the brain is stuck, rewinding to `best_checkpoint_url`
    (the last meaningful checkpoint the worker recorded) lets the plan
    re-approach with a fresh vision pass. The tool forces the token
    forward + busts the vision cache so the next mutation blocks on
    genuinely fresh bboxes rather than any lingering cache from the
    stuck-state page.
    """

    name = "browser_rewind_to_checkpoint"
    description = (
        "Navigate back to the last known-good checkpoint URL when the "
        "current page is unresponsive or the plan is stuck. Invalidates "
        "vision cache + element fingerprints so the next vision pass "
        "reflects the rewound page."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, **kw: Any) -> str:
        session_id = self.s.resolve_session_id(session_id)
        target = (self.s.best_checkpoint_url or "").strip()
        print(f"\n>> browser_rewind_to_checkpoint(target={target[:80]!r})")
        if not target:
            return (
                "[rewind_failed:no_checkpoint] No best_checkpoint_url has "
                "been recorded for this session. Call browser_navigate "
                "directly with a URL you know works, or report failure."
            )

        # Advance token FIRST — any vision prefetch already in flight
        # will see the mismatch and drop its write; the next mutation
        # cannot unblock on stale bboxes.
        self.s.advance_observation_token("rewind")

        # Clear local fingerprints/vision state so `vision_is_fresh()`
        # cannot be true until a brand new pass lands.
        self.s.element_fingerprints.clear()
        self.s._last_vision_response = None
        self.s._last_vision_ts = 0.0
        self.s._last_vision_url = ""

        # Bust the shared vision cache for this session so a replay
        # won't resurrect the stuck-state bboxes.
        try:
            from vision_agent import get_vision_agent, vision_agent_enabled
            if vision_agent_enabled():
                agent = get_vision_agent()
                cache = getattr(agent, "_cache", None)
                if cache is not None and hasattr(cache, "bust_session"):
                    await cache.bust_session(session_id)
        except Exception as exc:
            print(f"  [rewind: vision cache bust skipped — {exc}]")

        # Navigate via the TS server — same endpoint BrowserNavigateTool
        # uses. Skipping the full BrowserNavigateTool path to avoid
        # re-triggering domain-pinning / CF guards that apply to forward
        # nav but not to a known-good rewind URL.
        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/navigate",
                json={"url": target, "waitUntil": "domcontentloaded"},
                timeout=30.0,
            )
        except Exception as exc:
            return f"[rewind_failed:network] {exc}"
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[rewind_failed:http_{r.status_code}] {err}"

        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        self.s.record_url(target)
        self.s.record_step(
            "browser_rewind_to_checkpoint",
            f"→ {target[:80]}",
            f"title={data.get('title', '?')}" if isinstance(data, dict) else "ok",
        )
        self.s.log_activity(f"rewind → {target[:60]}")

        # Fresh prefetch on the rewound page so the next mutation's gate
        # unblocks quickly rather than hitting a cold cache.
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(
                data if isinstance(data, dict) else {},
                f"Rewound to checkpoint: {target[:80]}",
            ),
            state=self.s,
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        startX=NumberSchema("Start X coordinate"),
        startY=NumberSchema("Start Y coordinate"),
        endX=NumberSchema("End X coordinate"),
        endY=NumberSchema("End Y coordinate"),
        steps=IntegerSchema("Number of intermediate steps (default 25, higher = smoother)", nullable=True),
        required=["session_id", "startX", "startY", "endX", "endY"],
    )
)

class BrowserGetRectTool(Tool):
    name = "browser_get_rect"
    description = (
        "Return getBoundingClientRect() for one or more CSS selectors. "
        "Pixel-exact, zero vision cost. Use to derive coordinates before "
        "calling browser_click_selector / browser_drag_selectors. "
        "Selectors ride as a JSON string (no ArraySchema in this layer)."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        selectors_json: str,
        ensure_visible: bool | None = None,
        **kw: Any,
    ) -> str:
        try:
            selectors = json.loads(selectors_json)
        except (TypeError, ValueError) as exc:
            return f"[get_rect_failed] selectors_json is not valid JSON: {exc}"
        if not isinstance(selectors, list) or not all(isinstance(s, str) for s in selectors):
            return "[get_rect_failed] selectors_json must decode to a list of strings."

        print(f"\n>> browser_get_rect({len(selectors)} selectors)")
        payload = {"selectors": selectors, "ensureVisible": bool(ensure_visible)}
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/rect",
            json=payload,
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        rects = data.get("rects") or []
        lines = ["Selector rects:"]
        for sel, rect in zip(selectors, rects):
            if rect is None:
                lines.append(f"  {sel} → NOT FOUND")
                continue
            lines.append(
                f"  {sel} → cx={rect['cx']:.1f} cy={rect['cy']:.1f} "
                f"w={rect['w']:.1f} h={rect['h']:.1f} "
                f"visible={rect['visible']} inViewport={rect['inViewport']}"
            )
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        selector=StringSchema("CSS selector of the element to click"),
        target_label=StringSchema(
            description=(
                "Short label of the element you expect this selector to "
                "match (e.g. 'Oregon checkbox', 'Apply filters button'). "
                "REQUIRED when the selector is vague (bare tag, comma "
                "fallback list, [role='button']). Optional when the "
                "selector is highly specific (#id, [data-testid='...'])."
            ),
            nullable=True,
        ),
        button=StringSchema("Mouse button: left|right|middle", nullable=True),
        click_count=IntegerSchema("Number of clicks (1 for single, 2 for double)", nullable=True),
        linear=BooleanSchema(
            description=(
                "If true (default), use deterministic teleport click (pixel-exact). "
                "Set false for stealth-critical contexts (captchas) that need Bezier humanisation."
            ),
            nullable=True,
        ),
        narration=StringSchema(
            description=(
                "Optional one-sentence narration of WHY you're clicking "
                "this selector and what you expect to happen. Stored on "
                "state; rendered back in the next turn's guidance as "
                "`[last_intended: ...]` for chain-of-thought trail. "
                "Never required."
            ),
            nullable=True,
        ),
        required=["session_id", "selector"],
    )
)
class BrowserClickSelectorTool(Tool):
    name = "browser_click_selector"
    description = (
        "Click the centre of a DOM element by CSS selector. Pixel-exact, "
        "zero Gemini cost. PREFER OVER browser_click_at(vision_index=...) "
        "whenever the target has a stable hook — chess squares "
        "(.square-54), form fields (#email), buttons with data-test-id, "
        "captcha handles. Fails fast if the selector is missing or zero-size."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        selector: str,
        button: str | None = None,
        click_count: int | None = None,
        linear: bool | None = None,
        narration: str | None = None,
        target_label: str | None = None,
        **kw: Any,
    ) -> str:
        # Arch v3 fix #5: state-freshness gate.
        gate = self.s.must_screenshot_before_state_change("browser_click_selector")
        if gate:
            return gate
        # Arch v3 fix A + D: structural + vision-alignment selector check.
        # Layer A catches kitchen-sink probes ("summary, button,
        # [role='button']"). Layer D catches DOM-stale selectors on
        # dynamic filter pages where the brain claims a target vision
        # didn't actually emit (#region-united-states, #accordion-region,
        # etc., that worked on a prior page state but no longer match).
        sel_refuse = _validate_selector_target_label(
            selector, target_label, state=self.s,
        )
        if sel_refuse:
            return sel_refuse
        # v5: stash optional narration for next-turn `[last_intended]`.
        if narration:
            self.s._last_narration = str(narration)[:240]
        print(f"\n>> browser_click_selector({selector!r})")
        # Playwright-pseudo guard. Selectors like `:has-text('X')`,
        # `:contains('X')`, `:visible`, `:hidden`, `>>` (Playwright chain)
        # are NOT standard CSS — Puppeteer's querySelector silently
        # returns null and the brain doesn't know whether the selector
        # was wrong or the element doesn't exist. Surfacing a structured
        # error stops the wild-guess pivot to compound selectors.
        pseudo_block = _detect_playwright_pseudo(selector)
        if pseudo_block:
            return pseudo_block
        # Vague-selector pre-flight. The match_count warning currently
        # surfaces only AFTER the click — by then the brain has often
        # pivoted to browser_eval to find a better selector. Catch
        # selectors that look vague (no id / data-testid / nth-* /
        # [for=] / [name=] discriminator) BEFORE the click and refuse
        # if querySelectorAll would match >1. Cheap: one /evaluate
        # round-trip only when the selector text-pattern is suspect.
        if (
            os.environ.get("SELECTOR_VAGUE_REFUSAL", "1") != "0"
            and _looks_vague_selector(selector)
        ):
            try:
                count_resp = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                    json={
                        "script": (
                            "(() => { try { return "
                            f"document.querySelectorAll({json.dumps(selector)}).length;"
                            " } catch(e) { return -1; } })()"
                        ),
                    },
                    timeout=10.0,
                )
                _body = count_resp.json() if count_resp.status_code < 400 else None
                _matched = (
                    _body.get("result") if isinstance(_body, dict) else None
                )
                if isinstance(_matched, int) and _matched > 1:
                    return (
                        f"[selector_too_vague: would match {_matched} elements] "
                        f"{selector!r} has no id (#…), data-testid, :nth-of-type, "
                        f"[for=…] or [name=…] discriminator — querySelector would "
                        f"silently click the first match, which is rarely the right "
                        f"one. Recovery:\n"
                        f"  1. browser_screenshot — vision labels the actual target\n"
                        f"  2. browser_click_at(vision_index=V_n) — pixel-exact\n"
                        f"  3. OR narrow the selector with #id, [data-testid=…], "
                        f":nth-of-type(N), [for=…], or [name=…].\n"
                        f"Override: SELECTOR_VAGUE_REFUSAL=0."
                    )
            except Exception as exc:
                # Pre-flight is opportunistic — never block on its failure.
                print(f"   [vague-selector pre-flight skipped — {exc}]")
        # Phase 1.1: hard sync gate.
        sync_block = await self.s.ensure_vision_synced(reason="browser_click_selector")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls += 1

        payload: dict[str, Any] = {"selector": selector, "ensureVisible": True}
        if button is not None:
            payload["button"] = button
        if click_count is not None:
            payload["clickCount"] = click_count
        if linear is not None:
            payload["linear"] = linear

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/click-selector",
            json=payload,
            timeout=15.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            # Phase 3.1: record cursor failure so the script lockout
            # gate counts this as a tried-and-failed cursor strategy.
            self.s.record_cursor_failure(
                strategy="click_selector",
                target=selector,
                reason=str(err)[:120],
            )
            return f"[click_selector_failed] {err}"
        data = r.json()
        clicked = data.get("clicked", {})
        _pre_url_for_subgoal = self.s.current_url or ""
        self.s.record_step(
            "browser_click_selector",
            f"{selector} @ ({clicked.get('x','?')},{clicked.get('y','?')})",
            data.get("url", ""),
        )
        # click_selector is a mutation — advance the observation token
        # and schedule a vision prefetch so the next screenshot is warm.
        self.s.advance_observation_token("click_selector")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        caption = (
            f"Clicked {selector} at "
            f"({clicked.get('x','?')},{clicked.get('y','?')})"
        )
        # When a selector matches >1 elements, querySelector silently picks
        # the first. Surfacing match_count tells the brain to narrow the
        # selector instead of accepting whatever it got — stops the
        # `selector: 'button'` and `a[role='button'][href='#']` family of
        # vague-selector misclicks. Engine surfaces match_count via
        # src/server/http.ts:/click-selector → page.ts:clickSelector.
        match_count = data.get("match_count")
        if isinstance(match_count, int) and match_count > 1:
            caption += (
                f"\n[selector_ambiguous: matched {match_count} elements, "
                f"clicked first — narrow the selector with id, "
                f"data-testid, :nth-of-type, or attribute filter to pick "
                f"the intended target]"
            )
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        # Post-click expander rescan (v5): when the brain just clicked an
        # expander it learned about from the most recent inventory, the
        # page now reveals child filters. Re-scan and surface them so the
        # next action picks the right CHILD instead of guessing or
        # re-running inventory_filters manually.
        expander_note = await self._maybe_rescan_after_expander_click(
            session_id, selector,
        )
        if expander_note:
            caption += expander_note
        subgoal_note = await self.s.check_active_task_step(
            session_id, pre_url=_pre_url_for_subgoal,
        )
        if subgoal_note:
            caption += subgoal_note
        return await _append_fresh_vision(
            _vision_task,
            _maybe_no_effect_prefix(
                data, "browser_click_selector", caption,
                session_state=self.s,
            ),
            state=self.s,
        )

    async def _maybe_rescan_after_expander_click(
        self, session_id: str, clicked_selector: str,
    ) -> str:
        """If `clicked_selector` matches an expander from the most
        recent inventory_filters manifest AND that expander was
        collapsed, re-scan filters and surface the newly-visible
        children. Cheap (~100ms server-side, no vision cost). Returns
        empty string when not applicable (no manifest, selector not an
        expander, expander already open, hierarchy disabled).
        """
        if os.environ.get("INVENTORY_HIERARCHY", "1") == "0":
            return ""
        manifest = getattr(self.s, "last_filter_manifest", None)
        if not isinstance(manifest, dict):
            return ""
        expanders = manifest.get("expanders") or []
        if not expanders:
            return ""
        matched_expander = None
        for ex in expanders:
            if ex.get("selector") == clicked_selector:
                matched_expander = ex
                break
        if matched_expander is None:
            return ""
        if matched_expander.get("expanded"):
            # Was already open; clicking it likely collapsed it. Don't
            # re-scan in that case — the brain just hid children.
            return ""
        try:
            # noScrollWalk=true: the post-click rescan is looking ONLY
            # for newly-revealed children of the expander we just
            # clicked, which are local to that expander (no need to
            # walk the whole page top-to-bottom). Skipping the scroll
            # walk eliminates the visible page-scroll motion users
            # were observing across multiple parallel expander clicks.
            # Kill switch INVENTORY_QUICK_RESCAN=0 reverts to
            # full-scroll rescan.
            _quick = os.environ.get("INVENTORY_QUICK_RESCAN", "1") != "0"
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/inventory-filters",
                json={"noScrollWalk": _quick},
                timeout=10.0,
            )
            r.raise_for_status()
            new_data = r.json()
        except Exception as exc:
            return f"\n[expander_rescan_skipped: {exc!s:.60s}]"
        new_options = new_data.get("options") or []
        new_expanders = new_data.get("expanders") or []
        # Refresh the cached manifest so the NEXT click can again
        # detect expander interactions.
        try:
            self.s.last_filter_manifest = {
                "session_id": session_id,
                "scope": new_data.get("scope") or "document",
                "options": new_options,
                "expanders": new_expanders,
                "captured_at": time.time(),
            }
        except Exception:
            pass
        # Children are options whose parent_label matches the expander
        # we just clicked. Cap the surfaced list to keep the caption tight.
        parent_label = matched_expander.get("label") or ""
        children = [
            o for o in new_options
            if (o.get("parent_label") or "") == parent_label
        ]
        if not children:
            return (
                f"\n[expander_clicked label={parent_label!r}] "
                f"Re-scanned manifest but found no child options under "
                f"this label yet — the expand may still be animating, or "
                f"this expander reveals non-checkbox content. Take a "
                f"screenshot to verify."
            )
        sample = children[:8]
        sample_str = ", ".join(
            f"{o.get('label','?')!r}→{o.get('selector','?')}" for o in sample
        )
        more = f" (+{len(children) - len(sample)} more)" if len(children) > len(sample) else ""
        return (
            f"\n[expander_opened label={parent_label!r}] "
            f"{len(children)} children now visible: {sample_str}{more}. "
            f"Click the specific child you want with "
            f"browser_click_selector(<selector from list above>)."
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        from_selector=StringSchema("CSS selector of the drag source element"),
        to_selector=StringSchema("CSS selector of the drag destination element"),
        method=StringSchema(
            "One of 'auto' (default: try click_click, fall back to drag), "
            "'click_click' (two discrete clicks — more robust for chess/grid), "
            "'drag' (mousedown → move → mouseup — classical drag). ",
            nullable=True,
        ),
        hold_ms=IntegerSchema(
            "Milliseconds to pause between the two clicks when method=click_click, "
            "or to hold before drag start. Default 120.",
            nullable=True,
        ),
        linear=BooleanSchema(
            description="If true (default), deterministic paths. Set false for stealth-critical drags.",
            nullable=True,
        ),
        required=["session_id", "from_selector", "to_selector"],
    )
)

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        from_selector=StringSchema("CSS selector of the drag source element"),
        to_selector=StringSchema("CSS selector of the drag destination element"),
        method=StringSchema(
            "One of 'auto' (default: try click_click, fall back to drag), "
            "'click_click' (two discrete clicks — more robust for chess/grid), "
            "'drag' (mousedown → move → mouseup — classical drag). ",
            nullable=True,
        ),
        hold_ms=IntegerSchema(
            "Milliseconds to pause between the two clicks when method=click_click, "
            "or to hold before drag start. Default 120.",
            nullable=True,
        ),
        linear=BooleanSchema(
            description="If true (default), deterministic paths. Set false for stealth-critical drags.",
            nullable=True,
        ),
        required=["session_id", "from_selector", "to_selector"],
    )
)
class BrowserDragSelectorsTool(Tool):
    name = "browser_drag_selectors"
    description = (
        "Drag from one CSS-selected element to another. Pixel-exact. "
        "Default method 'auto' tries click-click first (safer on react-dnd "
        "and grid boards like chess.com) and falls back to classical drag "
        "if the DOM didn't mutate. PREFER OVER browser_drag(x1,y1,x2,y2) "
        "whenever both endpoints have stable selectors."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        from_selector: str,
        to_selector: str,
        method: str | None = None,
        hold_ms: int | None = None,
        linear: bool | None = None,
        **kw: Any,
    ) -> str:
        method = method or "auto"
        if method not in ("auto", "click_click", "drag"):
            return f"[drag_selectors_failed] method must be auto|click_click|drag, got {method!r}"
        print(f"\n>> browser_drag_selectors({from_selector!r} → {to_selector!r}, method={method})")
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        payload: dict[str, Any] = {
            "fromSelector": from_selector,
            "toSelector": to_selector,
            "method": method,
        }
        if hold_ms is not None:
            payload["holdMs"] = hold_ms
        if linear is not None:
            payload["linear"] = linear

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/drag-selectors",
            json=payload,
            timeout=30.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[drag_selectors_failed] {err}"
        data = r.json()
        outcome = data.get("outcome", {})
        self.s.record_step(
            "browser_drag_selectors",
            f"{from_selector}→{to_selector} via {outcome.get('methodUsed','?')}",
            data.get("url", ""),
        )
        frm = outcome.get("from", {})
        to = outcome.get("to", {})
        caption = (
            f"Dragged {from_selector} → {to_selector} "
            f"via {outcome.get('methodUsed','?')} "
            f"({frm.get('x','?')},{frm.get('y','?')}) → ({to.get('x','?')},{to.get('y','?')}) "
            f"mutated={outcome.get('mutated', False)}"
        )
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        points_json=StringSchema(
            "JSON array of {x, y} points, e.g. '[{\"x\":100,\"y\":200},{\"x\":150,\"y\":220}]'. "
            "At least two points required."
        ),
        step_ms=IntegerSchema(
            "Milliseconds between intermediate mouseMove events. Default 16 (~60fps).",
            nullable=True,
        ),
        hold_ms=IntegerSchema("Pre-press hold duration at points[0]. Default 50.", nullable=True),
        button=StringSchema("Mouse button: left|right|middle. Default left.", nullable=True),
        required=["session_id", "points_json"],
    )
)

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        points_json=StringSchema(
            "JSON array of {x, y} points, e.g. '[{\"x\":100,\"y\":200},{\"x\":150,\"y\":220}]'. "
            "At least two points required."
        ),
        step_ms=IntegerSchema(
            "Milliseconds between intermediate mouseMove events. Default 16 (~60fps).",
            nullable=True,
        ),
        hold_ms=IntegerSchema("Pre-press hold duration at points[0]. Default 50.", nullable=True),
        button=StringSchema("Mouse button: left|right|middle. Default left.", nullable=True),
        required=["session_id", "points_json"],
    )
)
class BrowserDragPathTool(Tool):
    name = "browser_drag_path"
    description = (
        "Drag along an arbitrary polyline of (x,y) points. For jigsaw "
        "captcha traces, connect-the-dots, signature drawing, or any "
        "free-form gesture where a straight start→end drag won't work."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        points_json: str,
        step_ms: int | None = None,
        hold_ms: int | None = None,
        button: str | None = None,
        **kw: Any,
    ) -> str:
        try:
            points = json.loads(points_json)
        except (TypeError, ValueError) as exc:
            return f"[drag_path_failed] points_json is not valid JSON: {exc}"
        if not isinstance(points, list) or len(points) < 2:
            return "[drag_path_failed] points_json must decode to a list of ≥2 {x,y} objects."
        for i, p in enumerate(points):
            if not isinstance(p, dict) or not isinstance(p.get("x"), (int, float)) \
               or not isinstance(p.get("y"), (int, float)):
                return f"[drag_path_failed] point[{i}] must be {{x: number, y: number}}"

        print(f"\n>> browser_drag_path({len(points)} points)")
        self.s.actions_since_screenshot += 1

        payload: dict[str, Any] = {"points": points}
        if step_ms is not None:
            payload["stepMs"] = step_ms
        if hold_ms is not None:
            payload["holdMs"] = hold_ms
        if button is not None:
            payload["button"] = button

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/drag-path",
            json=payload,
            timeout=30.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[drag_path_failed] {err}"
        data = r.json()
        self.s.record_step(
            "browser_drag_path",
            f"{len(points)} points",
            data.get("url", ""),
        )
        caption = f"Dragged along polyline of {len(points)} points"
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        selector=StringSchema(
            "CSS selector for the slider element (e.g. 'input[type=range][name=monthlyContribution]'). "
            "Frame-aware: the backend probes every frame on the page."
        ),
        value_json=StringSchema(
            "Target value as JSON. Examples: '300' for a single slider, '[25, 75]' for a "
            "dual-thumb range. Values are absolute (use the slider's own units) unless "
            "as='ratio' is set, in which case they are 0.0–1.0 positions along the track."
        ),
        value_mode=StringSchema(
            "Value interpretation: 'absolute' (default; matches the slider's own min/max) "
            "or 'ratio' (0.0-1.0 position along the track).",
            nullable=True,
        ),
        method=StringSchema(
            "Strategy: 'auto' (default), 'range-input' (direct value+input event), "
            "'keyboard' (focus + arrow keys), 'drag' (pixel drag). Use 'auto' unless debugging.",
            nullable=True,
        ),
        required=["session_id", "selector", "value_json"],
    )
)

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        selector=StringSchema(
            "CSS selector for the slider element (e.g. 'input[type=range][name=monthlyContribution]'). "
            "Frame-aware: the backend probes every frame on the page."
        ),
        value_json=StringSchema(
            "Target value as JSON. Examples: '300' for a single slider, '[25, 75]' for a "
            "dual-thumb range. Values are absolute (use the slider's own units) unless "
            "as='ratio' is set, in which case they are 0.0–1.0 positions along the track."
        ),
        value_mode=StringSchema(
            "Value interpretation: 'absolute' (default; matches the slider's own min/max) "
            "or 'ratio' (0.0-1.0 position along the track).",
            nullable=True,
        ),
        method=StringSchema(
            "Strategy: 'auto' (default), 'range-input' (direct value+input event), "
            "'keyboard' (focus + arrow keys), 'drag' (pixel drag). Use 'auto' unless debugging.",
            nullable=True,
        ),
        required=["session_id", "selector", "value_json"],
    )
)
class BrowserSetSliderTool(Tool):
    name = "browser_set_slider"
    description = (
        "Set a slider's value by number. Works for native <input type=range>, "
        "ARIA sliders (role=slider / aria-valuenow), and CSS-custom widgets. "
        "Prefer this over browser_drag for sliders — it auto-picks the most "
        "reliable strategy and crosses iframe boundaries. For dual-thumb "
        "sliders (e.g. an age range) pass value_json='[lo, hi]'. Returns the "
        "strategy used plus before/after values so you can verify the slide."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        selector: str,
        value_json: str,
        value_mode: str | None = None,
        method: str | None = None,
        **kw: Any,
    ) -> str:
        try:
            parsed = json.loads(value_json)
        except (TypeError, ValueError) as exc:
            return f"[set_slider_failed] value_json is not valid JSON: {exc}"
        if isinstance(parsed, (int, float)):
            value_payload: Any = float(parsed)
        elif (
            isinstance(parsed, list)
            and len(parsed) == 2
            and all(isinstance(n, (int, float)) for n in parsed)
        ):
            value_payload = [float(parsed[0]), float(parsed[1])]
        else:
            return "[set_slider_failed] value_json must decode to a number or [lo, hi] list"

        print(f"\n>> browser_set_slider({selector!r} → {value_payload})")
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        payload: dict[str, Any] = {"selector": selector, "value": value_payload}
        if value_mode is not None:
            payload["as"] = value_mode
        if method is not None:
            payload["method"] = method

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/set-slider",
            json=payload,
            timeout=30.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[set_slider_failed] {err}"
        data = r.json()
        outcome = data.get("outcome", {}) or {}
        strategy = outcome.get("strategy", "?")
        before = outcome.get("before")
        after = outcome.get("after")
        err = outcome.get("error")
        self.s.record_step(
            "browser_set_slider",
            f"{selector} → {value_payload} via {strategy}",
            data.get("url", ""),
        )
        if strategy == "unresolved" or err:
            return f"[set_slider_failed] {err or 'unresolved'} (selector={selector})"
        caption = (
            f"Set slider {selector} via {strategy}: {before} → {after} "
            f"(min={outcome.get('min')}, max={outcome.get('max')})"
        )
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index of the slider HANDLE (the "
                "draggable thumb), as shown in the latest screenshot's "
                "`V_n` listing. The tool automatically finds the adjacent "
                "slider_widget bbox (the track) for target-x computation."
            ),
        ),
        value=NumberSchema(
            description=(
                "Target value. Interpreted per value_mode. For 'absolute' "
                "pass the actual reading (e.g. 300 for $300/month); the "
                "tool reads min/max from the adjacent rendered label text. "
                "For 'ratio' pass 0.0–1.0 position along the track."
            ),
        ),
        value_mode=StringSchema(
            "'absolute' (default) or 'ratio'. Use 'ratio' when you can't "
            "read the min/max from the page.",
            nullable=True,
        ),
        value_min=NumberSchema(
            "Override for the slider's minimum if vision didn't surface it. "
            "Only used when value_mode='absolute'.",
            nullable=True,
        ),
        value_max=NumberSchema(
            "Override for the slider's maximum if vision didn't surface it. "
            "Only used when value_mode='absolute'.",
            nullable=True,
        ),
        required=["session_id", "vision_index", "value"],
    )
)

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index of the slider HANDLE (the "
                "draggable thumb), as shown in the latest screenshot's "
                "`V_n` listing. The tool automatically finds the adjacent "
                "slider_widget bbox (the track) for target-x computation."
            ),
        ),
        value=NumberSchema(
            description=(
                "Target value. Interpreted per value_mode. For 'absolute' "
                "pass the actual reading (e.g. 300 for $300/month); the "
                "tool reads min/max from the adjacent rendered label text. "
                "For 'ratio' pass 0.0–1.0 position along the track."
            ),
        ),
        value_mode=StringSchema(
            "'absolute' (default) or 'ratio'. Use 'ratio' when you can't "
            "read the min/max from the page.",
            nullable=True,
        ),
        value_min=NumberSchema(
            "Override for the slider's minimum if vision didn't surface it. "
            "Only used when value_mode='absolute'.",
            nullable=True,
        ),
        value_max=NumberSchema(
            "Override for the slider's maximum if vision didn't surface it. "
            "Only used when value_mode='absolute'.",
            nullable=True,
        ),
        required=["session_id", "vision_index", "value"],
    )
)
class BrowserSetSliderAtTool(Tool):
    name = "browser_set_slider_at"
    description = (
        "Drag a slider to a target value using its VISION bbox index. "
        "Prefer this over browser_set_slider when the page uses custom "
        "slider widgets (Chase/JPM calculators, filter ranges, any "
        "React/Angular slider with no native range input or aria-valuenow). "
        "Workflow: (1) call browser_screenshot → the vision agent emits "
        "role=slider_handle / slider_widget / text_block bboxes per "
        "slider; (2) pick the V_n of the HANDLE you want to move; (3) "
        "call this tool with the target numeric value. The tool finds "
        "the adjacent track, dispatches a humanised bezier drag, and "
        "returns the post-drag rendered label text so you can verify."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        vision_index: int,
        value: float,
        value_mode: str | None = None,
        value_min: float | None = None,
        value_max: float | None = None,
        **kw: Any,
    ) -> str:
        # Share the same lock as browser_drag_slider_until: the CDP/patchright
        # cursor is session-scoped, parallel drags clobber each other.
        if self.s.slider_drag_lock is None:
            self.s.slider_drag_lock = asyncio.Lock()
        async with self.s.slider_drag_lock:
            return await self._execute_inner(
                session_id, vision_index, value,
                value_mode, value_min, value_max,
            )

    async def _execute_inner(
        self,
        session_id: str,
        vision_index: int,
        value: float,
        value_mode: str | None,
        value_min: float | None,
        value_max: float | None,
    ) -> str:
        print(
            f"\n>> browser_set_slider_at(V{vision_index} → {value} "
            f"mode={value_mode or 'absolute'})"
        )
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        resp = self.s.vision_for_target_resolution()
        if resp is None:
            return (
                "[set_slider_at_failed:no_vision] No recent vision response. "
                "Call browser_screenshot first so slider bboxes are indexed."
            )

        handle_bbox = resp.get_bbox(int(vision_index))
        if handle_bbox is None:
            return (
                f"[set_slider_at_failed:bad_vision_index] V{vision_index} "
                f"is out of range (only {len(resp.bboxes)} bboxes cached)."
            )
        handle_role = (getattr(handle_bbox, "role", "") or "").lower()
        if handle_role not in ("slider_handle", "slider", "input"):
            # Accept other roles loudly but keep going — vision may
            # mis-tag a handle as 'input' or 'other'.
            print(
                f"   (note: V{vision_index} role={handle_role!r}, "
                "expected slider_handle — continuing)"
            )

        iw, ih = resp.image_width, resp.image_height
        if iw <= 0 or ih <= 0:
            return "[set_slider_at_failed:no_image_dims] Re-screenshot first."
        dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
        hx0, hy0, hx1, hy1 = handle_bbox.to_pixels(iw, ih, dpr=dpr_val)
        handle_pix = {"x": hx0, "y": hy0, "w": hx1 - hx0, "h": hy1 - hy0}
        handle_cy = hy0 + (hy1 - hy0) / 2.0

        # Find the associated track: nearest role=slider_widget whose
        # vertical centre sits within ±handle.h of the handle centre and
        # whose horizontal span encloses the handle.
        track_pix: dict[str, int] | None = None
        track_bbox = None
        best_dy: float | None = None
        for cand in resp.bboxes:
            if (getattr(cand, "role", "") or "").lower() != "slider_widget":
                continue
            cx0, cy0, cx1, cy1 = cand.to_pixels(iw, ih, dpr=dpr_val)
            ccy = cy0 + (cy1 - cy0) / 2.0
            dy = abs(ccy - handle_cy)
            if dy > max(hy1 - hy0, 24):
                continue
            if cx0 > hx0 or cx1 < hx1:
                # Track must enclose the handle horizontally.
                # (Handles at either extreme still sit within the track
                # bounds because the track includes min→max span.)
                continue
            if best_dy is None or dy < best_dy:
                best_dy = dy
                track_bbox = cand
                track_pix = {
                    "x": cx0, "y": cy0, "w": cx1 - cx0, "h": cy1 - cy0,
                }

        # Find the adjacent value-label text_block for min/max parsing +
        # before-readback. Heuristic: role=text_block whose centre Y is
        # within ±(handle.h + 40) of the handle's centre.
        label_text: str = ""
        for cand in resp.bboxes:
            if (getattr(cand, "role", "") or "").lower() != "text_block":
                continue
            cx0, cy0, cx1, cy1 = cand.to_pixels(iw, ih, dpr=dpr_val)
            ccy = cy0 + (cy1 - cy0) / 2.0
            if abs(ccy - handle_cy) <= (hy1 - hy0) + 40:
                label_text = (getattr(cand, "label", "") or "").strip()
                break

        mode = (value_mode or "absolute").lower()
        if mode == "ratio":
            ratio = max(0.0, min(1.0, float(value)))
        else:
            mn, mx = value_min, value_max
            if (mn is None or mx is None) and label_text:
                # Parse "0 to 10" / "$0 — $583" / "25 to 75" / "0 - 100"
                import re as _re
                nums = _re.findall(
                    r"-?\d+(?:\.\d+)?",
                    label_text.replace("$", "").replace("%", ""),
                )
                if len(nums) >= 2:
                    try:
                        parsed = [float(n) for n in nums[:2]]
                        mn = parsed[0] if mn is None else mn
                        mx = parsed[1] if mx is None else mx
                    except ValueError:
                        pass
            if mn is None or mx is None:
                return (
                    "[set_slider_at_failed:no_minmax] Cannot infer min/max "
                    "from adjacent label; pass value_min/value_max or use "
                    "value_mode='ratio'. label_seen="
                    + (repr(label_text) if label_text else "none")
                )
            span = float(mx) - float(mn)
            if abs(span) < 1e-9:
                ratio = 0.5
            else:
                ratio = (float(value) - float(mn)) / span
                ratio = max(0.0, min(1.0, ratio))

        if track_pix is None:
            # Fall back: use the handle's bbox as a tiny pseudo-track.
            # The drag still fires from the handle centre, but end = start,
            # so this is effectively a no-op. Return diagnostic.
            return (
                f"[set_slider_at_failed:no_track] Could not find a "
                f"role='slider_widget' bbox adjacent to V{vision_index}. "
                "Re-screenshot; if the issue persists, use browser_set_slider "
                "with a DOM selector instead."
            )

        payload = {"handle": handle_pix, "track": track_pix, "ratio": ratio}
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/set-slider-at",
            json=payload,
            timeout=30.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[set_slider_at_failed] {err}"
        data = r.json()
        outcome = data.get("outcome", {}) or {}
        self.s.record_step(
            "browser_set_slider_at",
            f"V{vision_index} → {value} (ratio={ratio:.3f})",
            data.get("url", ""),
        )
        lines = [
            f"Dragged slider V{vision_index} to {value} "
            f"(ratio={ratio:.2f}) via vision-drag",
            f"  handle={outcome.get('handle_bbox')}",
            f"  track={outcome.get('track_bbox')}",
            f"  target_px={outcome.get('target_px')}",
        ]
        if label_text:
            lines.append(f"  label_before={label_text!r}")
        lines.append(
            "  NEXT: call browser_screenshot to read "
            "the rendered post-drag value label."
        )
        if data.get("elements"):
            lines.append(str(data["elements"]))
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        required=["session_id"],
    )
)

class BrowserListSliderHandlesTool(Tool):
    name = "browser_list_slider_handles"
    description = (
        "Enumerate all slider handles on the page via DOM introspection "
        "(NO vision). Walks every frame, including cross-origin ones, "
        "and returns each handle's index, frame_url, kind, bbox (in "
        "document CSS pixels), and the closest row-level label text. "
        "Use this when vision is flaky or returning empty bboxes, or "
        "when you already know the slider's logical label (e.g. "
        "'Monthly contribution') and want to pick by text. Then pass "
        "the bbox directly into browser_drag_slider_until via the "
        "`handle_bbox` arg — skips the vision lookup entirely."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        session_id = self.s.resolve_session_id(session_id)
        print("\n>> browser_list_slider_handles()")
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/list-slider-handles",
            json={},
            timeout=20.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[list_slider_handles_failed] {err}"
        data = r.json() or {}
        handles = data.get("handles") or []
        if not handles:
            return (
                "[list_slider_handles:empty] No slider handles found in "
                "any frame. Page may still be loading — scroll or wait, "
                "then retry. If the page clearly has sliders, they may "
                "use non-standard markup; fall back to vision via "
                "browser_screenshot."
            )
        lines = [f"Found {len(handles)} slider handle(s):"]
        for h in handles:
            lines.append(
                f"  [{h.get('index')}] kind={h.get('kind')} "
                f"bbox={h.get('bbox')} "
                f"label={h.get('label', '')!r} "
                f"frame={(h.get('frame_url') or '')[:80]}"
            )
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index of the slider HANDLE "
                "(the draggable thumb) from the most recent screenshot. "
                "Either this OR handle_bbox_json is required."
            ),
            nullable=True,
        ),
        handle_bbox_json=StringSchema(
            description=(
                "Alternative to vision_index: pass the handle bbox "
                "directly as a JSON object {\"x\":.., \"y\":.., \"w\":.., \"h\":..} "
                "in CSS pixel document coords. Use this when vision is "
                "unreliable — call browser_list_slider_handles to get "
                "bboxes straight from the DOM, then pass one here."
            ),
            nullable=True,
        ),
        label_hint=StringSchema(
            description=(
                "Alternative discovery: a substring of the slider's label "
                "(e.g. 'Monthly contribution'). The tool will call "
                "browser_list_slider_handles, pick the handle whose label "
                "contains this hint (case-insensitive, whitespace collapsed), "
                "and use its bbox. Use when neither vision_index nor "
                "handle_bbox_json is convenient."
            ),
            nullable=True,
        ),
        target_value=NumberSchema(
            description=(
                "The numeric value to slide to. The tool drags the handle "
                "while watching the rendered label text, stopping when the "
                "label shows this value (±tolerance)."
            ),
        ),
        label_pattern=StringSchema(
            description=(
                "JS regex matching the rendered label — the FIRST capture "
                "group must be the numeric value. Example for Chase: "
                "'Monthly contribution[^:]*:\\s*\\$?(\\d+(?:\\.\\d+)?)'. "
                "If omitted, matches any number in a text node on the same "
                "visual row as the handle (works for most sliders with a "
                "single value near the track)."
            ),
            nullable=True,
        ),
        tolerance=NumberSchema(
            "Allowed |target - observed| gap. Default 0 (exact match).",
            nullable=True,
        ),
        max_iterations=IntegerSchema(
            "Safety cap on step iterations. Default 25.",
            nullable=True,
        ),
        step_px=IntegerSchema(
            "Initial pixel step. The tool auto-adapts from observed "
            "value-per-pixel sensitivity. Default 8.",
            nullable=True,
        ),
        direction=StringSchema(
            "'auto' (default; inferred from current vs target), 'left', 'right'.",
            nullable=True,
        ),
        required=["session_id", "target_value"],
    )
)

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index of the slider HANDLE "
                "(the draggable thumb) from the most recent screenshot. "
                "Either this OR handle_bbox_json is required."
            ),
            nullable=True,
        ),
        handle_bbox_json=StringSchema(
            description=(
                "Alternative to vision_index: pass the handle bbox "
                "directly as a JSON object {\"x\":.., \"y\":.., \"w\":.., \"h\":..} "
                "in CSS pixel document coords. Use this when vision is "
                "unreliable — call browser_list_slider_handles to get "
                "bboxes straight from the DOM, then pass one here."
            ),
            nullable=True,
        ),
        label_hint=StringSchema(
            description=(
                "Alternative discovery: a substring of the slider's label "
                "(e.g. 'Monthly contribution'). The tool will call "
                "browser_list_slider_handles, pick the handle whose label "
                "contains this hint (case-insensitive, whitespace collapsed), "
                "and use its bbox. Use when neither vision_index nor "
                "handle_bbox_json is convenient."
            ),
            nullable=True,
        ),
        target_value=NumberSchema(
            description=(
                "The numeric value to slide to. The tool drags the handle "
                "while watching the rendered label text, stopping when the "
                "label shows this value (±tolerance)."
            ),
        ),
        label_pattern=StringSchema(
            description=(
                "JS regex matching the rendered label — the FIRST capture "
                "group must be the numeric value. Example for Chase: "
                "'Monthly contribution[^:]*:\\s*\\$?(\\d+(?:\\.\\d+)?)'. "
                "If omitted, matches any number in a text node on the same "
                "visual row as the handle (works for most sliders with a "
                "single value near the track)."
            ),
            nullable=True,
        ),
        tolerance=NumberSchema(
            "Allowed |target - observed| gap. Default 0 (exact match).",
            nullable=True,
        ),
        max_iterations=IntegerSchema(
            "Safety cap on step iterations. Default 25.",
            nullable=True,
        ),
        step_px=IntegerSchema(
            "Initial pixel step. The tool auto-adapts from observed "
            "value-per-pixel sensitivity. Default 8.",
            nullable=True,
        ),
        direction=StringSchema(
            "'auto' (default; inferred from current vs target), 'left', 'right'.",
            nullable=True,
        ),
        required=["session_id", "target_value"],
    )
)
class BrowserDragSliderUntilTool(Tool):
    name = "browser_drag_slider_until"
    description = (
        "Closed-loop slider drag. Holds the mouse down on the handle, "
        "steps incrementally, reads the rendered value label from the "
        "iframe DOM after each step, and stops when the label shows the "
        "target value. THE right tool for custom widgets where vision "
        "can't reliably identify the full track geometry (Chase/JPM "
        "calculators, React/Angular sliders with no aria-valuenow). "
        "Unlike browser_set_slider_at (open-loop), this never overshoots "
        "and recovers automatically from non-linear widget scaling. "
        "Workflow: (1) browser_screenshot → vision returns slider_handle "
        "V_n values; (2) call this tool with vision_index=V_n and your "
        "numeric target; (3) inspect the returned trace + final_value to "
        "verify the label reached the target."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        target_value: float,
        vision_index: int | None = None,
        handle_bbox_json: str | None = None,
        label_hint: str | None = None,
        label_pattern: str | None = None,
        tolerance: float | None = None,
        max_iterations: int | None = None,
        step_px: int | None = None,
        direction: str | None = None,
        **kw: Any,
    ) -> str:
        gate = await _feedback_gate("browser_drag_slider_until")
        if gate:
            return gate
        ok, gate_msg = await _require_fresh_vision(
            self.s, session_id,
            reason=f"browser_drag_slider_until(target={target_value})",
        )
        if not ok:
            return gate_msg
        # Serialize drags on this session. If the LLM fired this tool in
        # parallel for multiple sliders, we queue them up so the cursor
        # owns one slider at a time. Without this lock, concurrent drags
        # fight for the same CDP mouse and produce garbage.
        if self.s.slider_drag_lock is None:
            self.s.slider_drag_lock = asyncio.Lock()
        async with self.s.slider_drag_lock:
            return await self._execute_inner(
                session_id, target_value, vision_index, handle_bbox_json,
                label_hint, label_pattern, tolerance, max_iterations,
                step_px, direction,
            )

    async def _resolve_handle_bbox(
        self, session_id: str,
        vision_index: int | None,
        handle_bbox_json: str | None,
        label_hint: str | None,
    ) -> tuple[dict[str, float] | None, str]:
        """Returns (bbox | None, source_description). Tries, in order:
        direct handle_bbox_json → label_hint via DOM enum → vision_index.
        Returns None + reason string on failure."""
        # 1. Direct bbox — highest priority, no indirection.
        if handle_bbox_json:
            try:
                bb = json.loads(handle_bbox_json)
            except (TypeError, ValueError) as exc:
                return None, f"bad handle_bbox_json: {exc}"
            if not isinstance(bb, dict):
                return None, "handle_bbox_json must decode to a dict"
            for k in ("x", "y", "w", "h"):
                if not isinstance(bb.get(k), (int, float)):
                    return None, f"handle_bbox_json missing numeric {k!r}"
            return bb, "handle_bbox_json"

        # 2. Label hint — DOM enum, pick best fuzzy match.
        if label_hint:
            try:
                r = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/list-slider-handles",
                    json={},
                    timeout=20.0,
                )
                handles = (r.json() or {}).get("handles") or [] if r.status_code < 400 else []
            except Exception as exc:
                return None, f"list-slider-handles failed: {exc}"
            if not handles:
                return None, "list-slider-handles returned no sliders"
            norm = label_hint.lower().strip()
            # Score each handle: label contains hint → big win; else token overlap.
            best = None
            best_score = -1.0
            for h in handles:
                lab = (h.get("label") or "").lower().strip()
                if not lab:
                    continue
                if norm in lab:
                    score = 1.0 + min(1.0, len(norm) / max(1, len(lab)))
                else:
                    ht = set(norm.split())
                    lt = set(lab.split())
                    if not ht:
                        continue
                    score = len(ht & lt) / len(ht)
                if score > best_score:
                    best_score = score
                    best = h
            if not best or best_score < 0.5:
                sample = [f"[{h.get('index')}] {h.get('label')!r}" for h in handles[:8]]
                return None, (
                    f"no handle label matched {label_hint!r}. "
                    f"candidates: {sample}"
                )
            return best.get("bbox"), f"label_hint={label_hint!r}"

        # 3. Vision index — legacy path.
        if vision_index is not None:
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return None, (
                    "no cached vision response (call browser_screenshot "
                    "first, or pass handle_bbox_json / label_hint)"
                )
            handle_bbox = resp.get_bbox(int(vision_index))
            if handle_bbox is None:
                return None, (
                    f"V{vision_index} out of range "
                    f"({len(resp.bboxes)} bboxes cached)"
                )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return None, "vision has no image dims; re-screenshot first"
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            hx0, hy0, hx1, hy1 = handle_bbox.to_pixels(iw, ih, dpr=dpr_val)
            return (
                {"x": hx0, "y": hy0, "w": hx1 - hx0, "h": hy1 - hy0},
                f"vision_index=V{vision_index}",
            )

        return None, "provide vision_index, handle_bbox_json, or label_hint"

    async def _execute_inner(
        self,
        session_id: str,
        target_value: float,
        vision_index: int | None,
        handle_bbox_json: str | None,
        label_hint: str | None,
        label_pattern: str | None,
        tolerance: float | None,
        max_iterations: int | None,
        step_px: int | None,
        direction: str | None,
    ) -> str:
        src = (
            f"V{vision_index}" if vision_index is not None
            else (f"hint={label_hint!r}" if label_hint
                  else ("bbox=json" if handle_bbox_json else "?"))
        )
        print(
            f"\n>> browser_drag_slider_until({src} → {target_value}"
            f"{' pattern=' + repr(label_pattern) if label_pattern else ''})"
        )
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        handle_pix, source_desc = await self._resolve_handle_bbox(
            session_id, vision_index, handle_bbox_json, label_hint,
        )
        if handle_pix is None:
            msg = f"[drag_slider_until_failed:no_handle] {source_desc}"
            print(f"   {msg}")
            return msg
        print(f"   resolved handle via {source_desc}: {handle_pix}")

        payload: dict[str, Any] = {
            "handle": handle_pix,
            "target_value": float(target_value),
        }
        if label_pattern is not None:
            payload["label_pattern"] = label_pattern
        if tolerance is not None:
            payload["tolerance"] = float(tolerance)
        if max_iterations is not None:
            payload["max_iterations"] = int(max_iterations)
        if step_px is not None:
            payload["step_px"] = int(step_px)
        if direction is not None:
            payload["direction"] = direction

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/drag-slider-until",
            json=payload,
            timeout=60.0,  # longer — closed-loop can take a few seconds
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[drag_slider_until_failed] {err}"
        data = r.json()
        out = data.get("outcome", {}) or {}
        self.s.record_step(
            "browser_drag_slider_until",
            f"{source_desc} → {target_value} in {out.get('iterations')} iters",
            data.get("url", ""),
        )
        # The drag moved the handle — treat as a page mutation so the
        # next click waits for fresh vision of the post-drag DOM.
        self.s.advance_observation_token("drag_slider_until")
        _schedule_vision_prefetch(self.s, session_id)
        final_v = out.get("final_value")
        init_v = out.get("initial_value")
        completed = bool(out.get("completed"))
        lines: list[str] = []
        if not completed:
            # Prefix with the FAILED tag so the agent treats this as a
            # loud failure (same convention as all other _failed returns).
            if init_v is None:
                reason = "initial_readback_failed"
            elif final_v is None:
                reason = "value_lost_mid_drag"
            else:
                reason = "target_not_reached"
            lines.append(
                f"[drag_slider_until_failed:{reason}] "
                f"{source_desc} target={target_value} "
                f"final={final_v} initial={init_v}"
            )
        else:
            lines.append(
                f"Closed-loop slider {source_desc} → target={target_value} "
                f"COMPLETED in {out.get('iterations')} iterations: "
                f"{init_v} → {final_v}"
            )
        lines.append(f"  label_text={out.get('label_text')!r}")
        trace = out.get("trace") or []
        if trace:
            lines.append("  trace (last 4):")
            for row in trace[-4:]:
                lines.append(
                    f"    iter={row.get('iter')} "
                    f"cursor_x={row.get('cursor_x')} "
                    f"value={row.get('value')}"
                )
        if not completed:
            lines.append(
                "  Fix: if label_text shows NO_MATCH, your label_pattern "
                "regex didn't match any nearby text — the 'nearby text' "
                "list in label_text shows what IS there. Adjust the regex. "
                "If label_text looks right but target wasn't reached, widen "
                "tolerance or raise max_iterations. Always call sliders "
                "SEQUENTIALLY — never in a parallel batch."
            )
        if data.get("elements"):
            lines.append(str(data["elements"]))
        return _maybe_no_effect_prefix(
            data, "browser_drag_slider_until", "\n".join(lines),
            session_state=self.s,
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        bbox_json=StringSchema(
            "JSON object {x, y, w, h} describing the viewport region to crop, in CSS pixels."
        ),
        quality=IntegerSchema("JPEG quality 1–100, default 80.", nullable=True),
        required=["session_id", "bbox_json"],
    )
)

class BrowserImageRegionTool(Tool):
    name = "browser_image_region"
    description = (
        "Screenshot a bounded region of the viewport and return base64 JPEG. "
        "Cheaper than a full-page Gemini pass for solvers that need to "
        "template-match a captcha piece, OCR a small area, or run a tiny "
        "focused vision query."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        bbox_json: str,
        quality: int | None = None,
        **kw: Any,
    ) -> str:
        try:
            bbox = json.loads(bbox_json)
        except (TypeError, ValueError) as exc:
            return f"[image_region_failed] bbox_json is not valid JSON: {exc}"
        for k in ("x", "y", "w", "h"):
            if not isinstance(bbox.get(k), (int, float)):
                return f"[image_region_failed] bbox must have numeric {k}"

        payload: dict[str, Any] = {"bbox": bbox}
        if quality is not None:
            payload["quality"] = quality
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/image-region",
            json=payload,
            timeout=15.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[image_region_failed] {err}"
        data = r.json()
        b64 = data.get("base64", "")
        return (
            f"image_region: {bbox['w']}x{bbox['h']} at ({bbox['x']},{bbox['y']}), "
            f"base64_len={len(b64)}\n{b64[:200]}..."
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        hint=StringSchema(
            "Optional solver name to force (chess_com, slider_captcha, jigsaw_captcha, "
            "rotation_captcha, grid_drag). Skip for auto-detect.",
            nullable=True,
        ),
        max_steps=IntegerSchema(
            "Maximum solver iterations. Default 10 — enough for most chess puzzles "
            "and captchas; increase for long puzzle lines.",
            nullable=True,
        ),
        required=["session_id"],
    )
)

@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        hint=StringSchema(
            "Optional solver name to force (chess_com, slider_captcha, jigsaw_captcha, "
            "rotation_captcha, grid_drag). Skip for auto-detect.",
            nullable=True,
        ),
        max_steps=IntegerSchema(
            "Maximum solver iterations. Default 10 — enough for most chess puzzles "
            "and captchas; increase for long puzzle lines.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserSolvePuzzleTool(Tool):
    name = "browser_solve_puzzle"
    description = (
        "Auto-detect the puzzle on the current page (chess position, "
        "slider/jigsaw/rotation captcha, generic grid-drag) and run a "
        "dedicated solver through extract → plan → execute → verify. "
        "Uses selector- and coordinate-exact primitives under the hood "
        "(zero Gemini round-trips in the move loop). Use whenever the "
        "page presents a puzzle-like challenge."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        hint: str | None = None,
        max_steps: int | None = None,
        **kw: Any,
    ) -> str:
        print(f"\n>> browser_solve_puzzle(session={session_id}, hint={hint!r}, max_steps={max_steps})")
        from superbrowser_bridge.puzzle_solvers import detect as _detect, solve as _solve
        from superbrowser_bridge.puzzle_solvers.browser import HttpSolverBrowser

        # Pull a DOM snapshot + URL to feed the detector (cheap GET).
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                state_resp = await client.get(
                    f"{SUPERBROWSER_URL}/session/{session_id}/state",
                    params={"vision": "false"},
                )
                state_resp.raise_for_status()
                state_data = state_resp.json()
        except Exception as exc:
            return f"[solve_puzzle_failed] cannot read session state: {exc}"

        url = state_data.get("url", "") or ""
        dom_snippet = state_data.get("elements") or ""

        solver, conf = _detect(url, dom_snippet, hint=hint)
        if solver is None:
            return (
                "[solve_puzzle_no_match] No solver matched the current page "
                f"(url={url!r}, confidence={conf:.2f}). Pass hint=<solver name> "
                "to force, or implement a new solver for this page type."
            )

        print(f">> selected solver: {solver.name} (confidence={conf:.2f})")
        async with HttpSolverBrowser(session_id, SUPERBROWSER_URL) as browser:
            result = await _solve(solver, browser, max_steps=max_steps or 10)

        self.s.record_step(
            "browser_solve_puzzle",
            f"{solver.name} success={result.success} steps={result.steps_taken}",
            url,
        )
        lines = [
            f"Puzzle solver: {result.solver}",
            f"Success: {result.success}",
            f"Steps taken: {result.steps_taken}",
        ]
        if result.error:
            lines.append(f"Error: {result.error}")
        if result.actions:
            lines.append(f"Actions ({len(result.actions)}):")
            for a in result.actions[:8]:
                lines.append(f"  - {a.kind}: {a.reason or ''}")
            if len(result.actions) > 8:
                lines.append(f"  … ({len(result.actions) - 8} more)")
        if result.final_state:
            # Strip large base64 payloads before logging.
            redacted = {
                k: (f"<{len(v)} bytes>" if isinstance(v, str) and len(v) > 200 else v)
                for k, v in result.final_state.items()
            }
            lines.append(f"Final state: {redacted}")
        return "\n".join(lines)


# ─── Arch v3: TaskBrief + state-check + verify-action tools ──────────


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        expected=StringSchema(
            "Optional: short description of what state you expect "
            "(e.g. 'WiFi filter applied; results <100'). Helps the "
            "vision agent target its observation.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserStateCheckTool(Tool):
    """Cheap state-only vision call — returns PageState + flags, no bboxes.

    Use when you want to verify "where am I / did my filter take effect"
    without committing to a click. ~30% the token cost of a full
    browser_screenshot. Bypasses the actions_since_screenshot==0 gate
    because it's an explicit observation hop, not amnesia.
    """

    name = "browser_state_check"
    description = (
        "Quick state-only observation — returns funnel position, active "
        "filters, result counts, last-action verdict, stuck indicators. "
        "No bboxes (use browser_screenshot when you need click targets). "
        "Lower cost than browser_screenshot; use it freely to verify "
        "constraint progress."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        expected: str | None = None,
        **kw: Any,
    ) -> Any:
        session_id = self.s.resolve_session_id(session_id)
        intent = "state_check"
        if expected:
            intent = f"state_check: {expected[:200]}"
        peek_hash = ""
        try:
            peek_elements = await _fetch_elements(session_id, self.s)
            peek_hash = BrowserSessionState.hash_page_content(peek_elements)
        except Exception:
            pass
        allowed, reason = self.s.should_allow_screenshot(
            self.s.current_url, peek_hash, intent=intent,
        )
        if not allowed:
            return reason
        self.s.screenshot_budget -= 1
        try:
            r = await _request_with_backoff(
                "GET",
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true", "bounds": "true"},
                timeout=15.0,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            return (
                f"[state_check_failed] {type(exc).__name__}: {str(exc)[:200]}"
            )
        b64 = data.get("screenshot") or ""
        if not b64:
            return "[state_check_failed] no screenshot in /state response"
        elements = data.get("elements", "") or ""
        url = data.get("url") or self.s.current_url
        return await self.s.build_tool_result_blocks(
            b64,
            f"state_check: {expected[:120]}" if expected else "state_check",
            intent=intent,
            url=url,
            elements=elements,
            elements_with_bounds=data.get("selectorEntries"),
            device_pixel_ratio=float(data.get("devicePixelRatio") or 1.0),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        expected=StringSchema(
            "What you expected the previous action to do (e.g. "
            "'modal closes and toast says Saved', 'WiFi chip becomes "
            "active and result count drops')."
        ),
        bbox_ref=IntegerSchema(
            "Optional 1-based V_n you clicked. Helps vision focus on the "
            "right area for the verdict.",
            nullable=True,
        ),
        required=["session_id", "expected"],
    )
)
class BrowserVerifyActionTool(Tool):
    """Post-action verifier — did the previous click/type produce the
    expected outcome?

    Returns a verdict (succeeded | failed | uncertain) and a
    recommendation (continue | undo | retry) plus a PageState snapshot.
    The brain decides what to do with the recommendation — this tool
    never auto-undoes anything.
    """

    name = "browser_verify_action"
    description = (
        "Verify the previous action produced the expected outcome. "
        "Returns action_outcome + recommendation + page_state. Use "
        "after risky clicks on dense scenes; cheap form is "
        "browser_state_check."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        expected: str,
        bbox_ref: int | None = None,
        **kw: Any,
    ) -> Any:
        session_id = self.s.resolve_session_id(session_id)
        intent = f"verify_action: {expected[:200]}"
        peek_hash = ""
        try:
            peek_elements = await _fetch_elements(session_id, self.s)
            peek_hash = BrowserSessionState.hash_page_content(peek_elements)
        except Exception:
            pass
        allowed, reason = self.s.should_allow_screenshot(
            self.s.current_url, peek_hash, intent=intent,
        )
        if not allowed:
            return reason
        self.s.screenshot_budget -= 1
        try:
            r = await _request_with_backoff(
                "GET",
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true", "bounds": "true"},
                timeout=15.0,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            return (
                f"[verify_action_failed] {type(exc).__name__}: {str(exc)[:200]}"
            )
        b64 = data.get("screenshot") or ""
        if not b64:
            return "[verify_action_failed] no screenshot in /state response"
        elements = data.get("elements", "") or ""
        url = data.get("url") or self.s.current_url
        result_blocks = await self.s.build_tool_result_blocks(
            b64,
            f"verify_action: expected={expected[:160]}",
            intent=intent,
            url=url,
            elements=elements,
            elements_with_bounds=data.get("selectorEntries"),
            device_pixel_ratio=float(data.get("devicePixelRatio") or 1.0),
        )
        verifier_outcome = ""
        try:
            from superbrowser_bridge.action_verifier import (
                build_verifier_result,
                render_verifier_text,
            )
            resp = self.s._last_vision_response
            verifier = build_verifier_result(
                resp,
                expected=expected,
                pre_summary=getattr(self.s, "_last_vision_summary", "") or "",
            )
            verifier_outcome = verifier.get("action_outcome") or ""
            verify_text = render_verifier_text(verifier)
            if isinstance(result_blocks, list) and verify_text:
                if result_blocks and result_blocks[0].get("type") == "text":
                    result_blocks[0]["text"] = (
                        result_blocks[0]["text"] + "\n\n" + verify_text
                    )
                else:
                    result_blocks.insert(0, {"type": "text", "text": verify_text})
        except Exception as exc:
            print(f"  [verify_action_render: skipped — {exc}]")
        try:
            self.s.interaction_ledger.append({
                "tool": "browser_verify_action",
                "bbox": bbox_ref,
                "expected": expected[:120],
                "outcome": verifier_outcome,
            })
            if len(self.s.interaction_ledger) > self.s._INTERACTION_LEDGER_CAP:
                self.s.interaction_ledger = self.s.interaction_ledger[
                    -self.s._INTERACTION_LEDGER_CAP :
                ]
        except Exception:
            pass
        return result_blocks


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        constraint_updates=ArraySchema(
            description="Constraint updates to apply.",
            items=ObjectSchema(
                canonical_value=StringSchema(
                    "Canonical name of the constraint to update "
                    "(matches existing TaskBrief constraints by canonical_value)."
                ),
                status=StringSchema(
                    "New status: unverified | satisfied | failed | not_applicable"
                ),
                evidence=StringSchema(
                    "Short justification for the new status",
                    nullable=True,
                ),
                required=["canonical_value", "status"],
            ),
            nullable=True,
        ),
        cot_note=StringSchema(
            "Short chain-of-thought note (≤200 chars) about the "
            "current strategy / next intent. Appears in the brief.",
            nullable=True,
        ),
        new_constraint=ObjectSchema(
            text=StringSchema("Verbatim phrase from the user's query."),
            kind=StringSchema(
                "filter | attribute | negative | numeric | ordering"
            ),
            canonical_value=StringSchema(
                "Lowercase canonical name (e.g. 'wifi', 'price')."
            ),
            operator=StringSchema(
                "eq|lte|gte|contains|not|ascending|descending",
                nullable=True,
            ),
            threshold=StringSchema("Numeric threshold", nullable=True),
            unit=StringSchema("USD|stars|miles", nullable=True),
            description=(
                "Optional: a constraint the brief missed at extraction "
                "time. Use sparingly — extraction usually catches them."
            ),
            nullable=True,
            required=["text", "kind", "canonical_value"],
        ),
        required=["session_id"],
    )
)
class BrowserUpdateTaskBriefTool(Tool):
    """Update the TaskBrief mid-task.

    Use when:
      - The brain learned a constraint cannot be satisfied on this site
        (mark not_applicable with evidence).
      - A new constraint surfaced from inspecting the page (rare).
      - You want to record a chain-of-thought note for the next worker.
    """

    name = "browser_update_task_brief"
    description = (
        "Update TaskBrief constraints + append a chain-of-thought note. "
        "The brief is the brain's persistent working memory; updates "
        "survive session restarts."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        constraint_updates: list[dict] | None = None,
        cot_note: str | None = None,
        new_constraint: dict | None = None,
        **kw: Any,
    ) -> str:
        brief = getattr(self.s, "task_brief", None)
        if brief is None:
            return (
                "[update_task_brief_failed] No TaskBrief on this session. "
                "It is built by the orchestrator at delegation time; if "
                "you see this on a fresh worker, the orchestrator path "
                "is misconfigured."
            )
        applied = 0
        notes: list[str] = []
        for upd in constraint_updates or []:
            if not isinstance(upd, dict):
                continue
            cv = (upd.get("canonical_value") or "").strip().lower()
            if not cv:
                continue
            idx = brief.find_constraint_by_canonical(cv)
            if idx < 0:
                notes.append(f"  - skipped {cv!r}: no matching constraint")
                continue
            status = upd.get("status") or ""
            evidence = upd.get("evidence") or ""
            if brief.mark_constraint(idx, status, evidence, self.s.current_url):
                applied += 1
        if new_constraint and isinstance(new_constraint, dict):
            try:
                from superbrowser_bridge.task_brief import Constraint
                brief.constraints.append(Constraint.from_dict(new_constraint))
                brief.version += 1
                applied += 1
            except Exception as exc:
                notes.append(f"  - skipped new_constraint: {exc}")
        if cot_note:
            turn = getattr(self.s, "_brain_turn_counter", 0) or 0
            brief.add_cot_note(turn, cot_note)
        total, sat, fail = brief.counts()
        return (
            f"[task_brief_updated] {applied} change(s) applied. "
            f"constraints={sat}/{total} satisfied"
            + (f", {fail} failed" if fail else "")
            + ("\n" + "\n".join(notes) if notes else "")
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        focus_constraint_idx=IntegerSchema(
            "1-based index of the TaskBrief constraint you're attacking "
            "next. Defaults to the system's current_focus_idx (the "
            "[FOCUS] line). Pass -1 to use the system recommendation.",
            nullable=True,
        ),
        planned_tool=StringSchema(
            "The tool you're about to call after this preplan. Pick the "
            "lowest tier on the TOOL LADDER that fits: click_at | "
            "click_selector | type_at | type | scroll | "
            "browser_run_script | browser_navigate | etc."
        ),
        planned_target_label=StringSchema(
            "Visible label of the element you're targeting (e.g. "
            "'WiFi filter chip', 'Search button'). Required for "
            "click_at / click_selector / type_at — the system uses it "
            "to ratchet the tool ladder per-target."
        ),
        planned_target_vision_index=IntegerSchema(
            "1-based V_n index from the latest screenshot, when known. "
            "-1 if you don't have a V_n (e.g. selector / script paths).",
            nullable=True,
        ),
        expected_outcome=StringSchema(
            "One-sentence prediction of what should happen (e.g. "
            "'WiFi chip becomes checked and result count drops', "
            "'Modal closes and toast says Saved'). Compared against "
            "vision verdict to confirm progress."
        ),
        expected_postcondition=StringSchema(
            "Postcondition kind: 'bbox_state_change' (toggles, "
            "checkboxes, filter chips), 'dom_mutated' (generic clicks), "
            "'url_changed' (navigations), or omit for auto-derive from "
            "planned_tool.",
            nullable=True,
        ),
        required=["session_id", "planned_tool", "expected_outcome"],
    )
)
class BrowserPreplanTool(Tool):
    """Declare your next action against the TaskBrief before mutating
    the page.

    The preplan gate refuses every state-change tool unless a fresh
    preplan has been declared since the last action. This forces the
    *vision → preplan → action → verify* ritual: you cannot drift off
    the user's query because every action ties back to a constraint
    and an expected outcome the system can verify.

    Lower-cost replacement for the "guess and check" loop: a 1-line
    declaration costs ~50 tokens; a misclick that wastes an iteration
    costs hundreds. Call this BETWEEN every fresh screenshot and the
    next mutating tool.
    """

    name = "browser_preplan"
    description = (
        "Declare your next action: which TaskBrief constraint you're "
        "attacking, which tool, what target, and what should happen. "
        "Required before every state-change tool (click/type/drag/"
        "navigate/run_script). Cheap discipline that prevents "
        "hallucinated progress on multi-constraint queries."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    def _check_tool_ladder(
        self,
        planned_tool: str,
        planned_target_label: str,
        resolved_focus: int,
    ) -> str | None:
        """Arch v4 Move 4 — refuse a higher-tier tool when lower tiers
        haven't been attempted-and-failed for the same target. Returns
        the refusal message on block, or None to allow.

        Tier rules:
          1 (click_at, type_at, etc.) — always allowed.
          2 (click_selector, type, etc.) — allowed if Tier 1 has
            ≥1 attempt OR the brain explicitly notes "no V_n with
            this label" (we approximate via planned_target_vision_index
            == -1, which is the brain's signal that vision missed it).
          3 (run_script, eval) — allowed only when Tier 1 + Tier 2
            both attempted with at least one failure each on the same
            target.
          4 (browser_navigate) — only enforced for SAME-DOMAIN
            navigates after cold-start. Cross-domain navigates and
            cold-start navigates bypass entirely.

        Each tier has a kill switch: TOOL_LADDER_TIER2=0,
        TOOL_LADDER_TIER3=0, TOOL_LADDER_TIER4=0. The whole layer can
        be disabled via TOOL_LADDER=0.
        """
        if os.environ.get("TOOL_LADDER", "1") == "0":
            return None
        s = self.s
        tier = s._tier_for_tool(planned_tool)
        if tier <= 1 or tier == 0:
            return None  # Tier 1 always allowed; observation tools too.

        # Ledger lookup: synthesize the key the way record_step would,
        # using a transient pseudo-lock since the real lock isn't set
        # until after this check passes.
        pseudo_lock = PreplanLock(
            focus_constraint_idx=int(resolved_focus),
            planned_tool=planned_tool,
            planned_target_label=planned_target_label,
        )
        key = s._ledger_key_for_lock(pseudo_lock)
        rec = s.tool_attempts.get(key, {})
        t1_attempts = rec.get("tier1", 0)
        t1_failed = rec.get("tier1_failed", 0)
        t2_attempts = rec.get("tier2", 0)
        t2_failed = rec.get("tier2_failed", 0)

        sid = s.session_id or "<session_id>"

        if tier == 2:
            if os.environ.get("TOOL_LADDER_TIER2", "1") == "0":
                return None
            # Allowed if Tier 1 attempted, OR brain signalled "vision
            # missed this label" (planned_target_vision_index == -1
            # in the lock — but we don't have access here without
            # another arg; fall back to "is_cold_start" + no V_n in
            # latest vision response heuristic — checked by the
            # selector validation gate in session_tools instead).
            # To keep the gate surgical, allow Tier 2 when EITHER
            # condition holds: t1_attempts > 0 OR vision-missed (the
            # selector tool's own gates already enforce
            # SELECTOR_VISION_ALIGNMENT for the missing-label case,
            # so we don't double-enforce here).
            return None

        if tier == 3:
            if os.environ.get("TOOL_LADDER_TIER3", "1") == "0":
                return None
            if t1_failed >= 1 and t2_failed >= 1:
                return None
            return (
                f"[preplan_ladder_violation tier=3 tool={planned_tool!r} "
                f"target={planned_target_label!r}]\n"
                f"You declared a Tier-3 tool (browser_run_script / "
                f"browser_eval) for this target, but the cursor ladder "
                f"hasn't been exhausted. Current per-target ledger:\n"
                f"  Tier 1 (click_at/type_at): {t1_attempts} attempts, "
                f"{t1_failed} failed\n"
                f"  Tier 2 (click_selector/type): {t2_attempts} "
                f"attempts, {t2_failed} failed\n"
                f"JS dispatches (isTrusted=false) trip Akamai/"
                f"PerimeterX/DataDome. Cursor clicks don't. Re-call "
                f"browser_preplan(session_id='{sid}', planned_tool="
                f"'click_at', planned_target_label="
                f"'{planned_target_label}', ...) — try a fresh V_n "
                f"first. Override: TOOL_LADDER_TIER3=0."
            )

        if tier == 4:
            if os.environ.get("TOOL_LADDER_TIER4", "1") == "0":
                return None
            # Same-domain only. Cross-domain navigates are legit
            # multi-site flows; cold-start sessions also bypass.
            if s.is_cold_start:
                return None
            current = (s.current_url or "").strip()
            # Compare hosts; "" current_url means we don't know yet,
            # so let it through (caller can re-gate).
            try:
                from urllib.parse import urlsplit
                cur_host = urlsplit(current).netloc.lower()
            except Exception:
                cur_host = ""
            # Brain's planned_target_label often holds the target URL
            # for navigate calls; check it against current host.
            target = (planned_target_label or "").strip()
            try:
                from urllib.parse import urlsplit
                tgt_host = urlsplit(target).netloc.lower()
            except Exception:
                tgt_host = ""
            same_domain = bool(
                cur_host and tgt_host and cur_host == tgt_host
            )
            if not same_domain:
                # Cross-domain or unknown — allow.
                return None
            # Same-domain: require ≥1 cursor tactic to have failed.
            if t1_failed >= 1 or t2_failed >= 1:
                return None
            return (
                f"[preplan_ladder_violation tier=4 tool=browser_navigate "
                f"target={planned_target_label!r}]\n"
                f"You declared a same-domain navigate, but no cursor "
                f"tactic has failed for the current target yet. "
                f"Same-domain navigates skip click handlers and look "
                f"like a bot to anti-fraud systems. On the current "
                f"page, try clicking the visible link/button via "
                f"browser_click_at(V_n) first. Override: "
                f"TOOL_LADDER_TIER4=0."
            )

        return None  # unreachable

    async def execute(
        self,
        session_id: str,
        planned_tool: str,
        expected_outcome: str,
        focus_constraint_idx: int | None = None,
        planned_target_label: str = "",
        planned_target_vision_index: int = -1,
        expected_postcondition: str | None = None,
        **kw: Any,
    ) -> str:
        if os.environ.get("PREPLAN_GATE", "1") == "0":
            # Even with the gate disabled, the tool still records the
            # declaration so downstream verification + ratchet logic
            # have something to work with.
            pass
        brief = getattr(self.s, "task_brief", None)
        # Resolve focus index. Brain may pass 1-based (UI form) or -1
        # (default). Convert to 0-based and validate.
        if focus_constraint_idx is None or focus_constraint_idx == -1:
            sys_focus = getattr(brief, "current_focus_idx", -1) if brief else -1
            resolved_focus = sys_focus
        else:
            # Brain passes 1-based; store 0-based.
            resolved_focus = int(focus_constraint_idx) - 1
        warnings: list[str] = []
        if brief is not None and brief.constraints:
            if not (0 <= resolved_focus < len(brief.constraints)):
                warnings.append(
                    f"focus_constraint_idx out of range "
                    f"(got {focus_constraint_idx}, brief has "
                    f"{len(brief.constraints)} constraints); using "
                    f"system focus {brief.current_focus_idx + 1}."
                )
                resolved_focus = brief.current_focus_idx
            else:
                c = brief.constraints[resolved_focus]
                if c.status == "satisfied":
                    warnings.append(
                        f"focus #{resolved_focus + 1} {c.canonical_value!r} "
                        f"is already satisfied; consider picking the "
                        f"system focus #{brief.current_focus_idx + 1} "
                        f"instead."
                    )
                elif c.status == "failed":
                    warnings.append(
                        f"focus #{resolved_focus + 1} {c.canonical_value!r} "
                        f"was previously marked failed; if you're "
                        f"retrying intentionally, proceed."
                    )

        # Auto-derive expected_postcondition when omitted. Heuristic:
        # navigate → url_changed; click on label that hints stateful
        # control → bbox_state_change; everything else → dom_mutated.
        derived_pc = (expected_postcondition or "").strip().lower()
        if not derived_pc:
            tname = (planned_tool or "").lower()
            label_lower = (planned_target_label or "").lower()
            if "navigate" in tname:
                derived_pc = "url_changed"
            elif (
                "click" in tname
                and any(
                    h in label_lower for h in (
                        "toggle", "switch", "checkbox", "radio",
                        "filter chip", "filter:", "chip", " on ", " off ",
                    )
                )
            ):
                derived_pc = "bbox_state_change"
            else:
                derived_pc = "dom_mutated"

        # Arch v4 Move 4 — TOOL LADDER ratchet enforcement. Before we
        # lock the plan, check whether the declared tool is allowed for
        # the current target given prior attempts. Refuses Tier 3
        # (run_script) until Tier 1 + Tier 2 both attempted-and-failed
        # for the same target; refuses Tier 4 (same-domain navigate)
        # unless ≥1 cursor tactic failed OR the session is cold-start.
        # Cross-domain navigate bypasses Tier 4 entirely.
        ladder_refusal = self._check_tool_ladder(
            planned_tool=str(planned_tool or ""),
            planned_target_label=str(planned_target_label or ""),
            resolved_focus=resolved_focus,
        )
        if ladder_refusal is not None:
            return ladder_refusal

        # Build and stash the lock.
        iter_n = getattr(self.s, "_brain_turn_counter", 0) or 0
        lock = PreplanLock(
            focus_constraint_idx=int(resolved_focus),
            planned_tool=str(planned_tool or ""),
            planned_target_label=str(planned_target_label or "")[:120],
            planned_target_vision_index=int(planned_target_vision_index),
            expected_outcome=str(expected_outcome or "")[:240],
            expected_postcondition=str(derived_pc)[:32],
            set_at_iter=int(iter_n),
        )
        self.s.preplan_lock = lock
        self.s.preplan_lock_consumed = False
        self.s.preplan_consecutive_refusals = 0

        # Compose the confirmation. Brain reads this and proceeds to
        # the declared tool call.
        focus_text = ""
        if brief is not None and 0 <= resolved_focus < len(brief.constraints):
            fc = brief.constraints[resolved_focus]
            focus_text = (
                f"focus=#{resolved_focus + 1} "
                f"{fc.canonical_value!r} ({fc.kind}, {fc.status})"
            )
        elif brief is not None and brief.constraints:
            focus_text = "focus=<unset> (no constraint resolved)"
        else:
            focus_text = "focus=<no brief>"
        target_bit = ""
        if planned_target_label:
            target_bit = f" target={planned_target_label!r}"
            if planned_target_vision_index and planned_target_vision_index > 0:
                target_bit += f" V_{planned_target_vision_index}"
        warn_bit = ""
        if warnings:
            warn_bit = "\n  warnings: " + "; ".join(warnings)
        return (
            f"[preplan_locked] {focus_text}, tool={planned_tool!r}"
            f"{target_bit}, expect={derived_pc!r}, "
            f"outcome={expected_outcome[:120]!r}.{warn_bit}\n"
            f"You may now call {planned_tool} once. The next state-"
            f"change tool will consume this lock; after it runs you'll "
            f"need to re-preplan."
        )


def register_session_tools(bot: "Nanobot", state: BrowserSessionState | None = None) -> BrowserSessionState:
    """Register all browser session tools with a nanobot instance.

    Args:
        bot: The Nanobot instance to register tools on.
        state: Optional shared state. If None, creates a new one.

    Returns:
        The BrowserSessionState used (for external access if needed).
    """
    if state is None:
        state = BrowserSessionState()

    # Arch v4 Phase G — narrow the click family. Live runs showed the
    # brain drifting to click_selector → eval → navigate when click_at
    # would have worked, because the inventory had multiple click
    # variants. Per user direction: ONE click tool only (vision bbox),
    # plus targeted-only scroll. Type, select, and slider tools keep
    # both their DOM-index and vision-bbox variants — the user said
    # "typing tool is necessary; slider and others are necessary".
    #
    # Removed from registration (classes still exist for tests +
    # legacy paths): BrowserClickTool (raw coords),
    # BrowserClickSelectorTool (DOM selector), BrowserScrollTool
    # (untargeted scroll — use scroll_until). Override:
    # REGISTER_LEGACY_TOOLS=1 to restore them (debug only).
    register_legacy = os.environ.get("REGISTER_LEGACY_TOOLS") == "1"
    # Arch v4.2: trimmed tool surface. The trace from
    # the wineaccess run showed the brain calling 5 meta-tools per
    # click (preplan + state_check + look_again + verify_action +
    # screenshot/get_markdown), each adding a big block of text it
    # then had to read. Redundant meta-vision tools were the root of
    # "too much extra information." OLD lightweight architecture
    # (`/root/runagent-superbrowser/`) had only screenshot +
    # get_markdown for observation; we mirror that here. The classes
    # for the removed tools stay in code so REGISTER_LEGACY_V4_TOOLS=1
    # can re-enable them for A/B comparison.
    tools = [
        BrowserOpenTool(state),
        # Arch v4.4: BrowserNavigateTool is NOT registered by default.
        # browser_open handles cold-start navigation; everything else
        # should go through browser_click_at on visible V_n bboxes.
        # In every real-run trace, in-task browser_navigate calls were
        # hallucinated URLs (constructed paths like
        # /collections/white-wine, /search?query=..., /store/white-wine?region=)
        # that 404'd or redirected, burning the screenshot budget.
        # Re-enable via REGISTER_BROWSER_NAVIGATE=1 for debugging.
        # BrowserNavigateTool(state),
        BrowserScreenshotTool(state),
        # BrowserLookAgainTool removed: just call browser_screenshot.
        BrowserClickAtTool(state),         # ONE click tool — vision bbox
        BrowserTypeAtTool(state),          # vision-bbox type
        BrowserFixTextAtTool(state),       # vision-bbox text correction
        BrowserTypeTool(state),            # DOM-index type
        BrowserKeysTool(state),            # Enter / Escape / Tab keys
        BrowserScrollUntilTool(state),     # targeted scroll-to-text only
        BrowserSelectTool(state),
        BrowserSelectOptionTool(state),
        BrowserFormPlanTool(state),
        BrowserEvalTool(state),            # Tier 3 — gated
        BrowserRunScriptTool(state),       # Tier 3 — gated
        BrowserWaitForTool(state),
        BrowserDragTool(state),            # vision-anchored drag
        BrowserGetRectTool(state),         # DOM rect helper
        BrowserDragSelectorsTool(state),   # selector-based drag
        BrowserDragPathTool(state),        # polyline drag (puzzles)
        BrowserSetSliderTool(state),       # DOM-index slider
        BrowserSetSliderAtTool(state),     # vision-bbox slider
        BrowserListSliderHandlesTool(state),
        BrowserDragSliderUntilTool(state),
        BrowserImageRegionTool(state),     # detail vision on a region
        BrowserSolvePuzzleTool(state),
        BrowserGetMarkdownTool(),          # stateless text extraction
        BrowserDialogTool(),               # stateless
        BrowserDetectCaptchaTool(state),
        BrowserCaptchaScreenshotTool(state),
        BrowserSolveCaptchaTool(state),
        BrowserAskUserTool(state),
        BrowserVerifyFactTool(state),
        BrowserRequestHelpTool(state),
        BrowserEscalateTool(state),        # t1 → t3 migration
        BrowserPlanNextStepsTool(state),   # hierarchical planner
        # Arch v4.1 (Fix 2a): set_task_plan/plan_replan/plan_skip_step
        # gone — TaskBrief.checklist is the single source of truth.
        # Arch v4.2: state_check / verify_action / look_again /
        # update_task_brief / preplan also removed from default
        # registration. Constraint flips happen automatically via
        # reconcile_from_page_state + reconcile_from_url; there's
        # nothing for the brain to manually verify or update. Brain
        # observes via browser_screenshot, acts via browser_click_at,
        # repeats. Re-enable with REGISTER_LEGACY_V4_TOOLS=1.
        # BrowserStateCheckTool(state),
        # BrowserVerifyActionTool(state),
        # BrowserUpdateTaskBriefTool(state),
        # BrowserPreplanTool(state),
        BrowserFormBeginTool(state),
        BrowserFormStatusTool(state),
        BrowserFormCommitTool(state),
        BrowserRewindToCheckpointTool(state),
        BrowserCloseTool(state),
    ]
    if register_legacy:
        # Debug-only: restore the removed-by-default tools when the
        # caller wants the full v3 surface for comparison.
        tools.extend([
            BrowserClickTool(state),
            BrowserClickSelectorTool(state),
            BrowserScrollTool(state),
            BrowserInventoryFiltersTool(state),
        ])
    # Arch v4.1 (Fix 2a): opt-in re-registration of the deprecated
    # task_plan trio for A/B comparison. Off by default.
    if os.environ.get("ENABLE_LEGACY_TASKPLAN", "0") == "1":
        tools.extend([
            BrowserSetTaskPlanTool(state),
            BrowserPlanSkipStepTool(state),
            BrowserPlanReplanTool(state),
        ])
    # Arch v4.2: opt-in re-registration of the v4 meta-tools that were
    # removed from the default surface (look_again / state_check /
    # verify_action / update_task_brief / preplan). Off by default.
    if os.environ.get("REGISTER_LEGACY_V4_TOOLS", "0") == "1":
        tools.extend([
            BrowserLookAgainTool(state),
            BrowserStateCheckTool(state),
            BrowserVerifyActionTool(state),
            BrowserUpdateTaskBriefTool(state),
            BrowserPreplanTool(state),
        ])
    # Arch v4.4: opt-in re-registration of browser_navigate, removed
    # from the default surface because every observed in-task call was
    # a URL hallucination. browser_open does the cold-start navigation.
    if os.environ.get("REGISTER_BROWSER_NAVIGATE", "0") == "1":
        tools.append(BrowserNavigateTool(state))
    for tool in tools:
        bot._loop.tools.register(tool)
    return state
