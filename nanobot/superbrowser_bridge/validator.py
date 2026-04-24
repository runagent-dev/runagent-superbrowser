"""Mandatory validator — propose, validate, then fire.

The "brain" (worker LLM) historically chose a tool + bbox_index in one step
and the dispatcher fired blindly. That's the root cause of two symptoms the
user reported:

  1. Brain ignores fresh vision/bbox state — it commits to a [V3] picked N
     turns ago and the page has shifted since.
  2. Bbox misses "tools section" items — vision's 10-25 cap hides sidebar
     actions the brain needs, and no DOM-fallback resurfaces them.

This module enforces the fix. Every mutation tool (click_at, type_at) routes
through `validate(state, ProposedAction)` BEFORE the dispatch call. The
validator:

  - Re-checks vision freshness against the current observation_token.
  - Builds a `FusedPerception` merging vision bboxes + DOM clickables.
  - Consults the active subgoal's `Precondition` — "can this action even
    happen right now?".
  - Resolves the bbox_index/xpath/intent to a concrete `FusedElement`.
  - Scores label ↔ intent via `semantic_tools._score_bbox`; rejects < 0.5.
  - Enforces the scene-blocker gate (no clicking under a modal).
  - Synthesizes intent from the bbox label when the caller didn't supply
    one (back-compat for legacy `browser_click_at(vision_index=V2)` calls).

On rejection, returns a `ValidationResult.required_action`:
  - "re_perceive" — element not in current frame, triggers coverage pass
  - "rewind" — page state drifted, needs checkpoint rollback (caller's
     responsibility to honor)
  - None — brain error, report to the LLM as a structured `[validator_
     rejected:*]` caption and let it retry.

The validator returns a `ValidatedAction` the dispatcher consumes —
dispatch MUST use `resolved.click_point_px` / `resolved.xpath`, never the
raw `bbox_index` from the brain. That's how we guarantee no stale-index
clicks slip through.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from superbrowser_bridge.perception_fusion import (
    FusedElement,
    FusedPerception,
    _label_overlap,
    build_fused_perception,
    detect_active_blocker,
)


VALIDATOR_VERSION = 1
DEFAULT_MIN_SCORE = 0.5


# ---------------------------------------------------------------- data shapes


@dataclass
class ProposedAction:
    """What the brain wants to do — before validation."""

    tool: str  # "click_at" | "type_at" | "keys" | ...
    bbox_index: Optional[int] = None  # 1-based vision index the brain picked
    xpath: Optional[str] = None
    raw_x: Optional[float] = None
    raw_y: Optional[float] = None
    intent: str = ""  # natural-language description, e.g. "submit button"
    value: Optional[str] = None  # for type_at
    subgoal_id: Optional[str] = None


@dataclass
class ResolvedTarget:
    """Concrete coordinates + metadata the dispatcher will use."""

    source: str  # "vision" | "dom" | "fused" | "raw_coord"
    label: str
    score: float
    click_point_px: tuple[int, int]
    bbox_pixels: Optional[tuple[int, int, int, int]] = None
    xpath: Optional[str] = None
    bbox_index: Optional[int] = None
    scene_layer: Optional[str] = None


@dataclass
class ValidatedAction:
    """Validated, dispatch-ready action. Only the dispatcher consumes this."""

    proposed: ProposedAction
    resolved: ResolvedTarget
    observation_token: int
    subgoal_id: Optional[str]
    precondition_satisfied: bool
    validator_version: int = VALIDATOR_VERSION


@dataclass
class ValidationResult:
    """Verdict. `ok=True` → caller reads `action` and dispatches.

    `ok=False` → caller reads `reason` + `required_action`; the human-
    readable `caption` is the brain-facing tag it should return.
    """

    ok: bool
    action: Optional[ValidatedAction] = None
    reason: str = ""
    required_action: Optional[str] = None  # "re_perceive" | "rewind" | None
    caption: str = ""
    telemetry: dict = field(default_factory=dict)


# ---------------------------------------------------------------- validator


async def validate(
    state: Any,
    session_id: str,
    proposed: ProposedAction,
) -> ValidationResult:
    """Run the full propose → validate pipeline for a mutation tool."""
    stats = _ensure_stats(state)
    stats["calls"] = stats.get("calls", 0) + 1

    # Lazy imports — these modules import session_tools; importing at
    # module top would create a circular.
    from superbrowser_bridge.session_tools import _require_fresh_vision

    # -- Step 1: fresh vision gate ---------------------------------------
    ok, gate_msg = await _require_fresh_vision(
        state, session_id,
        reason=f"validator:{proposed.tool}",
    )
    if not ok:
        stats["rejected_stale"] = stats.get("rejected_stale", 0) + 1
        return ValidationResult(
            ok=False,
            reason="stale_vision",
            required_action="re_perceive",
            caption=gate_msg or "[validator_rejected:stale_vision]",
            telemetry={"stage": "freshness"},
        )

    vision_resp = getattr(state, "_last_vision_response", None)
    # Session state uses `current_token` (see BrowserSessionState.advance_
    # observation_token); the variable here stays generic because the
    # FusedPerception snapshot is keyed on "observation", not token name.
    observation_token = int(getattr(state, "current_token", 0) or 0)
    dom_entries = list(getattr(state, "last_selector_entries", []) or [])
    subgoal = _active_subgoal(state)

    intent_labels = _intent_label_set(proposed, subgoal)
    fused = build_fused_perception(
        vision_response=vision_resp,
        dom_entries=dom_entries,
        observation_token=observation_token,
        intent_labels=intent_labels,
    )

    # -- Step 2: precondition gate ---------------------------------------
    precondition_ok = True
    if subgoal is not None and hasattr(subgoal, "check_precondition"):
        pc = subgoal.check_precondition(fused, intent_hint=proposed.intent)
        if not pc.satisfied:
            stats["rejected_precondition"] = stats.get("rejected_precondition", 0) + 1
            required = pc.required_action or "re_perceive"
            label = getattr(getattr(subgoal, "precondition", None), "element_label", "")
            return ValidationResult(
                ok=False,
                reason="precondition_not_satisfied",
                required_action=required,
                caption=(
                    f"[validator_rejected:coverage_miss subgoal={subgoal.id} "
                    f"needs={label!r}] The active subgoal expects "
                    f"'{label}' to be visible before acting. Vision+DOM "
                    f"fusion did not find a match — call browser_screenshot "
                    f"or browser_scroll to surface it, then retry."
                ),
                telemetry={"stage": "precondition", "subgoal": subgoal.id},
            )
        precondition_ok = True

    # -- Step 3: resolve target ------------------------------------------
    element, resolve_reason = _resolve_element(fused, proposed, vision_resp)
    if element is None:
        stats["rejected_unresolved"] = stats.get("rejected_unresolved", 0) + 1
        return ValidationResult(
            ok=False,
            reason=resolve_reason,
            required_action="re_perceive",
            caption=(
                f"[validator_rejected:{resolve_reason}] Could not resolve "
                f"target for {proposed.tool}. bbox_index={proposed.bbox_index}, "
                f"xpath={proposed.xpath!r}, intent={proposed.intent!r}. "
                "Take a fresh screenshot and reissue with a valid target."
            ),
            telemetry={"stage": "resolve", "reason": resolve_reason},
        )

    # Synthesize intent from element label if the brain didn't provide one
    # (back-compat with legacy browser_click_at(V_n) calls). We do this
    # AFTER resolution so we're synthesizing from the resolved target's
    # actual label, not a guess from a different bbox.
    effective_intent = proposed.intent.strip()
    if not effective_intent and element.label:
        effective_intent = element.label
        stats["intent_synthesized"] = stats.get("intent_synthesized", 0) + 1

    # -- Step 3.5: global blocker-unaddressed gate -----------------------
    # When the fused perception says a wall is up (scene graph, flags,
    # error_page, sparse-page heuristic), a click on anything whose
    # label doesn't match the dismiss_hint is almost always the brain
    # trying to route around the wall. Reject it — the worker_hook's
    # forced semantic_click will fire on the next turn, and the brain
    # can retry with the correct target. This is the hallucination
    # prevention layer: even if vision didn't emit `scene.layers` or
    # the brain ignored the [BLOCKER_DETECTED] nudge, we refuse to
    # land an off-target click.
    #
    # Exception: raw_coord clicks (no label to compare) pass through.
    # If the brain is using raw coords it presumably knows what it's
    # doing — and our label-based check has nothing to compare against.
    blocker_info = detect_active_blocker(vision_resp)
    if (
        blocker_info is not None
        and element.source != "raw_coord"
        and proposed.tool in ("click_at", "type_at")
    ):
        dismiss = blocker_info.dismiss_hint
        target_label = element.label or effective_intent
        overlap = _label_overlap(target_label, dismiss)
        intent_overlap = _label_overlap(effective_intent, dismiss)
        # Allow through only when EITHER the resolved target OR the
        # declared intent matches the dismiss_hint. Both independently
        # protect: the brain might say intent="Continue Anyway" but
        # pick the wrong bbox, or vice versa.
        if overlap < 0.5 and intent_overlap < 0.5:
            stats["rejected_blocker_unaddressed"] = (
                stats.get("rejected_blocker_unaddressed", 0) + 1
            )
            return ValidationResult(
                ok=False,
                reason="blocker_unaddressed",
                required_action=None,
                caption=(
                    f"[validator_rejected:blocker_unaddressed "
                    f"dismiss={dismiss!r} source={blocker_info.source}] "
                    f"A wall is covering the page and your target "
                    f"({target_label!r}) is not the dismiss element. "
                    f"Call browser_semantic_click(target={dismiss!r}) "
                    f"FIRST — the page below is not reachable until "
                    "this is gone."
                ),
                telemetry={
                    "stage": "blocker_unaddressed",
                    "dismiss": dismiss,
                    "source": blocker_info.source,
                    "target_overlap": overlap,
                    "intent_overlap": intent_overlap,
                },
            )

    # -- Step 4: label/intent scoring ------------------------------------
    score = _score_target(element, effective_intent, tool=proposed.tool)
    min_score = _min_score_for(proposed.tool)
    if effective_intent and score < min_score:
        stats["rejected_label"] = stats.get("rejected_label", 0) + 1
        return ValidationResult(
            ok=False,
            reason="label_mismatch",
            required_action=None,
            caption=(
                f"[validator_rejected:label_mismatch] {proposed.tool} intent "
                f"{effective_intent!r} does not match resolved target "
                f"{element.label!r} (score={score:.2f} < {min_score:.2f}). "
                "Either pick a different target or refine your intent."
            ),
            telemetry={
                "stage": "label",
                "score": score,
                "label": element.label,
                "intent": effective_intent,
            },
        )

    # -- Step 5: blocker-layer gate --------------------------------------
    scene = getattr(vision_resp, "scene", None)
    active_blocker = (
        getattr(scene, "active_blocker_layer_id", None)
        if scene is not None else None
    )
    if active_blocker:
        elem_layer = element.scene_layer
        if elem_layer and elem_layer != active_blocker:
            dismiss_hint = _dismiss_hint_for(scene, active_blocker)
            stats["rejected_blocker"] = stats.get("rejected_blocker", 0) + 1
            hint = f" Dismiss '{dismiss_hint}' first." if dismiss_hint else ""
            return ValidationResult(
                ok=False,
                reason="blocker_layer",
                required_action=None,
                caption=(
                    f"[validator_rejected:blocker_layer active={active_blocker}] "
                    f"A blocker layer covers the page, and the target sits in "
                    f"layer {elem_layer}.{hint}"
                ),
                telemetry={
                    "stage": "blocker",
                    "active": active_blocker,
                    "target_layer": elem_layer,
                },
            )

    # -- Step 6: confidence gate (vision only) ---------------------------
    # Preserve the existing VISION_MIN_CLICK_CONFIDENCE threshold — it
    # protects against Gemini emitting low-confidence ghosts.
    if element.vision_bbox is not None and proposed.tool == "click_at":
        try:
            min_conf = float(
                os.environ.get("VISION_MIN_CLICK_CONFIDENCE") or "0.45"
            )
        except ValueError:
            min_conf = 0.45
        conf = float(getattr(element.vision_bbox, "confidence", 0.5) or 0.5)
        if conf < min_conf:
            stats["rejected_confidence"] = stats.get("rejected_confidence", 0) + 1
            return ValidationResult(
                ok=False,
                reason="low_confidence",
                required_action="re_perceive",
                caption=(
                    f"[validator_rejected:low_confidence] Target bbox "
                    f"confidence={conf:.2f} < {min_conf:.2f}. Re-screenshot "
                    "then retry with a higher-confidence target."
                ),
                telemetry={
                    "stage": "confidence", "confidence": conf,
                },
            )

    # -- Step 7: assemble ValidatedAction --------------------------------
    iw = int(getattr(vision_resp, "image_width", 0) or 0) if vision_resp else 0
    ih = int(getattr(vision_resp, "image_height", 0) or 0) if vision_resp else 0
    dpr = float(getattr(vision_resp, "dpr", 1.0) or 1.0) if vision_resp else 1.0
    bbox_pixels: Optional[tuple[int, int, int, int]] = None
    if element.vision_bbox is not None and iw > 0 and ih > 0:
        bbox_pixels = element.vision_bbox.to_pixels(iw, ih, dpr=dpr)
        click_px = (
            (bbox_pixels[0] + bbox_pixels[2]) // 2,
            (bbox_pixels[1] + bbox_pixels[3]) // 2,
        )
    elif element.source == "raw_coord":
        # Pass-through: the brain supplied exact pixel coords; don't
        # derive from an empty synthetic rect.
        click_px = (
            int(proposed.raw_x or 0),
            int(proposed.raw_y or 0),
        )
    else:
        click_px = element.click_point_px(iw, ih, dpr=dpr)

    resolved = ResolvedTarget(
        source=element.source,
        label=element.label,
        score=score,
        click_point_px=click_px,
        bbox_pixels=bbox_pixels,
        xpath=element.xpath,
        bbox_index=proposed.bbox_index,
        scene_layer=element.scene_layer,
    )
    stats["ok"] = stats.get("ok", 0) + 1
    if element.source == "fused":
        stats["dom_fusion_hits"] = stats.get("dom_fusion_hits", 0) + 1
    elif element.source == "dom":
        stats["dom_recovery_hits"] = stats.get("dom_recovery_hits", 0) + 1

    return ValidationResult(
        ok=True,
        action=ValidatedAction(
            proposed=proposed,
            resolved=resolved,
            observation_token=observation_token,
            subgoal_id=(subgoal.id if subgoal is not None else None),
            precondition_satisfied=precondition_ok,
        ),
        reason="ok",
        caption=(
            f"[validator:ok score={score:.2f} source={element.source}]"
        ),
        telemetry={
            "stage": "ok",
            "source": element.source,
            "score": score,
            "subgoal": getattr(subgoal, "id", "") if subgoal else "",
        },
    )


# ---------------------------------------------------------------- helpers


def _ensure_stats(state: Any) -> dict:
    stats = getattr(state, "validator_stats", None)
    if not isinstance(stats, dict):
        stats = {}
        try:
            state.validator_stats = stats
        except Exception:
            pass
    return stats


def _active_subgoal(state: Any) -> Any:
    graph = getattr(state, "task_graph", None)
    if graph is None:
        return None
    if hasattr(graph, "current"):
        try:
            return graph.current()
        except Exception:
            return None
    return None


def _intent_label_set(
    proposed: ProposedAction,
    subgoal: Any,
) -> list[str]:
    """Gather label strings used to seed DOM-orphan recovery in fusion."""
    labels: list[str] = []
    if proposed.intent:
        labels.append(proposed.intent)
    if subgoal is not None:
        pc = getattr(subgoal, "precondition", None)
        if pc is not None and getattr(pc, "element_label", ""):
            labels.append(pc.element_label)
        for lf in getattr(subgoal, "look_for", None) or []:
            if isinstance(lf, str) and lf.strip():
                labels.append(lf)
    return labels


def _resolve_element(
    fused: FusedPerception,
    proposed: ProposedAction,
    vision_resp: Any,
) -> tuple[Optional[FusedElement], str]:
    """Pick the FusedElement the proposed action aims at.

    Precedence:
      1. xpath (exact DOM identity)
      2. bbox_index (vision's ordinal)
      3. intent label match
      4. raw (x, y) — only when the tool is a coord-click — we wrap it
         as a pseudo vision-less element.
    """
    if proposed.xpath:
        elem = fused.resolve_by_xpath(proposed.xpath)
        if elem is not None:
            return elem, "ok"
        return None, "xpath_not_found"

    if proposed.bbox_index is not None:
        elem = fused.resolve_by_bbox_index(int(proposed.bbox_index))
        if elem is not None:
            return elem, "ok"
        return None, "bad_vision_index"

    if proposed.intent:
        elem = fused.resolve_by_label(proposed.intent)
        if elem is not None:
            return elem, "ok"

    if proposed.raw_x is not None and proposed.raw_y is not None:
        # Raw coords: no element to validate against label-wise, but we
        # still want freshness + blocker + precondition checks. Create a
        # synthetic element so downstream code is homogeneous.
        from superbrowser_bridge.perception_fusion import Rect
        return FusedElement(
            label=proposed.intent or "(raw coords)",
            source="raw_coord",
            rect_norm=Rect(),
            score=0.0,
            vision_bbox=None,
            dom_element=None,
            scene_layer=None,
            role="other",
        ), "ok"

    return None, "no_target"


def _score_target(element: FusedElement, intent: str, *, tool: str) -> float:
    """Delegates to semantic_tools._score_bbox so the scoring heuristic
    stays canonical. `want_input=True` for type_at boosts input-class
    role matches.
    """
    if not intent:
        # No intent → nothing to score against. For back-compat we treat
        # this as "trust the brain's pick" and return the element's
        # fusion score (already ≥ 0.5 for DOM-fused, 1.0 for vision).
        return element.score or 0.5

    # Raw-coord clicks have no label — fall through to fusion score
    # (typically 0.0 for raw_coord). In that case we only enforce
    # freshness + precondition + blocker; label gate is disabled.
    if element.source == "raw_coord":
        return 1.0  # label gate N/A for coord clicks

    # Semantic tools' scorer operates on an object exposing
    # `.label` / `.role` / `.clickable` — FusedElement carries those.
    from superbrowser_bridge.semantic_tools import _score_bbox
    want_input = tool.startswith("type") or tool in ("browser_type_at", "browser_type")

    class _Shim:
        def __init__(self, fe: FusedElement) -> None:
            self.label = fe.label
            self.role = fe.role
            self.clickable = True
            self.role_in_scene = (
                "blocker" if fe.scene_layer and "modal" in (fe.scene_layer or "").lower()
                else "target"
            )

    return _score_bbox(_Shim(element), intent, want_input=want_input)


def _min_score_for(tool: str) -> float:
    env_key = "VALIDATOR_MIN_SCORE"
    try:
        return float(os.environ.get(env_key) or DEFAULT_MIN_SCORE)
    except ValueError:
        return DEFAULT_MIN_SCORE


def _dismiss_hint_for(scene: Any, layer_id: str) -> str:
    try:
        for layer in getattr(scene, "layers", []) or []:
            if getattr(layer, "id", None) == layer_id:
                return (getattr(layer, "dismiss_hint", "") or "").strip()
    except Exception:
        return ""
    return ""


__all__ = [
    "ProposedAction",
    "ResolvedTarget",
    "ValidatedAction",
    "ValidationResult",
    "VALIDATOR_VERSION",
    "validate",
]
