"""Unit tests for Pillar B — plan visibility + structured guidance.

Verifies:
  * The task graph is prepended to build_text_only output on every
    tool reply (plan-always-visible).
  * rebuild_subgoals preserves `done` subgoals verbatim while
    rewriting pending ones (mocked LLM response).
  * Worker-hook structured guidance retains retry-hint semantics and
    a 3rd-strike promotes to `severity="force"` with
    `next_tool="browser_rewind_to_checkpoint"`.

No network calls. Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_adaptive_graph.py
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any


def _state_with_graph():
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_graph import Signal, Subgoal, TaskGraph
    s = BrowserSessionState()
    s.session_id = "test"
    s.task_graph = TaskGraph(
        subgoals={
            "g1": Subgoal(
                id="g1", description="search for thing",
                expected_signals=[Signal(kind="url_contains", payload={"text": "/results"})],
                status="done",
            ),
            "g2": Subgoal(
                id="g2", description="pick first result",
                expected_signals=[Signal(kind="url_contains", payload={"text": "/product"})],
                status="active",
            ),
            "g3": Subgoal(
                id="g3", description="add to cart",
                expected_signals=[Signal(kind="url_contains", payload={"text": "/cart"})],
                status="pending",
            ),
        },
        active_id="g2",
    )
    return s


def test_plan_always_visible_in_build_text_only() -> None:
    s = _state_with_graph()
    out = s.build_text_only(
        {"url": "https://example.test/", "title": "t"},
        "Clicked [1]",
    )
    assert "[PLAN]" in out
    assert "g1: search for thing" in out
    assert "g2: pick first result" in out
    assert "Clicked [1]" in out
    # Plan should come FIRST so the brain re-anchors before reading
    # the rest of the tool reply.
    assert out.index("[PLAN]") < out.index("Clicked [1]")
    print("✓ test_plan_always_visible_in_build_text_only")


def test_plan_renders_status_markers() -> None:
    s = _state_with_graph()
    out = s.build_text_only({}, "")
    # done=✓, active=▶, pending=·
    assert "✓ g1" in out
    assert "▶ g2" in out
    assert "· g3" in out
    print("✓ test_plan_renders_status_markers")


def test_plan_absent_when_no_task_graph() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    assert s.task_graph is None
    out = s.build_text_only({"url": "https://example.test/"}, "hi")
    assert "[PLAN]" not in out
    print("✓ test_plan_absent_when_no_task_graph")


def test_rebuild_subgoals_preserves_done_and_swaps_pending() -> None:
    """Mock _make_text_client so the rebuild path runs deterministically.
    The stub client returns a replacement subgoal for pending items."""
    import superbrowser_bridge.task_graph as TG

    class _Msg:
        content = (
            '{"subgoals": [{"id": "g2", "description": "consent page: accept",'
            ' "look_for": ["Accept"], "expected_signals": ['
            '{"kind": "url_contains", "payload": {"text": "/product"}}]}'
            ',{"id": "g3", "description": "add to cart", "look_for": [],'
            ' "expected_signals": [{"kind": "url_contains", "payload":'
            ' {"text": "/cart"}}]}]}'
        )

    class _Choice:
        message = _Msg()

    class _Completion:
        choices = [_Choice()]

    class _Completions:
        async def create(self, **kw: Any) -> _Completion:
            return _Completion()

    class _Chat:
        completions = _Completions()

    class _FakeClient:
        chat = _Chat()

    def _fake_make(*a: Any, **kw: Any) -> tuple[Any, str]:
        return _FakeClient(), "test-model"

    TG._make_text_client = _fake_make  # type: ignore[assignment]

    s = _state_with_graph()
    graph = s.task_graph

    async def run() -> Any:
        return await TG.rebuild_subgoals(
            graph,
            task_instruction="buy widget",
            current_url="https://example.test/consent",
            url_changed=True,
        )

    new_graph, reason = asyncio.run(run())
    assert reason, f"rebuild should have returned a reason, got {reason!r}"
    # Done subgoal preserved verbatim.
    assert "g1" in new_graph.subgoals
    assert new_graph.subgoals["g1"].description == "search for thing"
    assert new_graph.subgoals["g1"].status == "done"
    # Pending/active subgoals rewritten. (The LLM stub emits ids g2+g3.)
    assert new_graph.subgoals["g2"].description == "consent page: accept"
    # Active pointer lands on the first pending subgoal.
    assert new_graph.active_id == "g2"
    assert new_graph.current().description == "consent page: accept"
    print("✓ test_rebuild_subgoals_preserves_done_and_swaps_pending")


def test_rebuild_no_op_without_pending() -> None:
    from superbrowser_bridge.task_graph import Signal, Subgoal, TaskGraph, rebuild_subgoals
    g = TaskGraph(
        subgoals={
            "g1": Subgoal(
                id="g1", description="only", expected_signals=[],
                status="done",
            ),
        },
        active_id="g1",
    )

    async def run() -> Any:
        return await rebuild_subgoals(
            g, task_instruction="x", current_url="y", url_changed=True,
        )

    new_graph, reason = asyncio.run(run())
    assert new_graph is g
    assert reason == ""
    print("✓ test_rebuild_no_op_without_pending")


def test_structured_guidance_is_emitted_into_hook_state() -> None:
    """Exercise after_iteration and verify the structured_guidance list
    ends up on the hook_state dict. We feed a repeated-tool scenario
    so the loop detector + retry builder emit at least one entry."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook, StructuredGuidance

    s = BrowserSessionState()
    s.session_id = "t1"
    s.current_url = "https://example.test/"
    # Push three identical failed clicks so LoopDetector nudges fire.
    for _ in range(3):
        s.step_history.append({
            "tool": "browser_click",
            "args": "index=5",
            "result": "[click_failed:unknown_index]",
            "url": "https://example.test/",
        })

    class _Ctx:
        iteration = 5
        messages: list[dict] = [{"role": "tool", "content": "ok"}]
        hook_state: dict = {}

    ctx = _Ctx()
    hook = BrowserWorkerHook(s, max_iterations=50)
    asyncio.run(hook.after_iteration(ctx))
    parts = ctx.hook_state.get("structured_guidance")
    assert isinstance(parts, list)
    assert all(isinstance(p, StructuredGuidance) for p in parts)
    print("✓ test_structured_guidance_is_emitted_into_hook_state")


def test_force_rewind_promoted_after_three_strikes() -> None:
    """A 3rd identical (tool, args, result) must produce a forced
    rewind and stash _forced_next_tool on state."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "t1"
    s.current_url = "https://example.test/"
    s.best_checkpoint_url = "https://example.test/home"
    for _ in range(3):
        s.step_history.append({
            "tool": "browser_click",
            "args": "index=5",
            "result": "[click_failed:unknown_index]",
            "url": "https://example.test/",
        })

    class _Ctx:
        iteration = 5
        messages: list[dict] = [{"role": "tool", "content": "ok"}]
        hook_state: dict = {}

    hook = BrowserWorkerHook(s, max_iterations=50)
    asyncio.run(hook.after_iteration(_Ctx()))
    assert getattr(s, "_forced_next_tool", None) == "browser_rewind_to_checkpoint"
    print("✓ test_force_rewind_promoted_after_three_strikes")


def test_force_dropped_when_brain_disobeys() -> None:
    """If the brain picks a different tool after a force, we clear the
    force instead of re-shouting every turn."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "t1"
    s.current_url = "https://example.test/"
    s.best_checkpoint_url = "https://example.test/home"
    s._forced_next_tool = "browser_rewind_to_checkpoint"
    # Brain did something else — picked screenshot instead.
    s.step_history.append({
        "tool": "browser_screenshot",
        "args": "",
        "result": "fresh page",
        "url": "https://example.test/",
    })

    class _Ctx:
        iteration = 7
        messages: list[dict] = [{"role": "tool", "content": "ok"}]
        hook_state: dict = {}

    hook = BrowserWorkerHook(s, max_iterations=50)
    hook._force_rewind_emitted = True  # simulate prior emission
    asyncio.run(hook.after_iteration(_Ctx()))
    assert getattr(s, "_forced_next_tool", None) is None
    print("✓ test_force_dropped_when_brain_disobeys")


def main() -> int:
    tests = [
        test_plan_always_visible_in_build_text_only,
        test_plan_renders_status_markers,
        test_plan_absent_when_no_task_graph,
        test_rebuild_subgoals_preserves_done_and_swaps_pending,
        test_rebuild_no_op_without_pending,
        test_structured_guidance_is_emitted_into_hook_state,
        test_force_rewind_promoted_after_three_strikes,
        test_force_dropped_when_brain_disobeys,
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
