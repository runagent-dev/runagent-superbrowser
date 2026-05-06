"""Trace-4 regression tests (Phases M, N, O, Q from the wineaccess
2026-05-05 23:01–23:05 trace).

Phase M — brief_mark predicate-evidence validation
Phase N — UUID-in-path fabrication guard
Phase O — search-query path NOT credited as filter
Phase Q — V_n cascade verification (already covered by trace3 tests;
          adds the narrow case where 3+ bad_vision_index trip the
          force-reobserve gate via worker_hook.)
"""

from __future__ import annotations

from superbrowser_bridge.session_tools import BrowserSessionState
from superbrowser_bridge.task_brief import TaskBrief
from superbrowser_bridge.worker_hook import BrowserWorkerHook


# ----------------- Phase M: brief_mark validation -----------------


def test_brief_mark_refuses_evidence_about_wrong_region():
    """The trace-4 cascade trigger: brain marks Oregon DONE with
    evidence about United States."""
    brief = TaskBrief(
        "task",
        [
            {
                "label": "Oregon region",
                "kind": "filter",
                "predicate": {
                    "vision_active_label": ["Oregon"],
                    "url_contains": ["oregon"],
                },
            },
        ],
    )
    ok = brief.mark(
        1, "done",
        "Region accordion shows United States; results include Pacific NW wines",
        validate_evidence=True,
    )
    assert ok is False
    assert brief.constraints[0].status == "open"
    # Sentinel preserved for the tool layer to extract.
    assert "[refused:" in (brief.constraints[0].evidence or "")


def test_brief_mark_accepts_predicate_value_in_evidence():
    brief = TaskBrief(
        "task",
        [
            {
                "label": "Oregon region",
                "kind": "filter",
                "predicate": {
                    "vision_active_label": ["Oregon"],
                    "url_contains": ["oregon"],
                },
            },
        ],
    )
    ok = brief.mark(
        1, "done",
        "Selected Oregon checkbox; URL now contains oregon",
        validate_evidence=True,
    )
    assert ok is True
    assert brief.constraints[0].status == "done"


def test_brief_mark_validation_skips_extraction_kind():
    """Extraction constraints aren't subject to predicate-evidence
    validation — the evidence is inherently free-form."""
    brief = TaskBrief(
        "task",
        [
            {
                "label": "Extract product page details",
                "kind": "extraction",
                "predicate": {"manual": True},
            },
        ],
    )
    ok = brief.mark(
        1, "done",
        "Captured all fields from the product page",
        validate_evidence=True,
    )
    assert ok is True
    assert brief.constraints[0].status == "done"


def test_brief_mark_validation_falls_back_to_label_when_predicate_manual():
    """When predicate is just {manual: True}, validation falls back to
    label tokens (minus generic category words)."""
    brief = TaskBrief(
        "task",
        [
            {
                "label": "Highest critic score among matches",
                "kind": "filter",
                "predicate": {"manual": True},
            },
        ],
    )
    # Evidence references "critic" — a label token.
    ok = brief.mark(
        1, "done",
        "Identified the highest-scoring critic-rated match",
        validate_evidence=True,
    )
    assert ok is True
    # Evidence with NO discriminating tokens → refused.
    brief2 = TaskBrief(
        "task",
        [
            {
                "label": "Highest critic score among matches",
                "kind": "filter",
                "predicate": {"manual": True},
            },
        ],
    )
    ok2 = brief2.mark(1, "done", "Did some clicks", validate_evidence=True)
    assert ok2 is False


def test_brief_mark_category_words_dont_credit_evidence():
    """'region' in evidence shouldn't satisfy a 'region'-categorized
    constraint — only the specific value (Oregon, Willamette) does."""
    brief = TaskBrief(
        "task",
        [
            {
                "label": "Oregon region",
                "kind": "filter",
                "predicate": {"vision_active_label": ["Oregon"]},
            },
        ],
    )
    ok = brief.mark(
        1, "done",
        "Region accordion is now expanded showing region options",
        validate_evidence=True,
    )
    assert ok is False  # 'region' is a stop category, doesn't credit


# ----------------- Phase N: UUID-in-path guard -----------------


def test_observed_link_hrefs_record_url_adds_to_set():
    s = BrowserSessionState()
    s.record_url("https://example.com/page1")
    assert "https://example.com/page1" in s.observed_link_hrefs


def test_harvest_link_hrefs_filters_non_http():
    s = BrowserSessionState()
    added = s.harvest_link_hrefs([
        "https://example.com/a",
        "/relative/b",
        "mailto:hello@example.com",
        "javascript:void(0)",
        "tel:+1234567890",
        "https://example.com/c",
    ])
    assert added == 3  # https, relative, https
    assert "https://example.com/a" in s.observed_link_hrefs
    assert "/relative/b" in s.observed_link_hrefs
    assert "mailto:hello@example.com" not in s.observed_link_hrefs
    assert "javascript:void(0)" not in s.observed_link_hrefs


def test_harvest_link_hrefs_caps_at_1024():
    s = BrowserSessionState()
    s.observed_link_hrefs = {f"https://example.com/{i}" for i in range(1024)}
    added = s.harvest_link_hrefs(["https://example.com/new"])
    # Already at cap → no new add.
    assert added == 0
    assert "https://example.com/new" not in s.observed_link_hrefs


# ----------------- Phase O: search-query path -----------------


def test_search_url_does_not_flip_filter_constraint():
    """Trace-4 case: /store/search/oregon/ doesn't auto-flip Oregon
    filter constraint."""
    brief = TaskBrief(
        "t",
        [{"label": "Oregon region", "kind": "filter", "predicate": {"url_contains": ["oregon"]}}],
    )
    brief.reconcile_from_url(
        "https://www.wineaccess.com/store/search/white%20wine%20Oregon/?ordering=-score"
    )
    assert brief.constraints[0].status == "open"


def test_real_filter_path_does_flip_filter_constraint():
    brief = TaskBrief(
        "t",
        [{"label": "Oregon region", "kind": "filter", "predicate": {"url_contains": ["oregon"]}}],
    )
    brief.reconcile_from_url("https://www.wineaccess.com/store/regions/oregon/")
    assert brief.constraints[0].status == "done"


def test_navigation_constraint_still_flips_on_search_url():
    """Navigation constraints (kind=navigation) still flip on search
    URLs because navigating to results IS the goal there."""
    brief = TaskBrief(
        "t",
        [{"label": "Open results", "kind": "navigation", "predicate": {"url_contains": ["oregon"]}}],
    )
    brief.reconcile_from_url("https://www.wineaccess.com/store/search/oregon/")
    assert brief.constraints[0].status == "done"


def test_search_strip_handles_results_path():
    """The strip regex should match /search/, /results/, /find/ paths."""
    brief = TaskBrief(
        "t",
        [{"label": "Oregon", "kind": "filter", "predicate": {"url_contains": ["oregon"]}}],
    )
    brief.reconcile_from_url("https://example.com/results/oregon/")
    assert brief.constraints[0].status == "open"


# ----------------- Phase Q: V_n cascade -----------------


def test_force_reobserve_fires_after_3_cursor_failures(monkeypatch):
    """Three bad_vision_index calls should set
    state._force_reobserve_pending=True via the worker hook's
    cursor-cascade detector."""
    state = BrowserSessionState()
    state._brain_turn_counter = 1
    # Simulate 3 bad_vision_index failures recorded recently.
    state.record_cursor_failure(
        strategy="click_at", target="V47", reason="bad_vision_index"
    )
    state._brain_turn_counter = 2
    state.record_cursor_failure(
        strategy="click_at", target="V51", reason="bad_vision_index"
    )
    state._brain_turn_counter = 3
    state.record_cursor_failure(
        strategy="click_at", target="V72", reason="bad_vision_index"
    )
    # Make epoch look stale (3 turns old).
    state._vision_epoch_turn = 0
    hook = BrowserWorkerHook(state, max_iterations=50)
    hook._maybe_set_force_reobserve(state.cursor_failure_records)
    assert state._force_reobserve_pending is True
