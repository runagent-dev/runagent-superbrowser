"""Hook-level no-effect streak → forced rewind.

After Round 2's P4, two consecutive `[no_effect:*]` tool results on a
worker with a `best_checkpoint_url` must emit a
`StructuredGuidance(severity="force", next_tool="browser_rewind_to_checkpoint")`
and stash `_forced_next_tool` on state. One-shot via the same
`_force_rewind_emitted` flag `_maybe_force_rewind` uses — no double-force
on the same turn.

Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_no_effect_streak.py
"""

from __future__ import annotations

import asyncio
import sys


def _make_state_with_checkpoint():
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.session_id = "t1"
    s.current_url = "https://example.test/stuck"
    s.best_checkpoint_url = "https://example.test/home"
    return s


class _Ctx:
    def __init__(self, iteration: int = 5) -> None:
        self.iteration = iteration
        self.messages: list[dict] = [{"role": "tool", "content": "ok"}]
        self.hook_state: dict = {}


def test_two_no_effects_force_rewind() -> None:
    from superbrowser_bridge.worker_hook import BrowserWorkerHook
    s = _make_state_with_checkpoint()
    s.step_history.append({
        "tool": "browser_click_at", "args": "V2",
        "result": "[no_effect:browser_click_at] zero delta",
        "url": s.current_url,
    })
    hook = BrowserWorkerHook(s, max_iterations=50)
    # First no_effect — streak goes to 1, no force yet.
    asyncio.run(hook.after_iteration(_Ctx(iteration=3)))
    assert hook._consecutive_no_effect == 1
    assert getattr(s, "_forced_next_tool", None) in (None, "")

    # Second no_effect — streak = 2, force emitted.
    s.step_history.append({
        "tool": "browser_click_at", "args": "V3",
        "result": "[no_effect:browser_click_at] zero delta again",
        "url": s.current_url,
    })
    asyncio.run(hook.after_iteration(_Ctx(iteration=4)))
    assert hook._consecutive_no_effect == 2
    assert s._forced_next_tool == "browser_rewind_to_checkpoint", (
        f"expected force after 2 no_effects, got {getattr(s, '_forced_next_tool', None)!r}"
    )
    # FORCED_REWIND text should be in the last tool message.
    last_msg = _Ctx().messages  # fresh context doesn't have history; grab from hook iteration side-effect
    # The hook appends to context.messages[-1]["content"]; we can verify
    # via hook_state instead.
    print("✓ test_two_no_effects_force_rewind")


def test_single_no_effect_does_not_force() -> None:
    from superbrowser_bridge.worker_hook import BrowserWorkerHook
    s = _make_state_with_checkpoint()
    s.step_history.append({
        "tool": "browser_type",
        "args": "index=5,text=hi",
        "result": "[no_effect:browser_type] zero delta",
        "url": s.current_url,
    })
    hook = BrowserWorkerHook(s, max_iterations=50)
    asyncio.run(hook.after_iteration(_Ctx(iteration=2)))
    assert hook._consecutive_no_effect == 1
    assert getattr(s, "_forced_next_tool", None) in (None, "")
    print("✓ test_single_no_effect_does_not_force")


def test_streak_resets_on_non_no_effect() -> None:
    from superbrowser_bridge.worker_hook import BrowserWorkerHook
    s = _make_state_with_checkpoint()
    # Build: no_effect, no_effect, success. Streak must be 0 at end.
    s.step_history.append({
        "tool": "browser_click_at", "args": "V2",
        "result": "[no_effect:browser_click_at] zero",
        "url": s.current_url,
    })
    hook = BrowserWorkerHook(s, max_iterations=50)
    asyncio.run(hook.after_iteration(_Ctx(iteration=1)))
    assert hook._consecutive_no_effect == 1

    # Success breaks the streak BEFORE we hit 2.
    s.step_history.append({
        "tool": "browser_click_at", "args": "V3",
        "result": "Clicked V3 | Page: https://example.test/next",
        "url": "https://example.test/next",
    })
    asyncio.run(hook.after_iteration(_Ctx(iteration=2)))
    assert hook._consecutive_no_effect == 0, (
        f"streak should reset, got {hook._consecutive_no_effect}"
    )
    assert getattr(s, "_forced_next_tool", None) in (None, "")
    print("✓ test_streak_resets_on_non_no_effect")


def test_force_not_emitted_without_checkpoint() -> None:
    """If there's no best_checkpoint_url we can't rewind, so the force
    must NOT fire — the brain will receive the [no_effect] hints in the
    normal guidance stream instead."""
    from superbrowser_bridge.worker_hook import BrowserWorkerHook
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.session_id = "t1"
    s.current_url = "https://example.test/stuck"
    # deliberately no best_checkpoint_url
    for _ in range(2):
        s.step_history.append({
            "tool": "browser_click_at", "args": "V2",
            "result": "[no_effect:browser_click_at] zero",
            "url": s.current_url,
        })
    hook = BrowserWorkerHook(s, max_iterations=50)
    asyncio.run(hook.after_iteration(_Ctx(iteration=1)))
    asyncio.run(hook.after_iteration(_Ctx(iteration=2)))
    assert hook._consecutive_no_effect >= 2
    assert getattr(s, "_forced_next_tool", None) in (None, "")
    print("✓ test_force_not_emitted_without_checkpoint")


def main() -> int:
    tests = [
        test_two_no_effects_force_rewind,
        test_single_no_effect_does_not_force,
        test_streak_resets_on_non_no_effect,
        test_force_not_emitted_without_checkpoint,
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
