"""Autocomplete reporting must never assert more than it saw.

The bug these guard, observed on booking.com: after typing, the harness
told the model

    [AUTOCOMPLETE_OPEN suggestions=?] A suggestion dropdown is open.
    Call browser_screenshot, then browser_click_at(vision_index=V_n)
    on the matching V_n.

in two situations where that was false.

  1. No dropdown at all. `detected` was
     `out.length > 0 || isAutocompleteInput || popupVisible`, and
     `isAutocompleteInput` is true for any field carrying role=combobox
     or an aria-controls — properties of the widget, not of its state.
     Reproduced with a listbox at display:none, zero options and
     aria-expanded=false. The model was ordered to click an item from a
     list that did not exist, so it produced one.

  2. The PREVIOUS query's dropdown. Typing "zzqqxxwwvv" into
     booking.com's destination field left five "New York" entries
     rendered and visible; the scan harvested them and offered them as
     matches. Nothing compared the items to the text just typed.

A third case was structural: the probe slept a flat 300ms, shorter than
a network round trip, so a widget still fetching read as empty.

Run:
    source venv/bin/activate && PYTHONPATH=nanobot python -m pytest \
        nanobot/superbrowser_bridge/tests/test_autocomplete_state.py -q
"""

from __future__ import annotations

import pytest

from superbrowser_bridge.session_tools.tools.input_text import _autocomplete_caption


def _scan(state, items=(), **kw):
    d = {
        "state": state,
        "suggestions": [{"text": t} for t in items],
        "elapsed_ms": kw.pop("elapsed_ms", 900),
        "is_autocomplete_input": kw.pop("is_autocomplete_input", True),
    }
    d.update(kw)
    return d


class TestNeverClaimsAnOpenDropdown:
    def test_empty_does_not_say_a_dropdown_is_open(self):
        cap = _autocomplete_caption(_scan("empty"), "new york")
        assert "dropdown is open" not in cap
        assert "AUTOCOMPLETE_EMPTY" in cap

    def test_empty_forbids_inventing_a_suggestion(self):
        cap = _autocomplete_caption(_scan("empty"), "new york")
        assert "Do NOT invent" in cap
        # and must not instruct a click on a nonexistent item
        assert "on the matching V_n" not in cap

    def test_empty_never_emits_a_question_mark_count(self):
        # "suggestions=?" was the tell: a count the probe did not have,
        # printed as though a list existed.
        cap = _autocomplete_caption(_scan("empty"), "new york")
        assert "suggestions=?" not in cap

    def test_pending_refuses_to_describe_the_turn(self):
        cap = _autocomplete_caption(_scan("pending", elapsed_ms=2500), "new york")
        assert "AUTOCOMPLETE_PENDING" in cap
        assert "still loading" in cap
        assert "Do NOT describe or click" in cap
        assert "2500ms" in cap


class TestUnrelatedSuggestions:
    def test_unrelated_is_not_reported_as_a_match(self):
        cap = _autocomplete_caption(
            _scan("unrelated", ["New York", "LaGuardia Airport"]), "zzqqxxwwvv",
        )
        assert "AUTOCOMPLETE_UNRELATED" in cap
        assert "none of its items match" in cap
        assert "earlier query" in cap
        assert "fuzzy fallback" in cap
        # The third cause is the one the user hit: results still in flight.
        assert "still on their way" in cap

    def test_unrelated_only_claims_the_list_held_still_when_it_did(self):
        # Swapping one stale list for another still counts as a change, so
        # the caption must not assert stillness it did not observe.
        held = _autocomplete_caption(_scan("unrelated", ["New York"], changed=False), "zzz")
        moved = _autocomplete_caption(_scan("unrelated", ["New York"], changed=True), "zzz")
        assert "did not change when you typed" in held
        assert "did not change when you typed" not in moved

    def test_unrelated_names_the_text_that_was_actually_typed(self):
        # The model has to be able to see the mismatch for itself.
        cap = _autocomplete_caption(_scan("unrelated", ["New York"]), "zzqqxxwwvv")
        assert '"zzqqxxwwvv"' in cap
        assert "New York" in cap

    def test_unrelated_forbids_clicking_on_the_assumption_of_a_match(self):
        cap = _autocomplete_caption(_scan("unrelated", ["New York"]), "zzqqxxwwvv")
        assert "do NOT click one" in cap
        assert "on the matching V_n" not in cap


class TestOpen:
    def test_open_keeps_the_working_instruction(self):
        cap = _autocomplete_caption(
            _scan("open", ["New York", "Newark"]), "new york",
        )
        assert "[AUTOCOMPLETE_OPEN suggestions=2]" in cap
        assert "A suggestion dropdown is open" in cap
        assert "browser_click_at(vision_index=V_n)" in cap

    def test_open_lists_what_is_on_screen(self):
        cap = _autocomplete_caption(_scan("open", ["New York", "Newark"]), "new york")
        assert "New York" in cap and "Newark" in cap


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


class TestGiveUp:
    """The loop this breaks: a worker spending 20+ iterations summoning a
    dropdown that was never going to appear.

    Observed on an Expedia package search — type, screenshot, eval,
    list_elements, get_rect, image_region, retype, fix_text_at,
    edit_text_at, wait_for, type again, screenshot again. Nothing in the
    system told it to stop: the caption kept explaining how to get a
    list, the form checklist was re-injected every iteration, and
    browser_form_commit refuses to submit while a field is pending.
    """

    def test_first_failure_still_suggests_recovery(self):
        cap = _autocomplete_caption(_scan("empty"), "New York", attempts=1)
        assert "AUTOCOMPLETE_GIVE_UP" not in cap

    def test_second_failure_releases_the_worker(self):
        cap = _autocomplete_caption(_scan("empty"), "New York", attempts=2)
        assert "AUTOCOMPLETE_GIVE_UP" in cap
        assert "STOP retyping" in cap

    def test_give_up_names_the_dead_ends_that_were_actually_tried(self):
        cap = _autocomplete_caption(_scan("unrelated", ["Paris"]), "New York", attempts=3)
        assert "eval/markdown/region crops" in cap

    def test_give_up_offers_a_concrete_way_forward(self):
        cap = _autocomplete_caption(_scan("empty"), "New York", attempts=2)
        assert "direct URL" in cap
        assert "as typed" in cap

    def test_give_up_pre_empts_the_form_checklist_nag(self):
        # The checklist is re-injected every iteration and would otherwise
        # contradict the release.
        cap = _autocomplete_caption(_scan("empty"), "New York", attempts=2)
        assert "checklist" in cap and "proceed anyway" in cap

    def test_an_open_list_never_gives_up(self):
        cap = _autocomplete_caption(_scan("open", ["New York"]), "New York", attempts=5)
        assert "AUTOCOMPLETE_GIVE_UP" not in cap
        assert "dropdown is open" in cap

    def test_pending_also_releases_once_it_is_hopeless(self):
        cap = _autocomplete_caption(_scan("pending"), "New York", attempts=2)
        assert "AUTOCOMPLETE_GIVE_UP" in cap


def _session():
    from superbrowser_bridge.form_session import FormFillSession
    return FormFillSession.begin(
        intent="search packages",
        started_at_turn=0,
        fields=[{"label": "Going to", "value": "San Francisco", "autocomplete": True}],
    )


class TestFormFieldRelease:
    def test_await_autocomplete_is_no_longer_a_dead_end(self):
        from superbrowser_bridge.form_session import FieldStatus
        sess = _session()
        sess.mark_typed(label_or_index="Going to", value_typed="San Francisco", turn=1)
        assert sess.fields["going to"].status is FieldStatus.AWAIT_AUTOCOMPLETE

        released = sess.mark_autocomplete_unavailable("Going to", observed_value="San Francisco")
        assert released is not None
        assert sess.fields["going to"].status is FieldStatus.FILLED
        assert sess.fields["going to"].autocomplete_unavailable is True
        assert sess.autocomplete_pending_for is None

    def test_a_released_field_leaves_the_pending_checklist(self):
        # The checklist is re-injected by the worker hook every iteration,
        # so a field that can never leave it is a permanent instruction to
        # keep working on it.
        from superbrowser_bridge.form_session import FieldStatus
        sess = _session()
        sess.mark_typed(label_or_index="Going to", value_typed="San Francisco", turn=1)
        # "[?]" is the await-autocomplete marker: an open demand.
        assert "[?]" in sess.remaining_checklist()
        assert "pending=" in sess.commit_summary()

        sess.mark_autocomplete_unavailable("Going to")
        assert "[?]" not in sess.remaining_checklist()
        assert "pending=" not in sess.commit_summary()
        assert sess.fields["going to"].status is FieldStatus.FILLED
