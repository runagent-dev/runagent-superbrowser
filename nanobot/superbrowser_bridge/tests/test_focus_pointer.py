"""Unit tests for Arch v4 Phase B: TaskBrief focus pointer.

Covers:
  - compute_focus picks first unverified by kind ordering when prereq +
    funnel signals are absent (filter > attribute > numeric > negative
    > ordering)
  - prerequisite_idx skips a constraint whose prereq is unverified
  - prerequisite chain unblocks once the prereq flips to satisfied
  - funnel-aware preference flips kind ordering on destination_input,
    results_list, etc.
  - mark_constraint recomputes current_focus_idx after a flip
  - to_dict / from_dict round-trips current_focus_idx + prerequisite_idx
  - merge_brief_progress recomputes focus on the new brief
  - to_brain_text full + focus_line render the [FOCUS] label correctly
  - [FOCUS] line appears in the iteration prompt; FOCUS_LINE=0 disables

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_focus_pointer
"""

from __future__ import annotations

import asyncio
import os
import sys


def _brief(kinds_with_status: list[tuple[str, str]], **kwargs) -> "TaskBrief":
    """Helper: build a TaskBrief from a list of (kind, status) tuples."""
    from superbrowser_bridge.task_brief import TaskBrief, Constraint

    constraints = [
        Constraint(
            text=f"c{i}",
            kind=k,
            canonical_value=f"c{i}",
            status=s,
        )
        for i, (k, s) in enumerate(kinds_with_status)
    ]
    return TaskBrief(original_query="x", constraints=constraints, **kwargs)


# ── compute_focus: kind ordering fallback ───────────────────────────


def test_compute_focus_kind_ordering_filter_first() -> None:
    from superbrowser_bridge.task_brief import compute_focus

    # Order: numeric, filter, attribute. Filter wins despite being
    # second in the list.
    b = _brief([("numeric", "unverified"), ("filter", "unverified"),
                ("attribute", "unverified")])
    assert compute_focus(b) == 1


def test_compute_focus_kind_ordering_full_ladder() -> None:
    from superbrowser_bridge.task_brief import compute_focus

    # filter > attribute > numeric > negative > ordering
    b = _brief([("ordering", "unverified"), ("negative", "unverified"),
                ("numeric", "unverified"), ("attribute", "unverified"),
                ("filter", "unverified")])
    assert compute_focus(b) == 4  # filter

    # No filter — attribute wins.
    b2 = _brief([("ordering", "unverified"), ("negative", "unverified"),
                 ("numeric", "unverified"), ("attribute", "unverified")])
    assert compute_focus(b2) == 3  # attribute

    # No filter or attribute — numeric wins.
    b3 = _brief([("ordering", "unverified"), ("negative", "unverified"),
                 ("numeric", "unverified")])
    assert compute_focus(b3) == 2  # numeric


def test_compute_focus_skips_satisfied_constraints() -> None:
    from superbrowser_bridge.task_brief import compute_focus

    b = _brief([("filter", "satisfied"), ("filter", "unverified"),
                ("filter", "satisfied")])
    assert compute_focus(b) == 1  # only unverified is index 1


def test_compute_focus_returns_neg_one_when_all_done() -> None:
    from superbrowser_bridge.task_brief import compute_focus

    b = _brief([("filter", "satisfied"), ("attribute", "satisfied"),
                ("numeric", "failed")])
    assert compute_focus(b) == -1


def test_compute_focus_empty_brief_returns_neg_one() -> None:
    from superbrowser_bridge.task_brief import TaskBrief, compute_focus

    b = TaskBrief(original_query="x")
    assert compute_focus(b) == -1


# ── compute_focus: prerequisite chain ───────────────────────────────


def test_compute_focus_skips_blocked_by_prerequisite() -> None:
    from superbrowser_bridge.task_brief import compute_focus

    # c0: attribute "destination" (unverified, the prereq)
    # c1: filter "wifi" (unverified, prereq=0 — blocked)
    # Even though filter normally beats attribute, c1 is blocked, so
    # c0 wins.
    b = _brief([("attribute", "unverified"), ("filter", "unverified")])
    b.constraints[1].prerequisite_idx = 0
    assert compute_focus(b) == 0


def test_compute_focus_unblocks_when_prereq_satisfied() -> None:
    from superbrowser_bridge.task_brief import compute_focus

    b = _brief([("attribute", "satisfied"), ("filter", "unverified")])
    b.constraints[1].prerequisite_idx = 0
    # Prereq is now satisfied → c1 is eligible. Filter beats nothing
    # left so c1 wins.
    assert compute_focus(b) == 1


def test_compute_focus_returns_neg_one_when_only_blocked_unverified() -> None:
    from superbrowser_bridge.task_brief import compute_focus

    # Only c1 is unverified, but it's blocked by a failed prereq.
    # The prereq was never satisfied → c1 stays blocked → no focus.
    b = _brief([("attribute", "failed"), ("filter", "unverified")])
    b.constraints[1].prerequisite_idx = 0
    assert compute_focus(b) == -1


# ── compute_focus: funnel-aware ─────────────────────────────────────


def test_compute_focus_funnel_destination_input_prefers_attribute() -> None:
    """On destination_input funnel, attribute wins over filter even
    though the static fallback would pick filter."""
    from superbrowser_bridge.task_brief import compute_focus

    b = _brief([("filter", "unverified"), ("attribute", "unverified")])
    page_state = type("PS", (), {"funnel": "destination_input"})()
    assert compute_focus(b, page_state) == 1  # attribute


def test_compute_focus_funnel_results_list_keeps_filter_first() -> None:
    from superbrowser_bridge.task_brief import compute_focus

    b = _brief([("attribute", "unverified"), ("filter", "unverified")])
    page_state = type("PS", (), {"funnel": "results_list"})()
    assert compute_focus(b, page_state) == 1  # filter (matches funnel)


def test_compute_focus_funnel_dict_form_works() -> None:
    """page_state may be a dict (e.g. serialized PageState). The picker
    tolerates both dataclass-shaped and dict-shaped inputs."""
    from superbrowser_bridge.task_brief import compute_focus

    b = _brief([("filter", "unverified"), ("attribute", "unverified")])
    page_state = {"funnel": "destination_input"}
    assert compute_focus(b, page_state) == 1  # attribute


def test_compute_focus_funnel_no_match_falls_back_to_kind() -> None:
    """If the funnel maps to kinds none of which are eligible, fall
    through to kind-ordering rather than returning -1."""
    from superbrowser_bridge.task_brief import compute_focus

    # Funnel says "checkout" prefers attribute/filter, but only
    # numeric/ordering are available.
    b = _brief([("ordering", "unverified"), ("numeric", "unverified")])
    page_state = type("PS", (), {"funnel": "checkout"})()
    # Falls through to kind-ordering: numeric beats ordering.
    assert compute_focus(b, page_state) == 1


# ── mark_constraint recomputes focus ────────────────────────────────


def test_mark_constraint_recomputes_focus() -> None:
    from superbrowser_bridge.task_brief import compute_focus

    b = _brief([("filter", "unverified"), ("attribute", "unverified")])
    b.current_focus_idx = compute_focus(b)
    assert b.current_focus_idx == 0  # filter wins

    # Flip the filter to satisfied — focus should auto-advance to the
    # attribute.
    b.mark_constraint(0, "satisfied", evidence="test")
    assert b.current_focus_idx == 1


def test_mark_constraint_focus_minus_one_when_done() -> None:
    from superbrowser_bridge.task_brief import compute_focus

    b = _brief([("filter", "unverified")])
    b.current_focus_idx = compute_focus(b)
    assert b.current_focus_idx == 0
    b.mark_constraint(0, "satisfied")
    assert b.current_focus_idx == -1


# ── to_dict / from_dict round-trip ──────────────────────────────────


def test_to_dict_from_dict_preserves_focus_and_prereq() -> None:
    from superbrowser_bridge.task_brief import TaskBrief

    b = _brief([("attribute", "unverified"), ("filter", "unverified")])
    b.constraints[1].prerequisite_idx = 0
    b.current_focus_idx = 0
    d = b.to_dict()
    assert d["current_focus_idx"] == 0
    assert d["constraints"][1]["prerequisite_idx"] == 0

    b2 = TaskBrief.from_dict(d)
    assert b2.current_focus_idx == 0
    assert b2.constraints[1].prerequisite_idx == 0
    assert b2.constraints[0].prerequisite_idx == -1  # default preserved


def test_from_dict_handles_legacy_briefs_without_focus() -> None:
    """Briefs serialized before Phase B don't have current_focus_idx;
    from_dict should default to -1, not crash."""
    from superbrowser_bridge.task_brief import TaskBrief

    legacy = {
        "original_query": "x",
        "constraints": [
            {"text": "c0", "kind": "filter", "canonical_value": "c0",
             "status": "unverified"},
        ],
    }
    b = TaskBrief.from_dict(legacy)
    assert b.current_focus_idx == -1
    assert b.constraints[0].prerequisite_idx == -1


# ── merge_brief_progress recomputes focus ───────────────────────────


def test_merge_brief_progress_recomputes_focus_on_new() -> None:
    """After merging old progress into new brief, focus should reflect
    the new brief's state — even if old had a stale focus index."""
    from superbrowser_bridge.task_brief import (
        TaskBrief, Constraint, merge_brief_progress,
    )

    old = TaskBrief(
        original_query="x",
        constraints=[
            Constraint(text="wifi", kind="filter", canonical_value="wifi",
                       status="satisfied", evidence="vision saw wifi=on"),
        ],
        current_focus_idx=0,
    )
    new = TaskBrief(
        original_query="x",
        constraints=[
            Constraint(text="wifi", kind="filter", canonical_value="wifi",
                       status="unverified"),
            Constraint(text="parking", kind="filter",
                       canonical_value="parking", status="unverified"),
        ],
        current_focus_idx=-1,
    )
    transferred = merge_brief_progress(old, new)
    assert transferred == 1
    assert new.constraints[0].status == "satisfied"
    # Focus should auto-advance to "parking" (index 1) since wifi was
    # transferred as satisfied.
    assert new.current_focus_idx == 1


# ── focus_line + to_brain_text ──────────────────────────────────────


def test_focus_line_renders_when_set() -> None:
    from superbrowser_bridge.task_brief import compute_focus

    b = _brief([("filter", "unverified")])
    b.current_focus_idx = compute_focus(b)
    line = b.focus_line()
    assert "[FOCUS]" in line
    assert "#1" in line
    assert "'c0'" in line
    assert "(filter, unverified)" in line


def test_focus_line_empty_when_unset() -> None:
    from superbrowser_bridge.task_brief import TaskBrief

    b = TaskBrief(original_query="x")
    assert b.focus_line() == ""


def test_to_brain_text_full_includes_focus_line() -> None:
    from superbrowser_bridge.task_brief import compute_focus

    b = _brief([("filter", "unverified"), ("attribute", "unverified")])
    b.current_focus_idx = compute_focus(b)
    text = b.to_brain_text(compact=False)
    assert "[FOCUS]" in text
    assert "Original: x" in text


# ── worker_hook surfaces [FOCUS] line ───────────────────────────────


def test_worker_hook_emits_focus_line_after_original_query() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_brief import compute_focus
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.task_brief = _brief([("filter", "unverified")])
    s.task_brief.current_focus_idx = compute_focus(s.task_brief)
    hook = BrowserWorkerHook(s)
    ctx = type(
        "Ctx", (),
        {"iteration": 0, "messages": [{"role": "tool", "content": "x"}]},
    )()
    asyncio.run(hook.after_iteration(ctx))
    msg = ctx.messages[-1]["content"]
    assert "[ORIGINAL_QUERY]" in msg
    assert "[FOCUS]" in msg
    # [FOCUS] should appear AFTER [ORIGINAL_QUERY] in the guidance text.
    assert msg.index("[ORIGINAL_QUERY]") < msg.index("[FOCUS]")


def test_focus_line_kill_switch_disables() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.task_brief import compute_focus
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.task_brief = _brief([("filter", "unverified")])
    s.task_brief.current_focus_idx = compute_focus(s.task_brief)
    hook = BrowserWorkerHook(s)
    ctx = type(
        "Ctx", (),
        {"iteration": 0, "messages": [{"role": "tool", "content": "x"}]},
    )()
    os.environ["FOCUS_LINE"] = "0"
    try:
        asyncio.run(hook.after_iteration(ctx))
    finally:
        del os.environ["FOCUS_LINE"]
    msg = ctx.messages[-1]["content"]
    assert "[FOCUS]" not in msg


# ── progress block uses current_focus_idx ───────────────────────────


def test_progress_block_uses_current_focus_idx() -> None:
    """The [PROGRESS] stuck-variant cites the focus pointer, not the
    first-unverified constraint."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    # Two unverified constraints; brain manually pinned focus to #2.
    s.task_brief = _brief([("filter", "unverified"), ("filter", "unverified")])
    s.task_brief.current_focus_idx = 1  # brain-pinned to second
    hook = BrowserWorkerHook(s)

    # 4 quiet turns to trigger filter-kind threshold (3).
    for i in range(4):
        ctx = type(
            "Ctx", (),
            {"iteration": i, "messages": [{"role": "tool", "content": "x"}]},
        )()
        asyncio.run(hook.after_iteration(ctx))
    msg = ctx.messages[-1]["content"]
    assert "No constraint flipped" in msg
    # The stuck line should cite #2 (brain-pinned focus), not #1.
    assert "#2" in msg


def main() -> int:
    tests = [
        # kind ordering fallback
        test_compute_focus_kind_ordering_filter_first,
        test_compute_focus_kind_ordering_full_ladder,
        test_compute_focus_skips_satisfied_constraints,
        test_compute_focus_returns_neg_one_when_all_done,
        test_compute_focus_empty_brief_returns_neg_one,
        # prerequisite chain
        test_compute_focus_skips_blocked_by_prerequisite,
        test_compute_focus_unblocks_when_prereq_satisfied,
        test_compute_focus_returns_neg_one_when_only_blocked_unverified,
        # funnel-aware
        test_compute_focus_funnel_destination_input_prefers_attribute,
        test_compute_focus_funnel_results_list_keeps_filter_first,
        test_compute_focus_funnel_dict_form_works,
        test_compute_focus_funnel_no_match_falls_back_to_kind,
        # mark_constraint recomputes
        test_mark_constraint_recomputes_focus,
        test_mark_constraint_focus_minus_one_when_done,
        # serialization
        test_to_dict_from_dict_preserves_focus_and_prereq,
        test_from_dict_handles_legacy_briefs_without_focus,
        # merge
        test_merge_brief_progress_recomputes_focus_on_new,
        # rendering
        test_focus_line_renders_when_set,
        test_focus_line_empty_when_unset,
        test_to_brain_text_full_includes_focus_line,
        # worker hook integration
        test_worker_hook_emits_focus_line_after_original_query,
        test_focus_line_kill_switch_disables,
        test_progress_block_uses_current_focus_idx,
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
