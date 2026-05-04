"""Unit tests for Arch v4 Phase C: browser_preplan tool + gate.

Covers:
  - Preplan tool sets state.preplan_lock with declared fields
  - Default focus_constraint_idx falls back to current_focus_idx
  - expected_postcondition auto-derived (navigate→url_changed,
    click+toggle-label→bbox_state_change, else dom_mutated)
  - Lock consumed by next state-change (gate refuses 2nd state-change)
  - Refusal text quotes the prior preplan
  - PREPLAN_GATE=0 disables the preplan layer (freshness still applies)
  - Deadlock backoff: 3 consecutive refusals → auto-yield + flag
  - PREPLAN_BACKOFF=0 keeps refusing on stuck loops
  - Warns when focus is out of range / already satisfied / failed
  - 1-based focus index in tool API maps to 0-based on the lock

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_preplan_gate
"""

from __future__ import annotations

import asyncio
import os
import sys

# Arch v4.2: PREPLAN_GATE defaults to OFF in v4.2 (BrowserPreplanTool
# is no longer registered in the default tool surface — see
# session_tools.py::register_session_tools). These tests exercise the
# gate's enforcement and refusal behavior, so they need it ON.
os.environ.setdefault("PREPLAN_GATE", "1")


def _state_with_brief():
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_brief import TaskBrief, Constraint, compute_focus

    s = BrowserSessionState()
    s.session_id = "session-x"
    s.task_brief = TaskBrief(
        original_query="find a hotel with WiFi and parking",
        constraints=[
            Constraint(text="WiFi", kind="filter", canonical_value="wifi",
                       status="unverified"),
            Constraint(text="parking", kind="filter",
                       canonical_value="parking", status="unverified"),
        ],
    )
    s.task_brief.current_focus_idx = compute_focus(s.task_brief)
    return s


# ── BrowserPreplanTool: parameter handling ──────────────────────────


def test_preplan_sets_lock_with_declared_fields() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    tool = BrowserPreplanTool(s)
    out = asyncio.run(tool.execute(
        session_id="session-x",
        focus_constraint_idx=1,  # 1-based: first constraint
        planned_tool="click_at",
        planned_target_label="WiFi filter chip",
        planned_target_vision_index=3,
        expected_outcome="WiFi chip becomes checked",
        expected_postcondition="bbox_state_change",
    ))
    assert "[preplan_locked]" in out
    assert s.preplan_lock is not None
    assert s.preplan_lock.focus_constraint_idx == 0  # 1-based -> 0-based
    assert s.preplan_lock.planned_tool == "click_at"
    assert s.preplan_lock.planned_target_label == "WiFi filter chip"
    assert s.preplan_lock.planned_target_vision_index == 3
    assert s.preplan_lock.expected_outcome == "WiFi chip becomes checked"
    assert s.preplan_lock.expected_postcondition == "bbox_state_change"
    assert s.preplan_lock_consumed is False


def test_preplan_default_focus_uses_system_recommendation() -> None:
    """When focus_constraint_idx is omitted/-1, falls back to
    brief.current_focus_idx."""
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    s.task_brief.current_focus_idx = 1  # parking
    tool = BrowserPreplanTool(s)
    asyncio.run(tool.execute(
        session_id="session-x",
        planned_tool="click_at",
        expected_outcome="parking chip becomes checked",
    ))
    assert s.preplan_lock.focus_constraint_idx == 1


def test_preplan_auto_derives_postcondition_navigate() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    tool = BrowserPreplanTool(s)
    asyncio.run(tool.execute(
        session_id="session-x",
        planned_tool="browser_navigate",
        expected_outcome="land on results page",
        # expected_postcondition omitted
    ))
    assert s.preplan_lock.expected_postcondition == "url_changed"


def test_preplan_auto_derives_postcondition_toggle_chip() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    tool = BrowserPreplanTool(s)
    asyncio.run(tool.execute(
        session_id="session-x",
        planned_tool="click_at",
        planned_target_label="WiFi filter chip",
        expected_outcome="becomes active",
    ))
    assert s.preplan_lock.expected_postcondition == "bbox_state_change"


def test_preplan_auto_derives_postcondition_default_dom_mutated() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    tool = BrowserPreplanTool(s)
    asyncio.run(tool.execute(
        session_id="session-x",
        planned_tool="click_at",
        planned_target_label="Search button",
        expected_outcome="results render",
    ))
    assert s.preplan_lock.expected_postcondition == "dom_mutated"


def test_preplan_warns_on_satisfied_focus() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    s.task_brief.constraints[0].status = "satisfied"
    tool = BrowserPreplanTool(s)
    out = asyncio.run(tool.execute(
        session_id="session-x",
        focus_constraint_idx=1,  # 1-based: the satisfied one
        planned_tool="click_at",
        expected_outcome="x",
    ))
    assert "already satisfied" in out


def test_preplan_warns_on_out_of_range_focus() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    tool = BrowserPreplanTool(s)
    out = asyncio.run(tool.execute(
        session_id="session-x",
        focus_constraint_idx=99,  # out of range
        planned_tool="click_at",
        expected_outcome="x",
    ))
    assert "out of range" in out


# ── Gate: lock consumption ──────────────────────────────────────────


def test_first_state_change_after_preplan_consumes_lock() -> None:
    """A state-change after a fresh preplan passes the gate AND marks
    the lock consumed."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, PreplanLock,
    )
    s = BrowserSessionState()
    s.session_id = "sid"
    s.preplan_lock = PreplanLock(
        focus_constraint_idx=0,
        planned_tool="click_at",
        expected_outcome="x",
    )
    s.preplan_lock_consumed = False
    # First call: passes both freshness AND preplan gate.
    assert s.must_screenshot_before_state_change("browser_click_at") is None
    assert s.preplan_lock_consumed is True


def test_second_state_change_without_re_preplan_refused() -> None:
    """After consuming the lock, the next state-change is refused
    unless a screenshot AND a fresh preplan run."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, PreplanLock,
    )
    s = BrowserSessionState()
    s.session_id = "sid"
    s.preplan_lock = PreplanLock(
        focus_constraint_idx=0,
        planned_tool="click_at",
        planned_target_label="WiFi",
        expected_outcome="x",
    )
    # First call: allowed.
    s.must_screenshot_before_state_change("browser_click_at")
    # Clear dirty (simulating a screenshot in between).
    s.clear_dom_dirty()
    # Second call: still has consumed lock, freshness OK, but preplan
    # is consumed → refused with preplan refusal text.
    msg = s.must_screenshot_before_state_change("browser_click_at")
    assert msg is not None
    assert "preplan_lock consumed" in msg
    assert "browser_preplan" in msg


def test_refusal_quotes_prior_preplan() -> None:
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, PreplanLock,
    )
    s = BrowserSessionState()
    s.session_id = "sid"
    s.preplan_lock = PreplanLock(
        focus_constraint_idx=0,
        planned_tool="click_at",
        planned_target_label="WiFi filter chip",
        expected_outcome="WiFi chip becomes checked",
    )
    # Consume.
    s.must_screenshot_before_state_change("browser_click_at")
    s.clear_dom_dirty()
    # Next refused.
    msg = s.must_screenshot_before_state_change("browser_eval")
    assert "click_at" in msg
    assert "WiFi filter chip" in msg
    assert "WiFi chip becomes checked" in msg


def test_no_prior_preplan_refuses_with_explicit_message() -> None:
    """A fresh session with no preplan refuses the first state-change."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.session_id = "sid"
    msg = s.must_screenshot_before_state_change("browser_click_at")
    assert msg is not None
    assert "(no prior preplan on this session)" in msg


# ── Kill switches ───────────────────────────────────────────────────


def test_preplan_gate_kill_switch_disables_layer() -> None:
    """PREPLAN_GATE=0 keeps freshness checks but disables preplan
    enforcement."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.session_id = "sid"
    os.environ["PREPLAN_GATE"] = "0"
    try:
        # No preplan, but gate yields.
        assert s.must_screenshot_before_state_change("browser_click_at") is None
    finally:
        # Restore to "1" (test-suite default) so downstream tests in
        # the same run still exercise the gate. v4.2 production default
        # is "0"; we override to "1" at module top.
        os.environ["PREPLAN_GATE"] = "1"


# ── Deadlock backoff ────────────────────────────────────────────────


def test_backoff_yields_after_three_consecutive_refusals() -> None:
    """Three preplan refusals in a row → next call yields with the
    backoff flag set, instead of refusing again."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.session_id = "sid"
    # Three refusals in a row (no preplan, dirty cleared between):
    for _ in range(3):
        s.clear_dom_dirty()
        msg = s.must_screenshot_before_state_change("browser_click_at")
        assert msg is not None  # all three refused
    assert s.preplan_consecutive_refusals == 3
    # Fourth call: backoff fires — allowed.
    s.clear_dom_dirty()
    out = s.must_screenshot_before_state_change("browser_click_at")
    assert out is None
    assert s.preplan_backoff_just_fired is True
    # Counter reset.
    assert s.preplan_consecutive_refusals == 0


def test_backoff_kill_switch_keeps_refusing() -> None:
    """PREPLAN_BACKOFF=0 means stuck loops keep refusing rather than
    auto-yielding."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.session_id = "sid"
    os.environ["PREPLAN_BACKOFF"] = "0"
    try:
        for _ in range(5):
            s.clear_dom_dirty()
            msg = s.must_screenshot_before_state_change("browser_click_at")
            assert msg is not None  # always refused
        assert s.preplan_backoff_just_fired is False
    finally:
        del os.environ["PREPLAN_BACKOFF"]


def test_successful_preplan_resets_refusal_counter() -> None:
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserPreplanTool, PreplanLock,
    )
    s = BrowserSessionState()
    s.session_id = "sid"
    # Two refusals.
    s.clear_dom_dirty()
    s.must_screenshot_before_state_change("browser_click_at")
    s.clear_dom_dirty()
    s.must_screenshot_before_state_change("browser_click_at")
    assert s.preplan_consecutive_refusals == 2
    # Now run a successful preplan.
    asyncio.run(BrowserPreplanTool(s).execute(
        session_id="sid",
        planned_tool="click_at",
        expected_outcome="x",
    ))
    # Counter reset.
    assert s.preplan_consecutive_refusals == 0
    # Now a state-change consumes the fresh lock without refusal.
    s.clear_dom_dirty()
    assert s.must_screenshot_before_state_change("browser_click_at") is None


# ── Backoff guidance reaches worker_hook ────────────────────────────


def test_worker_hook_surfaces_gate_backoff_after_yield() -> None:
    """When the gate yields, worker_hook injects [GATE_BACKOFF n=1]
    and clears the flag for next iteration."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook
    s = BrowserSessionState()
    s.session_id = "sid"
    s.preplan_backoff_just_fired = True
    hook = BrowserWorkerHook(s)
    ctx = type(
        "Ctx", (),
        {"iteration": 0, "messages": [{"role": "tool", "content": "x"}]},
    )()
    asyncio.run(hook.after_iteration(ctx))
    msg = ctx.messages[-1]["content"]
    assert "[GATE_BACKOFF n=1]" in msg
    assert s.preplan_backoff_just_fired is False  # cleared


def main() -> int:
    tests = [
        # tool param handling
        test_preplan_sets_lock_with_declared_fields,
        test_preplan_default_focus_uses_system_recommendation,
        test_preplan_auto_derives_postcondition_navigate,
        test_preplan_auto_derives_postcondition_toggle_chip,
        test_preplan_auto_derives_postcondition_default_dom_mutated,
        test_preplan_warns_on_satisfied_focus,
        test_preplan_warns_on_out_of_range_focus,
        # gate consumption
        test_first_state_change_after_preplan_consumes_lock,
        test_second_state_change_without_re_preplan_refused,
        test_refusal_quotes_prior_preplan,
        test_no_prior_preplan_refuses_with_explicit_message,
        # kill switch
        test_preplan_gate_kill_switch_disables_layer,
        # backoff
        test_backoff_yields_after_three_consecutive_refusals,
        test_backoff_kill_switch_keeps_refusing,
        test_successful_preplan_resets_refusal_counter,
        # hook surfacing
        test_worker_hook_surfaces_gate_backoff_after_yield,
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
