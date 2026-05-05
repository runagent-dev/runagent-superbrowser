"""Tests for TaskBrief — the multi-condition checklist tracker.

The unit suite covers reconciliation (URL + page-text + vision active
labels), focus computation, manual marking, and rendering. We use light
fakes for the vision response object so tests stay independent of the
vision_agent schemas.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from superbrowser_bridge.task_brief import TaskBrief


# ----------------------------- helpers ----------------------------------


@dataclass
class _FakeBBox:
    label: str = ""
    is_selected: bool = False
    is_active: bool = False


@dataclass
class _FakeVR:
    bboxes: list = field(default_factory=list)
    summary: str = ""
    relevant_text: str = ""


def _brief(items):
    return TaskBrief("test query", items)


# --------------------------- reconciliation -----------------------------


def test_reconcile_url_contains_flips_open_to_done():
    b = _brief([
        {"label": "Oregon region", "kind": "filter",
         "predicate": {"url_contains": ["oregon"]}},
        {"label": "Untouched", "kind": "filter",
         "predicate": {"url_contains": ["california"]}},
    ])
    flipped = b.reconcile_from_url("https://klwines.com/p/?region_slug=oregon")
    assert flipped is True
    assert b.constraints[0].status == "done"
    assert "oregon" in b.constraints[0].evidence
    assert b.constraints[1].status == "open"


def test_reconcile_url_contains_case_insensitive():
    b = _brief([
        {"label": "Oregon", "kind": "filter",
         "predicate": {"url_contains": ["OREGON"]}},
    ])
    b.reconcile_from_url("https://example.com/store/oregon-wines")
    assert b.constraints[0].status == "done"


def test_reconcile_url_param_match():
    b = _brief([
        {"label": "Max price 40", "kind": "filter",
         "predicate": {"url_param": {"max_price": ["40"]}}},
    ])
    b.reconcile_from_url("https://x.com/?max_price=40&sort=score")
    assert b.constraints[0].status == "done"
    assert "max_price" in b.constraints[0].evidence


def test_reconcile_url_param_misses_when_value_differs():
    b = _brief([
        {"label": "Max price 40", "kind": "filter",
         "predicate": {"url_param": {"max_price": ["40"]}}},
    ])
    b.reconcile_from_url("https://x.com/?max_price=80")
    assert b.constraints[0].status == "open"


def test_reconcile_page_text_flips_verification_kind():
    # Verification-kind constraints CAN auto-flip from page_text — that's
    # the whole point of "is this string visible on the page".
    b = _brief([
        {"label": "Result page shows fish pairing", "kind": "verification",
         "predicate": {"page_text": ["fish pairing"]}},
    ])
    flipped = b.reconcile_from_page_state(None, "## Wine details\nGreat fish pairing\n")
    assert flipped is True
    assert b.constraints[0].status == "done"


def test_reconcile_page_text_does_NOT_flip_filter_kind():
    # Filter sidebars render every option's label, so 'Oregon' appearing
    # in page text doesn't mean the Oregon filter is APPLIED. Filter
    # constraints must rely on URL or vision_active_label evidence.
    vr = _FakeVR(bboxes=[_FakeBBox(label="Oregon"), _FakeBBox(label="White Wine")])
    b = _brief([
        {"label": "Oregon region", "kind": "filter",
         "predicate": {"page_text": ["Oregon"]}},
    ])
    flipped = b.reconcile_from_page_state(vr, "Oregon wines")
    assert flipped is False
    assert b.constraints[0].status == "open"


def test_filter_kind_still_flips_via_active_label():
    # When vision actually flags the chip as selected, the filter flips —
    # the kind=filter restriction only blocks the page_text path.
    vr = _FakeVR(bboxes=[_FakeBBox(label="Oregon", is_selected=True)])
    b = _brief([
        {"label": "Oregon region", "kind": "filter",
         "predicate": {"vision_active_label": ["Oregon"]}},
    ])
    b.reconcile_from_page_state(vr, "")
    assert b.constraints[0].status == "done"


def test_reconcile_vision_active_label_only_matches_when_selected():
    # "Oregon" appears as a label but NOT as selected — should not flip
    # via vision_active_label predicate.
    vr = _FakeVR(bboxes=[_FakeBBox(label="Oregon", is_selected=False)])
    b = _brief([
        {"label": "Oregon active", "kind": "filter",
         "predicate": {"vision_active_label": ["Oregon"]}},
    ])
    b.reconcile_from_page_state(vr, "")
    assert b.constraints[0].status == "open"

    # When selected, it flips.
    vr2 = _FakeVR(bboxes=[_FakeBBox(label="Oregon", is_selected=True)])
    b.reconcile_from_page_state(vr2, "")
    assert b.constraints[0].status == "done"


def test_manual_predicate_never_auto_flips():
    b = _brief([
        {"label": "Extract top 3", "kind": "extraction",
         "predicate": {"manual": True, "page_text": ["top 3"]}},
    ])
    b.reconcile_from_url("https://x.com/results?top=3")
    b.reconcile_from_page_state(_FakeVR(), "Showing top 3 wines")
    assert b.constraints[0].status == "open"


def test_reconcile_does_not_reverse_done():
    b = _brief([
        {"label": "Oregon", "kind": "filter",
         "predicate": {"url_contains": ["oregon"]}},
    ])
    b.reconcile_from_url("https://x.com/oregon")
    assert b.constraints[0].status == "done"
    # Now the URL no longer contains 'oregon' — the item must STAY done.
    flipped = b.reconcile_from_url("https://x.com/california")
    assert flipped is False
    assert b.constraints[0].status == "done"


# --------------------------- focus & completion -------------------------


def test_next_focus_returns_first_open():
    b = _brief([
        {"label": "A", "predicate": {"url_contains": ["a"]}},
        {"label": "B", "predicate": {"url_contains": ["b"]}},
        {"label": "C", "predicate": {"url_contains": ["c"]}},
    ])
    b.reconcile_from_url("https://x.com/a")
    focus = b.next_focus()
    assert focus is not None
    assert focus.label == "B"


def test_next_focus_none_when_all_terminal():
    b = _brief([{"label": "Only"}])
    b.mark(1, "done", "manual")
    assert b.next_focus() is None
    assert b.is_complete() is True


def test_open_count_excludes_terminal_states():
    b = _brief([
        {"label": "A"}, {"label": "B"}, {"label": "C"},
    ])
    b.mark(1, "done")
    b.mark(2, "not_applicable")
    assert b.open_count() == 1


def test_mark_invalid_id_returns_false():
    b = _brief([{"label": "A"}])
    assert b.mark(99, "done") is False
    assert b.mark(1, "weird_status") is False


def test_mark_bumps_version_on_change_only():
    b = _brief([{"label": "A"}])
    v0 = b.version
    b.mark(1, "done")
    assert b.version == v0 + 1
    # Same status again — no bump.
    b.mark(1, "done")
    assert b.version == v0 + 1


# ------------------------------- rendering ------------------------------


def test_render_brief_shows_progress_and_focus():
    b = _brief([
        {"label": "White wine", "predicate": {"url_contains": ["white-wine"]}},
        {"label": "Oregon region", "predicate": {"url_contains": ["oregon"]}},
    ])
    b.reconcile_from_url("https://x.com/white-wine/")
    out = b.render_brief()
    assert "[BRIEF" in out
    assert "1/2" in out
    assert "oregon" in out  # slugified label of next focus


def test_render_focus_has_label_and_status():
    b = _brief([{"label": "Oregon region", "kind": "filter"}])
    out = b.render_focus()
    assert "[FOCUS]" in out
    assert "Oregon region" in out
    assert "filter" in out
    assert "open" in out


def test_render_focus_empty_when_complete():
    b = _brief([{"label": "Only"}])
    b.mark(1, "done")
    assert b.render_focus() == ""


def test_render_checklist_collapses_done_to_summary_line():
    # Done items get collapsed into a 'Done: #1' summary line; the
    # full label is hidden so the brain doesn't see it as a click
    # candidate. Open items still render in detail with status tags.
    b = _brief([
        {"label": "A", "predicate": {"url_contains": ["a"]}},
        {"label": "B"},
    ])
    b.reconcile_from_url("https://x.com/a")
    out = b.render_checklist()
    # Header shows new format with explicit counts.
    assert "1 done" in out and "1 remaining" in out
    # Done items collapsed.
    assert "Done: #1" in out
    assert "do not revisit" in out
    # The done item's label should NOT appear in detail.
    detail_section = out.split("Done:")[1] if "Done:" in out else out
    assert "1) A" not in detail_section
    # Active focus rendered with arrow + 'active' tag.
    assert "→" in out
    assert "2) B" in out
    assert "active — work on this" in out


def test_render_checklist_no_done_summary_when_zero_done():
    b = _brief([{"label": "A"}, {"label": "B"}])
    out = b.render_checklist()
    assert "Done:" not in out
    assert "1) A" in out
    assert "2) B" in out


def test_render_checklist_marks_failed_explicitly():
    b = _brief([{"label": "A"}, {"label": "B"}, {"label": "C"}])
    b.mark(2, "failed")
    out = b.render_checklist()
    # Failed items still rendered explicitly (may need retry).
    assert "2) B" in out
    assert "failed" in out


def test_render_checklist_truncates_long_lists():
    items = [{"label": f"item-{i}"} for i in range(20)]
    b = _brief(items)
    out = b.render_checklist(max_lines=5)
    # +15 more remaining-items not rendered.
    assert "+15 more" in out


def test_summary_open_items_excludes_done():
    b = _brief([
        {"label": "A"}, {"label": "B"}, {"label": "C"},
    ])
    b.mark(2, "done")
    summary = b.summary_open_items()
    assert "A" in summary
    assert "C" in summary
    assert "#2 B" not in summary


# ------------------------------ construction ----------------------------


def test_constructor_drops_items_without_label():
    b = TaskBrief("q", [
        {"label": "ok"},
        {"label": ""},
        {"kind": "filter"},  # no label
        "not a dict",
    ])
    assert len(b.constraints) == 1
    assert b.constraints[0].id == 1


def test_constructor_normalizes_unknown_kind():
    b = TaskBrief("q", [{"label": "x", "kind": "weird_kind"}])
    assert b.constraints[0].kind == "filter"


def test_empty_brief_is_complete():
    b = TaskBrief("q", [])
    assert b.is_complete() is True
    assert b.next_focus() is None
    assert b.open_count() == 0


def test_diagnostic_line_format():
    b = _brief([
        {"label": "A", "predicate": {"url_contains": ["a"]}},
        {"label": "B"},
    ])
    line = b.diagnostic_line()
    assert "[brief]" in line
    assert "open=2/2" in line
    assert "v=1" in line


# ---------------------------- heuristic decomposer -----------------------


from superbrowser_bridge.task_brief import heuristic_decompose, _looks_multi_condition


def test_heuristic_skips_simple_queries():
    assert _looks_multi_condition("show me x.com") is False
    assert _looks_multi_condition("login to dashboard") is False
    assert heuristic_decompose("login to dashboard") == []


def test_heuristic_decomposes_multi_filter_query():
    items = heuristic_decompose(
        "find white wines from oregon under $40 that pair with fish and dessert"
    )
    # We expect ≥3 items: at least region, price, and pairings get split out.
    assert len(items) >= 3
    labels = [i["label"].lower() for i in items]
    # Labels should mention the discriminative words.
    joined = " | ".join(labels)
    assert "oregon" in joined or "wine" in joined
    assert "40" in joined or "under" in joined


def test_heuristic_predicates_are_manual():
    items = heuristic_decompose(
        "find oregon wines under $40 that pair with fish and dessert"
    )
    assert items
    for it in items:
        assert it["predicate"] == {"manual": True}


def test_heuristic_kind_inference():
    items = heuristic_decompose(
        "find oregon wines under $40 sorted by score and return top 3"
    )
    kinds = {i["kind"] for i in items}
    # Should pick up at least one extraction (return) and one verification (sorted by).
    assert "extraction" in kinds or "verification" in kinds


def test_heuristic_caps_to_eight_items():
    q = ", ".join(["constraint number " + str(i) for i in range(20)])
    items = heuristic_decompose("filter by " + q)
    assert len(items) <= 8


def test_heuristic_drops_when_only_one_fragment_survives():
    # Query has a separator but only one fragment is meaningful.
    items = heuristic_decompose("a and b")  # both too short to keep
    assert items == []


# ---------------------- focus ↔ bbox label matching ---------------------


from superbrowser_bridge.task_brief import _label_match_score


def test_match_score_exact_substring():
    # 'Oregon' fully covers focus tokens minus 'region'; 1/2 coverage + boost
    score = _label_match_score("Oregon region", "Oregon")
    assert score >= 0.7


def test_match_score_zero_overlap():
    assert _label_match_score("Oregon region", "California") == 0.0


def test_match_score_partial_token_overlap():
    score = _label_match_score("Price under $40", "$40 max")
    # tokens: focus={"price","under","$40"} vs vbox={"$40","max"}
    # overlap = {"$40"} → coverage=1/3 ≈ 0.33; no substring boost → 0.33
    assert 0.30 <= score <= 0.40


def test_match_score_case_insensitive():
    s1 = _label_match_score("oregon REGION", "Oregon")
    s2 = _label_match_score("Oregon region", "OREGON")
    assert s1 == s2 and s1 > 0.5


def test_match_score_substring_boost():
    # "White Wine" contains all tokens of focus "White wine" → coverage=1.0
    # plus substring boost → 1.0 capped
    score = _label_match_score("white wine", "White Wine Selection")
    assert score >= 0.9


# ---------------------- recommend_bboxes ranking ------------------------


def test_recommend_bboxes_returns_top_k_sorted():
    b = TaskBrief("q", [
        {"label": "Oregon region", "kind": "filter", "predicate": {}},
    ])
    vr = _FakeVR(bboxes=[
        _FakeBBox(label="Sort by Score"),         # V1 — unrelated
        _FakeBBox(label="Region: California"),    # V2
        _FakeBBox(label="Oregon"),                # V3 — best match
        _FakeBBox(label="Add to Cart"),           # V4 — unrelated
        _FakeBBox(label="Filter: Region Oregon"), # V5 — also strong
    ])
    recs = b.recommend_bboxes(vr, top_k=3)
    # Top entry must be one of V3 or V5 (both contain 'Oregon').
    assert recs[0]["v_index"] in (3, 5)
    # Sorted descending by score.
    scores = [r["score"] for r in recs]
    assert scores == sorted(scores, reverse=True)


def test_recommend_bboxes_appends_v1_when_not_in_top_k():
    b = TaskBrief("q", [{"label": "Oregon region"}])
    vr = _FakeVR(bboxes=[
        _FakeBBox(label="Sort by Score"),  # V1 — unrelated
        _FakeBBox(label="Add to Cart"),    # V2
        _FakeBBox(label="Region: France"), # V3
        _FakeBBox(label="Oregon"),         # V4 — best
    ])
    recs = b.recommend_bboxes(vr, top_k=3)
    # V1 should appear in the result even though it's unrelated.
    v_indices = [r["v_index"] for r in recs]
    assert 1 in v_indices
    # And it should be flagged.
    v1 = next(r for r in recs if r["v_index"] == 1)
    assert v1["is_v1"] is True


def test_recommend_bboxes_vision_active_label_boost():
    # vision_active_label hints get a 1.5x boost. Compare scores of
    # the SAME bbox label with and without the predicate hint to
    # confirm the boost activates.
    b_with = TaskBrief("q", [{
        "label": "Oregon region",
        "kind": "filter",
        "predicate": {"vision_active_label": ["Oregon"]},
    }])
    b_without = TaskBrief("q", [{
        "label": "Oregon region",
        "kind": "filter",
        "predicate": {},
    }])
    vr = _FakeVR(bboxes=[_FakeBBox(label="Oregon")])
    rec_with = b_with.recommend_bboxes(vr, top_k=1)[0]
    rec_without = b_without.recommend_bboxes(vr, top_k=1)[0]
    # The hinted version should score strictly higher.
    assert rec_with["score"] > rec_without["score"]
    # And both should hit at least 0.5 since "Oregon" is a substring of focus.
    assert rec_with["score"] >= 0.9
    assert rec_without["score"] >= 0.5


def test_recommend_bboxes_empty_when_no_focus_or_bboxes():
    b = TaskBrief("q", [{"label": "x"}])
    b.mark(1, "done")
    vr = _FakeVR(bboxes=[_FakeBBox(label="anything")])
    assert b.recommend_bboxes(vr) == []
    # Reset, no bboxes
    b2 = TaskBrief("q", [{"label": "Oregon"}])
    assert b2.recommend_bboxes(_FakeVR(bboxes=[])) == []
    assert b2.recommend_bboxes(None) == []


def test_render_focus_bbox_shape():
    b = TaskBrief("q", [{"label": "Oregon region", "kind": "filter"}])
    vr = _FakeVR(bboxes=[
        _FakeBBox(label="Sort by Score"),
        _FakeBBox(label="Oregon"),
    ])
    out = b.render_focus_bbox(vr)
    assert "[FOCUS_BBOX]" in out
    assert "Oregon region" in out
    # Either V1 (unrelated) or V2 (match) — depends on tie-break;
    # the recommended line should mention Oregon as the chosen target.
    assert "→ recommended" in out
    assert "match" in out


def test_render_focus_bbox_empty_on_no_focus():
    b = TaskBrief("q", [])
    assert b.render_focus_bbox(_FakeVR(bboxes=[_FakeBBox(label="x")])) == ""
