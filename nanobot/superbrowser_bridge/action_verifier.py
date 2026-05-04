"""Post-action verifier — did the click/type actually do what was expected?

Background
----------
Arch v2 had no general post-action feedback loop. Misclicks on dense
scenes (filter modals, dropdowns, captcha tile grids) went undetected
until the brain noticed the page hadn't changed several turns later,
by which point the conversation history was polluted with stale
state.

Arch v3 adds two tiers:

  Tier A (free, automatic)
    `verify_action.py` extends Postcondition with `bbox_state_change`
    so DOM probes can confirm a clicked button became "pressed",
    a checkbox flipped, an input got a value. Zero vision spend.

  Tier B (paid, opt-in + auto-fire on dense scenes)
    `browser_verify_action(expected, bbox_ref)` — single focused
    vision call with `intent="verify_action"`. Returns:
      {
        "action_outcome": "succeeded" | "failed" | "uncertain",
        "recommendation": "continue" | "undo" | "retry",
        "element_state_delta": "...",
        "page_state": { ... PageState ... }
      }
    The "undo" recommendation is text only — the brain decides
    whether to navigate back, close a modal, etc.

Auto-fire logic
---------------
The worker hook auto-invokes Tier B after `browser_click_at` /
`browser_type_at` when the action target had ≥5 candidate bboxes
within 80px (dense scene). For non-dense clicks the brain calls the
tool explicitly when it wants a paid check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# Auto-fire dense-scene thresholds. These are intentionally loose —
# the goal is to catch obvious misclicks, not to second-guess every
# action. 5 candidates within 80px reliably catches filter-modal /
# dropdown / picker traffic without firing on isolated CTAs.
DENSE_SCENE_NEIGHBOR_RADIUS_PX = 80
DENSE_SCENE_NEIGHBOR_THRESHOLD = 5


@dataclass
class ActionContext:
    """Snapshot taken just before a state-change tool runs.

    Fields are filled by the action dispatch path and read by the
    verifier. Stored on `BrowserSessionState.last_action_context`.
    """

    tool_name: str = ""
    bbox_index: Optional[int] = None  # 1-based V_n the brain referenced
    bbox_label: str = ""
    bbox_center_px: Optional[tuple[int, int]] = None
    pre_url: str = ""
    pre_summary: str = ""
    pre_dom_hash: str = ""
    expected_outcome: str = ""
    captured_at: float = 0.0
    nearby_bbox_count: int = 0  # for dense-scene auto-fire decision

    def is_dense_scene(self) -> bool:
        return self.nearby_bbox_count >= DENSE_SCENE_NEIGHBOR_THRESHOLD


def count_nearby_bboxes(
    target_px: tuple[int, int],
    bboxes: list[Any],
    image_width: int,
    image_height: int,
    *,
    radius_px: int = DENSE_SCENE_NEIGHBOR_RADIUS_PX,
) -> int:
    """Return the number of bboxes whose CENTER falls within `radius_px`
    of `target_px`. Used to decide auto-fire of Tier B verification.

    `bboxes` is `VisionResponse.bboxes` (list of BBox). Boxes are in
    normalized [0, 1000] space; we denormalize to pixels for distance.
    """
    if not bboxes or image_width <= 0 or image_height <= 0:
        return 0
    tx, ty = target_px
    r2 = radius_px * radius_px
    count = 0
    for b in bboxes:
        try:
            ymin, xmin, ymax, xmax = (
                getattr(b, "box_2d", None) or [0, 0, 0, 0]
            )
        except Exception:
            continue
        if ymax <= ymin or xmax <= xmin:
            continue
        # Center of the bbox in image-space [0, 1000]
        cy_norm = (ymin + ymax) / 2.0
        cx_norm = (xmin + xmax) / 2.0
        cx = int(cx_norm * image_width / 1000.0)
        cy = int(cy_norm * image_height / 1000.0)
        dx = cx - tx
        dy = cy - ty
        if dx * dx + dy * dy <= r2:
            count += 1
    return count


def _normalize_outcome(verdict: str) -> str:
    s = (verdict or "").strip().lower()
    if s in {"succeeded", "failed", "uncertain"}:
        return s
    aliases = {
        "success": "succeeded", "ok": "succeeded", "passed": "succeeded",
        "fail": "failed", "broken": "failed",
        "unsure": "uncertain", "unknown": "uncertain", "no_action": "uncertain",
    }
    return aliases.get(s, "uncertain")


def _recommendation_from(verdict: str, expected: str) -> str:
    """Map (verdict, expected) -> recommendation."""
    v = _normalize_outcome(verdict)
    if v == "succeeded":
        return "continue"
    if v == "failed":
        # Default to undo when the action failed — brain can override
        # to retry if it has another tactic.
        return "undo"
    return "retry"


def build_verifier_result(
    vision_response: Any,
    *,
    expected: str,
    pre_summary: str = "",
) -> dict[str, Any]:
    """Translate a VisionResponse with verify_action intent into the
    verifier output shape.

    Reads `page_state.last_action_verdict` (populated by the
    verify_action prompt block). Falls back to inspecting changes_from_previous
    text when last_action_verdict is at default.
    """
    if vision_response is None:
        return {
            "action_outcome": "uncertain",
            "recommendation": "retry",
            "element_state_delta": "",
            "page_state": None,
            "expected": expected,
            "pre_summary": pre_summary,
            "advisor_notes": "vision unavailable",
        }
    page_state = getattr(vision_response, "page_state", None)
    verdict_obj = getattr(page_state, "last_action_verdict", None) if page_state else None
    verdict = "no_action"
    evidence = ""
    delta = ""
    if verdict_obj is not None:
        verdict = getattr(verdict_obj, "verdict", "no_action")
        evidence = getattr(verdict_obj, "evidence", "") or ""
        delta = getattr(verdict_obj, "delta_summary", "") or ""

    # Fallback: when verdict is no_action but we have changes_from_previous,
    # treat presence of changes as "uncertain" rather than "no_action" so
    # downstream callers don't loop on a stale signal.
    if verdict == "no_action":
        cfp = getattr(vision_response, "changes_from_previous", "") or ""
        if cfp:
            verdict = "uncertain"
            delta = delta or cfp[:200]

    outcome = _normalize_outcome(verdict)
    recommendation = _recommendation_from(verdict, expected)

    page_state_dict: Optional[dict[str, Any]] = None
    if page_state is not None and hasattr(page_state, "model_dump"):
        try:
            page_state_dict = page_state.model_dump()
        except Exception:
            page_state_dict = None

    return {
        "action_outcome": outcome,
        "recommendation": recommendation,
        "element_state_delta": (delta or evidence or "")[:240],
        "evidence": evidence[:240],
        "page_state": page_state_dict,
        "expected": expected,
        "pre_summary": pre_summary[:200],
    }


def render_verifier_text(result: dict[str, Any]) -> str:
    """Render the verifier dict as a brain-facing text block.

    Compact — intended to be appended to the next tool result so the
    brain sees verification feedback inline with the action it just
    took.
    """
    if not result:
        return ""
    outcome = result.get("action_outcome", "uncertain")
    rec = result.get("recommendation", "retry")
    delta = result.get("element_state_delta", "") or result.get("evidence", "")
    expected = result.get("expected", "")

    lines: list[str] = [f"[VERIFY] outcome={outcome}  recommend={rec}"]
    if expected:
        lines.append(f"  expected: {expected[:160]}")
    if delta:
        lines.append(f"  observed: {delta[:200]}")
    if rec == "undo":
        lines.append(
            "  guidance: action did not produce the expected outcome. "
            "Consider browser_navigate(back), close any modal, or pick a "
            "different bbox. Update task_brief CoT before next attempt."
        )
    elif rec == "retry":
        lines.append(
            "  guidance: outcome unclear — call browser_state_check or "
            "browser_screenshot to re-observe before deciding."
        )
    return "\n".join(lines)


__all__ = [
    "ActionContext",
    "DENSE_SCENE_NEIGHBOR_RADIUS_PX",
    "DENSE_SCENE_NEIGHBOR_THRESHOLD",
    "build_verifier_result",
    "count_nearby_bboxes",
    "render_verifier_text",
]
