"""Unit tests for Arch v4 Phase D: TOOL LADDER ratchet.

Covers:
  - Tier 1 (click_at) always allowed at preplan time
  - Tier 2 (click_selector) allowed without t1 prior (the selector
    tools have their own SELECTOR_VISION_ALIGNMENT gate)
  - Tier 3 (run_script / eval) refused unless Tier 1 + Tier 2 both
    have ≥1 attempted-and-failed for the same target
  - Tier 3 unblocked once both lower tiers have a recorded failure
  - Per-target ledger isolated: failure on target-A doesn't unlock
    Tier 3 for target-B
  - Cold-start navigate bypasses Tier 4
  - Cross-domain navigate bypasses Tier 4
  - Same-domain navigate after cold-start refused without prior
    cursor failure
  - Same-domain navigate allowed after ≥1 cursor failure
  - TOOL_LADDER=0 disables the whole layer
  - TOOL_LADDER_TIER3=0 / TIER4=0 disable per-tier
  - clear_tool_attempts_for_lock resets the ledger for one target

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_tool_ladder
"""

from __future__ import annotations

import asyncio
import os
import sys


def _state_with_brief():
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_brief import TaskBrief, Constraint, compute_focus

    s = BrowserSessionState()
    s.session_id = "session-x"
    s.task_brief = TaskBrief(
        original_query="hotel with WiFi and parking",
        constraints=[
            Constraint(text="WiFi", kind="filter", canonical_value="wifi",
                       status="unverified"),
            Constraint(text="parking", kind="filter",
                       canonical_value="parking", status="unverified"),
        ],
    )
    s.task_brief.current_focus_idx = compute_focus(s.task_brief)
    return s


# ── Tier 1 + Tier 2 allowed at preplan time ─────────────────────────


def test_tier1_click_at_always_allowed() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    out = asyncio.run(BrowserPreplanTool(s).execute(
        session_id="session-x",
        focus_constraint_idx=1,
        planned_tool="browser_click_at",
        planned_target_label="WiFi filter chip",
        expected_outcome="chip becomes checked",
    ))
    assert "[preplan_locked]" in out
    assert "ladder_violation" not in out


def test_tier2_click_selector_allowed_without_t1_prior() -> None:
    """Tier 2 selector tools have their own vision-alignment gate;
    the ladder doesn't double-enforce."""
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    out = asyncio.run(BrowserPreplanTool(s).execute(
        session_id="session-x",
        focus_constraint_idx=1,
        planned_tool="browser_click_selector",
        planned_target_label="WiFi",
        expected_outcome="x",
    ))
    assert "[preplan_locked]" in out


# ── Tier 3 refusal until lower tiers fail ──────────────────────────


def test_tier3_run_script_refused_with_no_prior_attempts() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    out = asyncio.run(BrowserPreplanTool(s).execute(
        session_id="session-x",
        focus_constraint_idx=1,
        planned_tool="browser_run_script",
        planned_target_label="WiFi",
        expected_outcome="x",
    ))
    assert "[preplan_ladder_violation tier=3" in out
    assert "Tier 1 (click_at/type_at): 0 attempts, 0 failed" in out
    assert s.preplan_lock is None  # lock NOT set on refusal


def test_tier3_refused_when_only_tier1_failed() -> None:
    from superbrowser_bridge.session_tools import (
        BrowserPreplanTool, PreplanLock,
    )
    s = _state_with_brief()
    # Simulate a Tier 1 failure for the WiFi target.
    s.preplan_lock = PreplanLock(
        focus_constraint_idx=0,
        planned_tool="browser_click_at",
        planned_target_label="WiFi",
    )
    s.record_step("browser_click_at", "V_3", "[click_silent] DOM unchanged")
    # Tier 1 has 1 attempt, 1 failed; Tier 2 has 0 attempts.
    out = asyncio.run(BrowserPreplanTool(s).execute(
        session_id="session-x",
        focus_constraint_idx=1,  # 1-based → WiFi
        planned_tool="browser_run_script",
        planned_target_label="WiFi",
        expected_outcome="x",
    ))
    assert "[preplan_ladder_violation tier=3" in out
    assert "Tier 1 (click_at/type_at): 1 attempts, 1 failed" in out
    assert "Tier 2" in out and "0 attempts" in out


def test_tier3_unblocked_after_both_lower_failed() -> None:
    from superbrowser_bridge.session_tools import (
        BrowserPreplanTool, PreplanLock,
    )
    s = _state_with_brief()
    # Tier 1 + Tier 2 each failed for WiFi.
    s.preplan_lock = PreplanLock(
        focus_constraint_idx=0,
        planned_tool="browser_click_at",
        planned_target_label="WiFi",
    )
    s.record_step("browser_click_at", "V_3", "[click_silent]")
    s.record_step("browser_click_selector", "[data-testid=wifi]",
                  "[click_selector_failed] not found")
    # Now Tier 3 should be allowed.
    out = asyncio.run(BrowserPreplanTool(s).execute(
        session_id="session-x",
        focus_constraint_idx=1,
        planned_tool="browser_run_script",
        planned_target_label="WiFi",
        expected_outcome="x",
    ))
    assert "[preplan_locked]" in out
    assert "ladder_violation" not in out


# ── per-target isolation ───────────────────────────────────────────


def test_per_target_ledger_isolated() -> None:
    """Failures on the WiFi target don't unlock Tier 3 for parking."""
    from superbrowser_bridge.session_tools import (
        BrowserPreplanTool, PreplanLock,
    )
    s = _state_with_brief()
    # Burn down WiFi's ladder.
    s.preplan_lock = PreplanLock(
        focus_constraint_idx=0,
        planned_tool="browser_click_at",
        planned_target_label="WiFi",
    )
    s.record_step("browser_click_at", "V_3", "[click_silent]")
    s.record_step("browser_click_selector", "[data-testid=wifi]",
                  "[click_selector_failed]")
    # Now declare Tier 3 for PARKING — should be refused (parking
    # has its own ledger entry which is empty).
    out = asyncio.run(BrowserPreplanTool(s).execute(
        session_id="session-x",
        focus_constraint_idx=2,  # 1-based → parking
        planned_tool="browser_run_script",
        planned_target_label="Parking",
        expected_outcome="x",
    ))
    assert "[preplan_ladder_violation tier=3" in out
    assert "0 attempts" in out  # parking ledger empty


# ── Tier 4 navigate ─────────────────────────────────────────────────


def test_navigate_cold_start_bypasses_tier4() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    s.is_cold_start = True
    out = asyncio.run(BrowserPreplanTool(s).execute(
        session_id="session-x",
        planned_tool="browser_navigate",
        planned_target_label="https://example.com/foo",
        expected_outcome="page loads",
    ))
    assert "[preplan_locked]" in out
    assert "ladder_violation" not in out


def test_navigate_cross_domain_bypasses_tier4() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    s.is_cold_start = False
    s.current_url = "https://example.com/page"
    out = asyncio.run(BrowserPreplanTool(s).execute(
        session_id="session-x",
        planned_tool="browser_navigate",
        planned_target_label="https://other-site.com/page",
        expected_outcome="x",
    ))
    assert "[preplan_locked]" in out
    assert "ladder_violation" not in out


def test_navigate_same_domain_refused_without_prior_failure() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    s.is_cold_start = False
    s.current_url = "https://example.com/page"
    out = asyncio.run(BrowserPreplanTool(s).execute(
        session_id="session-x",
        planned_tool="browser_navigate",
        planned_target_label="https://example.com/other",
        expected_outcome="x",
    ))
    assert "[preplan_ladder_violation tier=4" in out
    assert "same-domain" in out


def test_navigate_same_domain_allowed_after_cursor_failure() -> None:
    from superbrowser_bridge.session_tools import (
        BrowserPreplanTool, PreplanLock,
    )
    s = _state_with_brief()
    s.is_cold_start = False
    s.current_url = "https://example.com/page"
    # Burn one cursor attempt for the navigate target's "label".
    s.preplan_lock = PreplanLock(
        focus_constraint_idx=0,
        planned_tool="browser_click_at",
        planned_target_label="https://example.com/other",
    )
    s.record_step("browser_click_at", "V_2", "[click_silent]")
    out = asyncio.run(BrowserPreplanTool(s).execute(
        session_id="session-x",
        planned_tool="browser_navigate",
        planned_target_label="https://example.com/other",
        expected_outcome="x",
    ))
    assert "[preplan_locked]" in out


# ── Kill switches ───────────────────────────────────────────────────


def test_tool_ladder_kill_switch_disables_entirely() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    os.environ["TOOL_LADDER"] = "0"
    try:
        out = asyncio.run(BrowserPreplanTool(s).execute(
            session_id="session-x",
            focus_constraint_idx=1,
            planned_tool="browser_run_script",
            planned_target_label="WiFi",
            expected_outcome="x",
        ))
        assert "[preplan_locked]" in out
    finally:
        del os.environ["TOOL_LADDER"]


def test_tier3_kill_switch_only() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    os.environ["TOOL_LADDER_TIER3"] = "0"
    try:
        out = asyncio.run(BrowserPreplanTool(s).execute(
            session_id="session-x",
            focus_constraint_idx=1,
            planned_tool="browser_run_script",
            planned_target_label="WiFi",
            expected_outcome="x",
        ))
        assert "[preplan_locked]" in out
    finally:
        del os.environ["TOOL_LADDER_TIER3"]


def test_tier4_kill_switch_only() -> None:
    from superbrowser_bridge.session_tools import BrowserPreplanTool
    s = _state_with_brief()
    s.is_cold_start = False
    s.current_url = "https://example.com/a"
    os.environ["TOOL_LADDER_TIER4"] = "0"
    try:
        out = asyncio.run(BrowserPreplanTool(s).execute(
            session_id="session-x",
            planned_tool="browser_navigate",
            planned_target_label="https://example.com/b",
            expected_outcome="x",
        ))
        assert "[preplan_locked]" in out
    finally:
        del os.environ["TOOL_LADDER_TIER4"]


# ── Ledger reset on success ────────────────────────────────────────


def test_clear_tool_attempts_resets_ledger() -> None:
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, PreplanLock,
    )
    s = BrowserSessionState()
    s.session_id = "sid"
    s.preplan_lock = PreplanLock(
        focus_constraint_idx=-1,
        planned_tool="browser_click_at",
        planned_target_label="WiFi",
    )
    s.record_step("browser_click_at", "x", "[click_silent]")
    assert s.tool_attempts  # has entries
    s.clear_tool_attempts_for_lock(s.preplan_lock)
    assert s.tool_attempts == {}


def test_record_step_clears_cold_start_flag() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    assert s.is_cold_start
    # browser_open / browser_navigate don't drop cold-start.
    s.record_step("browser_navigate", "https://x", "ok")
    assert s.is_cold_start
    s.record_step("browser_open", "https://x", "ok")
    assert s.is_cold_start
    # First non-navigate mutating tool drops it.
    s.record_step("browser_click_at", "V_1", "ok")
    assert not s.is_cold_start


def main() -> int:
    tests = [
        # tier 1/2
        test_tier1_click_at_always_allowed,
        test_tier2_click_selector_allowed_without_t1_prior,
        # tier 3
        test_tier3_run_script_refused_with_no_prior_attempts,
        test_tier3_refused_when_only_tier1_failed,
        test_tier3_unblocked_after_both_lower_failed,
        test_per_target_ledger_isolated,
        # tier 4
        test_navigate_cold_start_bypasses_tier4,
        test_navigate_cross_domain_bypasses_tier4,
        test_navigate_same_domain_refused_without_prior_failure,
        test_navigate_same_domain_allowed_after_cursor_failure,
        # kill switches
        test_tool_ladder_kill_switch_disables_entirely,
        test_tier3_kill_switch_only,
        test_tier4_kill_switch_only,
        # ledger reset
        test_clear_tool_attempts_resets_ledger,
        test_record_step_clears_cold_start_flag,
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
