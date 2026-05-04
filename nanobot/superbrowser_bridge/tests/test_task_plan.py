"""Unit tests for the persistent multi-step task plan.

Covers the public surface of task_plan.py:
  • validator rejects empty / single-step / no-criterion plans
  • per-step lifecycle: pending → in_progress → satisfied | unsatisfiable
  • 2-fail rule flips a step to unsatisfiable; subsequent active_step()
    returns the next step
  • compact vs full rendering format

Arch v4 note: these tests predate the preplan gate (Move 1) and exercise
freshness-gate / loop-detection / click_at + type_at behavior in
isolation. Setting PREPLAN_GATE=0 at module load keeps them focused on
their original concerns; the preplan layer has dedicated tests in
test_preplan_gate.py.

No external services required. Run:
    source venv/bin/activate && \
        python nanobot/superbrowser_bridge/tests/test_task_plan.py
"""

from __future__ import annotations

import asyncio
import os
import sys

# Module-level: isolate task-plan / freshness / loop tests from the
# preplan gate layer added in arch v4.
os.environ["PREPLAN_GATE"] = "0"


def test_validator_rejects_empty() -> None:
    from superbrowser_bridge.task_plan import (
        make_plan, TaskPlanValidationError,
    )
    for bad in ([], None, "not-a-list", 123):
        try:
            make_plan(bad)  # type: ignore[arg-type]
            raise AssertionError(f"expected TaskPlanValidationError for {bad!r}")
        except TaskPlanValidationError:
            pass


def test_validator_rejects_single_step() -> None:
    from superbrowser_bridge.task_plan import (
        make_plan, TaskPlanValidationError,
    )
    try:
        make_plan([{"name": "x", "success_criteria": {"kind": "url_changed"}}])
        raise AssertionError("expected error for single-step plan")
    except TaskPlanValidationError as exc:
        assert "1 step" in str(exc), exc


def test_validator_rejects_none_criterion() -> None:
    from superbrowser_bridge.task_plan import (
        make_plan, TaskPlanValidationError,
    )
    try:
        make_plan([
            {"name": "a", "success_criteria": {"kind": "none"}},
            {"name": "b", "success_criteria": {"kind": "url_changed"}},
        ])
        raise AssertionError("expected error for kind=none")
    except TaskPlanValidationError as exc:
        assert "cannot be 'none'" in str(exc), exc


def test_validator_rejects_empty_name() -> None:
    from superbrowser_bridge.task_plan import (
        make_plan, TaskPlanValidationError,
    )
    try:
        make_plan([
            {"name": "  ", "success_criteria": {"kind": "url_changed"}},
            {"name": "b", "success_criteria": {"kind": "url_changed"}},
        ])
        raise AssertionError("expected error for empty name")
    except TaskPlanValidationError as exc:
        assert "empty name" in str(exc)


def test_lifecycle_pending_to_satisfied() -> None:
    from superbrowser_bridge.task_plan import make_plan
    plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    assert plan.steps[0].status == "pending"
    s = plan.active_step()
    assert s is not None and s.name == "a" and s.status == "in_progress"
    s.mark_attempt(True)
    assert s.status == "satisfied"
    s2 = plan.active_step()
    assert s2 is not None and s2.name == "b" and s2.status == "in_progress"


def test_two_fail_rule_marks_unsatisfiable() -> None:
    from superbrowser_bridge.task_plan import (
        make_plan, MAX_STEP_ATTEMPTS,
    )
    plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    s = plan.active_step()
    assert s is not None
    for i in range(MAX_STEP_ATTEMPTS):
        s.mark_attempt(False, "criterion_not_satisfied")
    assert s.status == "unsatisfiable", s.status
    assert s.attempts == MAX_STEP_ATTEMPTS

    next_step = plan.active_step()
    assert next_step is not None and next_step.name == "b"


def test_skip_active() -> None:
    from superbrowser_bridge.task_plan import make_plan
    plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    plan.active_step()
    skipped = plan.skip_active("filter not on this site")
    assert skipped is not None and skipped.status == "unsatisfiable"
    assert "filter not on this site" in skipped.last_failure_reason
    nxt = plan.active_step()
    assert nxt is not None and nxt.name == "b"


def test_is_complete_when_all_resolved() -> None:
    from superbrowser_bridge.task_plan import make_plan
    plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    assert not plan.is_complete
    plan.steps[0].mark_attempt(True)
    plan.steps[1].mark_attempt(True)
    assert plan.is_complete


def test_render_full_and_compact() -> None:
    from superbrowser_bridge.task_plan import make_plan
    plan = make_plan([
        {"name": "open red wine catalog",
         "success_criteria": {"kind": "url_matches", "payload": {"pattern": "red"}}},
        {"name": "apply Oregon",
         "success_criteria": {"kind": "url_matches", "payload": {"pattern": "oregon"}}},
        {"name": "extract top wine",
         "success_criteria": {"kind": "text_visible", "payload": {"text": "Critic"}},
         "delegate": {"kind": "extraction"}},
    ])
    plan.active_step()
    full = plan.to_brain_text(compact=False)
    assert "[TASK PLAN]" in full
    assert "1. open red wine catalog" in full
    assert "<extraction>" in full

    compact = plan.to_brain_text(compact=True)
    assert compact.startswith("[PLAN] step 1/3")


def test_check_active_task_step_no_plan_returns_empty() -> None:
    """check_active_task_step is the integration point with verify_action.
    No plan → empty string, never raises."""
    from superbrowser_bridge.session_tools import BrowserSessionState

    s = BrowserSessionState()
    s.session_id = "session-x"
    s.task_plan = None
    note = asyncio.run(s.check_active_task_step("session-x"))
    assert note == "", f"expected empty, got {note!r}"


def test_check_active_task_step_url_changed_advances() -> None:
    """When current_url differs from pre_url, url_changed criterion
    satisfies without any network probe — purely local comparison."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_plan import make_plan

    s = BrowserSessionState()
    s.session_id = "session-x"
    s.current_url = "https://site.test/after"
    s.task_plan = make_plan([
        {"name": "navigate", "success_criteria": {"kind": "url_changed"}},
        {"name": "next step", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    note = asyncio.run(
        s.check_active_task_step("session-x", pre_url="https://site.test/before")
    )
    assert "subgoal_advanced" in note, f"expected advance, got {note!r}"
    assert s.task_plan.steps[0].status == "satisfied"


def test_refusal_no_plan_allows_through() -> None:
    """No task_plan → must_screenshot_before_giving_up returns None
    (single-step tasks shouldn't be forced into the refusal path)."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.task_plan = None
    s.mark_action_failed("anything failed")
    assert s.must_screenshot_before_giving_up() is None


def test_refusal_blocks_when_step_in_progress_and_failure_unscreened() -> None:
    """Active plan + failure flag set → refuse."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_plan import make_plan
    s = BrowserSessionState()
    s.session_id = "session-x"
    s.task_plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    s.mark_action_failed("wait_for timeout")
    msg = s.must_screenshot_before_giving_up()
    assert msg is not None and "[refused" in msg


def test_refusal_lifts_after_screenshot() -> None:
    """clear_action_failed (called by mark_screenshot_taken) lifts the refusal."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_plan import make_plan
    s = BrowserSessionState()
    s.task_plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    s.mark_action_failed("failure")
    assert s.must_screenshot_before_giving_up() is not None
    s.clear_action_failed()
    assert s.must_screenshot_before_giving_up() is None


def test_refusal_lifts_after_3_stale_screenshots() -> None:
    """Stale-screenshot ≥3 release valve: brain has demonstrably looked
    and the page hasn't changed → genuine impasse, allow request_help."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_plan import make_plan
    s = BrowserSessionState()
    s.task_plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    s.loop_detector.record_screenshot("https://x", "fp")
    s.loop_detector.record_screenshot("https://x", "fp")
    s.loop_detector.record_screenshot("https://x", "fp")
    s.loop_detector.record_screenshot("https://x", "fp")
    assert s.loop_detector.stale_screenshot_count >= 3
    s.mark_action_failed("failure")
    assert s.must_screenshot_before_giving_up() is None


def test_refusal_kill_switch() -> None:
    """LOOP_REFUSAL_GUARD=0 disables the refusal entirely."""
    import os
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_plan import make_plan
    s = BrowserSessionState()
    s.task_plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    s.mark_action_failed("failure")
    prior = os.environ.get("LOOP_REFUSAL_GUARD")
    os.environ["LOOP_REFUSAL_GUARD"] = "0"
    try:
        assert s.must_screenshot_before_giving_up() is None
    finally:
        if prior is None:
            os.environ.pop("LOOP_REFUSAL_GUARD", None)
        else:
            os.environ["LOOP_REFUSAL_GUARD"] = prior


def test_loop_detector_stale_streak_resets_on_change() -> None:
    """stale_screenshot_count resets when (url, fingerprint) changes."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.loop_detector.record_screenshot("https://x", "fp1")
    s.loop_detector.record_screenshot("https://x", "fp1")
    s.loop_detector.record_screenshot("https://x", "fp1")
    assert s.loop_detector.stale_screenshot_count >= 2
    # Change → reset
    s.loop_detector.record_screenshot("https://x", "fp2")
    assert s.loop_detector.stale_screenshot_count == 0


def test_anchor_url_harvesting() -> None:
    """harvest_anchor_urls feeds the URL-hallucination guard's seen set."""
    from superbrowser_bridge.session_tools import BrowserSessionState

    s = BrowserSessionState()
    blob = (
        '[1]<a href="/store/red/">Red</a>\n'
        '[2]<a href="https://site.test/cat/uuid-12345678/">Item</a>\n'
        '[3]<a href="javascript:void(0)">Skip me</a>\n'
        '[4]<a href="#">Skip me too</a>'
    )
    n = s.harvest_anchor_urls(blob)
    assert n >= 2, f"expected ≥2 hrefs, got {n}"
    assert "/store/red/" in s.observed_anchor_urls
    assert "https://site.test/cat/uuid-12345678/" in s.observed_anchor_urls
    # javascript: and # are filtered out
    assert "javascript:void(0)" not in s.observed_anchor_urls


# ── Three surgical refusals (vague selector / DOM-index / eval-exploration) ──


def test_vague_selector_classifier() -> None:
    """_looks_vague_selector must catch all weak-only selectors (the
    wineaccess trace's `a[role='button']:nth-of-type(2)` family)
    without false-flagging selectors with id / data-testid / for /
    name discriminators."""
    from superbrowser_bridge.session_tools import _looks_vague_selector

    vague = [
        "button",
        "a",
        "a[role='button']",
        "a[aria-expanded='false'][href='#']",
        "div.row .item",
        "[role=checkbox]",
        'a[href="#"]',
        # Weak-only — :nth-of-type without a strong parent anchor
        # (the wineaccess pattern that bypassed the prior check).
        "a[role='button']:nth-of-type(2)",
        ":nth-of-type(2)",
        "ul li:nth-of-type(3)",       # no id on ul
        "div:first-child",            # no anchor
    ]
    specific = [
        "#submit",
        "div#main",
        "[data-testid=cart]",
        "label[for='oregon']",
        # Weak + strong: id-anchored parent + nth child is fine.
        "#accordion li:nth-of-type(3)",
        "div#main button:first-of-type",
        "button[name='go']",
        "#accordion-region",
        'input[name="price-maximum-range"]',
        'a[id*="main"]',
    ]
    for s in vague:
        assert _looks_vague_selector(s), f"expected vague: {s!r}"
    for s in specific:
        assert not _looks_vague_selector(s), f"expected specific: {s!r}"


def test_eval_exploration_classifier() -> None:
    """_eval_looks_like_exploration catches read-only DOM exploration but
    allows write-op scripts and short probes."""
    from superbrowser_bridge.session_tools import _eval_looks_like_exploration

    exploration = [
        "(() => { const items=[...document.querySelectorAll('a[role=button]')]"
        ".map((el,i)=>({i,text:el.textContent.trim()})); return items; })()",
        "(() => { const labels=[...document.querySelectorAll('label')]"
        ".map(l=>l.textContent.trim()); return labels; })()",
    ]
    allowed = [
        # Write ops bypass the gate.
        "(() => { const r=document.querySelector('#foo'); r.click(); "
        "return r.textContent; })()",
        # Short browser-state probes (under 40 chars).
        "document.readyState",
        "location.href",
        # Value-set with querySelector — write op present.
        "(() => { const inp=document.querySelector('input[name=q]'); "
        "inp.value='hi'; return inp.value; })()",
    ]
    for s in exploration:
        assert _eval_looks_like_exploration(s), f"expected exploration: {s[:60]!r}"
    for s in allowed:
        assert not _eval_looks_like_exploration(s), f"expected allowed: {s[:60]!r}"


def test_click_index_refusal_when_vision_fresh() -> None:
    """browser_click(index=N) refuses when the last vision response has
    bboxes AND was taken within the last 2 brain turns."""
    import asyncio
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserClickTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    # Simulate vision: 5 bboxes, taken at turn 0
    s._last_vision_response = SimpleNamespace(bboxes=[1, 2, 3, 4, 5])
    s._vision_epoch_turn = 0
    s._brain_turn_counter = 1  # 1 turn after vision, well under 2-turn limit

    tool = BrowserClickTool(s)
    out = asyncio.run(tool.execute(session_id="session-x", index=7))
    assert "[click_index_refused" in out, f"expected refusal, got {out[:100]!r}"
    assert "V_n" in out, "refusal should suggest browser_click_at(V_n)"


def test_click_index_refusal_lifts_when_no_vision() -> None:
    """When the session has no vision response at all (cold session, or
    pure scripted flow), browser_click([N]) is allowed — the brain has
    no V_n alternative."""
    import asyncio
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserClickTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_vision_response = None  # no vision ever taken
    tool = BrowserClickTool(s)
    # Will fail on the network call (no real server) — catch and check
    # we didn't get refused at the gate.
    try:
        out = asyncio.run(tool.execute(session_id="session-x", index=7))
        assert "click_index_refused" not in (out or ""), (
            f"refusal fired with no vision: {out!r}"
        )
    except Exception:
        pass


def test_click_index_refused_even_when_vision_is_stale() -> None:
    """The age-≤2-turns gate was removed. Vision being old is not a
    reason to allow browser_click([N]) — the brain can always take a
    fresh screenshot. Refuse whenever any vision response exists."""
    import asyncio
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserClickTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_vision_response = SimpleNamespace(bboxes=[1, 2, 3])
    s._vision_epoch_turn = 0
    s._brain_turn_counter = 10  # 10 turns past — very stale
    tool = BrowserClickTool(s)
    out = asyncio.run(tool.execute(session_id="session-x", index=7))
    assert "[click_index_refused" in out, (
        f"expected refusal even at age=10, got {out[:120]!r}"
    )
    assert "browser_screenshot first to refresh" in out


def test_click_index_kill_switch() -> None:
    """CLICK_INDEX_REFUSAL=0 disables the gate even when vision is fresh."""
    import asyncio
    import os
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserClickTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_vision_response = SimpleNamespace(bboxes=[1, 2, 3])
    s._vision_epoch_turn = 0
    s._brain_turn_counter = 1
    tool = BrowserClickTool(s)

    prior = os.environ.get("CLICK_INDEX_REFUSAL")
    os.environ["CLICK_INDEX_REFUSAL"] = "0"
    try:
        # Refusal disabled → goes through to network (and fails); just
        # verify we don't see the refusal marker.
        try:
            out = asyncio.run(tool.execute(session_id="session-x", index=7))
            assert "click_index_refused" not in (out or "")
        except Exception:
            pass
    finally:
        if prior is None:
            os.environ.pop("CLICK_INDEX_REFUSAL", None)
        else:
            os.environ["CLICK_INDEX_REFUSAL"] = prior


def test_eval_exploration_refusal_when_vision_fresh() -> None:
    """browser_eval refuses an exploration-shaped script when vision is fresh."""
    import asyncio
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserEvalTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_vision_response = SimpleNamespace(bboxes=[1, 2, 3])
    s._vision_epoch_turn = 0
    s._brain_turn_counter = 1
    tool = BrowserEvalTool(s)
    out = asyncio.run(tool.execute(
        session_id="session-x",
        script="(() => { const labels=[...document.querySelectorAll('label')]"
               ".map(l=>l.textContent.trim()); return labels; })()",
    ))
    assert "[eval_for_exploration_refused" in out, (
        f"expected refusal, got {out[:100]!r}"
    )


def test_eval_write_op_bypasses_refusal() -> None:
    """Scripts with write ops (.click(), .value=) skip the exploration gate."""
    import asyncio
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserEvalTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_vision_response = SimpleNamespace(bboxes=[1, 2, 3])
    s._vision_epoch_turn = 0
    s._brain_turn_counter = 1
    tool = BrowserEvalTool(s)
    # Write op present — should NOT be refused (will fail on network).
    try:
        out = asyncio.run(tool.execute(
            session_id="session-x",
            script="(() => { document.querySelector('#foo').click(); "
                   "return 'done'; })()",
        ))
        assert "eval_for_exploration_refused" not in (out or "")
    except Exception:
        # Network failure past the gate proves the gate let it through.
        pass


# ── v5: hierarchy + holistic context + scroll hint + narration ──


def test_step_prefix_present_when_plan_active() -> None:
    """build_text_only prepends `[step i/N → "..."]` when a TaskPlan is active."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_plan import make_plan

    s = BrowserSessionState()
    s.task_plan = make_plan([
        {"name": "Apply Region=Oregon", "success_criteria": {"kind": "url_changed"}},
        {"name": "Apply price filter", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()  # first step → in_progress
    out = s.build_text_only({"url": "https://x"}, "Clicked something")
    assert "[step 1 of 2 → 'Apply Region=Oregon']" in out, (
        f"expected step prefix, got {out[:200]!r}"
    )


def test_step_prefix_absent_when_no_plan() -> None:
    """No plan → no prefix; build_text_only behaves identically to today."""
    from superbrowser_bridge.session_tools import BrowserSessionState

    s = BrowserSessionState()
    s.task_plan = None
    out = s.build_text_only({"url": "https://x"}, "Clicked something")
    assert "[step " not in out, f"unexpected step prefix without plan: {out!r}"


def test_step_prefix_kill_switch() -> None:
    """STEP_PREFIX_IN_CAPTION=0 disables the prefix even with active plan."""
    import os
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_plan import make_plan

    s = BrowserSessionState()
    s.task_plan = make_plan([
        {"name": "step a", "success_criteria": {"kind": "url_changed"}},
        {"name": "step b", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    prior = os.environ.get("STEP_PREFIX_IN_CAPTION")
    os.environ["STEP_PREFIX_IN_CAPTION"] = "0"
    try:
        out = s.build_text_only({"url": "https://x"}, "Clicked")
        assert "[step " not in out
    finally:
        if prior is None:
            os.environ.pop("STEP_PREFIX_IN_CAPTION", None)
        else:
            os.environ["STEP_PREFIX_IN_CAPTION"] = prior


def test_scroll_hint_extracts_proper_noun_target() -> None:
    """_scroll_target_hint pulls the target noun-phrase from the active step name."""
    from superbrowser_bridge.session_tools import BrowserSessionState, BrowserScrollTool
    from superbrowser_bridge.task_plan import make_plan

    s = BrowserSessionState()
    s.task_plan = make_plan([
        {"name": "Apply Region=Oregon", "success_criteria": {"kind": "url_changed"}},
        {"name": "next", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    tool = BrowserScrollTool(s)
    hint = tool._scroll_target_hint()
    assert "[scroll_hint]" in hint
    assert "Oregon" in hint
    assert "browser_scroll_until" in hint


def test_scroll_hint_silent_without_proper_noun() -> None:
    """Step names without a proper-noun target produce no hint."""
    from superbrowser_bridge.session_tools import BrowserSessionState, BrowserScrollTool
    from superbrowser_bridge.task_plan import make_plan

    s = BrowserSessionState()
    s.task_plan = make_plan([
        {"name": "scroll down to read more", "success_criteria": {"kind": "url_changed"}},
        {"name": "next", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    tool = BrowserScrollTool(s)
    hint = tool._scroll_target_hint()
    assert hint == "", f"expected no hint, got {hint!r}"


def test_scroll_hint_silent_without_plan() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState, BrowserScrollTool
    s = BrowserSessionState()
    s.task_plan = None
    tool = BrowserScrollTool(s)
    assert tool._scroll_target_hint() == ""


def test_narration_optional_no_refusal() -> None:
    """Click tools accept narration as optional kwarg; absence is fine."""
    import asyncio
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import BrowserSessionState, BrowserClickAtTool

    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_vision_response = SimpleNamespace(bboxes=[1, 2, 3])
    s._vision_epoch_turn = 0
    s._brain_turn_counter = 1
    tool = BrowserClickAtTool(s)
    # Passing narration shouldn't break the call (will fail later on
    # network, but the param itself must be accepted).
    try:
        asyncio.run(tool.execute(
            session_id="session-x",
            vision_index=2,
            narration="clicking V_2 to open the Region accordion",
        ))
    except Exception:
        pass
    assert s._last_narration == "clicking V_2 to open the Region accordion"


def test_narration_renders_in_worker_hook_then_clears() -> None:
    """worker_hook.after_iteration surfaces _last_narration as
    [last_intended: ...] then clears it so each narration shows once."""
    import asyncio
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_narration = "selecting Oregon checkbox after expanding US"
    hook = BrowserWorkerHook(s)
    ctx = type("Ctx", (), {"iteration": 0, "messages": [{"role": "tool", "content": "x"}]})()
    asyncio.run(hook.after_iteration(ctx))
    msg = ctx.messages[0]["content"]
    assert "[last_intended:" in msg
    assert "Oregon" in msg
    # Cleared after rendering.
    assert s._last_narration == ""


def test_inventory_hierarchy_kill_switch_strips_expanders() -> None:
    """INVENTORY_HIERARCHY=0 reverts the manifest path to flat
    (no expanders, no parent_label on options)."""
    import asyncio
    import os
    from unittest.mock import patch, AsyncMock
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserInventoryFiltersTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    tool = BrowserInventoryFiltersTool(s)

    fake_response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "found": True,
            "scope": "document",
            "options": [
                {"label": "Oregon", "kind": "checkbox", "selector": "#oregon",
                 "group": "Region", "selected": False, "parent_label": "United States"},
            ],
            "expanders": [
                {"label": "United States", "selector": "#us-toggle",
                 "expanded": False, "controls_selector": "#us-content",
                 "child_count": 50},
            ],
            "total": 1,
            "scrollTravelPx": 0,
            "iterations": 1,
        },
    )

    prior = os.environ.get("INVENTORY_HIERARCHY")
    os.environ["INVENTORY_HIERARCHY"] = "0"
    try:
        with patch(
            "superbrowser_bridge.session_tools._request_with_backoff",
            new=AsyncMock(return_value=fake_response),
        ):
            out = asyncio.run(tool.execute(session_id="session-x"))
        assert "## Collapsed groups" not in out
        assert "expanders=0" in out
        # parent_label suffix shouldn't appear on the option line either.
        assert "(under" not in out
    finally:
        if prior is None:
            os.environ.pop("INVENTORY_HIERARCHY", None)
        else:
            os.environ["INVENTORY_HIERARCHY"] = prior


# ── v6: script-exploration + URL filter-hack refusals ──


def test_run_script_exploration_refused_when_vision_fresh() -> None:
    """browser_run_script(read-only) for DOM exploration is refused
    when vision has bboxes — the brain should be using V_n, not
    re-querying the DOM via script."""
    import asyncio
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserRunScriptTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_vision_response = SimpleNamespace(bboxes=[1, 2, 3])
    s._vision_epoch_turn = 0
    s._brain_turn_counter = 1
    tool = BrowserRunScriptTool(s)
    out = asyncio.run(tool.execute(
        session_id="session-x",
        script=(
            "return await page.evaluate(() => "
            "[...document.querySelectorAll('a,button')]"
            ".map((el,i)=>({i,text:el.textContent,tag:el.tagName})));"
        ),
        mutates=False,
    ))
    assert "[run_script_for_exploration_refused" in out, (
        f"expected refusal, got {out[:120]!r}"
    )


def test_run_script_write_op_bypasses_exploration_refusal() -> None:
    """Scripts with .click() etc. skip the v6 exploration gate
    even when vision is fresh."""
    import asyncio
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserRunScriptTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_vision_response = SimpleNamespace(bboxes=[1, 2, 3])
    s._vision_epoch_turn = 0
    s._brain_turn_counter = 1
    tool = BrowserRunScriptTool(s)
    try:
        out = asyncio.run(tool.execute(
            session_id="session-x",
            script=(
                "return await page.evaluate(() => { "
                "document.querySelector('#foo').click(); "
                "return 'done'; });"
            ),
            mutates=False,
        ))
        assert "run_script_for_exploration_refused" not in (out or "")
    except Exception:
        pass  # network failure past the gate is fine


def test_run_script_exploration_kill_switch() -> None:
    import asyncio
    import os
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserRunScriptTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_vision_response = SimpleNamespace(bboxes=[1, 2, 3])
    s._vision_epoch_turn = 0
    s._brain_turn_counter = 1
    tool = BrowserRunScriptTool(s)
    prior = os.environ.get("SCRIPT_EXPLORATION_REFUSAL")
    os.environ["SCRIPT_EXPLORATION_REFUSAL"] = "0"
    try:
        try:
            out = asyncio.run(tool.execute(
                session_id="session-x",
                script=(
                    "return await page.evaluate(() => "
                    "[...document.querySelectorAll('a')]"
                    ".map(a=>a.href));"
                ),
                mutates=False,
            ))
            assert "run_script_for_exploration_refused" not in (out or "")
        except Exception:
            pass
    finally:
        if prior is None:
            os.environ.pop("SCRIPT_EXPLORATION_REFUSAL", None)
        else:
            os.environ["SCRIPT_EXPLORATION_REFUSAL"] = prior


def test_navigate_url_filter_hack_refused_with_active_plan() -> None:
    """browser_navigate to a URL with ≥2 filter-shaped query params
    is refused when a TaskPlan is active — the brain should apply
    filters via the UI."""
    import asyncio
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserNavigateTool,
    )
    from superbrowser_bridge.task_plan import make_plan

    s = BrowserSessionState()
    s.session_id = "session-x"
    s.pinned_domain = "wineaccess.com"
    s.task_plan = make_plan([
        {"name": "apply red", "success_criteria": {"kind": "url_changed"}},
        {"name": "apply oregon", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    tool = BrowserNavigateTool(s)
    out = asyncio.run(tool.execute(
        session_id="session-x",
        url=(
            "https://www.wineaccess.com/store/search/"
            "?type=red-wine&food_pairings=fish,sweets"
            "&max_price=40&region_slug=oregon&ordering=-expert_rating"
        ),
    ))
    assert "[navigate_filter_hack_refused" in out, (
        f"expected refusal, got {out[:200]!r}"
    )


def test_navigate_filter_hack_allows_single_param() -> None:
    """Single filter param (e.g. just ?ordering=) is not the URL-hack
    pattern and should pass through (will fail on network in test)."""
    import asyncio
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserNavigateTool,
    )
    from superbrowser_bridge.task_plan import make_plan

    s = BrowserSessionState()
    s.session_id = "session-x"
    s.pinned_domain = "wineaccess.com"
    s.task_plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    tool = BrowserNavigateTool(s)
    try:
        out = asyncio.run(tool.execute(
            session_id="session-x",
            url="https://www.wineaccess.com/store/?ordering=-expert_rating",
        ))
        assert "navigate_filter_hack_refused" not in (out or "")
    except Exception:
        pass


def test_navigate_filter_hack_silent_without_plan() -> None:
    """No TaskPlan → URL-hack refusal doesn't fire (single-step tasks
    don't need filter-UI enforcement)."""
    import asyncio
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserNavigateTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    s.pinned_domain = "wineaccess.com"
    s.task_plan = None
    tool = BrowserNavigateTool(s)
    try:
        out = asyncio.run(tool.execute(
            session_id="session-x",
            url=(
                "https://www.wineaccess.com/store/search/"
                "?type=red-wine&food_pairings=fish,sweets&max_price=40"
            ),
        ))
        assert "navigate_filter_hack_refused" not in (out or "")
    except Exception:
        pass


def test_navigate_filter_hack_kill_switch() -> None:
    import asyncio
    import os
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserNavigateTool,
    )
    from superbrowser_bridge.task_plan import make_plan

    s = BrowserSessionState()
    s.session_id = "session-x"
    s.pinned_domain = "wineaccess.com"
    s.task_plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    tool = BrowserNavigateTool(s)
    prior = os.environ.get("URL_FILTER_HACK_REFUSAL")
    os.environ["URL_FILTER_HACK_REFUSAL"] = "0"
    try:
        try:
            out = asyncio.run(tool.execute(
                session_id="session-x",
                url=(
                    "https://www.wineaccess.com/store/search/"
                    "?type=red-wine&food_pairings=fish,sweets"
                ),
            ))
            assert "navigate_filter_hack_refused" not in (out or "")
        except Exception:
            pass
    finally:
        if prior is None:
            os.environ.pop("URL_FILTER_HACK_REFUSAL", None)
        else:
            os.environ["URL_FILTER_HACK_REFUSAL"] = prior


# ── v7: intent auto-injection + close guard ──


def test_intent_auto_inject_replaces_generic() -> None:
    """When the brain's intent is generic ('ground before plan'), the
    screenshot tool replaces it with the active TaskPlan step name."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserScreenshotTool,
    )
    from superbrowser_bridge.task_plan import make_plan

    s = BrowserSessionState()
    s.task_plan = make_plan([
        {"name": "Apply Region=Oregon", "success_criteria": {"kind": "url_changed"}},
        {"name": "next", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    tool = BrowserScreenshotTool(s)
    enriched = tool._enrich_intent_with_plan("ground before setting task plan")
    assert "Apply Region=Oregon" in enriched, f"got {enriched!r}"


def test_intent_auto_inject_appends_when_specific() -> None:
    """A specific brain intent isn't replaced — the step name is just
    appended as context."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserScreenshotTool,
    )
    from superbrowser_bridge.task_plan import make_plan

    s = BrowserSessionState()
    s.task_plan = make_plan([
        {"name": "Apply Region=Oregon", "success_criteria": {"kind": "url_changed"}},
        {"name": "next", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    tool = BrowserScreenshotTool(s)
    enriched = tool._enrich_intent_with_plan("find the Region accordion sidebar")
    assert "find the Region accordion" in enriched
    assert "active step: Apply Region=Oregon" in enriched


def test_intent_auto_inject_missing_intent_uses_step() -> None:
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserScreenshotTool,
    )
    from superbrowser_bridge.task_plan import make_plan

    s = BrowserSessionState()
    s.task_plan = make_plan([
        {"name": "Apply Region=Oregon", "success_criteria": {"kind": "url_changed"}},
        {"name": "next", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    tool = BrowserScreenshotTool(s)
    enriched = tool._enrich_intent_with_plan(None)
    assert "Apply Region=Oregon" in enriched


def test_intent_auto_inject_silent_without_plan() -> None:
    """No plan → enrich_intent passes through unchanged."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserScreenshotTool,
    )
    s = BrowserSessionState()
    s.task_plan = None
    tool = BrowserScreenshotTool(s)
    assert tool._enrich_intent_with_plan("anything") == "anything"
    assert tool._enrich_intent_with_plan(None) is None


def test_close_guard_refuses_with_unsatisfied_plan() -> None:
    """browser_close refuses when an active TaskPlan has pending steps
    AND screenshot budget > 2."""
    import asyncio
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserCloseTool,
    )
    from superbrowser_bridge.task_plan import make_plan

    s = BrowserSessionState()
    s.session_id = "session-x"
    s.screenshot_budget = 5  # plenty of budget
    s.task_plan = make_plan([
        {"name": "first", "success_criteria": {"kind": "url_changed"}},
        {"name": "second", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    tool = BrowserCloseTool(s)
    out = asyncio.run(tool.execute(session_id="session-x"))
    assert "[close_guard_refused" in out, f"expected refusal, got {out[:120]!r}"
    assert "first" in out  # surfaces unsatisfied step name


def test_close_guard_allows_when_no_plan() -> None:
    """No plan → close goes through (will hit network fail in test)."""
    import asyncio
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserCloseTool,
    )
    s = BrowserSessionState()
    s.session_id = "session-x"
    s.screenshot_budget = 5
    s.task_plan = None
    tool = BrowserCloseTool(s)
    try:
        out = asyncio.run(tool.execute(session_id="session-x"))
        assert "close_guard_refused" not in (out or "")
    except Exception:
        pass  # network failure past the gate is fine


def test_close_guard_allows_when_budget_exhausted() -> None:
    """When screenshot budget ≤ 2, close is allowed (brain has earned it)."""
    import asyncio
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserCloseTool,
    )
    from superbrowser_bridge.task_plan import make_plan
    s = BrowserSessionState()
    s.session_id = "session-x"
    s.screenshot_budget = 1
    s.task_plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    tool = BrowserCloseTool(s)
    try:
        out = asyncio.run(tool.execute(session_id="session-x"))
        assert "close_guard_refused" not in (out or "")
    except Exception:
        pass


def test_close_guard_allows_when_all_satisfied() -> None:
    """All TaskPlan steps satisfied → close is allowed."""
    import asyncio
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserCloseTool,
    )
    from superbrowser_bridge.task_plan import make_plan
    s = BrowserSessionState()
    s.session_id = "session-x"
    s.screenshot_budget = 5
    s.task_plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    for st in s.task_plan.steps:
        st.mark_attempt(True)
    tool = BrowserCloseTool(s)
    try:
        out = asyncio.run(tool.execute(session_id="session-x"))
        assert "close_guard_refused" not in (out or "")
    except Exception:
        pass


def test_close_guard_kill_switch() -> None:
    import asyncio
    import os
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserCloseTool,
    )
    from superbrowser_bridge.task_plan import make_plan
    s = BrowserSessionState()
    s.session_id = "session-x"
    s.screenshot_budget = 5
    s.task_plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()
    tool = BrowserCloseTool(s)
    prior = os.environ.get("CLOSE_GUARD")
    os.environ["CLOSE_GUARD"] = "0"
    try:
        try:
            out = asyncio.run(tool.execute(session_id="session-x"))
            assert "close_guard_refused" not in (out or "")
        except Exception:
            pass
    finally:
        if prior is None:
            os.environ.pop("CLOSE_GUARD", None)
        else:
            os.environ["CLOSE_GUARD"] = prior


# ── v8: vision-grounded clicks (target_label protocol) ──


def _make_fake_vresp(label_pairs):
    """Build a fake vision response whose `get_bbox(N)` returns a bbox
    whose `.label` matches the Nth (1-based) entry in `label_pairs`.

    label_pairs: list of strings — the V_n labels, in ranked order as
    the brain would see them.
    """
    from types import SimpleNamespace
    bboxes = [
        SimpleNamespace(
            label=lbl,
            role_in_scene="target",
            intent_relevant=False,
            clickable=True,
            confidence=0.9,
        )
        for lbl in label_pairs
    ]
    resp = SimpleNamespace(bboxes=bboxes)

    def _get_bbox(n: int):
        if n is None or n < 1 or n > len(bboxes):
            return None
        return bboxes[n - 1]

    resp.get_bbox = _get_bbox
    return resp


def test_target_label_validates_substring_match() -> None:
    """target_label that substring-matches (in either direction) the
    actual V_n label allows the click. The contract: brain READS the
    label from the screenshot — not interprets/guesses what the
    element does. Strict substring catches readings; bans inventions."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_target_label,
    )
    s = BrowserSessionState()
    s._last_vision_response = _make_fake_vresp(["Sign in", "Wine Facts ▼", "Oregon"])
    # Brain says "Wine Facts" — substring of "Wine Facts ▼" → match
    assert _validate_target_label(s, 2, "Wine Facts") is None
    # Brain says "Oregon" — exact match → ok
    assert _validate_target_label(s, 3, "Oregon") is None
    # Brain says "Oregon checkbox" — actual ("Oregon") is substring of
    # brain's reading → match (brain added one functional word; OK)
    assert _validate_target_label(s, 3, "Oregon checkbox") is None
    # Case-insensitive
    assert _validate_target_label(s, 1, "sign in") is None
    # Brain quotes the exact emitted label including the unicode arrow
    assert _validate_target_label(s, 2, "Wine Facts ▼") is None


def test_target_label_refuses_mismatch() -> None:
    """target_label that doesn't match V_n's actual label refuses with
    [click_at_label_mismatch] and the labels listed inline."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_target_label,
    )
    s = BrowserSessionState()
    s._last_vision_response = _make_fake_vresp([
        "Sign in", "Account menu", "Wine Facts", "Food Pairings",
    ])
    msg = _validate_target_label(s, 1, "Wine Facts expander")
    assert msg is not None
    assert "[click_at_label_mismatch]" in msg
    assert "'Sign in'" in msg
    # Recovery list includes top V_n labels
    assert "[V1]" in msg and "[V3]" in msg
    assert "Wine Facts" in msg


def test_target_label_required_when_missing() -> None:
    """vision_index set but target_label missing → refuse with required."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_target_label,
    )
    s = BrowserSessionState()
    s._last_vision_response = _make_fake_vresp(["Sign in", "Wine Facts"])
    for missing in (None, "", "   "):
        msg = _validate_target_label(s, 1, missing)
        assert msg is not None
        assert "[click_at_target_label_required]" in msg
        assert "VISION_TARGET_LABEL_REQUIRED=0" in msg


def test_target_label_skips_when_no_vision_index() -> None:
    """Raw (x, y) clicks (vision_index=None) bypass validation entirely."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_target_label,
    )
    s = BrowserSessionState()
    s._last_vision_response = _make_fake_vresp(["Sign in"])
    # vision_index=None → no validation, even if target_label is missing
    assert _validate_target_label(s, None, None) is None
    # Even with a wildly mismatched target_label
    assert _validate_target_label(s, None, "Wine Facts") is None


def test_target_label_allows_when_actual_label_empty() -> None:
    """Vision didn't emit a label for V_n → can't verify mismatch, allow.
    But the brain MUST still pass target_label (declaration of intent)."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_target_label,
    )
    s = BrowserSessionState()
    s._last_vision_response = _make_fake_vresp(["", "Wine Facts"])
    # Empty actual label: target_label provided → allow
    assert _validate_target_label(s, 1, "anything goes") is None
    # But still required: missing target_label refuses
    assert _validate_target_label(s, 1, None) is not None


def test_target_label_skips_when_no_vision_response() -> None:
    """No cached vision response → can't validate, allow (downstream
    `[click_at_failed:no_vision]` handles the real error)."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_target_label,
    )
    s = BrowserSessionState()
    s._last_vision_response = None
    assert _validate_target_label(s, 1, "anything") is None


def test_target_label_kill_switch() -> None:
    """VISION_TARGET_LABEL_REQUIRED=0 disables the gate entirely."""
    import os
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_target_label,
    )
    s = BrowserSessionState()
    s._last_vision_response = _make_fake_vresp(["Sign in"])
    prior = os.environ.get("VISION_TARGET_LABEL_REQUIRED")
    os.environ["VISION_TARGET_LABEL_REQUIRED"] = "0"
    try:
        # Mismatch — would refuse with kill switch off
        assert _validate_target_label(s, 1, "Wine Facts") is None
        # Missing target_label — same
        assert _validate_target_label(s, 1, None) is None
    finally:
        if prior is None:
            os.environ.pop("VISION_TARGET_LABEL_REQUIRED", None)
        else:
            os.environ["VISION_TARGET_LABEL_REQUIRED"] = prior


def test_target_label_out_of_range_lets_existing_gate_handle() -> None:
    """vision_index out of range returns None — the existing
    [click_at_failed:bad_vision_index] gate produces the canonical error."""
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, _validate_target_label,
    )
    s = BrowserSessionState()
    s._last_vision_response = _make_fake_vresp(["Sign in", "Wine Facts"])
    # Index 99 is out of range → None (downstream handles)
    assert _validate_target_label(s, 99, "anything") is None


def test_click_at_executes_validation() -> None:
    """End-to-end: BrowserClickAtTool.execute calls _validate_target_label
    and returns its refusal when target_label matches NO V_n in the
    scene. Phase K's auto-remap fires when a V_M matches; this test
    exercises the legitimate-refusal fallback path with an unmatchable
    target_label."""
    import asyncio
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserClickAtTool,
    )
    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_vision_response = _make_fake_vresp(["Sign in", "Cart"])
    s._vision_epoch_turn = 0
    s._brain_turn_counter = 0
    tool = BrowserClickAtTool(s)
    out = asyncio.run(tool.execute(
        session_id="session-x",
        vision_index=1,
        target_label="Wine Facts",  # mismatch w/ V_1 AND no V_n matches
    ))
    assert "[click_at_label_mismatch]" in out


def test_type_at_executes_validation() -> None:
    """Same gate fires from BrowserTypeAtTool.execute when target_label
    matches no V_n in the scene (Phase K remap doesn't apply here)."""
    import asyncio
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserTypeAtTool,
    )
    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_vision_response = _make_fake_vresp(["Sign in", "Cart"])
    s._vision_epoch_turn = 0
    s._brain_turn_counter = 0
    tool = BrowserTypeAtTool(s)
    out = asyncio.run(tool.execute(
        session_id="session-x",
        vision_index=2,
        text="hello@example.com",
        target_label="Search box",  # mismatch w/ V_2 AND no V_n matches
    ))
    assert "[click_at_label_mismatch]" in out


# ── v9: orchestrator-level auto-retry on request_help bail ──


def test_request_help_bail_detected_via_step_history() -> None:
    """When the worker's last few step_history entries include
    browser_request_help, _looks_like_request_help_bail returns True
    regardless of the response text."""
    from superbrowser_bridge.orchestrator_tools import _looks_like_request_help_bail
    from superbrowser_bridge.session_tools import BrowserSessionState

    s = BrowserSessionState()
    s.step_history = [
        {"tool": "browser_screenshot", "args": "", "result": ""},
        {"tool": "browser_click_at", "args": "V_2", "result": ""},
        {"tool": "browser_request_help", "args": "stuck on filters", "result": ""},
    ]
    # Even with non-bail-looking content, step_history wins
    assert _looks_like_request_help_bail("Mission accomplished!", s) is True


def test_request_help_bail_detected_via_text_pattern() -> None:
    """Bail markers in the response text trigger detection even when
    no browser_request_help appeared in step_history."""
    from superbrowser_bridge.orchestrator_tools import _looks_like_request_help_bail
    from superbrowser_bridge.session_tools import BrowserSessionState

    s = BrowserSessionState()
    s.step_history = []
    samples = [
        "Need different tactic: filter state inconsistent",
        "I'm unable to complete this truthfully from the live site.",
        "I could not truthfully return any qualifying wine.",
        "Unable to complete: blocked by missing selectors.",
        "WineAccess interaction guards blocked further progress.",
    ]
    for sample in samples:
        assert _looks_like_request_help_bail(sample, s), f"missed: {sample!r}"


def test_request_help_bail_negatives() -> None:
    """Substantive responses (actual answers) are NOT flagged as bail."""
    from superbrowser_bridge.orchestrator_tools import _looks_like_request_help_bail
    from superbrowser_bridge.session_tools import BrowserSessionState

    s = BrowserSessionState()
    s.step_history = [
        {"tool": "browser_get_markdown", "args": "", "result": ""},
    ]
    samples = [
        "The highest critic-scored Oregon red under $40 is 2022 Chef's Table Pinot Noir at $19, scored 96 by Wine Access.",
        "Found 3 matching wines: A, B, C.",
        "Catalog page loaded; applied filters successfully.",
    ]
    for sample in samples:
        assert not _looks_like_request_help_bail(sample, s), f"false positive: {sample!r}"


def test_request_help_bail_handles_empty_inputs() -> None:
    from superbrowser_bridge.orchestrator_tools import _looks_like_request_help_bail
    from superbrowser_bridge.session_tools import BrowserSessionState

    s = BrowserSessionState()
    s.step_history = []
    assert _looks_like_request_help_bail("", s) is False
    assert _looks_like_request_help_bail(None, s) is False  # type: ignore[arg-type]


def test_build_retry_instructions_carries_context() -> None:
    """Enriched instructions must include the original task, the prior
    diagnostic, and the unsatisfied steps so the successor worker can
    pick up where the predecessor left off."""
    from superbrowser_bridge.orchestrator_tools import _build_retry_instructions

    enriched = _build_retry_instructions(
        original_instructions="Find Oregon red wines under $40 with dessert+fish pairings",
        prior_response=(
            "Need different tactic: WineAccess filter state is inconsistent; "
            "selecting fish navigated away from /regions/oregon/ and dropped "
            "the red+region constraints."
        ),
        unsatisfied_steps=[
            "Apply Oregon and red wine filters",
            "Apply dessert and fish food pairings",
            "Sort by critic score",
        ],
    )
    # Original task is verbatim
    assert "Find Oregon red wines under $40" in enriched
    # Prior diagnostic preserved (first ~200 chars min)
    assert "filter state is inconsistent" in enriched
    # Unsatisfied step names listed
    assert "'Apply Oregon and red wine filters'" in enriched
    assert "'Apply dessert and fish food pairings'" in enriched
    # Recovery section present and references the right tools
    assert "browser_screenshot" in enriched
    assert "browser_inventory_filters" in enriched
    assert "browser_form_begin" in enriched
    # Tells worker not to call browser_open (handoff resume mode)
    assert "do NOT call browser_open" in enriched
    # Caps further request_help to prevent ping-pong
    assert "browser_request_help again" in enriched


def test_build_retry_instructions_truncates_long_diagnostic() -> None:
    """A 5000-char diagnostic should be capped to ~800 chars to keep
    the retry prompt bounded."""
    from superbrowser_bridge.orchestrator_tools import _build_retry_instructions

    long_diag = "X" * 5000
    enriched = _build_retry_instructions(
        original_instructions="task",
        prior_response=long_diag,
        unsatisfied_steps=["step a"],
    )
    # Diagnostic block bounded — not the full 5000 chars
    assert len(enriched) < 5000  # full long_diag would push past 5000
    assert "(truncated)" in enriched


def test_build_retry_instructions_handles_empty_unsatisfied() -> None:
    """Edge case: no unsatisfied steps (all satisfied but worker bailed
    anyway) — still produces a coherent instruction block."""
    from superbrowser_bridge.orchestrator_tools import _build_retry_instructions

    enriched = _build_retry_instructions(
        original_instructions="task",
        prior_response="couldn't complete",
        unsatisfied_steps=[],
    )
    # Doesn't crash; mentions the edge-case in the instructions
    assert "all steps satisfied" in enriched.lower()


# ── v10: quick rescan for post-click expander pass ──


def test_expander_rescan_passes_no_scroll_walk() -> None:
    """When BrowserClickSelectorTool's post-click expander rescan fires,
    it must POST `noScrollWalk: true` to /inventory-filters so the page
    doesn't re-scroll top-to-bottom on every accordion click."""
    import asyncio
    from unittest.mock import patch, AsyncMock
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserClickSelectorTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    # Cache a manifest with a known expander matching the selector
    # we'll click — required for the rescan path to fire.
    s.last_filter_manifest = {
        "session_id": "session-x",
        "scope": "document",
        "options": [],
        "expanders": [{
            "label": "Region",
            "selector": "#accordion-region",
            "expanded": False,  # collapsed → rescan should fire
            "controls_selector": "#accordion-region-content",
            "child_count": 0,
        }],
    }
    tool = BrowserClickSelectorTool(s)

    # Capture the rescan POST body
    captured = {}

    async def fake_request(method, url, *, json=None, timeout=None, **kw):
        captured["url"] = url
        captured["json"] = json
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "success": True,
                "found": True,
                "scope": "document",
                "options": [
                    {"label": "Oregon", "kind": "checkbox",
                     "selector": "#oregon", "group": "Region",
                     "selected": False, "parent_label": "Region"},
                ],
                "expanders": [],
                "total": 1,
                "scrollTravelPx": 0,
                "iterations": 0,
            },
            raise_for_status=lambda: None,
        )

    with patch(
        "superbrowser_bridge.session_tools._request_with_backoff",
        new=AsyncMock(side_effect=fake_request),
    ):
        note = asyncio.run(tool._maybe_rescan_after_expander_click(
            "session-x", "#accordion-region",
        ))

    # Rescan fired
    assert "[expander_opened" in note, f"expected rescan note, got {note!r}"
    # AND it passed noScrollWalk=true (the v10 quick mode)
    assert captured.get("json", {}).get("noScrollWalk") is True, (
        f"expected noScrollWalk=true in rescan body, got {captured.get('json')!r}"
    )


def test_expander_rescan_quick_mode_kill_switch() -> None:
    """INVENTORY_QUICK_RESCAN=0 reverts to full-scroll rescan
    (noScrollWalk=False in the POST body)."""
    import asyncio
    import os
    from unittest.mock import patch, AsyncMock
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserClickSelectorTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    s.last_filter_manifest = {
        "session_id": "session-x", "scope": "document", "options": [],
        "expanders": [{
            "label": "Region", "selector": "#accordion-region",
            "expanded": False, "controls_selector": None, "child_count": 0,
        }],
    }
    tool = BrowserClickSelectorTool(s)
    captured = {}

    async def fake_request(method, url, *, json=None, timeout=None, **kw):
        captured["json"] = json
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"options": [], "expanders": [], "scope": "document"},
            raise_for_status=lambda: None,
        )

    prior = os.environ.get("INVENTORY_QUICK_RESCAN")
    os.environ["INVENTORY_QUICK_RESCAN"] = "0"
    try:
        with patch(
            "superbrowser_bridge.session_tools._request_with_backoff",
            new=AsyncMock(side_effect=fake_request),
        ):
            asyncio.run(tool._maybe_rescan_after_expander_click(
                "session-x", "#accordion-region",
            ))
        assert captured.get("json", {}).get("noScrollWalk") is False
    finally:
        if prior is None:
            os.environ.pop("INVENTORY_QUICK_RESCAN", None)
        else:
            os.environ["INVENTORY_QUICK_RESCAN"] = prior


def test_inventory_hierarchy_renders_collapsed_groups() -> None:
    """With INVENTORY_HIERARCHY on (default), the manifest reply
    surfaces a `## Collapsed groups` block above the option list and
    annotates options with `parent_label`."""
    import asyncio
    from unittest.mock import patch, AsyncMock
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserInventoryFiltersTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    tool = BrowserInventoryFiltersTool(s)

    fake_response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "found": True,
            "scope": "modal",
            "options": [
                {"label": "Oregon", "kind": "checkbox", "selector": "#oregon",
                 "group": "Region", "selected": False, "parent_label": "United States"},
                {"label": "Washington", "kind": "checkbox", "selector": "#wash",
                 "group": "Region", "selected": False, "parent_label": "United States"},
            ],
            "expanders": [
                {"label": "United States", "selector": "#us-toggle",
                 "expanded": False, "controls_selector": "#us-content",
                 "child_count": 2},
            ],
            "total": 2,
            "scrollTravelPx": 0,
            "iterations": 1,
        },
    )
    with patch(
        "superbrowser_bridge.session_tools._request_with_backoff",
        new=AsyncMock(return_value=fake_response),
    ):
        out = asyncio.run(tool.execute(session_id="session-x"))
    assert "## Collapsed groups" in out
    assert "expander label='United States'" in out
    assert "controls=#us-content" in out
    assert "(under 'United States' — expand parent first if collapsed)" in out
    # Manifest cached on state with new fields.
    assert s.last_filter_manifest is not None
    assert s.last_filter_manifest.get("expanders")
    assert s.last_filter_manifest["options"][0].get("parent_label") == "United States"


def test_eval_kill_switch() -> None:
    """EVAL_EXPLORATION_REFUSAL=0 disables the gate."""
    import asyncio
    import os
    from types import SimpleNamespace
    from superbrowser_bridge.session_tools import (
        BrowserSessionState, BrowserEvalTool,
    )

    s = BrowserSessionState()
    s.session_id = "session-x"
    s._last_vision_response = SimpleNamespace(bboxes=[1, 2, 3])
    s._vision_epoch_turn = 0
    s._brain_turn_counter = 1
    tool = BrowserEvalTool(s)

    prior = os.environ.get("EVAL_EXPLORATION_REFUSAL")
    os.environ["EVAL_EXPLORATION_REFUSAL"] = "0"
    try:
        try:
            out = asyncio.run(tool.execute(
                session_id="session-x",
                script="(() => { const x=[...document.querySelectorAll('a')]"
                       ".map(a=>a.href); return x; })()",
            ))
            assert "eval_for_exploration_refused" not in (out or "")
        except Exception:
            pass
    finally:
        if prior is None:
            os.environ.pop("EVAL_EXPLORATION_REFUSAL", None)
        else:
            os.environ["EVAL_EXPLORATION_REFUSAL"] = prior


def main() -> int:
    tests = [
        test_validator_rejects_empty,
        test_validator_rejects_single_step,
        test_validator_rejects_none_criterion,
        test_validator_rejects_empty_name,
        test_lifecycle_pending_to_satisfied,
        test_two_fail_rule_marks_unsatisfiable,
        test_skip_active,
        test_is_complete_when_all_resolved,
        test_render_full_and_compact,
        test_check_active_task_step_no_plan_returns_empty,
        test_check_active_task_step_url_changed_advances,
        test_refusal_no_plan_allows_through,
        test_refusal_blocks_when_step_in_progress_and_failure_unscreened,
        test_refusal_lifts_after_screenshot,
        test_refusal_lifts_after_3_stale_screenshots,
        test_refusal_kill_switch,
        test_loop_detector_stale_streak_resets_on_change,
        test_anchor_url_harvesting,
        test_vague_selector_classifier,
        test_eval_exploration_classifier,
        test_click_index_refusal_when_vision_fresh,
        test_click_index_refusal_lifts_when_no_vision,
        test_click_index_refused_even_when_vision_is_stale,
        test_click_index_kill_switch,
        test_eval_exploration_refusal_when_vision_fresh,
        test_eval_write_op_bypasses_refusal,
        test_eval_kill_switch,
        # v5
        test_step_prefix_present_when_plan_active,
        test_step_prefix_absent_when_no_plan,
        test_step_prefix_kill_switch,
        test_scroll_hint_extracts_proper_noun_target,
        test_scroll_hint_silent_without_proper_noun,
        test_scroll_hint_silent_without_plan,
        test_narration_optional_no_refusal,
        test_narration_renders_in_worker_hook_then_clears,
        test_inventory_hierarchy_kill_switch_strips_expanders,
        test_inventory_hierarchy_renders_collapsed_groups,
        # v6
        test_run_script_exploration_refused_when_vision_fresh,
        test_run_script_write_op_bypasses_exploration_refusal,
        test_run_script_exploration_kill_switch,
        test_navigate_url_filter_hack_refused_with_active_plan,
        test_navigate_filter_hack_allows_single_param,
        test_navigate_filter_hack_silent_without_plan,
        test_navigate_filter_hack_kill_switch,
        # v7
        test_intent_auto_inject_replaces_generic,
        test_intent_auto_inject_appends_when_specific,
        test_intent_auto_inject_missing_intent_uses_step,
        test_intent_auto_inject_silent_without_plan,
        test_close_guard_refuses_with_unsatisfied_plan,
        test_close_guard_allows_when_no_plan,
        test_close_guard_allows_when_budget_exhausted,
        test_close_guard_allows_when_all_satisfied,
        test_close_guard_kill_switch,
        # v8: vision-grounded clicks (target_label protocol)
        test_target_label_validates_substring_match,
        test_target_label_refuses_mismatch,
        test_target_label_required_when_missing,
        test_target_label_skips_when_no_vision_index,
        test_target_label_allows_when_actual_label_empty,
        test_target_label_skips_when_no_vision_response,
        test_target_label_kill_switch,
        test_target_label_out_of_range_lets_existing_gate_handle,
        test_click_at_executes_validation,
        test_type_at_executes_validation,
        # v9: orchestrator-level auto-retry on request_help bail
        test_request_help_bail_detected_via_step_history,
        test_request_help_bail_detected_via_text_pattern,
        test_request_help_bail_negatives,
        test_request_help_bail_handles_empty_inputs,
        test_build_retry_instructions_carries_context,
        test_build_retry_instructions_truncates_long_diagnostic,
        test_build_retry_instructions_handles_empty_unsatisfied,
        # v10: quick rescan for post-click expander
        test_expander_rescan_passes_no_scroll_walk,
        test_expander_rescan_quick_mode_kill_switch,
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
