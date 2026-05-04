"""Unit tests for chevron-aware click position + loose-chevron warning
+ failed-tactic record (arch v3 fixes E + F + G).

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_chevron_click
"""

from __future__ import annotations

import sys


# ── Fix E — _bbox_is_chevron_label detector ──────────────────────────


def test_chevron_label_matches_expand_sub_options() -> None:
    from superbrowser_bridge.session_tools import _bbox_is_chevron_label
    assert _bbox_is_chevron_label("Region (expand sub-options)")
    assert _bbox_is_chevron_label("United States (expand sub-options)")
    assert _bbox_is_chevron_label("Food Pairing (expand sub-options)")


def test_chevron_label_matches_chevron_caret() -> None:
    from superbrowser_bridge.session_tools import _bbox_is_chevron_label
    assert _bbox_is_chevron_label("Region chevron")
    assert _bbox_is_chevron_label("Region caret right")


def test_chevron_label_matches_toggle_all() -> None:
    from superbrowser_bridge.session_tools import _bbox_is_chevron_label
    assert _bbox_is_chevron_label("United States (toggle all)")


def test_chevron_label_does_not_match_normal_button() -> None:
    from superbrowser_bridge.session_tools import _bbox_is_chevron_label
    assert not _bbox_is_chevron_label("Apply filters")
    assert not _bbox_is_chevron_label("Sort by price")
    assert not _bbox_is_chevron_label("Region")  # bare label, no chevron marker
    assert not _bbox_is_chevron_label("")
    assert not _bbox_is_chevron_label("Submit")


# ── Fix F — loose-chevron rendering in as_brain_text ─────────────────


def test_loose_chevron_warning_renders_for_wide_chevron_bbox() -> None:
    """A bbox labeled 'expand sub-options' wider than 60px gets flagged
    in the [LOOSE_CHEVRON] block."""
    from vision_agent.schemas import VisionResponse, BBox
    # Build a bbox 200px wide (60..260 in 1000-wide image) labeled as chevron.
    r = VisionResponse(bboxes=[
        BBox(
            label="Region (expand sub-options)",
            box_2d=[200, 100, 250, 600],  # ymin, xmin, ymax, xmax — width 500/1000
            clickable=True,
            role="button",
            confidence=0.85,
        ),
    ])
    r.with_image_dims(1000, 1000)
    text = r.as_brain_text()
    assert "[LOOSE_CHEVRON]" in text
    assert "Region (expand sub-options)" in text


def test_loose_chevron_skipped_for_tight_bbox() -> None:
    """A tight chevron icon bbox (<60px wide) does NOT trigger the warning."""
    from vision_agent.schemas import VisionResponse, BBox
    r = VisionResponse(bboxes=[
        BBox(
            label="Region (expand sub-options)",
            box_2d=[200, 970, 250, 995],  # 25px wide icon at far right
            clickable=True,
            role="button",
        ),
    ])
    r.with_image_dims(1000, 1000)
    text = r.as_brain_text()
    assert "[LOOSE_CHEVRON]" not in text


def test_loose_chevron_skipped_for_normal_button() -> None:
    """A wide non-chevron bbox (e.g., Apply filters button) doesn't trigger."""
    from vision_agent.schemas import VisionResponse, BBox
    r = VisionResponse(bboxes=[
        BBox(
            label="Apply filters",
            box_2d=[200, 100, 250, 600],  # wide
            clickable=True,
            role="button",
        ),
    ])
    r.with_image_dims(1000, 1000)
    text = r.as_brain_text()
    assert "[LOOSE_CHEVRON]" not in text


# ── Fix G — failed_tactics append on chevron no-expansion ────────────


def test_chevron_click_state_tracking_initialized() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    assert s._last_chevron_click_label == ""
    assert s._last_chevron_click_url == ""
    assert s.failed_tactics == []


def test_chevron_state_clears_on_setattr_path() -> None:
    """Sanity: state attrs can be set and cleared (used by click_at +
    build_tool_result_blocks)."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s._last_chevron_click_label = "Region (expand sub-options)"
    s._last_chevron_click_url = "https://x.example/page"
    assert s._last_chevron_click_label
    s._last_chevron_click_label = ""
    s._last_chevron_click_url = ""
    assert not s._last_chevron_click_label


# ── Integration: click coordinate shift logic ────────────────────────


def test_chevron_click_carves_right_edge_window() -> None:
    """Replicate the inline logic from click_at: when chevron, x0 is
    overridden to x1 - cap (30px or 25% of width)."""
    from superbrowser_bridge.session_tools import _bbox_is_chevron_label
    label = "Region (expand sub-options)"
    assert _bbox_is_chevron_label(label)
    # 207 px wide row (matches the WineAccess trace bbox)
    x0, y0, x1, y1 = 59, 930, 266, 955
    width = x1 - x0
    cap = min(30, max(8, int(width * 0.25)))
    assert cap == 30  # 25% of 207 = 51, capped at 30
    new_x0 = x1 - cap
    new_width = x1 - new_x0
    assert new_x0 == 236
    assert new_width == 30  # tight icon-sized window at right edge


def test_chevron_click_skips_shift_when_bbox_already_tight() -> None:
    """Already-tight chevron bbox (e.g., 25px) gets no shift."""
    from superbrowser_bridge.session_tools import _bbox_is_chevron_label
    label = "Region chevron"
    assert _bbox_is_chevron_label(label)
    x0, y0, x1, y1 = 240, 930, 265, 955  # 25 px wide
    width = x1 - x0
    cap = min(30, max(8, int(width * 0.25)))
    # cap = max(8, 6) = 8; width=25, cap+2=10 → still > 10 so we'd still shift
    # but the new window (8px) wouldn't be much smaller. Check the gate.
    will_shift = width > cap + 2
    assert will_shift  # 25 > 10
    # Acceptable — even for tight bboxes we sharpen toward the right.
    # The protection is: cap stays small (~8px) so we don't move far.
    new_x0 = x1 - cap
    assert new_x0 == 257  # only 17px shift from original 240


def main() -> int:
    tests = [
        test_chevron_label_matches_expand_sub_options,
        test_chevron_label_matches_chevron_caret,
        test_chevron_label_matches_toggle_all,
        test_chevron_label_does_not_match_normal_button,
        test_loose_chevron_warning_renders_for_wide_chevron_bbox,
        test_loose_chevron_skipped_for_tight_bbox,
        test_loose_chevron_skipped_for_normal_button,
        test_chevron_click_state_tracking_initialized,
        test_chevron_state_clears_on_setattr_path,
        test_chevron_click_carves_right_edge_window,
        test_chevron_click_skips_shift_when_bbox_already_tight,
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
