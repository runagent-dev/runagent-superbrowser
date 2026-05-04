"""Unit tests for Arch v4 Phase K: click_at auto-resolves vision_index
by target_label.

When the brain calls browser_click_at(vision_index=N, target_label="X")
and V_N's label doesn't match "X" but V_M does, the bridge silently
remaps to V_M instead of returning a [click_at_label_mismatch] wall-
of-labels refusal. Makes target_label the source of truth and
vision_index a tiebreaker hint.

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_click_at_auto_remap
"""

from __future__ import annotations

import asyncio
import os
import sys


class _Bbox:
    """Tiny stand-in for vision_agent.schemas.BBox."""
    def __init__(self, label: str):
        self.label = label


def _state_with_bboxes(labels: list[str]):
    """Return a BrowserSessionState whose _last_vision_response has the
    given V_n labels."""
    from superbrowser_bridge.session_tools import BrowserSessionState

    s = BrowserSessionState()
    s.session_id = "sid"

    class _Vresp:
        def __init__(self, lbls):
            self.bboxes = [_Bbox(l) for l in lbls]
        def get_bbox(self, idx):
            i = int(idx) - 1
            if 0 <= i < len(self.bboxes):
                return self.bboxes[i]
            return None

    s._last_vision_response = _Vresp(labels)
    return s


# ── Helper: _resolve_vision_index_by_label ──────────────────────────


def test_remap_when_vN_label_mismatches_but_vM_matches() -> None:
    """Arch v4 (Step 3 follow-up): auto-remap is now off by default.
    Real-run traces showed it silently rewriting the brain's V_n to a
    fuzzy-label-matched V_M that turned out to be the wrong target
    (e.g. a product card heading instead of a filter chip), producing
    "lands on a single bottle instead of filtering" failures. With
    auto-remap off the function returns the original V_n unchanged;
    the click tool's existing label-mismatch refusal forces the brain
    to re-evaluate. Test verifies the new default behavior; flipping
    CLICK_AT_AUTO_REMAP=1 still re-enables the legacy remap (covered
    by `test_remap_kill_switch_via_env` below)."""
    import os
    from superbrowser_bridge.session_tools import (
        _resolve_vision_index_by_label,
    )
    s = _state_with_bboxes(["Cart", "Shop", "Sign in", "Search", "White"])
    new_idx, note = _resolve_vision_index_by_label(
        s, vision_index=1, target_label="White",
        bboxes=s._last_vision_response.bboxes,
    )
    # Default-off: brain's vision_index is preserved.
    assert new_idx == 1
    assert note is None
    assert s._click_at_remap_count == 0
    # Opt-in path still works.
    os.environ["CLICK_AT_AUTO_REMAP"] = "1"
    try:
        new_idx2, note2 = _resolve_vision_index_by_label(
            s, vision_index=1, target_label="White",
            bboxes=s._last_vision_response.bboxes,
        )
    finally:
        del os.environ["CLICK_AT_AUTO_REMAP"]
    assert new_idx2 == 5
    assert note2 is not None
    assert "click_at_remap V_1→V_5" in note2


def test_no_remap_when_vN_already_matches() -> None:
    from superbrowser_bridge.session_tools import (
        _resolve_vision_index_by_label,
    )
    s = _state_with_bboxes(["White", "Shop", "Cart"])
    new_idx, note = _resolve_vision_index_by_label(
        s, vision_index=1, target_label="White",
        bboxes=s._last_vision_response.bboxes,
    )
    assert new_idx == 1
    assert note is None
    assert s._click_at_remap_count == 0


def test_no_remap_when_no_vM_matches() -> None:
    """Brain's target_label doesn't match anything in the bbox list →
    fall through to existing refusal (return original index, no note)."""
    from superbrowser_bridge.session_tools import (
        _resolve_vision_index_by_label,
    )
    s = _state_with_bboxes(["Cart", "Shop", "Sign in"])
    new_idx, note = _resolve_vision_index_by_label(
        s, vision_index=1, target_label="WhiteWine",
        bboxes=s._last_vision_response.bboxes,
    )
    assert new_idx == 1
    assert note is None
    assert s._click_at_remap_count == 0


def test_no_remap_when_target_label_empty() -> None:
    from superbrowser_bridge.session_tools import (
        _resolve_vision_index_by_label,
    )
    s = _state_with_bboxes(["Cart", "White"])
    for empty in (None, "", "   "):
        s._click_at_remap_count = 0
        new_idx, note = _resolve_vision_index_by_label(
            s, vision_index=1, target_label=empty,
            bboxes=s._last_vision_response.bboxes,
        )
        assert new_idx == 1
        assert note is None


def test_no_remap_when_bboxes_empty() -> None:
    from superbrowser_bridge.session_tools import (
        _resolve_vision_index_by_label,
    )
    s = _state_with_bboxes([])
    new_idx, note = _resolve_vision_index_by_label(
        s, vision_index=1, target_label="White", bboxes=[],
    )
    assert new_idx == 1
    assert note is None


def test_kill_switch_disables_remap() -> None:
    from superbrowser_bridge.session_tools import (
        _resolve_vision_index_by_label,
    )
    s = _state_with_bboxes(["Cart", "White"])
    os.environ["CLICK_AT_AUTO_REMAP"] = "0"
    try:
        new_idx, note = _resolve_vision_index_by_label(
            s, vision_index=1, target_label="White",
            bboxes=s._last_vision_response.bboxes,
        )
        assert new_idx == 1
        assert note is None
    finally:
        del os.environ["CLICK_AT_AUTO_REMAP"]


def test_remap_cap_honored() -> None:
    """Once _click_at_remap_count hits CLICK_AT_REMAP_MAX, no further
    remaps fire — the brain has to start using target_label correctly."""
    from superbrowser_bridge.session_tools import (
        _resolve_vision_index_by_label,
    )
    s = _state_with_bboxes(["Cart", "White"])
    s._click_at_remap_count = 3  # at default cap
    new_idx, note = _resolve_vision_index_by_label(
        s, vision_index=1, target_label="White",
        bboxes=s._last_vision_response.bboxes,
    )
    assert new_idx == 1
    assert note is None


def test_remap_cap_via_env() -> None:
    """The CLICK_AT_REMAP_MAX cap still works once remap is opted into
    via CLICK_AT_AUTO_REMAP=1 (Arch v4: now opt-in not opt-out)."""
    from superbrowser_bridge.session_tools import (
        _resolve_vision_index_by_label,
    )
    s = _state_with_bboxes(["Cart", "White"])
    os.environ["CLICK_AT_REMAP_MAX"] = "1"
    os.environ["CLICK_AT_AUTO_REMAP"] = "1"
    try:
        # First remap: allowed.
        new_idx, note = _resolve_vision_index_by_label(
            s, vision_index=1, target_label="White",
            bboxes=s._last_vision_response.bboxes,
        )
        assert new_idx == 2 and note is not None
        # Second remap: blocked by cap.
        s._click_at_remap_count = 1  # already at cap
        new_idx, note = _resolve_vision_index_by_label(
            s, vision_index=1, target_label="White",
            bboxes=s._last_vision_response.bboxes,
        )
        assert new_idx == 1 and note is None
    finally:
        del os.environ["CLICK_AT_REMAP_MAX"]
        del os.environ["CLICK_AT_AUTO_REMAP"]


# ── Worker hook nudge ───────────────────────────────────────────────


def test_worker_hook_emits_remapped_nudge() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.step_history.append({
        "tool": "browser_click_at",
        "args": "V_1, White",
        "result": (
            "[click_at_remap V_1→V_5 reason=\"target_label='White' "
            "matched V_5 label 'White wine', not V_1\"]\n"
            "Clicked V5(...) → bbox=(...)"
        ),
        "url": "x", "time": "12:00:00",
    })
    hook = BrowserWorkerHook(s)
    ctx = type(
        "Ctx", (),
        {"iteration": 1, "messages": [{"role": "tool", "content": "x"}]},
    )()
    asyncio.run(hook.after_iteration(ctx))
    msg = ctx.messages[-1]["content"]
    assert "[CLICK_AT_REMAPPED]" in msg
    assert "tiebreaker" in msg


def test_worker_hook_remap_nudge_fires_once_then_resets() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.step_history.append({
        "tool": "browser_click_at",
        "args": "x",
        "result": "[click_at_remap V_1→V_5 reason=...]\nClicked V5",
        "url": "x", "time": "12:00:00",
    })
    hook = BrowserWorkerHook(s)
    ctx1 = type(
        "Ctx", (),
        {"iteration": 1, "messages": [{"role": "tool", "content": "x"}]},
    )()
    asyncio.run(hook.after_iteration(ctx1))
    assert "[CLICK_AT_REMAPPED]" in ctx1.messages[-1]["content"]
    # Second iteration with the same step → nudge does NOT re-fire.
    ctx2 = type(
        "Ctx", (),
        {"iteration": 2, "messages": [{"role": "tool", "content": "x"}]},
    )()
    asyncio.run(hook.after_iteration(ctx2))
    assert "[CLICK_AT_REMAPPED]" not in ctx2.messages[-1]["content"]


def test_worker_hook_remap_nudge_kill_switch() -> None:
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.worker_hook import BrowserWorkerHook

    s = BrowserSessionState()
    s.session_id = "sid"
    s.step_history.append({
        "tool": "browser_click_at",
        "args": "x", "result": "[click_at_remap ...]",
        "url": "x", "time": "12:00:00",
    })
    hook = BrowserWorkerHook(s)
    os.environ["CLICK_AT_REMAP_NUDGE"] = "0"
    try:
        ctx = type(
            "Ctx", (),
            {"iteration": 1, "messages": [{"role": "tool", "content": "x"}]},
        )()
        asyncio.run(hook.after_iteration(ctx))
        assert "[CLICK_AT_REMAPPED]" not in ctx.messages[-1]["content"]
    finally:
        del os.environ["CLICK_AT_REMAP_NUDGE"]


def main() -> int:
    tests = [
        # helper
        test_remap_when_vN_label_mismatches_but_vM_matches,
        test_no_remap_when_vN_already_matches,
        test_no_remap_when_no_vM_matches,
        test_no_remap_when_target_label_empty,
        test_no_remap_when_bboxes_empty,
        test_kill_switch_disables_remap,
        test_remap_cap_honored,
        test_remap_cap_via_env,
        # worker hook nudge
        test_worker_hook_emits_remapped_nudge,
        test_worker_hook_remap_nudge_fires_once_then_resets,
        test_worker_hook_remap_nudge_kill_switch,
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
