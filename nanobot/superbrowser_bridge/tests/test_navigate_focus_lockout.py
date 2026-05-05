"""Tests for the per-focus navigate lockout state machine.

The trace pattern this guards: brain hits filter_hack_refused on a
constructed URL, then immediately tries another URL variant for the
same focus. Without the lockout, each variant burns one iteration; with
the lockout, the second navigate is short-circuited with a stronger
[NAV_LOCKED_FOR_FOCUS] message that points the brain at brief_mark.

The lockout state itself lives on `BrowserSessionState`. It's set by
the `_record_nav_refusal` helper inside `tools/navigation.py` and
cleared by the worker hook on a deliberation tool call.
"""

from __future__ import annotations

from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.session_tools.tools.navigation import (
    _record_nav_refusal,
)
from superbrowser_bridge.task_brief import TaskBrief


def _state_with_brief(*labels: str) -> BrowserSessionState:
    s = BrowserSessionState()
    s.task_brief = TaskBrief(
        "q",
        [{"label": lbl, "kind": "filter", "predicate": {"manual": True}}
         for lbl in labels],
    )
    return s


# ----------------------------- recording --------------------------------


def test_nav_refusal_records_focus_id_for_lockout():
    s = _state_with_brief("White wine", "Price under $40")
    _record_nav_refusal(s, "https://x.com/?max_price=40", "filter_hack")
    # First open constraint is #1 (White wine).
    assert s.last_navigate_refusal_focus_id == 1


def test_nav_refusal_appends_to_brief_attempt_ledger():
    s = _state_with_brief("White wine")
    _record_nav_refusal(s, "https://x.com/", "filter_hack")
    # The brief should now have one (failed) attempt on focus #1.
    assert s.task_brief.attempts_on(1) == 1
    assert s.task_brief.failed_attempts_on(1) == 1


def test_nav_refusal_no_brief_is_safe_no_op():
    s = BrowserSessionState()
    s.task_brief = None
    _record_nav_refusal(s, "https://x.com/", "filter_hack")
    # Lockout untouched, no crash.
    assert s.last_navigate_refusal_focus_id is None


def test_nav_refusal_all_done_is_safe_no_op():
    s = _state_with_brief("White wine")
    s.task_brief.constraints[0].status = "done"
    _record_nav_refusal(s, "https://x.com/", "filter_hack")
    # next_focus() returned None → nothing recorded, no lockout set.
    assert s.last_navigate_refusal_focus_id is None


# -------------------- lockout-clear conditions --------------------------
#
# The lockout is cleared by the worker hook when the brain takes a
# deliberation action OR when the focus advances past the locked id.
# We mirror the hook's logic here so the contract is documented.


def test_lockout_should_clear_on_focus_advance():
    s = _state_with_brief("White wine", "Price under $40")
    _record_nav_refusal(s, "https://x.com/", "filter_hack")
    assert s.last_navigate_refusal_focus_id == 1
    # Mark #1 done (e.g. via brief_mark or an URL match).
    s.task_brief.mark(1, "done", "manual")
    # Hook contract: when next_focus().id != lockout_id, clear it.
    cur_focus = s.task_brief.next_focus()
    assert cur_focus is not None and cur_focus.id == 2
    if cur_focus.id != s.last_navigate_refusal_focus_id:
        s.last_navigate_refusal_focus_id = None
    assert s.last_navigate_refusal_focus_id is None


def test_lockout_state_default_is_none():
    s = BrowserSessionState()
    assert s.last_navigate_refusal_focus_id is None
