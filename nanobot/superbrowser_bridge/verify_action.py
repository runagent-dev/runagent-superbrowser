"""Post-action verification against a declared postcondition.

Mirrors `type_verify.py` in shape: re-entrance guard, LRU cache, async
entry point, dataclass outcome. Unlike type_verify — which uses a Gemini
reflector — this module deliberately avoids LLM calls. Every postcondition
kind resolves via one short `page.evaluate` or a `state`-level comparison.
The point is to make the existing vision/brain loop cheaper and more
reliable, not to add another LLM hop.

When every DOM signal is ambiguous, the caller may choose to trigger a
vision pass with `intent="verify_action"` (existing bucket in prompts.py)
— that decision lives outside this module.

Integration:
    # In interactive_session.click_at, after wait_for_load_state:
    verification = await verify_action.verify_after(
        t3manager, sid, postcond_dict, pre_state=pre_state,
    )
    return {"success": True, ..., "verification": verification.to_dict()}

Postcondition kinds (matching action_planner.Postcondition):
    bbox_disappeared  — the widget at `widget_px` no longer present / visible
    url_changed       — page.url != pre_state.url
    url_matches       — page.url matches payload["pattern"] (substring)
    text_visible      — body.innerText contains payload["text"]
    text_hidden       — body.innerText does NOT contain payload["text"]
    flag_cleared      — re-run captcha/blocker detect; payload["flag"] false
    focus_on_role     — document.activeElement matches payload["selector"]
    dom_mutated       — page_content_hash differs from pre_state.dom_hash
    none              — no-op, always verified=True (for planning filler)
"""

from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


VERIFY_ENABLED = os.environ.get("VERIFY_AFTER_CLICK", "1") != "0"

_RECENT_CACHE_CAP = 16
_recent_cache: "OrderedDict[tuple[str, str, str], tuple[float, bool]]" = OrderedDict()


@dataclass
class VerifyResult:
    """Outcome of a single postcondition check."""

    verified: bool
    reason: str = ""
    kind: str = "none"
    duration_ms: int = 0
    signals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "reason": self.reason,
            "kind": self.kind,
            "duration_ms": self.duration_ms,
            "signals": dict(self.signals),
        }


@dataclass
class PreState:
    """Snapshot captured BEFORE the action fires, used for delta checks."""

    url: str = ""
    dom_hash: str = ""
    body_text_sample: str = ""


# ── JS probes (one evaluate each, deliberately cheap) ────────────────

_BBOX_ABSENT_JS = """
(rect) => {
  const [x0, y0, x1, y1] = rect;
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  const el = document.elementFromPoint(cx, cy);
  if (!el) return {absent: true, reason: 'no element at center'};
  // If the element at the blocker's former center isn't fixed/absolute
  // with a high z-index anymore, we consider the blocker gone.
  let n = el;
  while (n && n !== document.body) {
    const cs = getComputedStyle(n);
    const z = parseInt(cs.zIndex, 10) || 0;
    const pos = cs.position;
    if ((pos === 'fixed' || pos === 'absolute') && z > 500) {
      const r = n.getBoundingClientRect();
      // Still looks like an overlay in the same place?
      const overlapX = Math.max(0, Math.min(x1, r.right) - Math.max(x0, r.left));
      const overlapY = Math.max(0, Math.min(y1, r.bottom) - Math.max(y0, r.top));
      const rectArea = (x1 - x0) * (y1 - y0);
      if (rectArea > 0 && (overlapX * overlapY) / rectArea > 0.3) {
        return {absent: false, reason: 'overlay still present',
                tag: n.tagName, id: n.id, cls: n.className};
      }
    }
    n = n.parentElement;
  }
  return {absent: true, reason: 'no overlay at coords'};
}
"""

_TEXT_PRESENT_JS = """
(needle) => {
  const body = document.body ? (document.body.innerText || '') : '';
  return body.toLowerCase().indexOf(String(needle).toLowerCase()) !== -1;
}
"""

_FOCUS_MATCH_JS = """
(selector) => {
  const ae = document.activeElement;
  if (!ae) return {matched: false, reason: 'no activeElement'};
  try {
    const m = ae.matches(selector);
    return {matched: !!m, tag: ae.tagName, id: ae.id, type: ae.type || ''};
  } catch (e) {
    return {matched: false, reason: 'bad selector'};
  }
}
"""


# ── Main entry ──────────────────────────────────────────────────────

async def verify_after(
    t3manager,
    session_id: str,
    postcondition: dict[str, Any],
    *,
    pre_state: Optional[PreState] = None,
    state: Any = None,
) -> VerifyResult:
    """Evaluate the postcondition against the current page state.

    Parameters
    ----------
    t3manager : T3SessionManager
        The manager that owns the session. Must expose `evaluate(sid, js, arg)`.
    session_id : str
    postcondition : dict
        Serialized Postcondition (kind + payload + timeout_ms).
    pre_state : PreState | None
        Snapshot captured BEFORE the action. Required for url_changed /
        dom_mutated / text_hidden. Can be omitted for self-contained
        checks (url_matches, text_visible, flag_cleared, focus_on_role).
    state : BrowserSessionState | None
        For re-entrance guard + audit only.

    Returns
    -------
    VerifyResult with verified + reason + signals.
    """
    if not VERIFY_ENABLED:
        return VerifyResult(verified=True, reason="disabled_via_env", kind="none")

    start = time.monotonic()
    kind = str(postcondition.get("kind", "none"))
    payload: dict[str, Any] = dict(postcondition.get("payload") or {})
    timeout_ms = int(postcondition.get("timeout_ms") or 2500)

    # Re-entrance guard: verify_after must never recursively trigger
    # itself. A captcha solver that calls click_at internally would
    # otherwise verify its own probe click.
    if state is not None and getattr(state, "_verify_in_progress", False):
        return VerifyResult(
            verified=True, kind=kind,
            reason="reentrance_suppressed",
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    if state is not None:
        state._verify_in_progress = True  # type: ignore[attr-defined]

    try:
        if kind == "none":
            return VerifyResult(
                verified=True, kind=kind, reason="no_postcondition",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        # Cache key: (session, kind, payload-signature). 2s TTL — just
        # long enough to deduplicate the immediate re-verify that can
        # happen if a hook fires during the click response.
        cache_key = (
            session_id or "_", kind,
            repr(sorted(payload.items()))[:200],
        )
        cached = _recent_cache.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < 2.0:
            return VerifyResult(
                verified=cached[1], kind=kind, reason="cached",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        verified, reason, signals = await _dispatch(
            t3manager, session_id, kind, payload, pre_state, timeout_ms,
        )
        dur = int((time.monotonic() - start) * 1000)
        _recent_cache[cache_key] = (time.monotonic(), verified)
        if len(_recent_cache) > _RECENT_CACHE_CAP:
            _recent_cache.popitem(last=False)
        return VerifyResult(
            verified=verified, kind=kind, reason=reason,
            duration_ms=dur, signals=signals,
        )
    except Exception as exc:
        logger.debug("verify_after error: %s", exc)
        return VerifyResult(
            verified=True,      # fail-open: don't block the loop
            kind=kind,
            reason=f"error:{str(exc)[:100]}",
            duration_ms=int((time.monotonic() - start) * 1000),
            signals={"fail_open": True},
        )
    finally:
        if state is not None:
            state._verify_in_progress = False  # type: ignore[attr-defined]


async def _dispatch(
    t3manager, session_id: str, kind: str, payload: dict[str, Any],
    pre_state: Optional[PreState], timeout_ms: int,
) -> tuple[bool, str, dict[str, Any]]:
    """Route to the per-kind checker. Returns (verified, reason, signals)."""
    if kind == "bbox_disappeared":
        rect = payload.get("widget_px") or payload.get("rect")
        if not rect or len(rect) < 4:
            return True, "no_rect_to_check", {}
        result = await t3manager.evaluate(session_id, _BBOX_ABSENT_JS, list(rect))
        if not isinstance(result, dict):
            return True, "probe_returned_non_dict", {}
        absent = bool(result.get("absent"))
        return absent, ("bbox_gone" if absent else "overlay_still_present"), result

    if kind == "url_changed":
        cur = await _page_url(t3manager, session_id)
        before = pre_state.url if pre_state else ""
        if not before:
            return True, "no_pre_url", {"current": cur}
        changed = cur and cur != before
        return bool(changed), ("url_changed" if changed else "url_same"), {
            "before": before, "after": cur,
        }

    if kind == "url_matches":
        cur = await _page_url(t3manager, session_id)
        pat = str(payload.get("pattern") or payload.get("substring") or "")
        if not pat:
            return True, "no_pattern", {"current": cur}
        ok = pat in (cur or "")
        return ok, ("url_matches" if ok else "url_mismatch"), {
            "current": cur, "pattern": pat,
        }

    if kind == "text_visible":
        needle = str(payload.get("text") or payload.get("needle") or "")
        if not needle:
            return True, "no_text", {}
        present = await t3manager.evaluate(session_id, _TEXT_PRESENT_JS, needle)
        return bool(present), ("text_visible" if present else "text_missing"), {
            "needle": needle,
        }

    if kind == "text_hidden":
        needle = str(payload.get("text") or payload.get("needle") or "")
        if not needle:
            return True, "no_text", {}
        present = await t3manager.evaluate(session_id, _TEXT_PRESENT_JS, needle)
        return (not bool(present)), (
            "text_hidden" if not present else "text_still_visible"
        ), {"needle": needle}

    if kind == "flag_cleared":
        flag = str(payload.get("flag") or "")
        signals: dict[str, Any] = {"flag": flag}
        if flag in ("captcha_present", "captcha"):
            try:
                from .antibot.captcha.detect import detect as detect_captcha
                info = await detect_captcha(t3manager, session_id)
                signals["captcha"] = {"type": info.type, "present": info.present}
                return (not info.present), (
                    "captcha_cleared" if not info.present
                    else f"captcha_still:{info.type}"
                ), signals
            except Exception as exc:
                return True, f"captcha_probe_failed:{exc!s}"[:80], signals
        if flag in ("blocker_present", "blocker", "ui_blocker"):
            try:
                from .antibot.ui_blockers import detect as detect_blockers
                hits = await detect_blockers(t3manager, session_id)
                signals["blockers"] = [
                    {"type": h.type, "severity": h.severity} for h in hits
                ]
                hard = [h for h in hits if h.severity == "hard"]
                return (len(hard) == 0), (
                    "no_hard_blockers" if not hard
                    else f"hard_blockers_remaining:{len(hard)}"
                ), signals
            except Exception as exc:
                return True, f"blocker_probe_failed:{exc!s}"[:80], signals
        return True, f"unknown_flag:{flag}", signals

    if kind == "focus_on_role":
        sel = str(payload.get("selector") or "")
        if not sel:
            return True, "no_selector", {}
        result = await t3manager.evaluate(session_id, _FOCUS_MATCH_JS, sel)
        matched = bool(result and result.get("matched"))
        return matched, ("focused" if matched else "not_focused"), result or {}

    if kind == "dom_mutated":
        # We rely on BrowserSessionState's hash_page_content externally.
        # The caller passes pre_state.dom_hash; we fetch fresh content via
        # t3's /state endpoint (same call the caller would make anyway).
        before = pre_state.dom_hash if pre_state else ""
        if not before:
            return True, "no_pre_hash", {}
        try:
            st = await t3manager.state(session_id)
            elements_str = (st or {}).get("elements") or ""
        except Exception as exc:
            return True, f"state_fetch_failed:{exc!s}"[:80], {}
        # Re-hash via BrowserSessionState's utility to stay in sync with
        # its keying. Import locally to avoid a module-level cycle.
        try:
            from .session_tools import BrowserSessionState
            now_hash = BrowserSessionState.hash_page_content(elements_str)
        except Exception:
            now_hash = ""
        changed = bool(now_hash) and now_hash != before
        return changed, ("dom_changed" if changed else "dom_same"), {
            "before": before, "after": now_hash,
        }

    return True, f"unknown_kind:{kind}", {}


async def _page_url(t3manager, session_id: str) -> str:
    """Fetch current URL via a state read. Cheap; state() is already cached."""
    try:
        st = await t3manager.state(session_id)
        return str((st or {}).get("url") or "")
    except Exception:
        return ""


def clear_cache() -> None:
    """For tests."""
    _recent_cache.clear()


__all__ = [
    "PreState",
    "VerifyResult",
    "verify_after",
    "clear_cache",
    "VERIFY_ENABLED",
]
