"""Unit tests for the click_selector target_label validator (arch v3 fix A).

Verifies the structural rules:
  - Vague selectors (kitchen-sink commas, bare tags, role-only) require
    a target_label.
  - Specific selectors (#id, [data-testid], named attributes) don't.
  - Kill switch SELECTOR_TARGET_LABEL_REQUIRED=0 disables.

No HTTP server / DOM probe required.

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_selector_validation
"""

from __future__ import annotations

import sys


def test_vague_selector_without_label_refused() -> None:
    """Kitchen-sink selector + missing label → refusal."""
    from superbrowser_bridge.session_tools import _validate_selector_target_label
    msg = _validate_selector_target_label(
        "summary, button, [role='button']", target_label=None,
    )
    assert msg is not None
    assert "target_label_required" in msg


def test_vague_selector_with_label_allowed() -> None:
    """Kitchen-sink selector + named target → allowed."""
    from superbrowser_bridge.session_tools import _validate_selector_target_label
    msg = _validate_selector_target_label(
        "summary, button, [role='button']",
        target_label="Apply filters button",
    )
    assert msg is None


def test_bare_tag_selector_without_label_refused() -> None:
    from superbrowser_bridge.session_tools import _validate_selector_target_label
    for sel in ("button", "a", "label", "summary"):
        msg = _validate_selector_target_label(sel, target_label=None)
        assert msg is not None, f"selector {sel!r} should be refused"


def test_id_selector_allowed_without_label() -> None:
    """#accordion-region / #email don't need a target_label."""
    from superbrowser_bridge.session_tools import _validate_selector_target_label
    for sel in ("#accordion-region", "#email", "#submit-button"):
        msg = _validate_selector_target_label(sel, target_label=None)
        assert msg is None, f"selector {sel!r} should be allowed"


def test_data_testid_selector_allowed_without_label() -> None:
    from superbrowser_bridge.session_tools import _validate_selector_target_label
    for sel in (
        "[data-testid='filter-oregon']",
        "[data-test-id='apply']",
        "[data-cy='checkout']",
    ):
        msg = _validate_selector_target_label(sel, target_label=None)
        assert msg is None, f"selector {sel!r} should be allowed"


def test_role_only_selector_without_label_refused() -> None:
    """Bare [role='button'] is too vague."""
    from superbrowser_bridge.session_tools import _validate_selector_target_label
    msg = _validate_selector_target_label("[role='button']", target_label=None)
    assert msg is not None


def test_attribute_match_selector_allowed() -> None:
    """label[for*='oregon' i] is specific enough."""
    from superbrowser_bridge.session_tools import _validate_selector_target_label
    msg = _validate_selector_target_label(
        "label[for*='oregon' i]", target_label=None,
    )
    assert msg is None


def test_kill_switch_disables() -> None:
    import os
    from superbrowser_bridge.session_tools import _validate_selector_target_label
    os.environ["SELECTOR_TARGET_LABEL_REQUIRED"] = "0"
    try:
        msg = _validate_selector_target_label(
            "summary, button, [role='button']", target_label=None,
        )
        assert msg is None
    finally:
        del os.environ["SELECTOR_TARGET_LABEL_REQUIRED"]


def test_vision_alignment_refuses_unseen_label() -> None:
    """Fix D: target_label provided but no V_n with matching label →
    refuse. Brain hallucinated a target vision didn't see."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_selector_target_label,
    )
    from vision_agent.schemas import VisionResponse, BBox

    s = BrowserSessionState()
    s._last_vision_response = VisionResponse(bboxes=[
        BBox(label="Apply filters", role="button", clickable=True),
        BBox(label="Sort by price", role="button", clickable=True),
    ])
    msg = _validate_selector_target_label(
        "#region-united-states",
        target_label="United States",
        state=s,
    )
    assert msg is not None
    assert "label_unseen" in msg
    assert "Apply" in msg or "Sort" in msg  # V_n list rendered


def test_vision_alignment_allows_when_label_matches() -> None:
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_selector_target_label,
    )
    from vision_agent.schemas import VisionResponse, BBox

    s = BrowserSessionState()
    s._last_vision_response = VisionResponse(bboxes=[
        BBox(label="United States (expand sub-options)", role="button"),
        BBox(label="Apply filters", role="button"),
    ])
    msg = _validate_selector_target_label(
        "#region-united-states",
        target_label="United States",
        state=s,
    )
    assert msg is None


def test_vision_alignment_skipped_when_no_vision_yet() -> None:
    """First action on a session — vision hasn't run yet; allow."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_selector_target_label,
    )

    s = BrowserSessionState()
    # _last_vision_response is None
    msg = _validate_selector_target_label(
        "#email", target_label="Email field", state=s,
    )
    assert msg is None


def test_vision_alignment_kill_switch() -> None:
    import os
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_selector_target_label,
    )
    from vision_agent.schemas import VisionResponse, BBox

    s = BrowserSessionState()
    s._last_vision_response = VisionResponse(bboxes=[
        BBox(label="Something else", role="button"),
    ])
    os.environ["SELECTOR_VISION_ALIGNMENT"] = "0"
    try:
        msg = _validate_selector_target_label(
            "#x", target_label="Oregon", state=s,
        )
        assert msg is None  # kill switch disables alignment check
    finally:
        del os.environ["SELECTOR_VISION_ALIGNMENT"]


def test_layer_e_redirect_when_vision_already_has_match() -> None:
    """Fix L: high-confidence clickable V_n with matching label →
    refuse the selector call and tell brain to use click_at(V_n)."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_selector_target_label,
    )
    from vision_agent.schemas import VisionResponse, BBox

    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_vision_response = VisionResponse(bboxes=[
        BBox(
            label="Apply filters",
            role="button",
            clickable=True,
            confidence=0.85,
        ),
        BBox(
            label="United States (expand sub-options)",
            role="button",
            clickable=True,
            confidence=0.82,
        ),
    ])
    msg = _validate_selector_target_label(
        "#region-united-states",
        target_label="United States",
        state=s,
    )
    assert msg is not None
    assert "redundant_with_v_n" in msg
    assert "vision_index=" in msg
    assert "click_at" in msg
    assert "United States" in msg


def test_layer_e_allows_when_v_n_low_confidence() -> None:
    """When the matching V_n has confidence <0.7, selector might be
    more reliable — don't redirect."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_selector_target_label,
    )
    from vision_agent.schemas import VisionResponse, BBox

    s = BrowserSessionState()
    s._last_vision_response = VisionResponse(bboxes=[
        BBox(
            label="United States",
            role="button",
            clickable=True,
            confidence=0.4,  # low
        ),
    ])
    msg = _validate_selector_target_label(
        "#region-united-states",
        target_label="United States",
        state=s,
    )
    assert msg is None  # allowed


def test_layer_e_allows_when_v_n_not_clickable() -> None:
    """A read-only V_n (clickable=false) doesn't justify redirecting
    away from the selector path."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_selector_target_label,
    )
    from vision_agent.schemas import VisionResponse, BBox

    s = BrowserSessionState()
    s._last_vision_response = VisionResponse(bboxes=[
        BBox(
            label="United States label",
            role="text_block",
            clickable=False,
            confidence=0.9,
        ),
    ])
    msg = _validate_selector_target_label(
        "#region-united-states",
        target_label="United States",
        state=s,
    )
    assert msg is None


def test_layer_e_kill_switch() -> None:
    import os
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_selector_target_label,
    )
    from vision_agent.schemas import VisionResponse, BBox

    s = BrowserSessionState()
    s._last_vision_response = VisionResponse(bboxes=[
        BBox(
            label="United States (expand sub-options)",
            role="button", clickable=True, confidence=0.85,
        ),
    ])
    os.environ["SELECTOR_PREFER_VISION"] = "0"
    try:
        msg = _validate_selector_target_label(
            "#region-united-states",
            target_label="United States",
            state=s,
        )
        # Kill switch disables Layer E; falls through to Layer D which
        # ALSO finds a match → returns None (allows).
        assert msg is None
    finally:
        del os.environ["SELECTOR_PREFER_VISION"]


def test_short_target_label_treated_as_missing() -> None:
    """A 1-2 char target_label is too short to count as naming intent."""
    from superbrowser_bridge.session_tools import _validate_selector_target_label
    msg = _validate_selector_target_label(
        "summary, button", target_label="ok",
    )
    assert msg is not None  # too short


def main() -> int:
    tests = [
        test_vague_selector_without_label_refused,
        test_vague_selector_with_label_allowed,
        test_bare_tag_selector_without_label_refused,
        test_id_selector_allowed_without_label,
        test_data_testid_selector_allowed_without_label,
        test_role_only_selector_without_label_refused,
        test_attribute_match_selector_allowed,
        test_kill_switch_disables,
        test_short_target_label_treated_as_missing,
        test_vision_alignment_refuses_unseen_label,
        test_vision_alignment_allows_when_label_matches,
        test_vision_alignment_skipped_when_no_vision_yet,
        test_vision_alignment_kill_switch,
        test_layer_e_redirect_when_vision_already_has_match,
        test_layer_e_allows_when_v_n_low_confidence,
        test_layer_e_allows_when_v_n_not_clickable,
        test_layer_e_kill_switch,
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
