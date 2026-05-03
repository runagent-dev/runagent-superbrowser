"""Unit tests for the cross-worker state handoff store.

Verifies:
  • save → take roundtrip preserves the fields the next worker needs
    to resume on the same browser session (session_id, current_url,
    task_plan, cursor_failure_strategies, observed_anchor_urls).
  • take is one-shot — a second take(same_key) returns None.
  • peek does NOT consume the entry.
  • Per-key isolation — saves for different task_ids don't collide.
  • TTL — entries older than _MAX_AGE_S are dropped.

No external services required. Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_handoff_store
"""

from __future__ import annotations

import sys
import time


def _fresh_state(session_id: str = "session-x", url: str = ""):
    from superbrowser_bridge.session_tools import BrowserSessionState
    s = BrowserSessionState()
    s.session_id = session_id
    s.current_url = url or "https://test.example/page"
    s.pinned_domain = "test.example"
    s.task_instruction = "do the thing"
    s.cursor_failure_strategies = {"click_at"}
    s.observed_anchor_urls = {"/a", "/b"}
    return s


def test_save_take_roundtrip() -> None:
    from superbrowser_bridge.handoff_store import save, take, clear
    from superbrowser_bridge.task_plan import make_plan

    clear()
    s = _fresh_state(
        session_id="session-fish",
        url="https://wineaccess.com/?food_pairings=fish%2Csweets",
    )
    s.task_plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.active_step()  # mark first as in_progress

    save("task-key-1", s)
    h = take("task-key-1")

    assert h is not None
    assert h.session_id == "session-fish"
    assert "food_pairings=fish" in h.current_url
    assert h.task_plan is s.task_plan
    assert h.task_plan.steps[0].status == "in_progress"
    assert "click_at" in h.cursor_failure_strategies
    assert "/a" in h.observed_anchor_urls


def test_take_is_one_shot() -> None:
    from superbrowser_bridge.handoff_store import save, take, clear

    clear()
    save("k", _fresh_state())
    assert take("k") is not None
    # Second take must return None — the entry was popped.
    assert take("k") is None


def test_peek_does_not_consume() -> None:
    from superbrowser_bridge.handoff_store import save, peek, take, clear

    clear()
    save("k", _fresh_state())
    p1 = peek("k")
    p2 = peek("k")
    t = take("k")
    assert p1 is not None
    assert p2 is not None
    assert t is not None
    assert take("k") is None  # take consumed it


def test_per_key_isolation() -> None:
    from superbrowser_bridge.handoff_store import save, take, clear

    clear()
    sa = _fresh_state(session_id="session-A", url="https://a.example")
    sb = _fresh_state(session_id="session-B", url="https://b.example")
    save("ka", sa)
    save("kb", sb)
    ha = take("ka")
    hb = take("kb")
    assert ha is not None and ha.session_id == "session-A"
    assert hb is not None and hb.session_id == "session-B"


def test_save_overwrites_same_key() -> None:
    from superbrowser_bridge.handoff_store import save, take, clear

    clear()
    save("k", _fresh_state(session_id="first"))
    save("k", _fresh_state(session_id="second"))
    h = take("k")
    assert h is not None and h.session_id == "second"


def test_empty_task_id_is_noop() -> None:
    """save("", state) and take("") must not raise and must not pollute."""
    from superbrowser_bridge.handoff_store import save, take, clear

    clear()
    save("", _fresh_state())
    assert take("") is None


def test_expired_entry_returns_none() -> None:
    """Entries older than _MAX_AGE_S are dropped on take/peek."""
    from superbrowser_bridge.handoff_store import save, take, clear, _store

    clear()
    save("k", _fresh_state())
    # Force the captured_at into the distant past.
    _store["k"].captured_at -= 10_000.0
    assert take("k") is None


def test_short_summary_includes_plan_progress() -> None:
    """short_summary surfaces session, url, and plan progress for the
    one-line resume log message."""
    from superbrowser_bridge.handoff_store import save, take, clear
    from superbrowser_bridge.task_plan import make_plan

    clear()
    s = _fresh_state(session_id="session-z", url="https://z.example/page")
    s.task_plan = make_plan([
        {"name": "a", "success_criteria": {"kind": "url_changed"}},
        {"name": "b", "success_criteria": {"kind": "url_changed"}},
    ])
    s.task_plan.steps[0].mark_attempt(True)  # 1/2 satisfied
    save("k", s)
    h = take("k")
    summary = h.short_summary()
    assert "session-z" in summary
    assert "z.example" in summary
    assert "1/2 steps satisfied" in summary


def test_hydrate_from_handoff_restores_state() -> None:
    """Verify the cross-module integration: BrowserSessionState.hydrate_from_handoff
    correctly populates a fresh state from a handoff snapshot."""
    from superbrowser_bridge.session_tools import BrowserSessionState
    from superbrowser_bridge.handoff_store import save, take, clear
    from superbrowser_bridge.task_plan import make_plan

    clear()
    s1 = _fresh_state(
        session_id="session-resume",
        url="https://wineaccess.com/store/search/?food_pairings=fish",
    )
    s1.task_plan = make_plan([
        {"name": "first", "success_criteria": {"kind": "url_changed"}},
        {"name": "second", "success_criteria": {"kind": "url_changed"}},
    ])
    s1.task_plan.active_step()
    save("resume-key", s1)

    s2 = BrowserSessionState()
    h = take("resume-key")
    s2.hydrate_from_handoff(h)

    assert s2.session_id == "session-resume"
    assert "food_pairings=fish" in s2.current_url
    assert s2.task_plan is not None
    assert s2.task_plan.steps[0].status == "in_progress"
    assert s2.cursor_failure_strategies == {"click_at"}
    assert s2.sessions_opened >= 1  # browser_open idempotency guard armed


def test_hydrate_with_none_is_safe() -> None:
    """Calling hydrate_from_handoff(None) when no prior handoff exists
    must not raise — used in the orchestrator's defensive path."""
    from superbrowser_bridge.session_tools import BrowserSessionState

    s = BrowserSessionState()
    s.hydrate_from_handoff(None)
    assert s.session_id == ""  # default unchanged


def main() -> int:
    tests = [
        test_save_take_roundtrip,
        test_take_is_one_shot,
        test_peek_does_not_consume,
        test_per_key_isolation,
        test_save_overwrites_same_key,
        test_empty_task_id_is_noop,
        test_expired_entry_returns_none,
        test_short_summary_includes_plan_progress,
        test_hydrate_from_handoff_restores_state,
        test_hydrate_with_none_is_safe,
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
