"""Unit tests for the inventory_filters vision guard (arch v3 fix M).

When vision already shows the filter panel as bboxes, calling
browser_inventory_filters would scroll server-side and invalidate the
V_n indices vision just emitted. This guard refuses with a redirect.

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_inventory_filters_guard
"""

from __future__ import annotations

import sys


def test_guard_refuses_when_4_filter_shaped_bboxes_present() -> None:
    """4+ filter-shaped bboxes (checkbox/radio role) → refuse."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _inventory_filters_redundant,
    )
    from vision_agent.schemas import VisionResponse, BBox

    s = BrowserSessionState()
    s._last_vision_response = VisionResponse(bboxes=[
        BBox(label="WiFi", role="checkbox", clickable=True),
        BBox(label="Parking", role="checkbox", clickable=True),
        BBox(label="Pool", role="checkbox", clickable=True),
        BBox(label="Breakfast", role="checkbox", clickable=True),
        BBox(label="Apply filters", role="button", clickable=True),
    ])
    msg = _inventory_filters_redundant(s)
    assert msg is not None
    assert "redundant" in msg
    assert "browser_click_at" in msg


def test_guard_refuses_with_filter_keyword_labels() -> None:
    """Bboxes whose labels contain 'filter'/'region'/'pairing' count
    even when the role isn't checkbox."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _inventory_filters_redundant,
    )
    from vision_agent.schemas import VisionResponse, BBox

    s = BrowserSessionState()
    s._last_vision_response = VisionResponse(bboxes=[
        BBox(label="Region (expand sub-options)", role="button", clickable=True),
        BBox(label="Food Pairing (expand sub-options)", role="button", clickable=True),
        BBox(label="Price filter", role="button", clickable=True),
        BBox(label="Sort by", role="button", clickable=True),
    ])
    msg = _inventory_filters_redundant(s)
    assert msg is not None


def test_guard_allows_when_few_filter_bboxes() -> None:
    """<4 filter-shaped bboxes — inventory may still be useful."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _inventory_filters_redundant,
    )
    from vision_agent.schemas import VisionResponse, BBox

    s = BrowserSessionState()
    s._last_vision_response = VisionResponse(bboxes=[
        BBox(label="Search", role="input", clickable=True),
        BBox(label="Submit", role="button", clickable=True),
        BBox(label="Cart", role="button", clickable=True),
    ])
    msg = _inventory_filters_redundant(s)
    assert msg is None


def test_guard_allows_when_no_vision() -> None:
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _inventory_filters_redundant,
    )

    s = BrowserSessionState()
    msg = _inventory_filters_redundant(s)
    assert msg is None


def test_guard_kill_switch() -> None:
    import os
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _inventory_filters_redundant,
    )
    from vision_agent.schemas import VisionResponse, BBox

    s = BrowserSessionState()
    s._last_vision_response = VisionResponse(bboxes=[
        BBox(label="Filter A", role="checkbox", clickable=True),
        BBox(label="Filter B", role="checkbox", clickable=True),
        BBox(label="Filter C", role="checkbox", clickable=True),
        BBox(label="Filter D", role="checkbox", clickable=True),
    ])
    os.environ["INVENTORY_FILTERS_VISION_GUARD"] = "0"
    try:
        msg = _inventory_filters_redundant(s)
        assert msg is None
    finally:
        del os.environ["INVENTORY_FILTERS_VISION_GUARD"]


def test_guard_counts_constraint_targeted_bboxes() -> None:
    """Bboxes whose role_in_scene='target' AND label contains a
    TaskBrief constraint canonical_value count toward the threshold."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _inventory_filters_redundant,
    )
    from superbrowser_bridge.task_brief import TaskBrief, Constraint
    from vision_agent.schemas import VisionResponse, BBox

    s = BrowserSessionState()
    s.task_brief = TaskBrief(
        original_query="x",
        constraints=[
            Constraint(canonical_value="oregon", kind="filter"),
            Constraint(canonical_value="willamette valley", kind="filter"),
            Constraint(canonical_value="dessert", kind="filter"),
            Constraint(canonical_value="fish", kind="filter"),
        ],
    )
    s._last_vision_response = VisionResponse(bboxes=[
        BBox(label="Oregon", role="button", role_in_scene="target",
             clickable=True),
        BBox(label="Willamette Valley", role="button", role_in_scene="target",
             clickable=True),
        BBox(label="Dessert", role="button", role_in_scene="target",
             clickable=True),
        BBox(label="Fish", role="button", role_in_scene="target",
             clickable=True),
        BBox(label="Random link", role="link", clickable=True),
    ])
    msg = _inventory_filters_redundant(s)
    assert msg is not None  # all 4 brief-targeted bboxes count


def main() -> int:
    tests = [
        test_guard_refuses_when_4_filter_shaped_bboxes_present,
        test_guard_refuses_with_filter_keyword_labels,
        test_guard_allows_when_few_filter_bboxes,
        test_guard_allows_when_no_vision,
        test_guard_kill_switch,
        test_guard_counts_constraint_targeted_bboxes,
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
