"""R4 tests — cursor-first bias.

Covers:
  * R4.4 — cursor-success tag on had_effect=True
  * R4.3 — script-usage warning when scripts dominate + vision has
    clickable bboxes
  * R4.2 — blocker auto-dismiss guidance (warn → force escalation)

No network. Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_cursor_bias.py
"""

from __future__ import annotations

import asyncio
import sys


# ---------------------------------------------------------- R4.4


def test_cursor_success_tag_on_effect() -> None:
    from superbrowser_bridge.session_tools import _maybe_no_effect_prefix
    data = {"effect": {"url_changed": False, "mutation_delta": 3, "focused_changed": False}}
    out = _maybe_no_effect_prefix(data, "browser_click_at", "Clicked V2")
    assert out.startswith("[cursor_success:browser_click_at]"), out
    assert "Clicked V2" in out
    print("✓ test_cursor_success_tag_on_effect")


def test_cursor_success_tag_only_for_cursor_tools() -> None:
    """Non-cursor tool wins (had_effect=True) should NOT get the
    cursor_success tag — the tag is specifically positive reinforcement
    for cursor choices, not scripts."""
    from superbrowser_bridge.session_tools import _maybe_no_effect_prefix
    data = {"effect": {"url_changed": True, "mutation_delta": 0, "focused_changed": False}}
    out = _maybe_no_effect_prefix(data, "browser_run_script", "ran script ok")
    assert "cursor_success" not in out, out
    print("✓ test_cursor_success_tag_only_for_cursor_tools")


def test_cursor_success_resets_script_streak() -> None:
    """A successful cursor action must reset consecutive_script_calls
    so the brain's occasional script + several cursor turns don't
    accumulate into a bogus `[script_warning]`."""
    from superbrowser_bridge.session_tools import (
        _maybe_no_effect_prefix, BrowserSessionState,
    )
    s = BrowserSessionState()
    s.consecutive_script_calls = 4
    data = {"effect": {"mutation_delta": 2, "url_changed": False, "focused_changed": False}}
    _maybe_no_effect_prefix(data, "browser_semantic_click", "Clicked Accept", session_state=s)
    assert s.consecutive_script_calls == 0
    print("✓ test_cursor_success_resets_script_streak")


# ---------------------------------------------------------- R4.3


def test_script_warning_skipped_below_threshold() -> None:
    from superbrowser_bridge.session_tools import (
        _maybe_script_usage_warning, BrowserSessionState,
    )
    s = BrowserSessionState()
    s.consecutive_script_calls = 2
    assert _maybe_script_usage_warning(s) == ""
    print("✓ test_script_warning_skipped_below_threshold")


def test_script_warning_skipped_without_clickable_bboxes() -> None:
    from superbrowser_bridge.session_tools import (
        _maybe_script_usage_warning, BrowserSessionState,
    )
    s = BrowserSessionState()
    s.consecutive_script_calls = 5
    # no vision response at all
    assert _maybe_script_usage_warning(s) == ""

    # vision response but no clickable bboxes
    class _B:
        def __init__(self, label, clickable=False):
            self.label = label
            self.clickable = clickable
    class _R:
        def __init__(self): self.bboxes = [_B("hero heading", clickable=False)]
    s._last_vision_response = _R()
    assert _maybe_script_usage_warning(s) == ""
    print("✓ test_script_warning_skipped_without_clickable_bboxes")


def test_script_warning_fires_with_alternatives() -> None:
    from superbrowser_bridge.session_tools import (
        _maybe_script_usage_warning, BrowserSessionState,
    )
    s = BrowserSessionState()
    s.consecutive_script_calls = 4

    class _B:
        def __init__(self, label, clickable=True):
            self.label = label
            self.clickable = clickable
    class _R:
        def __init__(self):
            self.bboxes = [
                _B("Accept cookies"),
                _B("Close"),
                _B("Continue Anyway"),
                _B("Learn more"),  # only top 3 should show
            ]
    s._last_vision_response = _R()
    w = _maybe_script_usage_warning(s)
    assert w.startswith("[script_warning]"), w
    assert "'Accept cookies'" in w
    assert "'Close'" in w
    assert "'Continue Anyway'" in w
    assert "browser_semantic_click" in w
    # Only top 3 — 'Learn more' shouldn't be in the rendered list
    assert "'Learn more'" not in w
    print("✓ test_script_warning_fires_with_alternatives")


# ---------------------------------------------------------- R4.2


def _make_hook_state_with_blocker(dismiss_hint: str = "Continue Anyway"):
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.session_id = "t1"
    s.current_url = "https://example.test/"
    # Fake vision response with an active blocker layer
    class _Layer:
        def __init__(self, id_, hint): self.id = id_; self.dismiss_hint = hint
    class _Scene:
        def __init__(self, blocker_id, layers):
            self.active_blocker_layer_id = blocker_id
            self.layers = layers
    class _Resp:
        def __init__(self, scene): self.scene = scene; self.bboxes = []
    layers = [_Layer("L1_country_modal", dismiss_hint)]
    s._last_vision_response = _Resp(_Scene("L1_country_modal", layers))
    return s


class _Ctx:
    def __init__(self, iteration: int = 5) -> None:
        self.iteration = iteration
        self.messages: list[dict] = [{"role": "tool", "content": "ok"}]
        self.hook_state: dict = {}


def test_blocker_emits_warn_then_force_escalation() -> None:
    """Proactive guidance: turn 1 already forces the dismiss. This test
    used to codify a warn→force escalation that gave the brain one free
    turn to route around a wall; that was exactly the hallucination
    pattern the SpotHero run exposed, so force fires immediately now."""
    from superbrowser_bridge.worker_hook import BrowserWorkerHook
    s = _make_hook_state_with_blocker("Continue Anyway")
    s.step_history.append({
        "tool": "browser_screenshot", "args": "",
        "result": "saw modal", "url": s.current_url,
    })
    hook = BrowserWorkerHook(s, max_iterations=50)

    # Turn 1: force immediately — brain doesn't get a "try something
    # else first" window.
    ctx1 = _Ctx(iteration=3)
    asyncio.run(hook.after_iteration(ctx1))
    sg1 = ctx1.hook_state.get("structured_guidance") or []
    blocker1 = [g for g in sg1 if getattr(g, "kind", "") == "blocker"]
    assert blocker1, "expected blocker guidance on turn 1"
    assert blocker1[-1].severity == "force", blocker1[-1].severity
    assert blocker1[-1].next_tool == "browser_semantic_click"
    assert blocker1[-1].param_override == {"target": "Continue Anyway"}

    # Brain ignored the force, ran a script instead — next turn should
    # still force (same hint persists).
    s.step_history.append({
        "tool": "browser_run_script", "args": "",
        "result": "ran script",
        "url": s.current_url,
    })
    ctx2 = _Ctx(iteration=4)
    asyncio.run(hook.after_iteration(ctx2))
    sg2 = ctx2.hook_state.get("structured_guidance") or []
    blocker2 = [g for g in sg2 if getattr(g, "kind", "") == "blocker"]
    assert blocker2, "expected blocker guidance on turn 2"
    assert blocker2[-1].severity == "force"
    print("✓ test_blocker_emits_warn_then_force_escalation")


def test_blocker_resets_when_brain_uses_cursor() -> None:
    """If the brain calls semantic_click/click_at, the blocker streak
    resets — we gave guidance, they responded, we don't keep shouting."""
    from superbrowser_bridge.worker_hook import BrowserWorkerHook
    s = _make_hook_state_with_blocker("Close")
    # First turn: a screenshot, blocker guidance fires at warn.
    s.step_history.append({
        "tool": "browser_screenshot", "args": "",
        "result": "ok", "url": s.current_url,
    })
    hook = BrowserWorkerHook(s, max_iterations=50)
    asyncio.run(hook.after_iteration(_Ctx(iteration=1)))
    assert hook._blocker_nudge_count == 1

    # Second turn: brain called semantic_click → streak resets.
    s.step_history.append({
        "tool": "browser_semantic_click", "args": "target=Close",
        "result": "[cursor_success:browser_semantic_click] Clicked 'Close'",
        "url": s.current_url,
    })
    asyncio.run(hook.after_iteration(_Ctx(iteration=2)))
    assert hook._blocker_nudge_count == 0
    print("✓ test_blocker_resets_when_brain_uses_cursor")


def test_blocker_guidance_skipped_without_hint() -> None:
    """If vision doesn't emit a dismiss_hint, we can't auto-guide —
    surface nothing rather than a generic 'dismiss the modal somehow'
    that would be less actionable than the vision caption already is."""
    from superbrowser_bridge.worker_hook import BrowserWorkerHook
    s = _make_hook_state_with_blocker("")  # no hint
    s.step_history.append({
        "tool": "browser_screenshot", "args": "",
        "result": "ok", "url": s.current_url,
    })
    hook = BrowserWorkerHook(s, max_iterations=50)
    ctx = _Ctx(iteration=3)
    asyncio.run(hook.after_iteration(ctx))
    sg = ctx.hook_state.get("structured_guidance") or []
    assert not any(getattr(g, "kind", "") == "blocker" for g in sg)
    print("✓ test_blocker_guidance_skipped_without_hint")


def main() -> int:
    tests = [
        test_cursor_success_tag_on_effect,
        test_cursor_success_tag_only_for_cursor_tools,
        test_cursor_success_resets_script_streak,
        test_script_warning_skipped_below_threshold,
        test_script_warning_skipped_without_clickable_bboxes,
        test_script_warning_fires_with_alternatives,
        test_blocker_emits_warn_then_force_escalation,
        test_blocker_resets_when_brain_uses_cursor,
        test_blocker_guidance_skipped_without_hint,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"✗ {t.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"✗ {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
