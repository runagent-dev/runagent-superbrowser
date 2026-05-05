"""Tests for the DOM↔vision crosscheck (Fix A) and adjacent guards.

These exercise the pure helpers — `_rect_iou`, `_dom_vision_crosscheck`
— and verify the threshold logic without needing a live TS server.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from superbrowser_bridge.session_tools import (
    _rect_iou,
    _dom_vision_crosscheck,
    _vision_dom_crosscheck,
    _resolve_v_by_label,
    BrowserSessionState,
)


# Vision bboxes use box_2d in [ymin, xmin, ymax, xmax] normalised to 0–1000.
# The IoU helper unscales using image_width / image_height that the caller
# attaches to the bbox via _attached_iw / _attached_ih.

@dataclass
class _BB:
    box_2d: list = field(default_factory=lambda: [0, 0, 0, 0])
    label: str = ""


def _attach(bb: _BB, iw: int, ih: int, dpr: float = 1.0) -> _BB:
    bb._attached_iw = iw
    bb._attached_ih = ih
    bb._attached_dpr = dpr
    return bb


def test_iou_perfect_overlap():
    # 1000-unit normalised bbox covering the same 100×100 px region as the
    # DOM rect. iw=ih=1000 so each unit = 1 px.
    bb = _attach(_BB(box_2d=[100, 100, 200, 200]), iw=1000, ih=1000)
    dom = {"x": 100, "y": 100, "w": 100, "h": 100}
    iou = _rect_iou(dom, bb)
    assert 0.99 <= iou <= 1.0, iou


def test_iou_disjoint():
    bb = _attach(_BB(box_2d=[0, 0, 50, 50]), iw=1000, ih=1000)
    dom = {"x": 500, "y": 500, "w": 100, "h": 100}
    assert _rect_iou(dom, bb) == 0.0


def test_iou_partial_overlap():
    # Top-left half overlap.
    bb = _attach(_BB(box_2d=[0, 0, 100, 100]), iw=1000, ih=1000)
    dom = {"x": 50, "y": 50, "w": 100, "h": 100}
    iou = _rect_iou(dom, bb)
    # 50×50 intersection / (100×100 + 100×100 − 50×50) = 2500 / 17500 ≈ 0.142
    assert 0.13 <= iou <= 0.16, iou


def test_iou_handles_zero_area():
    bb = _attach(_BB(box_2d=[100, 100, 100, 100]), iw=1000, ih=1000)
    dom = {"x": 100, "y": 100, "w": 0, "h": 0}
    assert _rect_iou(dom, bb) == 0.0


def test_iou_unscales_with_dpr():
    # iw=2000, ih=2000 with dpr=2 means each unit = 1 CSS px (image is
    # rendered at 2x).
    bb = _attach(_BB(box_2d=[200, 200, 400, 400]), iw=2000, ih=2000, dpr=2.0)
    dom = {"x": 200, "y": 200, "w": 200, "h": 200}
    assert _rect_iou(dom, bb) > 0.99


# ----- _dom_vision_crosscheck -----------------------------------------


@dataclass
class _VR:
    bboxes: list = field(default_factory=list)
    image_width: int = 1000
    image_height: int = 1000
    dpr: float = 1.0


def test_crosscheck_no_bounds_returns_zero():
    s = BrowserSessionState()
    iou, v, lbl = _dom_vision_crosscheck(s, 5)
    assert iou == 0.0 and v is None


def test_crosscheck_no_vision_returns_zero():
    s = BrowserSessionState()
    s.elements_bounds = {3: {"bounds": {"x": 10, "y": 10, "w": 20, "h": 20}, "text": "x"}}
    iou, v, lbl = _dom_vision_crosscheck(s, 3)
    assert iou == 0.0 and v is None


def test_crosscheck_finds_best_overlap():
    s = BrowserSessionState()
    s.elements_bounds = {
        7: {"bounds": {"x": 100, "y": 100, "w": 100, "h": 100}, "text": "Login"},
    }
    # Two bboxes — the second is a perfect match.
    vr = _VR(bboxes=[
        _BB(box_2d=[500, 500, 600, 600], label="Footer"),
        _BB(box_2d=[100, 100, 200, 200], label="Login button"),
    ])
    s._last_vision_response = vr
    iou, v, lbl = _dom_vision_crosscheck(s, 7)
    assert v == 2  # second bbox (1-indexed)
    assert lbl == "Login button"
    assert iou > 0.99


def test_crosscheck_returns_zero_when_no_overlap():
    s = BrowserSessionState()
    s.elements_bounds = {
        4: {"bounds": {"x": 700, "y": 700, "w": 50, "h": 50}, "text": "ad"},
    }
    vr = _VR(bboxes=[
        _BB(box_2d=[0, 0, 100, 100], label="Logo"),
        _BB(box_2d=[100, 100, 200, 200], label="Search"),
    ])
    s._last_vision_response = vr
    iou, v, _ = _dom_vision_crosscheck(s, 4)
    assert iou == 0.0


# --- Pre-nav deliberation gate (Fix B) state mechanics ---


def test_deliberation_turn_starts_at_zero():
    s = BrowserSessionState()
    assert s.last_deliberation_turn == 0


# --- No-progress nudge bookkeeping (Fix D) ---


def test_brief_version_starts_at_one():
    from superbrowser_bridge.task_brief import TaskBrief
    b = TaskBrief("q", [{"label": "a"}])
    assert b.version == 1


def test_brief_version_bumps_on_mark():
    from superbrowser_bridge.task_brief import TaskBrief
    b = TaskBrief("q", [{"label": "a"}])
    v0 = b.version
    b.mark(1, "done")
    assert b.version == v0 + 1


# ----- _vision_dom_crosscheck -----------------------------------------


def test_vision_dom_crosscheck_perfect_match():
    s = BrowserSessionState()
    s.elements_bounds = {
        7: {"bounds": {"x": 100, "y": 100, "w": 100, "h": 100}, "text": "Login"},
    }
    vr = _VR(bboxes=[
        _BB(box_2d=[100, 100, 200, 200], label="Login button"),
    ])
    s._last_vision_response = vr
    iou, dom_idx, txt = _vision_dom_crosscheck(s, 1)
    assert iou > 0.99
    assert dom_idx == 7
    assert "Login" in txt


def test_vision_dom_crosscheck_disjoint_returns_zero():
    s = BrowserSessionState()
    s.elements_bounds = {
        4: {"bounds": {"x": 700, "y": 700, "w": 50, "h": 50}, "text": "ad"},
    }
    vr = _VR(bboxes=[
        _BB(box_2d=[0, 0, 100, 100], label="Logo"),
    ])
    s._last_vision_response = vr
    iou, dom_idx, _ = _vision_dom_crosscheck(s, 1)
    assert iou == 0.0


def test_vision_dom_crosscheck_picks_best_dom():
    s = BrowserSessionState()
    s.elements_bounds = {
        1: {"bounds": {"x": 0, "y": 0, "w": 50, "h": 50}, "text": "far"},
        2: {"bounds": {"x": 100, "y": 100, "w": 100, "h": 100}, "text": "near"},
    }
    vr = _VR(bboxes=[_BB(box_2d=[100, 100, 200, 200], label="near-bbox")])
    s._last_vision_response = vr
    iou, dom_idx, _ = _vision_dom_crosscheck(s, 1)
    assert dom_idx == 2 and iou > 0.99


def test_vision_dom_crosscheck_no_bboxes_returns_zero():
    s = BrowserSessionState()
    s.elements_bounds = {1: {"bounds": {"x": 0, "y": 0, "w": 10, "h": 10}, "text": "x"}}
    iou, dom_idx, _ = _vision_dom_crosscheck(s, 1)
    assert iou == 0.0 and dom_idx is None


def test_vision_dom_crosscheck_out_of_range_returns_zero():
    s = BrowserSessionState()
    s.elements_bounds = {1: {"bounds": {"x": 0, "y": 0, "w": 10, "h": 10}, "text": "x"}}
    vr = _VR(bboxes=[_BB(box_2d=[0, 0, 10, 10], label="L")])
    s._last_vision_response = vr
    iou, _, _ = _vision_dom_crosscheck(s, 99)
    assert iou == 0.0


# ----- _resolve_v_by_label --------------------------------------------


def test_resolve_v_by_label_finds_match():
    s = BrowserSessionState()
    vr = _VR(bboxes=[
        _BB(box_2d=[0, 0, 100, 100], label="Sort by Critic Score"),
        _BB(box_2d=[100, 100, 200, 200], label="Max price input"),
    ])
    s._last_vision_response = vr
    v = _resolve_v_by_label(s, "Max price input")
    assert v == 2


def test_resolve_v_by_label_returns_none_when_no_match():
    s = BrowserSessionState()
    vr = _VR(bboxes=[
        _BB(box_2d=[0, 0, 100, 100], label="Sort"),
        _BB(box_2d=[100, 100, 200, 200], label="Logo"),
    ])
    s._last_vision_response = vr
    # No bbox label has anything to do with "Oregon checkbox".
    v = _resolve_v_by_label(s, "Oregon checkbox")
    assert v is None


def test_resolve_v_by_label_returns_none_when_no_vision():
    s = BrowserSessionState()
    v = _resolve_v_by_label(s, "anything")
    assert v is None


def test_resolve_v_by_label_returns_none_when_label_empty():
    s = BrowserSessionState()
    vr = _VR(bboxes=[_BB(box_2d=[0, 0, 100, 100], label="X")])
    s._last_vision_response = vr
    v = _resolve_v_by_label(s, "")
    assert v is None
