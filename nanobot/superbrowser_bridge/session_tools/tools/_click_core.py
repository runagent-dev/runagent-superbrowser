"""Shared click ladder: verify_after + js/keyboard escalation.

Both `BrowserClickTool` (DOM-index) and `BrowserClickAtTool` (vision
bbox) drive this helper after their primary `/click` POST settles. It
runs the same verification + escalation logic both tools need so silent
clicks are caught and recovered identically.

The historical asymmetry was:
  - `BrowserClickAtTool` ran a verify_after pass and, on failure,
    escalated through js then keyboard strategies — producing
    `[click_escalated strategy=js]` advisories that auto-recovered
    most "primary click went out, page didn't react" cases.
  - `BrowserClickTool` (DOM-index) ran only an inline effect-diff
    check and surfaced `[click_silent]` for the brain to retry.
    The retry hit the dead-click guard, forcing a re-screenshot.

Lifting the ladder into one place gives DOM-index clicks the same
self-healing behaviour without duplicating ~130 lines of logic.
"""

from __future__ import annotations

import os
from typing import Any, Optional, Union

from ..http_client import SUPERBROWSER_URL, _request_with_backoff


# --- Shared epoch-target resolver + per-epoch scroll-anchor gate --------------
# Every bbox-resolving tool (click_at, type_at, fix_text_at, select_option,
# slider) resolves a V_n against the FROZEN vision epoch the brain last saw and
# must reject it when the page has drifted underneath. The pieces below are the
# single source of that logic so the checks stay identical across tools and both
# tiers (t1 /evaluate + t3 patchright intercept share the same probe).

# Max scrollY delta (CSS px) tolerated between the epoch's anchor and the live
# page before a bbox is treated as stale. Mirrors the TS viewport_shift gate.
VIEWPORT_SHIFT_PX = int(os.environ.get("VIEWPORT_SHIFT_PX") or "12")

# Tiny read-only probe of the live scroll position. Works on both tiers.
_SCROLL_PROBE_JS = "(() => ({y: Math.round(window.scrollY || window.pageYOffset || 0)}))()"


async def probe_scroll_y(session_id: str) -> Optional[int]:
    """Read the live page scrollY (CSS px) via /evaluate. Returns None on any
    failure so callers can treat 'unknown' as 'don't block'."""
    try:
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": _SCROLL_PROBE_JS},
            timeout=8.0,
        )
        if r.status_code >= 400:
            return None
        body = r.json() or {}
        res = body.get("result")
        if isinstance(res, dict) and res.get("y") is not None:
            return int(res["y"])
    except Exception:
        return None
    return None


async def check_scroll_anchor(
    state: Any,
    session_id: str,
    resp: Any,
    *,
    fail_prefix: str,
    verb: str,
    vision_index: int,
) -> Optional[str]:
    """Per-epoch scroll gate: refuse a bbox dispatch when the page has scrolled
    since the epoch `resp` was numbered.

    The epoch is pinned to a specific scroll position (box_2d carries no scroll
    offset), so a click/type against V_n after a scroll would land at the wrong
    place. This closes the /evaluate-typed path (type_at/fix_text_at have no
    TS-side viewport gate) and revives the effectively-dead t3 gate — one probe,
    both tiers, all bbox tools. Returns a brain-facing error string when shifted
    (and marks the epoch dirty), else None.

    No-ops (returns None) when the response has no recorded anchor (-1), the gate
    is disabled (EPOCH_SCROLL_GATE=0), or the live probe fails — never blocks a
    dispatch on a probe hiccup.
    """
    if os.environ.get("EPOCH_SCROLL_GATE", "1") in ("0", "false", "no"):
        return None
    anchor = getattr(resp, "scroll_y", -1)
    if anchor is None or anchor < 0:
        return None
    live = await probe_scroll_y(session_id)
    if live is None:
        return None
    delta = abs(live - anchor)
    if delta <= VIEWPORT_SHIFT_PX:
        return None
    try:
        state.mark_epoch_dirty("viewport_shifted", session_id=session_id)
    except Exception:
        pass
    return (
        f"[{fail_prefix}:viewport_shifted] V{vision_index} was numbered at "
        f"scrollY={anchor} but the page is now at scrollY={live} (Δ={delta}px); "
        f"its bbox coordinates are stale. Call browser_screenshot to renumber the "
        f"V_n list before {verb}."
    )


async def resolve_epoch_target(
    state: Any,
    session_id: str,
    vision_index: int,
    *,
    fail_prefix: str,
    verb: str,
    extra_hint: str = "",
) -> Union[tuple[float, float, Any, Any], str]:
    """Resolve a V_n against the frozen vision epoch to a click point.

    Returns ``(target_x, target_y, bbox, resp)`` on success, or a brain-facing
    error STRING the caller returns verbatim. Consolidates the block duplicated
    across the bbox tools: epoch response → get_bbox → turn-age gate → image-dims
    → to_pixels(center) → scroll-anchor gate. ``fail_prefix`` is the bracketed
    failure-tag stem (e.g. ``"type_at_failed"``); ``verb`` completes the "before
    <verb>" hint ("clicking"/"typing"/"setting the value"); ``extra_hint`` is an
    optional tool-specific tail appended to the epoch_too_old message.

    (click_at keeps its own inline resolution — it interleaves blocker/dead-click
    gates — but shares `check_scroll_anchor`.)
    """
    resp = state.vision_for_target_resolution()
    if resp is None:
        return (
            f"[{fail_prefix}:no_vision] No recent vision response to resolve "
            f"vision_index against. Call browser_screenshot first, or pass raw (x, y)."
        )
    bbox = resp.get_bbox(int(vision_index))
    if bbox is None:
        n = len(getattr(resp, "bboxes", []) or [])
        return (
            f"[{fail_prefix}:bad_vision_index] V{vision_index} is out of range "
            f"(only {n} bboxes in the last vision response)."
        )
    try:
        max_age = int(os.environ.get("VISION_MAX_AGE_TURNS") or "1")
    except ValueError:
        max_age = 1
    if max_age > 0:
        age = max(0, state._brain_turn_counter - 1 - state._vision_epoch_turn)
        if age > max_age:
            msg = (
                f"[{fail_prefix}:epoch_too_old age_turns={age} max={max_age}] "
                f"V{vision_index} resolves against a vision snapshot taken {age} "
                f"actions ago; the page has likely changed and V{vision_index} may "
                f"now point at a different element. Call browser_screenshot to "
                f"refresh the V_n list before {verb}."
            )
            if extra_hint:
                msg = f"{msg} {extra_hint}"
            return msg
    iw, ih = resp.image_width, resp.image_height
    if iw <= 0 or ih <= 0:
        return (
            f"[{fail_prefix}:no_image_dims] Last vision response has no source image "
            f"dimensions; cannot denormalize box_2d. Call browser_screenshot."
        )
    dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
    x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
    target_x = (x0 + x1) / 2
    target_y = (y0 + y1) / 2
    shift = await check_scroll_anchor(
        state, session_id, resp,
        fail_prefix=fail_prefix, verb=verb, vision_index=vision_index,
    )
    if shift:
        return shift
    return (target_x, target_y, bbox, resp)


async def run_click_with_ladder(
    state: Any,
    session_id: str,
    *,
    log_target: str,
    primary_response: dict,
    alt_x: float,
    alt_y: float,
    alt_bbox: Optional[dict],
    postcondition: Optional[dict],
) -> str:
    """Run verify_after; on miss, escalate via js then keyboard.

    Args:
      state: BrowserSessionState
      session_id: session
      log_target: short label for log lines (e.g. "[42]" or "V3(...)")
      primary_response: the response dict from the first /click POST
      alt_x, alt_y: CSS-pixel coords used by the escalation strategies
      alt_bbox: optional {x0,y0,x1,y1} dict; when present, escalation
        sends bbox + x,y so the TS server can re-snap. When None,
        escalation sends just x,y (DOM-index case, post-snap coords).
      postcondition: dict from planner or default {"kind": "dom_mutated"}

    Returns:
      verify_note (possibly empty). One of:
        ""                                    — primary succeeded
        "\n[click_escalated strategy=js]"     — primary silent, js landed
        "\n[click_escalated strategy=keyboard]" — primary silent, kbd landed
        "\n[click_silent ...]"                — primary + all escalations silent
        "\n[VERIFY_MISS ...]"                 — non-default postcondition missed
    """
    if os.environ.get("VERIFY_AFTER_CLICK", "1") == "0" or postcondition is None:
        return ""

    try:
        from superbrowser_bridge.antibot import interactive_session as _t3mgr
        from superbrowser_bridge.verify_action import verify_after, PreState
    except Exception as exc:
        print(f"  [verify_action: skipped — import failed: {exc}]")
        return ""

    mgr = _t3mgr.default() if session_id.startswith("t3-") else None
    pre_state = PreState(
        url=state.current_url or "",
        dom_hash=state._last_dom_hash or "",
    )
    try:
        vr = await verify_after(
            mgr, session_id, postcondition,
            pre_state=pre_state,
            state=state,
        )
    except Exception as exc:
        print(f"  [verify_action: skipped — {exc}]")
        return ""

    if vr.verified:
        if os.environ.get("VERIFY_DEBUG") == "1":
            return f"\n[verify_ok kind={vr.kind}]"
        return ""

    # Verification failed. If it's the default (dom_mutated), try the
    # js/keyboard escalation ladder before reporting silent failure.
    is_silent_default = (
        postcondition.get("kind") == "dom_mutated"
        and not getattr(state._last_action_queue, "actions", None)
    )
    if (
        is_silent_default
        and os.environ.get("CLICK_LADDER_AUTO", "1") != "0"
    ):
        for alt_strategy in ("js", "keyboard"):
            try:
                if session_id.startswith("t3-"):
                    from superbrowser_bridge.antibot import (
                        interactive_session as _t3mgr2,
                    )
                    mgr2 = _t3mgr2.default()
                    alt_resp = await mgr2.click_at(
                        session_id, alt_x, alt_y,
                        bbox=alt_bbox,
                        strategy=alt_strategy,
                    )
                else:
                    alt_payload: dict[str, Any] = {
                        "x": alt_x, "y": alt_y,
                        "strategy": alt_strategy,
                    }
                    if alt_bbox:
                        alt_payload["bbox"] = alt_bbox
                    ar = await _request_with_backoff(
                        "POST",
                        f"{SUPERBROWSER_URL}/session/{session_id}/click",
                        json=alt_payload,
                        timeout=10.0,
                    )
                    if ar.status_code != 200:
                        continue
                    alt_body = ar.json() or {}
                    if alt_body.get("error"):
                        continue
                    alt_resp = {"success": True, **alt_body}
                if not isinstance(alt_resp, dict) or not alt_resp.get("success"):
                    continue
                vr2 = await verify_after(
                    mgr, session_id, postcondition,
                    pre_state=pre_state,
                    state=state,
                )
                if vr2.verified:
                    return (
                        f"\n[click_escalated strategy={alt_strategy}] "
                        f"Primary click on {log_target} was silent; "
                        f"{alt_strategy} strategy landed the action."
                    )
            except Exception as exc:
                print(f"  [click ladder ({alt_strategy}) failed: {exc}]")
                continue

        return (
            f"\n[click_silent reason={vr.reason}] Primary + escalated "
            f"(js/keyboard) clicks on {log_target} all landed no DOM "
            f"change. Target likely non-interactive, covered by an "
            f"overlay, or waiting on an async load. Call "
            f"browser_screenshot to re-vision, dismiss any active "
            f"blocker, or try a different target."
        )

    # Non-default postcondition missed — emit VERIFY_MISS for the brain
    # to re-plan rather than blindly retry.
    return (
        f"\n[VERIFY_MISS kind={vr.kind} reason={vr.reason}] The click "
        f"on {log_target} dispatched but the expected effect "
        f"({postcondition.get('kind')}) didn't land. Consider "
        f"browser_plan_next_steps to re-sequence, or try a different "
        f"target."
    )


async def maybe_scroll_bbox_into_view(
    state: Any,
    session_id: str,
    bbox: dict,
) -> Optional[dict]:
    """If `bbox` is below the fold (page or popup), scroll it into view.

    Returns the new bbox dict {x0,y0,x1,y1} (post-scroll) when a scroll
    happened, or None when no scroll was needed / it failed.

    Called by `BrowserClickAtTool` before dispatching the primary click,
    so dropdown options below the popup's clipped fold can be reached
    without the brain having to call browser_scroll_within first.
    """
    if not isinstance(bbox, dict):
        return None
    try:
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/scroll-to-bbox",
            json={"bbox": bbox},
            timeout=8.0,
        )
    except Exception as exc:
        print(f"  [auto_scroll: skipped — {exc}]")
        return None
    if r.status_code >= 400:
        return None
    body = r.json() or {}
    if not body.get("scrolled"):
        return None
    new_bbox = body.get("new_bbox")
    kind = body.get("container_kind", "?")
    delta = body.get("delta_y", 0)
    if isinstance(new_bbox, dict):
        print(
            f"  [bbox_scrolled_inner container={kind} delta_y={delta}]"
        )
        state.log_activity(
            "click_at(AUTO_SCROLL)",
            f"container={kind} delta_y={delta}",
        )
        return new_bbox
    return None


def lookup_postcondition(
    state: Any,
    *,
    vision_index: Optional[int],
    x: Optional[float],
    y: Optional[float],
) -> Optional[dict]:
    """Match this click against the top planned action and return its
    postcondition, or fall through to {"kind": "dom_mutated"}.

    Match priorities:
      - vision_index equals top action's target_vision_index
      - (x, y) falls inside top action's target bbox (±10px slack)

    The default (dom_mutated) runs when no planner postcondition
    applies. Set VERIFY_DEFAULT=0 to disable and preserve the old
    "no postcondition, no verification" behaviour.
    """
    queue = state._last_action_queue
    if queue is not None and getattr(queue, "actions", None):
        top = queue.actions[0]
        if vision_index is not None and top.target_vision_index is not None:
            if int(vision_index) == int(top.target_vision_index):
                return top.postcondition.to_dict()
        if x is not None and y is not None and top.target_bbox_pixels:
            x0, y0, x1, y1 = top.target_bbox_pixels
            if (x0 - 10) <= float(x) <= (x1 + 10) and \
                    (y0 - 10) <= float(y) <= (y1 + 10):
                return top.postcondition.to_dict()
    if os.environ.get("VERIFY_DEFAULT", "1") != "0":
        return {"kind": "dom_mutated"}
    return None
