"""Deterministic action planner.

Given (vision response, DOM blocker list, task instruction, recent step
history), produces an ordered `ActionQueue`: dismiss all hard blockers
first, then pursue the main goal. The queue is rendered into the
screenshot caption as a `[PLAN]` block so the worker LLM sees it without
a tool call, and it's cached by plan_hash to avoid re-computing identical
plans within a short window.

Pure function: no I/O, no async, no side effects beyond the process-local
cache. Easy to unit-test with table-driven fixtures.

Cross-validation contract
-------------------------
Vision agent emits `scene.layers` with bboxes tagged `role_in_scene`.
DOM detector emits `BlockerInfo` with pixel rects. Both are independent
signals for "is there a blocker?". The planner:

1. Collects candidate blockers from vision (bboxes where role_in_scene ==
   "blocker") and from DOM (BlockerInfo entries with severity == "hard"
   or confidence >= 0.8).
2. Deduplicates via rect IoU — if the vision bbox and a DOM bbox overlap
   more than IOU_DEDUP, they refer to the same on-screen element and
   the planner treats them as one. DOM beats vision for coords (DOM is
   pixel-exact; vision is a ~0.5-1% quantized box_2d projection).
3. Ranks: severity hard > soft, type cookie > newsletter > generic >
   vision-only, higher confidence first.
4. Emits one `dismiss` PlannedAction per unique blocker, with a
   `bbox_disappeared` postcondition tied to that element's widget rect.
5. Appends the main goal action from vision's suggested_actions
   (priority ≤ 2) — skipping any whose target bbox is itself a blocker.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from vision_agent.schemas import BBox, SceneLayer, VisionResponse
from superbrowser_bridge.antibot.ui_blockers import BlockerInfo


PostcondKind = Literal[
    "bbox_disappeared",
    "url_changed",
    "url_matches",
    "text_visible",
    "text_hidden",
    "flag_cleared",
    "focus_on_role",
    "dom_mutated",
    "none",
]

StepKind = Literal[
    "dismiss", "click", "type", "scroll", "wait", "done", "escalate",
]


@dataclass
class Postcondition:
    """What should be true after the action for it to count as successful."""

    kind: PostcondKind = "none"
    payload: dict[str, Any] = field(default_factory=dict)
    timeout_ms: int = 2500

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "payload": dict(self.payload),
            "timeout_ms": self.timeout_ms,
        }


@dataclass
class PlannedAction:
    step: StepKind
    reason: str
    source: Literal["blocker", "vision_suggestion", "task_synthesis", "escalation"]
    target_vision_index: Optional[int] = None      # [V_n] label in brain text
    target_bbox_pixels: Optional[list[float]] = None  # [x0,y0,x1,y1] CSS px
    target_label: str = ""
    postcondition: Postcondition = field(default_factory=Postcondition)


@dataclass
class ActionQueue:
    actions: list[PlannedAction]
    plan_hash: str
    generated_at: float

    def top(self) -> Optional[PlannedAction]:
        return self.actions[0] if self.actions else None

    def to_brain_text(self) -> str:
        """Compact rendering for the `[PLAN]` block in the screenshot caption."""
        if not self.actions:
            return "[PLAN] (no actions — scene looks free of blockers and no main goal inferred)"
        lines = ["[PLAN]  Execute in order (earlier steps unblock later ones):"]
        for i, a in enumerate(self.actions, start=1):
            vref = f" V{a.target_vision_index}" if a.target_vision_index else ""
            label = f' "{a.target_label}"' if a.target_label else ""
            pc = a.postcondition
            pc_s = (f"expect {pc.kind}"
                    if pc.kind != "none" else "")
            lines.append(
                f"  {i}. {a.step:<8s}{vref}{label}  — {a.reason}"
                + (f"  [{pc_s}]" if pc_s else "")
            )
        return "\n".join(lines)


# ── dedup + ranking helpers ─────────────────────────────────────────────

_IOU_DEDUP = 0.3


def _rect_iou(a: list[float], b: list[float]) -> float:
    """IoU on axis-aligned rects [x0, y0, x1, y1]."""
    if not a or not b or len(a) < 4 or len(b) < 4:
        return 0.0
    ix0 = max(a[0], b[0]); iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2]); iy1 = min(a[3], b[3])
    iw = max(0.0, ix1 - ix0)
    ih = max(0.0, iy1 - iy0)
    inter = iw * ih
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _center_inside(inner: list[float], outer: list[float]) -> bool:
    """Is the center of `inner` inside `outer`?"""
    if not inner or not outer or len(inner) < 4 or len(outer) < 4:
        return False
    cx = (inner[0] + inner[2]) / 2.0
    cy = (inner[1] + inner[3]) / 2.0
    return (outer[0] <= cx <= outer[2]) and (outer[1] <= cy <= outer[3])


def _is_same_blocker(
    vision_rect: list[float],
    dom_widget: list[float],
    dom_dismiss: Optional[list[float]],
) -> bool:
    """Are these two blocker signals pointing at the same on-screen thing?

    Real-world case: DOM detector returns the banner *container*; vision
    returns the Accept *button* inside it. IoU between container and
    button is low (~0.2) but semantically they're one blocker. So we
    accept any of:
      - IoU(vision, dom_widget) ≥ _IOU_DEDUP
      - IoU(vision, dom_dismiss) ≥ _IOU_DEDUP
      - vision bbox center lies inside dom_widget (button inside banner)
      - dom_dismiss center lies inside vision bbox (vision spans the banner)
    """
    if _rect_iou(vision_rect, dom_widget) >= _IOU_DEDUP:
        return True
    if dom_dismiss and _rect_iou(vision_rect, dom_dismiss) >= _IOU_DEDUP:
        return True
    if _center_inside(vision_rect, dom_widget):
        return True
    if dom_dismiss and _center_inside(dom_dismiss, vision_rect):
        return True
    return False


@dataclass
class _Blocker:
    """Internal normalized blocker record (after cross-validation)."""

    widget_px: list[float]
    dismiss_px: Optional[list[float]]
    dismiss_label: str
    severity: Literal["hard", "soft"]
    source_tags: list[str]
    vision_index: Optional[int] = None  # 1-based into ranked bbox list
    layer_id: Optional[str] = None
    confidence: float = 0.0


def _layer_bbox_px(layer: SceneLayer, iw: int, ih: int) -> Optional[list[float]]:
    if not layer.bbox or iw <= 0 or ih <= 0:
        return None
    x0, y0, x1, y1 = layer.bbox.to_pixels(iw, ih)
    return [float(x0), float(y0), float(x1), float(y1)]


def _collect_blockers(
    vresp: VisionResponse,
    dom_blockers: list[BlockerInfo],
) -> list[_Blocker]:
    """Merge vision + DOM blocker signals, deduplicating by IoU."""
    iw, ih = vresp.image_width, vresp.image_height

    # Vision bboxes marked blocker — ranked same as as_brain_text so the
    # [V_n] indices match.
    def _rank(b: BBox) -> tuple[int, int, int, float]:
        role_rank = 0 if b.role_in_scene == "blocker" else (
            1 if b.role_in_scene == "target" else 2
        )
        return (
            role_rank,
            0 if b.intent_relevant else 1,
            0 if b.clickable else 1,
            -b.confidence,
        )
    ranked = sorted(vresp.bboxes, key=_rank)

    vision_blockers: list[_Blocker] = []
    for i, b in enumerate(ranked, start=1):
        if b.role_in_scene != "blocker":
            continue
        if iw <= 0 or ih <= 0:
            continue
        x0, y0, x1, y1 = b.to_pixels(iw, ih)
        vision_blockers.append(_Blocker(
            widget_px=[float(x0), float(y0), float(x1), float(y1)],
            dismiss_px=[float(x0), float(y0), float(x1), float(y1)],
            dismiss_label=b.label or "dismiss",
            severity="hard" if (
                vresp.flags.modal_open or vresp.flags.login_wall
                or vresp.flags.captcha_present
            ) else "soft",
            source_tags=["vision"],
            vision_index=i,
            layer_id=b.layer_id,
            confidence=b.confidence,
        ))

    dom_only: list[_Blocker] = []
    for db in dom_blockers:
        if not db.widget_bbox:
            continue
        # DOM cookie/generic_modal blockers are always candidates;
        # newsletter is a soft candidate only if the planner sees enough
        # signal (confidence ≥ 0.7 built into our detector defaults).
        if db.severity != "hard" and db.confidence < 0.7:
            continue
        dom_only.append(_Blocker(
            widget_px=list(db.widget_bbox),
            dismiss_px=list(db.dismiss_bbox) if db.dismiss_bbox else None,
            dismiss_label=db.dismiss_label or db.type,
            severity=db.severity,
            source_tags=[f"dom:{db.type}"],
            confidence=db.confidence,
        ))

    # Merge — if a DOM rect overlaps a vision rect heavily, they refer to
    # the same element; promote DOM's pixel-exact coords but keep vision's
    # index for the caption.
    merged: list[_Blocker] = []
    for v in vision_blockers:
        partner = None
        for d in dom_only:
            if _is_same_blocker(v.widget_px, d.widget_px, d.dismiss_px):
                partner = d
                break
        if partner is not None:
            dom_only.remove(partner)
            merged.append(_Blocker(
                widget_px=partner.widget_px,     # DOM rect wins
                dismiss_px=partner.dismiss_px or v.dismiss_px,
                dismiss_label=partner.dismiss_label or v.dismiss_label,
                severity="hard" if "hard" in (v.severity, partner.severity) else "soft",
                source_tags=v.source_tags + partner.source_tags,
                vision_index=v.vision_index,
                layer_id=v.layer_id,
                confidence=max(v.confidence, partner.confidence),
            ))
        else:
            merged.append(v)
    merged.extend(dom_only)

    # Rank
    def _brank(b: _Blocker) -> tuple[int, int, float]:
        sev_rank = 0 if b.severity == "hard" else 1
        type_rank = 0
        for t in b.source_tags:
            if t == "dom:cookie":
                type_rank = min(type_rank, 0)
            elif t == "dom:newsletter":
                type_rank = min(type_rank, 1)
            elif t == "dom:generic_modal":
                type_rank = min(type_rank, 2)
            elif t == "vision":
                type_rank = min(type_rank, 1)
        return (sev_rank, type_rank, -b.confidence)
    merged.sort(key=_brank)
    return merged


def _dismiss_stagnant(recent_steps: list[dict], blocker: _Blocker) -> bool:
    """Has the same dismiss been attempted twice with no scene change?

    Looks at the last N recent_steps records from BrowserSessionState.
    Each record should include `tool`, `args`, and `result` keys. We
    consider a dismiss "attempted" if the tool was click_at/click and
    the coords fall inside the blocker's widget rect.
    """
    if not blocker.widget_px or len(recent_steps) < 2:
        return False
    x0, y0, x1, y1 = blocker.widget_px
    attempts = 0
    for rec in reversed(recent_steps[-8:]):
        if not isinstance(rec, dict):
            continue
        tool = rec.get("tool", "")
        if tool not in ("browser_click_at", "browser_click"):
            continue
        # BrowserSessionState.record_step stores args/result as summary
        # strings (e.g. "V3(10,20→100,40)"), not structured dicts.
        # Extract coords via regex when we see a string, otherwise use
        # dict access for the structured path.
        args = rec.get("args")
        cx: float | None = None
        cy: float | None = None
        if isinstance(args, dict):
            cx = args.get("x") or args.get("cx")
            cy = args.get("y") or args.get("cy")
        elif isinstance(args, str):
            import re as _re
            # Common shapes: "V3(10,20→100,40)", "(100,200)", "100,200"
            m = _re.search(r"\(?\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*[,→\)]", args)
            if m:
                cx, cy = m.group(1), m.group(2)
        if cx is None or cy is None:
            continue
        try:
            cxf = float(cx); cyf = float(cy)
        except (TypeError, ValueError):
            continue
        if x0 <= cxf <= x1 and y0 <= cyf <= y1:
            # Was there a "scene change" after this call?
            result = rec.get("result")
            if isinstance(result, dict):
                changes = result.get("changes_from_previous") or ""
            elif isinstance(result, str):
                # Summary strings like "url=... snapped→..." don't tell
                # us about scene change directly; treat as unknown.
                changes = ""
            else:
                changes = ""
            if not changes or changes.lower() in ("", "no change", "none"):
                attempts += 1
    return attempts >= 2


def _hash_plan_inputs(
    url: str, scene_fp: str, task: str,
) -> str:
    h = hashlib.sha1()
    h.update((url or "").encode("utf-8", errors="ignore"))
    h.update(b"|")
    h.update(scene_fp.encode("utf-8", errors="ignore"))
    h.update(b"|")
    h.update((task or "").encode("utf-8", errors="ignore"))
    return h.hexdigest()[:12]


def _scene_fingerprint(
    vresp: VisionResponse, blockers: list[BlockerInfo],
) -> str:
    """Cheap fingerprint that changes when the scene changes.

    Sensitive enough to bust cache when a modal appears/disappears, but
    insensitive to noise like minor bbox coord wiggles.
    """
    parts: list[str] = []
    if vresp.scene:
        for layer in vresp.scene.layers:
            parts.append(f"{layer.id}:{layer.kind}:{int(layer.blocks_interaction_below)}")
    parts.append(f"roles:{sum(1 for b in vresp.bboxes if b.role_in_scene == 'blocker')}"
                 f"/{sum(1 for b in vresp.bboxes if b.role_in_scene == 'target')}"
                 f"/{len(vresp.bboxes)}")
    parts.append(f"flags:{int(vresp.flags.modal_open)}"
                 f"{int(vresp.flags.login_wall)}"
                 f"{int(vresp.flags.captcha_present)}"
                 f"{int(vresp.flags.loading)}")
    parts.append(f"blockers:" + ",".join(f"{b.type}:{b.severity}" for b in blockers))
    return "|".join(parts)


# ── main entry point ────────────────────────────────────────────────────

# Process-local 3s cache keyed by plan_hash. Re-planning on an identical
# scene wastes turns; 3s is long enough to deduplicate rapid retries,
# short enough that a real scene change breaks it.
_CACHE_TTL_S = 3.0
_cache: dict[str, tuple[float, ActionQueue]] = {}


def plan(
    *,
    vresp: VisionResponse,
    blockers: list[BlockerInfo],
    task_instruction: str,
    url: str = "",
    recent_steps: Optional[list[dict]] = None,
) -> ActionQueue:
    """Sequence blockers then the main goal into an executable ActionQueue."""
    recent = recent_steps or []

    merged = _collect_blockers(vresp, blockers)
    scene_fp = _scene_fingerprint(vresp, blockers)
    phash = _hash_plan_inputs(url, scene_fp, task_instruction)

    now = time.monotonic()
    cached = _cache.get(phash)
    if cached and (now - cached[0]) < _CACHE_TTL_S:
        return cached[1]

    actions: list[PlannedAction] = []

    # Step 1: dismiss each unique blocker.
    for b in merged:
        if _dismiss_stagnant(recent, b):
            actions.append(PlannedAction(
                step="escalate",
                reason=(f"dismiss of {b.dismiss_label!r} attempted ≥2x with no "
                        f"scene change; escalate rather than loop"),
                source="escalation",
                target_vision_index=b.vision_index,
                target_bbox_pixels=b.dismiss_px or b.widget_px,
                target_label=b.dismiss_label,
                postcondition=Postcondition(kind="none"),
            ))
            # Don't also emit a dismiss for this blocker — escalation
            # handles it. Subsequent blockers still run normally.
            continue
        actions.append(PlannedAction(
            step="dismiss",
            reason=(f"clear {'+'.join(b.source_tags)} blocker "
                    f"({b.severity}) before main goal"),
            source="blocker",
            target_vision_index=b.vision_index,
            target_bbox_pixels=b.dismiss_px or b.widget_px,
            target_label=b.dismiss_label,
            postcondition=Postcondition(
                kind="bbox_disappeared",
                payload={
                    "widget_px": list(b.widget_px),
                    "dismiss_label": b.dismiss_label,
                    "layer_id": b.layer_id,
                },
                timeout_ms=2500,
            ),
        ))

    # Step 2: main goal — pick from vision.suggested_actions, skipping
    # any that points back into a blocker. `target_bbox_index` is 0-based
    # into `vresp.bboxes` as Gemini emitted it, NOT our ranked order.
    blocker_indices = {
        i for i, b in enumerate(vresp.bboxes) if b.role_in_scene == "blocker"
    }
    chrome_indices = {
        i for i, b in enumerate(vresp.bboxes) if b.role_in_scene == "chrome"
    }
    main_action = None
    for sa in sorted(vresp.suggested_actions, key=lambda a: a.priority):
        if sa.priority > 2:
            continue
        if sa.action in ("dismiss", "wait"):
            continue
        if sa.target_bbox_index is not None:
            if sa.target_bbox_index in blocker_indices:
                continue
            if sa.target_bbox_index in chrome_indices:
                continue
        main_action = sa
        break

    if main_action is not None:
        # Translate Gemini's 0-based index into the emitted bbox list to
        # a 1-based index in the RANKED list (matches as_brain_text V_n).
        target_bbox = None
        vidx_1 = None
        if main_action.target_bbox_index is not None:
            try:
                target_bbox = vresp.bboxes[main_action.target_bbox_index]
            except IndexError:
                target_bbox = None
        if target_bbox is not None:
            # Find this bbox in the ranked list.
            def _rank(b: BBox) -> tuple[int, int, int, float]:
                role_rank = 0 if b.role_in_scene == "blocker" else (
                    1 if b.role_in_scene == "target" else 2
                )
                return (
                    role_rank,
                    0 if b.intent_relevant else 1,
                    0 if b.clickable else 1,
                    -b.confidence,
                )
            ranked = sorted(vresp.bboxes, key=_rank)
            for i, b in enumerate(ranked, start=1):
                if b is target_bbox:
                    vidx_1 = i
                    break
        target_px = None
        if target_bbox and vresp.image_width and vresp.image_height:
            x0, y0, x1, y1 = target_bbox.to_pixels(
                vresp.image_width, vresp.image_height,
            )
            target_px = [float(x0), float(y0), float(x1), float(y1)]

        step_kind: StepKind
        if main_action.action == "type":
            step_kind = "type"
        elif main_action.action == "scroll":
            step_kind = "scroll"
        elif main_action.action == "navigate":
            step_kind = "click"  # navigation is still executed via click
        else:
            step_kind = "click"

        actions.append(PlannedAction(
            step=step_kind,
            reason=main_action.description or f"execute {main_action.action} for task",
            source="vision_suggestion",
            target_vision_index=vidx_1,
            target_bbox_pixels=target_px,
            target_label=(target_bbox.label if target_bbox else ""),
            postcondition=Postcondition(kind="dom_mutated", timeout_ms=2500),
        ))
    elif not merged:
        # No blockers, no vision suggestion — synthesize a `wait` so the
        # queue isn't empty. The worker can observe or re-plan.
        actions.append(PlannedAction(
            step="wait",
            reason="no blockers and no main-goal suggestion; worker should reassess",
            source="task_synthesis",
            postcondition=Postcondition(kind="none"),
        ))

    queue = ActionQueue(
        actions=actions,
        plan_hash=phash,
        generated_at=now,
    )
    _cache[phash] = (now, queue)
    # Trim cache opportunistically.
    if len(_cache) > 64:
        for k in list(_cache.keys())[: len(_cache) - 32]:
            _cache.pop(k, None)
    return queue


def clear_cache() -> None:
    """For tests: drop all cached plans."""
    _cache.clear()


__all__ = [
    "ActionQueue",
    "PlannedAction",
    "Postcondition",
    "plan",
    "clear_cache",
]
