"""Regression: Tier-3 bbox clicks must dispatch at the box CENTER, not the
top-left corner.

The bug: `http_client` posted the bbox's (x0,y0) as the click point and
`interactive_session.click_at` fell back to it on a snap-miss, so vision-bbox
clicks in T3 landed on the upper-left corner instead of the middle. This
asserts `http_client` now hands the box CENTER to `T3SessionManager.click_at`
(and still forwards the bbox so the snap can refine to the interactive
element), while an explicit (x, y) is respected verbatim.

Run:
    source venv/bin/activate && PYTHONPATH=nanobot \\
        python nanobot/superbrowser_bridge/tests/test_t3_bbox_center.py
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import patch

from superbrowser_bridge.session_tools import http_client as hc


class _FakeMgr:
    def __init__(self) -> None:
        self.rec: dict = {}

    async def click_at(self, sid, x, y, *, bbox=None, strategy="primary",
                       expected_label=None):
        self.rec = {"x": x, "y": y, "bbox": bbox}
        return {"ok": True}


def _dispatch_click(body: dict) -> dict:
    mgr = _FakeMgr()

    async def run() -> None:
        with patch("superbrowser_bridge.antibot.interactive_session.default",
                   return_value=mgr):
            await hc._t3_dispatch_from_http(
                "POST", hc.SUPERBROWSER_URL + "/session/t3-x/click",
                json_body=body)

    asyncio.run(run())
    return mgr.rec


def test_bbox_only_posts_center() -> None:
    rec = _dispatch_click({"bbox": {"x0": 100, "y0": 200, "x1": 300, "y1": 240}})
    # center = ((100+300)/2, (200+240)/2) = (200, 220), NOT the corner (100, 200).
    assert rec["x"] == 200.0 and rec["y"] == 220.0, rec
    assert rec["bbox"] is not None  # bbox still forwarded so the snap can refine
    print("✓ test_bbox_only_posts_center")


def test_explicit_xy_wins() -> None:
    rec = _dispatch_click(
        {"x": 55, "y": 66, "bbox": {"x0": 100, "y0": 200, "x1": 300, "y1": 240}})
    assert rec["x"] == 55.0 and rec["y"] == 66.0, rec
    print("✓ test_explicit_xy_wins")


def test_zero_area_bbox_is_finite() -> None:
    rec = _dispatch_click({"bbox": {"x0": 10, "y0": 10, "x1": 10, "y1": 10}})
    assert rec["x"] == 10.0 and rec["y"] == 10.0, rec
    print("✓ test_zero_area_bbox_is_finite")


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"✗ {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
