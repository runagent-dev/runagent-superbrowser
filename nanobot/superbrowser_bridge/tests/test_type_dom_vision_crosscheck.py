"""Tests for the type ↔ DOM/vision crosscheck reuse.

The `BrowserTypeTool` reuses `_dom_vision_crosscheck` (already covered by
`test_dom_vision_crosscheck.py`). What's new is the refusal *message*
format and the decision matrix transcribed for type. Since the tool
dispatches HTTP, we test the IoU-driven branch decisions directly via
the crosscheck helper plus the bookkeeping-side state asserts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from superbrowser_bridge.session_tools import (
    _dom_vision_crosscheck,
    BrowserSessionState,
)


@dataclass
class _BB:
    box_2d: list = field(default_factory=lambda: [0, 0, 0, 0])
    label: str = ""


@dataclass
class _VR:
    bboxes: list = field(default_factory=list)
    image_width: int = 1000
    image_height: int = 1000
    dpr: float = 1.0


def _attach(bb: _BB, iw: int = 1000, ih: int = 1000, dpr: float = 1.0) -> _BB:
    bb._attached_iw = iw
    bb._attached_ih = ih
    bb._attached_dpr = dpr
    return bb


# The thresholds the type tool uses (mirrors the click crosscheck).
TYPE_REFUSE_BELOW = 0.5
TYPE_WARN_BELOW = 0.7


def test_type_crosscheck_strong_overlap_passes_the_threshold():
    """IoU >= 0.7 → tool would allow silently."""
    s = BrowserSessionState()
    s.elements_bounds = {
        12: {"bounds": {"x": 100, "y": 100, "w": 100, "h": 100}, "text": "max"},
    }
    s._last_vision_response = _VR(bboxes=[
        _attach(_BB(box_2d=[100, 100, 200, 200], label="Max price input")),
    ])
    iou, v, lbl = _dom_vision_crosscheck(s, 12)
    assert iou >= TYPE_WARN_BELOW
    assert v == 1
    assert "Max" in lbl


def test_type_crosscheck_weak_overlap_falls_in_warn_band():
    """0.5 <= IoU < 0.7 → tool allows but warns. We reproduce the IoU=~0.6 case."""
    s = BrowserSessionState()
    # ~60% overlap: 80% of one side, perfect on the other → roughly 0.5–0.7 zone.
    s.elements_bounds = {
        9: {"bounds": {"x": 100, "y": 100, "w": 100, "h": 100}, "text": "field"},
    }
    s._last_vision_response = _VR(bboxes=[
        _attach(_BB(box_2d=[100, 100, 220, 220], label="Field")),
    ])
    iou, v, _ = _dom_vision_crosscheck(s, 9)
    # 100x100 intersection / (100*100 + 120*120 − 100*100) = 10000 / 14400 ≈ 0.69
    assert TYPE_REFUSE_BELOW <= iou < TYPE_WARN_BELOW
    assert v == 1


def test_type_crosscheck_weak_overlap_below_refuse_threshold():
    """IoU < 0.5 → tool would REFUSE. Mirrors the click_crosscheck path."""
    s = BrowserSessionState()
    s.elements_bounds = {
        25: {"bounds": {"x": 100, "y": 100, "w": 100, "h": 100}, "text": "btn"},
    }
    # Adjacent element with only ~25% overlap on one corner.
    s._last_vision_response = _VR(bboxes=[
        _attach(_BB(box_2d=[150, 150, 250, 250], label="Price Filter")),
    ])
    iou, v, lbl = _dom_vision_crosscheck(s, 25)
    # 50x50 intersection / (100*100 + 100*100 − 50*50) = 2500 / 17500 ≈ 0.143
    assert iou < TYPE_REFUSE_BELOW
    assert v == 1
    assert lbl == "Price Filter"  # the V_n the refusal message would suggest


def test_type_crosscheck_no_vision_overlap_returns_zero():
    """Index addresses something vision didn't see — type tool would REFUSE
    with [TYPE_NO_VISION_MATCH]."""
    s = BrowserSessionState()
    s.elements_bounds = {
        4: {"bounds": {"x": 700, "y": 700, "w": 50, "h": 50}, "text": "hidden"},
    }
    s._last_vision_response = _VR(bboxes=[
        _attach(_BB(box_2d=[0, 0, 100, 100], label="Logo")),
    ])
    iou, v, _ = _dom_vision_crosscheck(s, 4)
    assert iou == 0.0


def test_type_crosscheck_skipped_when_no_bounds():
    """Without elements_bounds, the helper returns (0, None, '') and the
    tool should fall through to the dispatch path silently."""
    s = BrowserSessionState()
    iou, v, lbl = _dom_vision_crosscheck(s, 5)
    assert iou == 0.0 and v is None and lbl == ""
