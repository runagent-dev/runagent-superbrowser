"""Unit tests for the vision-epoch freeze/resolve invariants.

The epoch pins "the V_n numbering the brain was last shown" so a
`browser_click_at(vision_index=V_n)` resolves against exactly that list —
not a newer background prefetch that renumbered it (the "brain clicks the
wrong box" drift). Two subtleties this guards:

  1. `_snapshot_vision_epoch` (used by the piggyback / freeze-on-show path
     in build_text_only) must NOT release the cross-tool same-element
     guard, while `freeze_vision_epoch` (a full screenshot re-observation)
     must — otherwise a mutating-tool reply would spuriously permit an
     immediate re-click of the just-clicked element.
  2. The turn-based age gate arithmetic that `browser_click_at` mirrors
     from `browser_type_at` (input_text.py).

Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_vision_epoch.py
"""

from __future__ import annotations

import sys

from superbrowser_bridge.session_tools.state import BrowserSessionState


def _state() -> BrowserSessionState:
    s = BrowserSessionState()
    s._last_vision_response = "RESP_A"
    s._last_vision_url = "https://ex.com/p"
    s.current_url = "https://ex.com/p"
    s._brain_turn_counter = 7
    return s


def test_snapshot_preserves_same_element_guard() -> None:
    s = _state()
    s.last_click_dom_index = 5
    s._snapshot_vision_epoch()
    assert s._vision_epoch_response == "RESP_A"
    assert s._vision_epoch_turn == 7
    # Piggyback freeze-on-show must NOT release the same-element guard.
    assert s.last_click_dom_index == 5
    print("✓ test_snapshot_preserves_same_element_guard")


def test_freeze_releases_same_element_guard() -> None:
    s = _state()
    s.last_click_dom_index = 5
    s.freeze_vision_epoch()
    assert s._vision_epoch_response == "RESP_A"
    # A full screenshot re-observation DOES release the guard.
    assert s.last_click_dom_index is None
    print("✓ test_freeze_releases_same_element_guard")


def test_resolution_frozen_on_same_url_live_on_nav() -> None:
    s = _state()
    s._snapshot_vision_epoch()
    # Same URL → resolve against the frozen epoch the brain last saw.
    assert s.vision_for_target_resolution() == "RESP_A"
    # Navigated away → epoch is stale, fall through to the live response.
    s._last_vision_response = "RESP_B_LIVE"
    s.current_url = "https://ex.com/other"
    assert s.vision_for_target_resolution() == "RESP_B_LIVE"
    print("✓ test_resolution_frozen_on_same_url_live_on_nav")


def test_click_age_gate_arithmetic() -> None:
    # Mirrors browser_click_at / browser_type_at: the turn counter is
    # incremented before resolution, so age = counter - 1 - epoch_turn.
    def age(counter: int, epoch_turn: int) -> int:
        return max(0, counter - 1 - epoch_turn)

    # Freeze at turn 7 → epoch_turn 7. First click (counter 8) is fresh.
    assert age(8, 7) == 0
    assert age(9, 7) == 1     # within default VISION_MAX_AGE_TURNS=1
    assert age(10, 7) == 2    # trips the gate at default max=1
    print("✓ test_click_age_gate_arithmetic")


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"✗ {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
