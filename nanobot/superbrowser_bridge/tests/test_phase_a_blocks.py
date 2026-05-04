"""Unit tests for Arch v4 Phase A: prompt-enrichment blocks.

Covers:
  - Move 0: TOOL LADDER block present in workspace_browser/SOUL.md
  - Move 5: [ORIGINAL_QUERY] pinned as first guidance line each iter
  - Move 5: [ORIGINAL_QUERY] suppressed by PIN_ORIGINAL_QUERY=0
  - Move 5: no [ORIGINAL_QUERY] when no brief
  - Move 7: [PROGRESS] +N satisfied delta on constraint flip
  - Move 7: [PROGRESS] suppressed on quiet turn below threshold
  - Move 7: [PROGRESS] stuck-variant fires at kind-aware threshold
  - Move 7: numeric kind has higher (6) threshold than filter (3)
  - Move 7: stuck-variant resets stagnation counter
  - Move 7: PROGRESS_BLOCK=0 disables the block

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_phase_a_blocks
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def _make_ctx(iteration: int = 0):
    return type(
        "Ctx",
        (),
        {"iteration": iteration, "messages": [{"role": "tool", "content": "x"}]},
    )()


def _last_msg(ctx) -> str:
    return ctx.messages[-1]["content"]


# ── Move 0 ──────────────────────────────────────────────────────────


def test_tool_ladder_block_present_in_worker_soul() -> None:
    """The worker SYSTEM prompt (workspace_browser/SOUL.md) contains the
    TOOL LADDER doctrine block declaring tier order."""
    soul = (
        Path(__file__).resolve().parents[2]
        / "workspace_browser"
        / "SOUL.md"
    )
    text = soul.read_text(encoding="utf-8")
    assert "TOOL LADDER" in text
    # The four rungs are explicitly named in tier order.
    assert "click_at" in text
    assert "click_selector" in text
    assert "browser_run_script" in text
    assert "browser_navigate" in text
    # Conflicting "SCRIPT FIRST" rule was rewritten to "CURSOR FIRST".
    assert "CURSOR FIRST" in text
    assert "SCRIPT FIRST" not in text


# ── Move 5 ──────────────────────────────────────────────────────────


def test_original_query_pinned_when_brief_exists() -> None:
    """[ORIGINAL_QUERY] line appears in the guidance text when a brief
    is set on the session state."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_brief import TaskBrief
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.task_brief = TaskBrief(
        original_query="find a hotel in Portland with WiFi and parking under $100",
    )
    hook = BrowserWorkerHook(s)
    ctx = _make_ctx()
    asyncio.run(hook.after_iteration(ctx))
    msg = _last_msg(ctx)
    assert "[ORIGINAL_QUERY]" in msg
    assert "Portland" in msg
    assert "WiFi" in msg


def test_original_query_full_verbatim_no_truncation() -> None:
    """The pin emits the full verbatim query, not a truncated form."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_brief import TaskBrief
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    long_q = (
        "I want a hotel in Portland Oregon for 2 nights starting March 14, "
        "with WiFi and free parking and breakfast included, under $100 per "
        "night, at least 4 stars, no smoking rooms, walking distance to "
        "downtown, with a fitness center if possible."
    )
    s = BrowserSessionState()
    s.session_id = "sid"
    s.task_brief = TaskBrief(original_query=long_q)
    hook = BrowserWorkerHook(s)
    ctx = _make_ctx()
    asyncio.run(hook.after_iteration(ctx))
    msg = _last_msg(ctx)
    assert long_q in msg


def test_original_query_kill_switch_disables() -> None:
    """PIN_ORIGINAL_QUERY=0 suppresses the [ORIGINAL_QUERY] line."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_brief import TaskBrief
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.task_brief = TaskBrief(original_query="find a hotel in Portland")
    hook = BrowserWorkerHook(s)
    ctx = _make_ctx()
    os.environ["PIN_ORIGINAL_QUERY"] = "0"
    try:
        asyncio.run(hook.after_iteration(ctx))
    finally:
        del os.environ["PIN_ORIGINAL_QUERY"]
    msg = _last_msg(ctx)
    assert "[ORIGINAL_QUERY]" not in msg


def test_original_query_skipped_when_no_brief() -> None:
    """No [ORIGINAL_QUERY] line when state.task_brief is None."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.task_brief = None
    hook = BrowserWorkerHook(s)
    ctx = _make_ctx()
    asyncio.run(hook.after_iteration(ctx))
    msg = _last_msg(ctx)
    assert "[ORIGINAL_QUERY]" not in msg


# ── Move 7 ──────────────────────────────────────────────────────────


def _seed_brief_with_constraints(kinds: list[str]) -> "TaskBrief":
    from superbrowser_bridge.task_brief import TaskBrief, Constraint

    return TaskBrief(
        original_query="x",
        constraints=[
            Constraint(
                text=f"c{i}", kind=k, canonical_value=f"c{i}",
                status="unverified",
            )
            for i, k in enumerate(kinds)
        ],
    )


def test_progress_emits_plus_satisfied_on_flip() -> None:
    """When a constraint flips unverified→satisfied, [PROGRESS] emits a
    delta line naming the flipped constraint."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.task_brief = _seed_brief_with_constraints(["filter", "filter"])
    hook = BrowserWorkerHook(s)

    # Iteration 1: seed (no prior snapshot, no emit).
    asyncio.run(hook.after_iteration(_make_ctx(iteration=0)))

    # Flip first constraint, run iter 2 — should emit the delta.
    s.task_brief.constraints[0].status = "satisfied"
    ctx = _make_ctx(iteration=1)
    asyncio.run(hook.after_iteration(ctx))
    msg = _last_msg(ctx)
    assert "[PROGRESS]" in msg
    assert "satisfied this turn" in msg
    assert "1/2 verified" in msg


def test_progress_suppressed_below_kind_threshold() -> None:
    """No [PROGRESS] line on quiet turns when stagnation count is under
    the focus constraint's kind-aware threshold."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    # Focus is filter (threshold 3); 2 quiet turns should be silent.
    s.task_brief = _seed_brief_with_constraints(["filter"])
    hook = BrowserWorkerHook(s)

    # Seed
    asyncio.run(hook.after_iteration(_make_ctx(iteration=0)))
    # 2 quiet turns
    ctx1 = _make_ctx(iteration=1)
    asyncio.run(hook.after_iteration(ctx1))
    ctx2 = _make_ctx(iteration=2)
    asyncio.run(hook.after_iteration(ctx2))
    assert "[PROGRESS]" not in _last_msg(ctx1)
    assert "[PROGRESS]" not in _last_msg(ctx2)


def test_progress_stuck_variant_at_filter_threshold() -> None:
    """At the filter-kind threshold (3 quiet turns), the stuck-variant
    [PROGRESS] line fires recommending re-evaluation of the focus."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.task_brief = _seed_brief_with_constraints(["filter"])
    hook = BrowserWorkerHook(s)

    # Seed + 3 quiet turns.
    asyncio.run(hook.after_iteration(_make_ctx(iteration=0)))
    asyncio.run(hook.after_iteration(_make_ctx(iteration=1)))
    asyncio.run(hook.after_iteration(_make_ctx(iteration=2)))
    ctx3 = _make_ctx(iteration=3)
    asyncio.run(hook.after_iteration(ctx3))
    msg = _last_msg(ctx3)
    assert "[PROGRESS]" in msg
    assert "No constraint flipped in" in msg
    assert "not_applicable" in msg


def test_progress_numeric_kind_has_higher_threshold() -> None:
    """Numeric constraints (sliders) have a 6-turn threshold; the stuck
    variant should NOT fire after only 3 turns of stagnation."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.task_brief = _seed_brief_with_constraints(["numeric"])
    hook = BrowserWorkerHook(s)

    asyncio.run(hook.after_iteration(_make_ctx(iteration=0)))
    asyncio.run(hook.after_iteration(_make_ctx(iteration=1)))
    asyncio.run(hook.after_iteration(_make_ctx(iteration=2)))
    ctx3 = _make_ctx(iteration=3)
    asyncio.run(hook.after_iteration(ctx3))
    # 3 quiet turns < numeric threshold (6) — no stuck variant yet.
    assert "No constraint flipped" not in _last_msg(ctx3)

    # Stretch to 6 quiet turns — should now fire.
    asyncio.run(hook.after_iteration(_make_ctx(iteration=4)))
    asyncio.run(hook.after_iteration(_make_ctx(iteration=5)))
    ctx6 = _make_ctx(iteration=6)
    asyncio.run(hook.after_iteration(ctx6))
    assert "No constraint flipped" in _last_msg(ctx6)


def test_progress_stuck_variant_resets_stagnation() -> None:
    """After firing the stuck variant, the stagnation counter is reset
    so the line doesn't re-fire every subsequent quiet turn."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.task_brief = _seed_brief_with_constraints(["filter"])
    hook = BrowserWorkerHook(s)

    asyncio.run(hook.after_iteration(_make_ctx(iteration=0)))
    asyncio.run(hook.after_iteration(_make_ctx(iteration=1)))
    asyncio.run(hook.after_iteration(_make_ctx(iteration=2)))
    ctx3 = _make_ctx(iteration=3)
    asyncio.run(hook.after_iteration(ctx3))
    assert "No constraint flipped" in _last_msg(ctx3)
    # The next quiet turn should NOT fire again — counter was reset.
    ctx4 = _make_ctx(iteration=4)
    asyncio.run(hook.after_iteration(ctx4))
    assert "No constraint flipped" not in _last_msg(ctx4)


def test_progress_block_kill_switch_disables() -> None:
    """PROGRESS_BLOCK=0 fully suppresses [PROGRESS] emission."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.task_brief = _seed_brief_with_constraints(["filter"])
    hook = BrowserWorkerHook(s)

    os.environ["PROGRESS_BLOCK"] = "0"
    try:
        asyncio.run(hook.after_iteration(_make_ctx(iteration=0)))
        # Force a flip — even with a flip, the block is suppressed.
        s.task_brief.constraints[0].status = "satisfied"
        ctx = _make_ctx(iteration=1)
        asyncio.run(hook.after_iteration(ctx))
    finally:
        del os.environ["PROGRESS_BLOCK"]
    assert "[PROGRESS]" not in _last_msg(ctx)


# ── Phase I: [CLICK_MISS_RETRY] nudge ──────────────────────────────


def test_click_miss_retry_nudge_fires_after_click_silent() -> None:
    """When the last step is browser_click_at with [click_silent] in
    its result, the hook injects a [CLICK_MISS_RETRY] nudge telling
    the brain to stay on click_at, not pivot to eval/run_script."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.step_history.append({
        "tool": "browser_click_at",
        "args": "V_1",
        "result": "[click_silent reason=dom_unchanged] click missed",
        "url": "https://x.com/", "time": "12:00:00",
    })
    hook = BrowserWorkerHook(s)
    ctx = _make_ctx(iteration=1)
    asyncio.run(hook.after_iteration(ctx))
    msg = _last_msg(ctx)
    assert "[CLICK_MISS_RETRY]" in msg
    assert "click_at AGAIN" in msg
    assert "browser_eval" in msg  # mention the don't-pivot list


def test_click_miss_retry_nudge_fires_on_verify_miss() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.step_history.append({
        "tool": "browser_click_at",
        "args": "V_1",
        "result": "[VERIFY_MISS kind=dom_mutated] click dispatched but no DOM change",
        "url": "x", "time": "12:00:00",
    })
    hook = BrowserWorkerHook(s)
    ctx = _make_ctx(iteration=1)
    asyncio.run(hook.after_iteration(ctx))
    assert "[CLICK_MISS_RETRY]" in _last_msg(ctx)


def test_click_miss_retry_nudge_fires_on_auto_retry_failure() -> None:
    """[BBOX_AUTO_RETRY ... outcome=failed] also triggers the nudge."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.step_history.append({
        "tool": "browser_click_at",
        "args": "V_1",
        "result": "Clicked V_1 [BBOX_AUTO_RETRY n=1 V_1→V_3 outcome=failed]",
        "url": "x", "time": "12:00:00",
    })
    hook = BrowserWorkerHook(s)
    ctx = _make_ctx(iteration=1)
    asyncio.run(hook.after_iteration(ctx))
    assert "[CLICK_MISS_RETRY]" in _last_msg(ctx)


def test_click_miss_retry_nudge_silent_on_success() -> None:
    """A successful click_at (no miss markers) does NOT fire the nudge."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.step_history.append({
        "tool": "browser_click_at",
        "args": "V_1",
        "result": "Clicked V_1 → bbox=(10,10,40,30) url=https://x.com/results",
        "url": "x", "time": "12:00:00",
    })
    hook = BrowserWorkerHook(s)
    ctx = _make_ctx(iteration=1)
    asyncio.run(hook.after_iteration(ctx))
    assert "[CLICK_MISS_RETRY]" not in _last_msg(ctx)


def test_click_miss_retry_nudge_fires_once_then_resets() -> None:
    """The nudge fires once per miss; doesn't spam every iteration
    until a non-click tool resets it."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.step_history.append({
        "tool": "browser_click_at",
        "args": "V_1",
        "result": "[click_silent] no DOM change",
        "url": "x", "time": "12:00:00",
    })
    hook = BrowserWorkerHook(s)
    ctx1 = _make_ctx(iteration=1)
    asyncio.run(hook.after_iteration(ctx1))
    assert "[CLICK_MISS_RETRY]" in _last_msg(ctx1)
    # Same step_history → second iteration should NOT re-fire.
    ctx2 = _make_ctx(iteration=2)
    asyncio.run(hook.after_iteration(ctx2))
    assert "[CLICK_MISS_RETRY]" not in _last_msg(ctx2)


def test_click_miss_retry_nudge_kill_switch() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.step_history.append({
        "tool": "browser_click_at", "args": "V_1",
        "result": "[click_silent]", "url": "x", "time": "12:00:00",
    })
    hook = BrowserWorkerHook(s)
    os.environ["CLICK_MISS_RETRY_NUDGE"] = "0"
    try:
        ctx = _make_ctx(iteration=1)
        asyncio.run(hook.after_iteration(ctx))
        assert "[CLICK_MISS_RETRY]" not in _last_msg(ctx)
    finally:
        del os.environ["CLICK_MISS_RETRY_NUDGE"]


def main() -> int:
    tests = [
        test_tool_ladder_block_present_in_worker_soul,
        test_original_query_pinned_when_brief_exists,
        test_original_query_full_verbatim_no_truncation,
        test_original_query_kill_switch_disables,
        test_original_query_skipped_when_no_brief,
        test_progress_emits_plus_satisfied_on_flip,
        test_progress_suppressed_below_kind_threshold,
        test_progress_stuck_variant_at_filter_threshold,
        test_progress_numeric_kind_has_higher_threshold,
        test_progress_stuck_variant_resets_stagnation,
        test_progress_block_kill_switch_disables,
        # Phase I — click-miss retry nudge
        test_click_miss_retry_nudge_fires_after_click_silent,
        test_click_miss_retry_nudge_fires_on_verify_miss,
        test_click_miss_retry_nudge_fires_on_auto_retry_failure,
        test_click_miss_retry_nudge_silent_on_success,
        test_click_miss_retry_nudge_fires_once_then_resets,
        test_click_miss_retry_nudge_kill_switch,
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
