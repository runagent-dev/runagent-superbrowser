"""Tests for the per-focus attempt ledger + [FOCUS_EXHAUSTED] directive.

Background: the wineaccess.com trace showed the brain spiral on a single
focus constraint (Price under $40) — type([33], "40") → type([41], "40")
→ type_at(V1, "40") → rewind → navigate(hallucinated URL). Each
individual guard (repeat-type, filter-hack, etc.) refused one bad call
but the brain kept re-rolling against the same focus. The per-focus
ledger lets the worker hook detect cumulative failure on a single
constraint and emit a kind-specific pivot directive.
"""

from __future__ import annotations

from superbrowser_bridge.task_brief import TaskBrief, _looks_numeric_range


# ------------------------------ helpers ---------------------------------


def _brief_with_focus_kind(kind: str, label: str = "Focus item") -> TaskBrief:
    return TaskBrief("q", [
        {"label": label, "kind": kind, "predicate": {"manual": True}},
        {"label": "next item", "kind": "filter", "predicate": {"manual": True}},
    ])


# ----------------------------- recording --------------------------------


def test_record_attempt_appends_to_focus_only():
    b = _brief_with_focus_kind("filter", "Price under $40")
    c = b.record_attempt(
        tool="browser_type",
        target="index=33",
        result="ok",
        iteration=1,
    )
    assert c is not None
    assert c.id == 1
    assert len(b.constraints[0].attempts) == 1
    assert b.constraints[1].attempts == []  # only the focus gets the entry


def test_failed_attempts_only_counts_failure_markers():
    b = _brief_with_focus_kind("filter")
    b.record_attempt("browser_type", "index=33", "ok (typed_into_empty)", 1)
    b.record_attempt("browser_type", "index=41", "REPEAT_TYPE: refused", 2)
    b.record_attempt("browser_navigate", "url=…", "BLOCKED: filter_hack", 3)
    assert b.attempts_on(1) == 3
    assert b.failed_attempts_on(1) == 2  # only the two refusals


def test_record_attempt_dedups_same_iteration_same_tool():
    b = _brief_with_focus_kind("filter")
    b.record_attempt("browser_type", "index=33", "ok", 1)
    b.record_attempt("browser_type", "index=33", "ok", 1)  # same iter, same tool
    assert b.attempts_on(1) == 1


def test_record_attempt_returns_none_when_all_done():
    b = TaskBrief("q", [{"label": "x", "kind": "filter"}])
    b.constraints[0].status = "done"
    assert b.record_attempt("browser_click", "i=1", "ok", 1) is None


def test_attempts_capped_at_20():
    b = _brief_with_focus_kind("filter")
    for i in range(30):
        b.record_attempt("browser_type", f"i={i}", "REFUSED", i)
    assert len(b.constraints[0].attempts) == 20
    # Should retain the most recent entries
    assert b.constraints[0].attempts[-1]["iter"] == 29


# ------------------------ FOCUS_EXHAUSTED render ------------------------


def test_focus_exhausted_fires_only_once_per_threshold():
    b = _brief_with_focus_kind("filter", "Price under $40")
    for i in range(3):
        b.record_attempt("browser_type", f"index={i}", "REFUSED", i)
    first = b.render_focus_exhausted(1, threshold=3)
    second = b.render_focus_exhausted(1, threshold=3)
    assert "[FOCUS_EXHAUSTED" in first
    assert second == ""  # idempotent


def test_focus_exhausted_routes_to_slider_for_numeric_filter():
    b = _brief_with_focus_kind("filter", "Price under $40")
    for i in range(3):
        b.record_attempt("browser_type", f"index={i}", "REFUSED", i)
    block = b.render_focus_exhausted(1, threshold=3)
    assert "browser_set_slider_at" in block
    assert "browser_list_slider_handles" in block


def test_focus_exhausted_routes_to_scroll_for_non_numeric_filter():
    b = _brief_with_focus_kind("filter", "White wine type")
    for i in range(3):
        b.record_attempt("browser_click", f"i={i}", "REFUSED", i)
    block = b.render_focus_exhausted(1, threshold=3)
    assert "browser_scroll_until" in block
    assert "browser_set_slider_at" not in block


def test_focus_exhausted_includes_brief_mark_escape_hatch():
    b = _brief_with_focus_kind("filter", "Pairs with rainbow")
    for i in range(3):
        b.record_attempt("browser_click", f"i={i}", "REFUSED", i)
    block = b.render_focus_exhausted(1, threshold=3)
    assert "browser_brief_mark" in block
    assert "not_applicable" in block


def test_focus_exhausted_escalates_at_threshold_5():
    b = _brief_with_focus_kind("filter", "Price under $40")
    for i in range(5):
        b.record_attempt("browser_type", f"i={i}", "REFUSED", i)
    block5 = b.render_focus_exhausted(1, threshold=5)
    assert "MANDATORY" in block5


def test_focus_exhausted_returns_empty_for_unknown_id():
    b = _brief_with_focus_kind("filter")
    assert b.render_focus_exhausted(999, threshold=3) == ""


# -------------------------- numeric-range hint --------------------------


def test_numeric_range_hint_recognises_price():
    assert _looks_numeric_range("Price under $40")
    assert _looks_numeric_range("Cost between 10 and 20")
    assert _looks_numeric_range("Rating ≥ 90")


def test_numeric_range_hint_recognises_year():
    assert _looks_numeric_range("Year 2018 or later")


def test_numeric_range_hint_skips_textual_filter():
    assert not _looks_numeric_range("White wine type")
    assert not _looks_numeric_range("Pairs with fish")


def test_numeric_range_hint_treats_bare_digits_as_numeric():
    # "3 stars" doesn't have an obvious price keyword but still is numeric.
    assert _looks_numeric_range("3 stars and up")
