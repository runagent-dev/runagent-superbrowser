"""Phase 6 Planner-handoff tests.

Covers parser robustness, disabled-mode behaviour, and the worker
hook's stall-replan trigger semantics. The full re-plan path that
actually calls the LLM is exercised via mocked decompose/replan.
"""

from __future__ import annotations

import asyncio
import os

from unittest.mock import AsyncMock, MagicMock

from superbrowser_bridge.planner_agent import (
    PlannerAgent,
    PlannerResult,
    default_planner,
)
from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.worker_hook import BrowserWorkerHook


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------- parser robustness ----------------

def test_parse_valid_steps():
    raw = (
        '{"steps": ['
        '{"label": "open google", "kind": "navigation"},'
        '{"label": "search headphones", "kind": "action"},'
        '{"label": "sort by rating", "kind": "filter"}'
        ']}'
    )
    r = PlannerAgent._parse(raw)
    assert len(r.steps) == 3
    assert r.steps[0]["label"] == "open google"
    assert r.steps[0]["kind"] == "navigation"
    assert r.steps[0]["predicate"] == {"manual": True}


def test_parse_abandon():
    raw = '{"abandon": true, "reason": "site requires login"}'
    r = PlannerAgent._parse(raw)
    assert r.abandon is True
    assert "login" in r.reason
    assert r.steps == []


def test_parse_invalid_json_returns_empty():
    r = PlannerAgent._parse("definitely not json")
    assert r.steps == []
    assert r.reason == "json_parse_error"


def test_parse_too_few_steps_rejected():
    raw = '{"steps": [{"label": "only one"}]}'
    r = PlannerAgent._parse(raw)
    assert r.steps == []
    assert "too_few_steps" in r.reason


def test_parse_invalid_kind_defaults_to_filter():
    raw = (
        '{"steps": ['
        '{"label": "step a", "kind": "made_up"},'
        '{"label": "step b", "kind": "filter"}'
        ']}'
    )
    r = PlannerAgent._parse(raw)
    assert r.steps[0]["kind"] == "filter"
    assert r.steps[1]["kind"] == "filter"


def test_parse_strips_markdown_fences():
    raw = "```json\n" + '{"steps": [{"label": "a"}, {"label": "b"}]}' + "\n```"
    r = PlannerAgent._parse(raw)
    assert len(r.steps) == 2


def test_parse_caps_at_six_steps():
    steps = ", ".join(f'{{"label": "step {i}"}}' for i in range(10))
    raw = f'{{"steps": [{steps}]}}'
    r = PlannerAgent._parse(raw)
    assert len(r.steps) == 6


# ---------------- disabled mode ----------------

def test_planner_disabled_without_credentials(monkeypatch):
    monkeypatch.delenv("PLANNER_API_KEY", raising=False)
    monkeypatch.delenv("VISION_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    p = PlannerAgent()
    assert not p.enabled
    r = _run(p.decompose("a long-enough task description that should still no-op"))
    assert r.steps == []
    assert r.reason == "planner_disabled"


def test_default_planner_singleton():
    p1 = default_planner()
    p2 = default_planner()
    assert p1 is p2


# ---------------- worker_hook integration ----------------

def test_worker_hook_has_replan_fields():
    state = BrowserSessionState()
    hook = BrowserWorkerHook(state, max_iterations=50)
    assert hasattr(hook, "_maybe_replan")
    assert hook._planner_replan_count == 0
    assert hook._last_planner_replan_iter == -1
    assert hook._PLANNER_STALL_THRESHOLD == 5
    assert hook._PLANNER_REPLAN_COOLDOWN == 8


def test_replan_installs_revised_brief(monkeypatch):
    """The full handoff: stall → planner → revised brief in state."""
    state = BrowserSessionState()
    state.task_instruction = "find headphones under 50"
    state.set_task_brief(
        "find headphones under 50",
        [
            {"label": "old step 1", "kind": "filter", "predicate": {"manual": True}},
            {"label": "old step 2", "kind": "filter", "predicate": {"manual": True}},
        ],
    )
    hook = BrowserWorkerHook(state, max_iterations=50)
    hook._stagnant_turns = 6  # past threshold

    # Mock the planner to return a revised plan.
    fake_planner = MagicMock()
    fake_planner.enabled = True
    fake_planner.replan = AsyncMock(
        return_value=PlannerResult(
            steps=[
                {"label": "new step A", "kind": "filter", "predicate": {"manual": True}},
                {"label": "new step B", "kind": "filter", "predicate": {"manual": True}},
                {"label": "new step C", "kind": "action", "predicate": {"manual": True}},
            ]
        )
    )

    import superbrowser_bridge.planner_agent as pa
    monkeypatch.setattr(pa, "default_planner", lambda: fake_planner)

    advice = _run(hook._maybe_replan(iteration=10, brief=state.task_brief))

    assert advice  # non-empty
    assert "revised plan" in advice.lower()
    # Brief was replaced with new steps
    assert len(state.task_brief.constraints) == 3
    assert state.task_brief.constraints[0].label == "new step A"
    # Stagnation reset to give the new brief a fair start
    assert hook._stagnant_turns == 0
    # Replan count incremented; cooldown updated
    assert hook._planner_replan_count == 1
    assert hook._last_planner_replan_iter == 10


def test_replan_abandon_returns_advice_no_brief_change(monkeypatch):
    state = BrowserSessionState()
    state.task_instruction = "find headphones"
    state.set_task_brief(
        "find headphones",
        [
            {"label": "step 1", "kind": "filter", "predicate": {"manual": True}},
            {"label": "step 2", "kind": "filter", "predicate": {"manual": True}},
        ],
    )
    hook = BrowserWorkerHook(state, max_iterations=50)
    original_constraint_count = len(state.task_brief.constraints)

    fake_planner = MagicMock()
    fake_planner.enabled = True
    fake_planner.replan = AsyncMock(
        return_value=PlannerResult(
            steps=[],
            abandon=True,
            reason="site requires SMS verification",
        )
    )
    import superbrowser_bridge.planner_agent as pa
    monkeypatch.setattr(pa, "default_planner", lambda: fake_planner)

    advice = _run(hook._maybe_replan(iteration=10, brief=state.task_brief))

    assert "unreachable" in advice
    assert "SMS verification" in advice
    # Brief preserved on abandon
    assert len(state.task_brief.constraints) == original_constraint_count
    assert hook._planner_replan_count == 1


def test_replan_no_credentials_returns_empty(monkeypatch):
    monkeypatch.delenv("PLANNER_API_KEY", raising=False)
    monkeypatch.delenv("VISION_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    state = BrowserSessionState()
    state.set_task_brief(
        "x",
        [
            {"label": "a", "predicate": {"manual": True}},
            {"label": "b", "predicate": {"manual": True}},
        ],
    )
    hook = BrowserWorkerHook(state)

    # Force fresh planner with no creds.
    import superbrowser_bridge.planner_agent as pa
    pa._INSTANCE = None
    monkeypatch.setattr(pa, "default_planner", lambda: PlannerAgent())

    advice = _run(hook._maybe_replan(iteration=5, brief=state.task_brief))
    assert advice == ""
