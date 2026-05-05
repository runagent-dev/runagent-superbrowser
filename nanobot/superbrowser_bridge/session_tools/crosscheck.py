"""DOM ↔ vision crosscheck helpers.

These give the click tools a reciprocal IoU lookup: from a DOM index
to the most overlapping vision bbox (and back). Used to flag stale
indices and pick a fresh V_n when the original target shifted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .dom_helpers import _rect_iou

if TYPE_CHECKING:
    from .state import BrowserSessionState  # noqa: F401

def _dom_vision_crosscheck(
    state: "BrowserSessionState", index: int
) -> tuple[float, int | None, str]:
    """Compute the best IoU between DOM index `index` and any cached
    vision bbox. Returns (best_iou, best_v_index, best_label).

    Returns (0.0, None, "") when:
      * no element bounds cached for index, or
      * no vision response cached, or
      * vision is stale, or
      * vision has no bboxes.
    """
    target = state.elements_bounds.get(index)
    if not target:
        return (0.0, None, "")
    vr = getattr(state, "_last_vision_response", None)
    if vr is None:
        return (0.0, None, "")
    bboxes = list(getattr(vr, "bboxes", []) or [])
    if not bboxes:
        return (0.0, None, "")
    iw = getattr(vr, "image_width", 0)
    ih = getattr(vr, "image_height", 0)
    dpr = float(getattr(vr, "dpr", 1.0) or 1.0)
    if not iw or not ih:
        return (0.0, None, "")
    best_iou = 0.0
    best_v = None
    best_label = ""
    target_rect = target["bounds"]
    for v_index, bb in enumerate(bboxes, start=1):
        # Attach unscaling hints so _rect_iou doesn't need a separate path.
        bb._attached_iw = iw
        bb._attached_ih = ih
        bb._attached_dpr = dpr
        iou = _rect_iou(target_rect, bb)
        if iou > best_iou:
            best_iou = iou
            best_v = v_index
            best_label = (getattr(bb, "label", "") or "").strip()
    return (best_iou, best_v, best_label)
def _vision_dom_crosscheck(
    state: "BrowserSessionState", v_index: int
) -> tuple[float, int | None, str]:
    """Inverse of _dom_vision_crosscheck: given vision V_n, find the
    DOM rect it overlaps best. Returns (best_iou, best_dom_index,
    dom_text). Used by click_at to confirm DOM agreement before
    clicking the bbox coordinates.
    """
    vr = getattr(state, "_last_vision_response", None)
    if vr is None:
        return (0.0, None, "")
    bboxes = list(getattr(vr, "bboxes", []) or [])
    if not bboxes or v_index < 1 or v_index > len(bboxes):
        return (0.0, None, "")
    iw = getattr(vr, "image_width", 0)
    ih = getattr(vr, "image_height", 0)
    dpr = float(getattr(vr, "dpr", 1.0) or 1.0)
    if not iw or not ih:
        return (0.0, None, "")
    bb = bboxes[v_index - 1]
    bb._attached_iw = iw
    bb._attached_ih = ih
    bb._attached_dpr = dpr
    elements_bounds = getattr(state, "elements_bounds", {}) or {}
    if not elements_bounds:
        return (0.0, None, "")
    best_iou = 0.0
    best_dom: int | None = None
    best_text = ""
    for dom_idx, info in elements_bounds.items():
        target_rect = info.get("bounds") if isinstance(info, dict) else None
        if not target_rect:
            continue
        iou = _rect_iou(target_rect, bb)
        if iou > best_iou:
            best_iou = iou
            best_dom = dom_idx
            best_text = (info.get("text") or "") if isinstance(info, dict) else ""
    return (best_iou, best_dom, best_text)
