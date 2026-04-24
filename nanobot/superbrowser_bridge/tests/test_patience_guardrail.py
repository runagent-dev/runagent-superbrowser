"""Patience guardrail tests (ABC round B).

Warns the brain when it calls `browser_navigate` after making cursor
progress on the current URL — that pattern is almost always a give-
up rather than a planned transition.

Escalation: 1st premature nav → warn. 2nd consecutive → force-rewind
to the last checkpoint.

No network. Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_patience_guardrail.py
"""

from __future__ import annotations

import asyncio
import sys


class _Ctx:
    def __init__(self, iteration: int = 5) -> None:
        self.iteration = iteration
        self.messages: list[dict] = [{"role": "tool", "content": "ok"}]
        self.hook_state: dict = {}


def _make_state(checkpoint: str = "https://example.test/home"):
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.session_id = "t1"
    s.current_url = "https://example.test/home"
    s.best_checkpoint_url = checkpoint
    return s


def test_no_warning_on_plain_navigate() -> None:
    """A navigate call with no prior cursor successes on the previous
    URL is fine — the brain is just starting a new flow. No guidance
    should fire."""
    from superbrowser_bridge.worker_hook import BrowserWorkerHook
    s = _make_state()
    s.step_history.append({
        "tool": "browser_open", "args": "",
        "result": "opened session", "url": "",
    })
    s.step_history.append({
        "tool": "browser_navigate", "args": "",
        "result": "ok", "url": "https://example.test/",
    })
    hook = BrowserWorkerHook(s, max_iterations=50)
    ctx = _Ctx(iteration=2)
    asyncio.run(hook.after_iteration(ctx))
    sg = ctx.hook_state.get("structured_guidance") or []
    assert not any(
        "[PATIENCE]" in getattr(g, "text", "") for g in sg
    ), f"unexpected PATIENCE warning: {[g.text for g in sg]}"
    print("✓ test_no_warning_on_plain_navigate")


def test_warn_on_navigate_after_cursor_progress() -> None:
    """Brain had 2 cursor_success tags on URL X, then navigated away.
    That's a premature pivot — emit a warn-severity PATIENCE guidance."""
    from superbrowser_bridge.worker_hook import BrowserWorkerHook
    s = _make_state()
    # Simulate two successful cursor actions on /home
    s.step_history.append({
        "tool": "browser_type", "args": "SFMOMA",
        "result": "[cursor_success:browser_type] Typed SFMOMA",
        "url": "https://example.test/home",
    })
    hook = BrowserWorkerHook(s, max_iterations=50)
    asyncio.run(hook.after_iteration(_Ctx(iteration=1)))  # bumps counter
    s.step_history.append({
        "tool": "browser_click_at", "args": "V2",
        "result": "[cursor_success:browser_click_at] Clicked suggestion",
        "url": "https://example.test/home",
    })
    asyncio.run(hook.after_iteration(_Ctx(iteration=2)))  # bumps counter
    # Now navigate away
    s.step_history.append({
        "tool": "browser_navigate", "args": "",
        "result": "navigated",
        "url": "https://example.test/other",
    })
    ctx3 = _Ctx(iteration=3)
    asyncio.run(hook.after_iteration(ctx3))
    sg = ctx3.hook_state.get("structured_guidance") or []
    patience = [g for g in sg if "[PATIENCE]" in getattr(g, "text", "")]
    assert patience, f"expected PATIENCE guidance, got {[g.text for g in sg]}"
    assert patience[0].severity == "warn"
    # Must NOT force yet — this is the first premature nav
    assert getattr(s, "_forced_next_tool", None) in (None, "")
    print("✓ test_warn_on_navigate_after_cursor_progress")


def test_force_rewind_on_second_premature_nav() -> None:
    """After a warn, if the brain navigates away AGAIN from another
    URL where it had progress, promote to force-rewind."""
    from superbrowser_bridge.worker_hook import BrowserWorkerHook
    s = _make_state(checkpoint="https://example.test/checkpoint")
    # Cycle 1: progress on /a, then nav away
    for _ in range(2):
        s.step_history.append({
            "tool": "browser_click", "args": "",
            "result": "[cursor_success:browser_click] ok",
            "url": "https://example.test/a",
        })
    hook = BrowserWorkerHook(s, max_iterations=50)
    # Drain cursor_success counters
    for i in range(len(s.step_history)):
        asyncio.run(hook.after_iteration(_Ctx(iteration=i)))
    s.step_history.append({
        "tool": "browser_navigate", "args": "",
        "result": "nav away",
        "url": "https://example.test/b",
    })
    asyncio.run(hook.after_iteration(_Ctx(iteration=10)))
    # Cycle 2: progress on /b, then nav away again
    for _ in range(2):
        s.step_history.append({
            "tool": "browser_type", "args": "",
            "result": "[cursor_success:browser_type] ok",
            "url": "https://example.test/b",
        })
    for i in range(11, 13):
        asyncio.run(hook.after_iteration(_Ctx(iteration=i)))
    s.step_history.append({
        "tool": "browser_navigate", "args": "",
        "result": "nav away again",
        "url": "https://example.test/c",
    })
    ctx_final = _Ctx(iteration=20)
    asyncio.run(hook.after_iteration(ctx_final))
    sg = ctx_final.hook_state.get("structured_guidance") or []
    forced = [g for g in sg if getattr(g, "severity", "") == "force"]
    assert forced, f"expected force after 2 premature navs, got {sg}"
    assert any(
        g.next_tool == "browser_rewind_to_checkpoint" for g in forced
    ), forced
    assert s._forced_next_tool == "browser_rewind_to_checkpoint"
    print("✓ test_force_rewind_on_second_premature_nav")


def test_same_url_nav_is_not_flagged() -> None:
    """browser_navigate to the SAME URL (refresh) is not a give-up
    pivot — don't flag it."""
    from superbrowser_bridge.worker_hook import BrowserWorkerHook
    s = _make_state()
    s.step_history.append({
        "tool": "browser_click", "args": "",
        "result": "[cursor_success:browser_click] ok",
        "url": "https://example.test/home",
    })
    hook = BrowserWorkerHook(s, max_iterations=50)
    asyncio.run(hook.after_iteration(_Ctx(iteration=1)))
    s.step_history.append({
        "tool": "browser_click", "args": "",
        "result": "[cursor_success:browser_click] ok",
        "url": "https://example.test/home",
    })
    asyncio.run(hook.after_iteration(_Ctx(iteration=2)))
    s.step_history.append({
        "tool": "browser_navigate", "args": "",
        "result": "refreshed",
        "url": "https://example.test/home",  # SAME URL
    })
    ctx = _Ctx(iteration=3)
    asyncio.run(hook.after_iteration(ctx))
    sg = ctx.hook_state.get("structured_guidance") or []
    assert not any("[PATIENCE]" in getattr(g, "text", "") for g in sg)
    print("✓ test_same_url_nav_is_not_flagged")


def main() -> int:
    tests = [
        test_no_warning_on_plain_navigate,
        test_warn_on_navigate_after_cursor_progress,
        test_force_rewind_on_second_premature_nav,
        test_same_url_nav_is_not_flagged,
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
