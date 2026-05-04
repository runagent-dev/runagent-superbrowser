"""Unit tests for the state-freshness gate (arch v3 fix #5).

The gate refuses a state-change tool when a previous state-change ran
without a screenshot in between — eliminating the click → eval → script
detour pattern observed in WineAccess + similar dense-filter sites.

Tests use the BrowserSessionState directly (no HTTP server). They drive
the dirty flag via record_step (the canonical entry point) and check
must_screenshot_before_state_change's verdicts.

Arch v4 note: the gate now also enforces the preplan-lock layer (Move 1).
These tests isolate freshness-only behavior by setting PREPLAN_GATE=0
in module-level setUp; the preplan layer has its own dedicated tests in
test_preplan_gate.py.

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_freshness_gate
"""

from __future__ import annotations

import os
import sys

# Module-level: isolate freshness gate from preplan layer for these tests.
os.environ["PREPLAN_GATE"] = "0"


def test_initial_state_allows_action() -> None:
    """A fresh session has no dirty flag, so the first action passes."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    assert not s.dom_dirty_since_screenshot
    assert s.must_screenshot_before_state_change("browser_click_at") is None


def test_record_step_sets_dirty_for_mutating_tools() -> None:
    """A click via record_step flips the dirty flag."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.record_step("browser_click_at", "V1=Shop", "ok")
    assert s.dom_dirty_since_screenshot
    assert s.last_mutating_tool == "browser_click_at"


def test_record_step_does_not_dirty_observation_tools() -> None:
    """browser_get_markdown / browser_screenshot do not dirty the flag."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.record_step("browser_get_markdown", "", "page text...")
    assert not s.dom_dirty_since_screenshot
    s.record_step("browser_screenshot", "", "screenshot ok")
    assert not s.dom_dirty_since_screenshot


def test_gate_refuses_mutate_after_mutate() -> None:
    """Click then eval — the eval gets refused until a screenshot."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.session_id = "session-x"
    # First click — no prior dirty, allowed
    assert s.must_screenshot_before_state_change("browser_click_at") is None
    s.record_step("browser_click_at", "V1=Shop", "ok")
    # Now try eval — refused
    msg = s.must_screenshot_before_state_change("browser_eval")
    assert msg is not None
    assert "refused" in msg.lower()
    assert "browser_screenshot" in msg
    assert "browser_state_check" in msg
    # And run_script
    assert s.must_screenshot_before_state_change("browser_run_script") is not None
    # And another click_at
    assert s.must_screenshot_before_state_change("browser_click_at") is not None


def test_gate_clears_after_clear_dom_dirty() -> None:
    """Clearing the flag (via screenshot path) re-enables actions."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.record_step("browser_click_at", "V1", "ok")
    assert s.must_screenshot_before_state_change("browser_eval") is not None
    s.clear_dom_dirty()
    assert s.must_screenshot_before_state_change("browser_eval") is None


def test_gate_does_not_block_observation_tools() -> None:
    """Observation tools (screenshot, get_markdown, state_check) are
    never refused by the freshness gate, even when dirty."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.record_step("browser_click_at", "V1", "ok")
    for obs in (
        "browser_screenshot", "browser_state_check",
        "browser_get_markdown", "browser_look_again",
    ):
        assert s.must_screenshot_before_state_change(obs) is None, (
            f"observation tool {obs!r} should never be refused"
        )


def test_gate_does_not_block_navigation() -> None:
    """browser_navigate is exempt — navigation is often a recovery move
    and its success path returns vision results that clear the flag."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.record_step("browser_click_at", "V1", "ok")
    assert s.must_screenshot_before_state_change("browser_navigate") is None
    assert s.must_screenshot_before_state_change("browser_open") is None


def test_gate_kill_switch() -> None:
    """STATE_FRESHNESS_GATE=0 disables the gate entirely."""
    import os
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.record_step("browser_click_at", "V1", "ok")
    os.environ["STATE_FRESHNESS_GATE"] = "0"
    try:
        assert s.must_screenshot_before_state_change("browser_eval") is None
    finally:
        del os.environ["STATE_FRESHNESS_GATE"]
    # Default behavior restored
    assert s.must_screenshot_before_state_change("browser_eval") is not None


def test_chained_observation_clears_then_chained_action_allowed() -> None:
    """Realistic flow: click → screenshot → click is allowed."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.record_step("browser_click_at", "V1=Shop", "ok")
    assert s.must_screenshot_before_state_change("browser_click_at") is not None
    # Brain takes a screenshot — flag clears (clear_dom_dirty is called
    # in build_tool_result_blocks; we simulate that explicitly here).
    s.clear_dom_dirty()
    # Now another click is allowed.
    assert s.must_screenshot_before_state_change("browser_click_at") is None


def test_parallel_batch_second_call_refused_immediately() -> None:
    """Fix C: same-turn parallel mutating batch — first gate-pass marks
    dirty IMMEDIATELY (not at record_step), so the second call refused."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    # Both calls happen "in the same turn" — neither has called record_step yet.
    # First call: gate allows AND marks dirty.
    first = s.must_screenshot_before_state_change("browser_click_selector")
    assert first is None
    # Second call (parallel): gate sees dirty=True from the first call's
    # gate-pass mark, refuses.
    second = s.must_screenshot_before_state_change("browser_click_selector")
    assert second is not None
    assert "refused" in second.lower()


def test_refusal_message_names_prior_tool_and_outcome() -> None:
    """The refusal text references the prior mutating tool and a
    summary so the brain knows what state it's recovering from."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.record_step("browser_click_at", "V1=United States", "click_silent — DOM unchanged")
    msg = s.must_screenshot_before_state_change("browser_eval")
    assert msg is not None
    assert "browser_click_at" in msg
    # The outcome is reflected somewhere in the message
    assert "click_silent" in msg or "DOM unchanged" in msg


def main() -> int:
    tests = [
        test_initial_state_allows_action,
        test_record_step_sets_dirty_for_mutating_tools,
        test_record_step_does_not_dirty_observation_tools,
        test_gate_refuses_mutate_after_mutate,
        test_gate_clears_after_clear_dom_dirty,
        test_gate_does_not_block_observation_tools,
        test_gate_does_not_block_navigation,
        test_gate_kill_switch,
        test_chained_observation_clears_then_chained_action_allowed,
        test_parallel_batch_second_call_refused_immediately,
        test_refusal_message_names_prior_tool_and_outcome,
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
