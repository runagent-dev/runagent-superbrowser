"""Tests for Round 3 semantic-target tools.

Verifies `_score_bbox` does what we need, and that the tools fail
cleanly when vision returns nothing matching.

No network. Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_semantic_tools.py
"""

from __future__ import annotations

import sys


class _FakeBbox:
    def __init__(self, label: str, role: str = "other",
                 clickable: bool = True, role_in_scene: str = "unknown") -> None:
        self.label = label
        self.role = role
        self.clickable = clickable
        self.role_in_scene = role_in_scene


def test_exact_label_match_scores_1() -> None:
    from superbrowser_bridge.semantic_tools import _score_bbox
    b = _FakeBbox("Accept cookies", role="button")
    assert _score_bbox(b, "Accept cookies") >= 0.9
    print("✓ test_exact_label_match_scores_1")


def test_substring_match_scores_high() -> None:
    from superbrowser_bridge.semantic_tools import _score_bbox
    b = _FakeBbox("Accept all cookies", role="button")
    assert _score_bbox(b, "accept") >= 0.8
    print("✓ test_substring_match_scores_high")


def test_button_role_boost() -> None:
    """Partial lexical match scores 0.6; role boost elevates a real
    button above a non-clickable div with the same text."""
    from superbrowser_bridge.semantic_tools import _score_bbox
    # Use a description that forces partial match (base < 1.0).
    btn = _FakeBbox("Continue shopping", role="button", clickable=True)
    div = _FakeBbox("Continue shopping", role="other", clickable=False)
    s_btn = _score_bbox(btn, "Continue")
    s_div = _score_bbox(div, "Continue")
    assert s_btn > s_div, f"button should outscore div ({s_btn} vs {s_div})"
    print("✓ test_button_role_boost")


def test_no_overlap_scores_zero() -> None:
    """Lexically unrelated description → 0.0 regardless of role boosts.
    A clickable link labeled 'About us' is NOT a 'Checkout' target."""
    from superbrowser_bridge.semantic_tools import _score_bbox
    b = _FakeBbox("About us", role="link", clickable=True)
    assert _score_bbox(b, "Checkout") == 0.0
    print("✓ test_no_overlap_scores_zero")


def test_input_boost_only_with_want_input() -> None:
    """Use a description that forces base=0.6 (partial token overlap)
    so the +0.15 input boost is visible rather than capped out at 1.0."""
    from superbrowser_bridge.semantic_tools import _score_bbox
    # Label tokens = {"where", "are", "you", "going"}. Desc tokens =
    # {"going", "somewhere"}. Overlap = 1/2 = 50% → base 0.6.
    inp = _FakeBbox("Where are you going", role="combobox", clickable=False)
    s_click = _score_bbox(inp, "going somewhere", want_input=False)
    s_type = _score_bbox(inp, "going somewhere", want_input=True)
    assert s_type > s_click, f"input boost should fire for typing ({s_type} vs {s_click})"
    print("✓ test_input_boost_only_with_want_input")


def test_role_in_scene_target_boost() -> None:
    """Partial-token-match forces base=0.6 so a target-vs-chrome
    role_in_scene difference shows in the final score without the
    1.0 cap swallowing everything."""
    from superbrowser_bridge.semantic_tools import _score_bbox
    # Both labels partially match "next step" via token "next". base=0.4.
    # Non-clickable divs so the button boost doesn't cap things out.
    t = _FakeBbox("Next step button here", role="div", role_in_scene="target", clickable=False)
    ch = _FakeBbox("Next step button here", role="div", role_in_scene="chrome", clickable=False)
    s_t = _score_bbox(t, "next step")
    s_ch = _score_bbox(ch, "next step")
    assert s_t > s_ch, f"target should outscore chrome ({s_t} vs {s_ch})"
    print("✓ test_role_in_scene_target_boost")


def test_labels_summary_truncates() -> None:
    from superbrowser_bridge.semantic_tools import _labels_summary
    bboxes = [_FakeBbox(f"Label {i}", role="button") for i in range(20)]
    out = _labels_summary(bboxes, limit=5)
    assert "Label 0" in out
    assert "+15 more" in out
    assert "Label 18" not in out  # beyond limit
    print("✓ test_labels_summary_truncates")


def main() -> int:
    tests = [
        test_exact_label_match_scores_1,
        test_substring_match_scores_high,
        test_button_role_boost,
        test_no_overlap_scores_zero,
        test_input_boost_only_with_want_input,
        test_role_in_scene_target_boost,
        test_labels_summary_truncates,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"✗ {t.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"✗ {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
