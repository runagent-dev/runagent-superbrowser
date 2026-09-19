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
