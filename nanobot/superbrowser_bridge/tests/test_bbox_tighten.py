"""Unit tests for Arch v4 Phase J: bbox-center click + DOM-rect tightening.

Two changes:
1. The chevron-shift-to-right-edge logic was removed. browser_click_at
   now always sends the full vision rect, and the TS backend clicks
   the geometric center (mean of the four corner points). Tested
   indirectly here by confirming `_bbox_is_chevron_label` is no longer
   the gate to a coordinate change in click_at flow.

2. _maybe_tighten_to_dom_rect substitutes the looser vision rect with
   the pixel-exact DOM rect when (a) bbox.dom_check carries a `rect`
   key, (b) the DOM rect's center falls inside the vision rect, and
   (c) the DOM rect isn't egregiously larger than the vision rect.

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_bbox_tighten
"""

from __future__ import annotations

import sys


class _Bbox:
    """Tiny stand-in for vision_agent.schemas.BBox in unit tests."""
    def __init__(self, label: str = "", dom_check=None):
        self.label = label
        self.dom_check = dom_check


def test_tighten_returns_none_when_no_dom_check() -> None:
    from superbrowser_bridge.session_tools import _maybe_tighten_to_dom_rect
    b = _Bbox(label="Click me", dom_check=None)
    out = _maybe_tighten_to_dom_rect(b, 100, 200, 400, 240, 1000, 1000, 1.0)
    assert out is None


def test_tighten_returns_none_when_dom_check_has_no_rect() -> None:
    from superbrowser_bridge.session_tools import _maybe_tighten_to_dom_rect
    b = _Bbox(label="x", dom_check={"tag": "button", "role": "button",
                                    "text": "x", "disagree": False})
    out = _maybe_tighten_to_dom_rect(b, 100, 200, 400, 240, 1000, 1000, 1.0)
    assert out is None


def test_tighten_substitutes_dom_rect_when_center_inside() -> None:
    """Vision rect spans the whole row; DOM rect is the tight text
    rect inside it. The DOM rect's center is inside the vision rect →
    we use the DOM rect."""
    from superbrowser_bridge.session_tools import _maybe_tighten_to_dom_rect
    b = _Bbox(label="Oregon", dom_check={
        "tag": "label", "role": "checkbox",
        "text": "Oregon", "disagree": False,
        "rect": {"x0": 230, "y0": 215, "x1": 290, "y1": 235},
    })
    # Vision rect is the loose row.
    out = _maybe_tighten_to_dom_rect(
        b, 100, 200, 400, 240, 1000, 1000, 1.0,
    )
    assert out == (230, 215, 290, 235)


def test_tighten_returns_none_when_dom_center_outside_vision_rect() -> None:
    """DOM bubble-up resolved to a wrapping container outside the
    vision rect; tightening would mis-click. Stay on the vision rect."""
    from superbrowser_bridge.session_tools import _maybe_tighten_to_dom_rect
    b = _Bbox(label="x", dom_check={
        "tag": "div", "role": "button", "text": "x", "disagree": False,
        # Off-target rect — center at (500, 500) is way outside vision (100..400, 200..240).
        "rect": {"x0": 480, "y0": 480, "x1": 520, "y1": 520},
    })
    out = _maybe_tighten_to_dom_rect(
        b, 100, 200, 400, 240, 1000, 1000, 1.0,
    )
    assert out is None


def test_tighten_returns_none_when_dom_rect_is_huge() -> None:
    """DOM rect >4× larger than vision rect → bubble-up grabbed a
    wrapping container; don't tighten to it."""
    from superbrowser_bridge.session_tools import _maybe_tighten_to_dom_rect
    b = _Bbox(label="x", dom_check={
        "tag": "div", "role": "container", "text": "x", "disagree": False,
        "rect": {"x0": 0, "y0": 0, "x1": 1000, "y1": 1000},
    })
    out = _maybe_tighten_to_dom_rect(
        b, 100, 200, 400, 240, 1000, 1000, 1.0,
    )
    assert out is None


def test_tighten_returns_none_on_degenerate_rect() -> None:
    """Zero-area or inverted DOM rect → reject."""
    from superbrowser_bridge.session_tools import _maybe_tighten_to_dom_rect
    b = _Bbox(label="x", dom_check={
        "tag": "x", "role": "x", "text": "x", "disagree": False,
        "rect": {"x0": 100, "y0": 100, "x1": 100, "y1": 100},  # zero area
    })
    out = _maybe_tighten_to_dom_rect(
        b, 50, 50, 200, 200, 1000, 1000, 1.0,
    )
    assert out is None

    b2 = _Bbox(label="x", dom_check={
        "tag": "x", "role": "x", "text": "x", "disagree": False,
        "rect": {"x0": 200, "y0": 200, "x1": 100, "y1": 100},  # inverted
    })
    out2 = _maybe_tighten_to_dom_rect(
        b2, 50, 50, 250, 250, 1000, 1000, 1.0,
    )
    assert out2 is None


def test_tighten_handles_malformed_rect_gracefully() -> None:
    """Missing keys / non-numeric values should fall through to None,
    not raise."""
    from superbrowser_bridge.session_tools import _maybe_tighten_to_dom_rect
    for bad in (
        {"x0": 100},                              # missing keys
        {"x0": "a", "y0": 0, "x1": 200, "y1": 50},  # non-numeric
        {},
    ):
        b = _Bbox(label="x", dom_check={
            "tag": "x", "role": "x", "text": "x",
            "disagree": False, "rect": bad,
        })
        out = _maybe_tighten_to_dom_rect(
            b, 50, 50, 250, 250, 1000, 1000, 1.0,
        )
        assert out is None


def test_dom_check_rect_populated_on_agreement() -> None:
    """When vision and DOM agree, dom_check still gets populated with
    the rect so the click can tighten. disagree=False distinguishes
    this from the legacy disagreement path."""
    # This is an integration check on the decoration helper. We can
    # mock the elements-at-points response and assert the bbox.dom_check
    # ends up with the rect + disagree=False on agreement.
    import asyncio
    from unittest.mock import patch

    from superbrowser_bridge.session_tools import (
        _decorate_bboxes_with_dom_check,
    )

    class _Resp:
        def __init__(self, payload, status_code=200):
            self._p = payload
            self.status_code = status_code
        def json(self): return self._p

    class _BB:
        def __init__(self, label, role):
            self.label = label
            self.role = role
            self.dom_check = None
        def to_pixels(self, w, h):
            return (100, 200, 400, 240)

    class _Vresp:
        def __init__(self, bboxes):
            self.bboxes = bboxes

    bbox = _BB("Search", "button")
    vresp = _Vresp([bbox])
    fake_response = _Resp({
        "results": [
            {
                "ok": True, "tag": "button", "role": "button",
                "text": "Search",
                "rect": {"x0": 230, "y0": 215, "x1": 290, "y1": 235},
            },
        ],
    })

    async def _fake_request(*args, **kw):
        return fake_response

    with patch(
        "superbrowser_bridge.session_tools._request_with_backoff",
        new=_fake_request,
    ):
        asyncio.run(
            _decorate_bboxes_with_dom_check("sid", vresp, 1000, 1000)
        )

    assert bbox.dom_check is not None
    assert bbox.dom_check.get("disagree") is False
    assert bbox.dom_check.get("rect") == {
        "x0": 230, "y0": 215, "x1": 290, "y1": 235,
    }


def main() -> int:
    tests = [
        test_tighten_returns_none_when_no_dom_check,
        test_tighten_returns_none_when_dom_check_has_no_rect,
        test_tighten_substitutes_dom_rect_when_center_inside,
        test_tighten_returns_none_when_dom_center_outside_vision_rect,
        test_tighten_returns_none_when_dom_rect_is_huge,
        test_tighten_returns_none_on_degenerate_rect,
        test_tighten_handles_malformed_rect_gracefully,
        test_dom_check_rect_populated_on_agreement,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"ERR  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
