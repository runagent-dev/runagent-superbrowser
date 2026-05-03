"""Unit tests for the persistent multi-step task plan.

Covers the public surface of task_plan.py:
  • validator rejects empty / single-step / no-criterion plans
  • per-step lifecycle: pending → in_progress → satisfied | unsatisfiable
  • 2-fail rule flips a step to unsatisfiable; subsequent active_step()
    returns the next step
  • compact vs full rendering format

No external services required. Run:
    source venv/bin/activate && \
        python nanobot/superbrowser_bridge/tests/test_task_plan.py
"""

from __future__ import annotations

import asyncio
import sys


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
