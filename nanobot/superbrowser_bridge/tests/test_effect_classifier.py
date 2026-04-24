"""Unit tests for the _classify_effect / _maybe_no_effect_prefix helpers.

These are the Python side of Round 2's P0 (post-action effect
verification). `_classify_effect` reads the `effect` field the TS
bridge now attaches to every mutation-tool response; the prefix helper
wraps a caption with `[no_effect:<tool>] …` when the delta is zero.

No network calls. Run:
    source venv/bin/activate && \\
        PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_effect_classifier.py
"""

from __future__ import annotations

import sys
import tempfile
import os


def _isolate_routing_store():
    """Point routing_store at a fresh tmpdir so tactic_penalty writes
    don't pollute the real learnings dir under /root/.../learnings/."""
    from superbrowser_bridge import routing, routing_store
    tmpdir = tempfile.mkdtemp(prefix="effect-test-")
    routing.LEARNINGS_DIR = tmpdir
    routing_store.LEARNINGS_DIR = tmpdir
    routing_store.DB_PATH = os.path.join(tmpdir, "routing.sqlite")
    routing_store._conn = None
    routing_store._migrated = False
    return tmpdir


def test_missing_effect_is_treated_as_had_effect() -> None:
    """Legacy behavior: a response without the new `effect` field must
    behave exactly as before (no `[no_effect]` prefix). Protects against
    regressions during staged rollout when TS ships first."""
    from superbrowser_bridge.session_tools import _classify_effect
    had, _ = _classify_effect({"success": True}, "browser_click")
    assert had is True

    had, _ = _classify_effect({}, "browser_click")
    assert had is True

    had, _ = _classify_effect(None, "browser_click")
    assert had is True
    print("✓ test_missing_effect_is_treated_as_had_effect")


def test_zero_delta_is_no_effect() -> None:
    from superbrowser_bridge.session_tools import _classify_effect
    had, reason = _classify_effect({
        "effect": {
            "url_changed": False,
            "mutation_delta": 0,
            "focused_changed": False,
        },
    }, "browser_click_at")
    assert had is False
    assert "browser_click_at" in reason
    print("✓ test_zero_delta_is_no_effect")


def test_url_change_alone_counts_as_effect() -> None:
    from superbrowser_bridge.session_tools import _classify_effect
    had, _ = _classify_effect({
        "effect": {"url_changed": True, "mutation_delta": 0, "focused_changed": False},
    }, "browser_click")
    assert had is True
    print("✓ test_url_change_alone_counts_as_effect")


def test_mutation_delta_alone_counts_as_effect() -> None:
    from superbrowser_bridge.session_tools import _classify_effect
    had, _ = _classify_effect({
        "effect": {"url_changed": False, "mutation_delta": 3, "focused_changed": False},
    }, "browser_click")
    assert had is True
    print("✓ test_mutation_delta_alone_counts_as_effect")


def test_focus_change_alone_counts_as_effect() -> None:
    """Clicking an input that was previously unfocused should register
    as an effect even if no DOM mutation happened. Otherwise the brain
    would treat "focus moved to the field I just clicked" as a failure."""
    from superbrowser_bridge.session_tools import _classify_effect
    had, _ = _classify_effect({
        "effect": {"url_changed": False, "mutation_delta": 0, "focused_changed": True},
    }, "browser_click_at")
    assert had is True
    print("✓ test_focus_change_alone_counts_as_effect")


def test_prefix_wraps_caption_on_no_effect() -> None:
    from superbrowser_bridge.session_tools import _maybe_no_effect_prefix
    out = _maybe_no_effect_prefix({
        "effect": {"url_changed": False, "mutation_delta": 0, "focused_changed": False},
    }, "browser_click_at", "Clicked V2")
    assert out.startswith("[no_effect:browser_click_at]")
    # Base caption is preserved so vision-cached bboxes still reach the brain.
    assert "Clicked V2" in out
    # Hint text carries the alternative tactics.
    assert "browser_click_selector" in out or "reactSetValue" in out
    print("✓ test_prefix_wraps_caption_on_no_effect")


def test_prefix_passthrough_on_effect() -> None:
    """After R4.4, successful cursor-tool replies get a
    `[cursor_success:...]` tag prepended (positive reinforcement so
    the brain sees "cursor worked, stay on this track" — specifically
    to counter the over-reach-for-scripts habit). The base caption is
    preserved verbatim after the tag."""
    from superbrowser_bridge.session_tools import _maybe_no_effect_prefix
    out = _maybe_no_effect_prefix({
        "effect": {"url_changed": False, "mutation_delta": 7, "focused_changed": False},
    }, "browser_click_at", "Clicked V2")
    assert out.startswith("[cursor_success:browser_click_at]"), out
    assert out.endswith("Clicked V2"), out
    print("✓ test_prefix_passthrough_on_effect")


def test_prefix_passthrough_non_cursor_tool_no_tag() -> None:
    """Non-cursor tools on had_effect=True get no tag — the cursor-
    success tag is targeted positive reinforcement, not a generic
    success marker."""
    from superbrowser_bridge.session_tools import _maybe_no_effect_prefix
    out = _maybe_no_effect_prefix({
        "effect": {"url_changed": True, "mutation_delta": 0, "focused_changed": False},
    }, "browser_run_script", "ran ok")
    assert out == "ran ok"
    print("✓ test_prefix_passthrough_non_cursor_tool_no_tag")


def test_prefix_records_tactic_failure() -> None:
    """`_maybe_no_effect_prefix` must record a per-domain tactic
    failure when there's a session state with a current URL. The
    delegation prompt reads this to suggest alternatives upfront."""
    _isolate_routing_store()
    from superbrowser_bridge.session_tools import (
        _maybe_no_effect_prefix, BrowserSessionState,
    )
    from superbrowser_bridge.routing import tactic_penalty_summary

    s = BrowserSessionState()
    s.current_url = "https://spothero.example/search"

    for _ in range(3):
        _maybe_no_effect_prefix(
            {"effect": {"url_changed": False, "mutation_delta": 0, "focused_changed": False}},
            "browser_click_at",
            "Clicked V2",
            session_state=s,
        )

    penalties = tactic_penalty_summary("spothero.example", min_count=2)
    assert penalties, "penalty should have been recorded"
    tools = dict(penalties)
    assert tools.get("browser_click_at") == 3, f"expected count=3, got {tools}"
    print("✓ test_prefix_records_tactic_failure")


def test_prefix_decays_on_successful_effect() -> None:
    """A successful use of the same tool on the same domain must
    decay the penalty — a genuinely unreliable tactic still shows
    pressure after one lucky success, but doesn't stay penalized
    forever on a domain that's now working."""
    _isolate_routing_store()
    from superbrowser_bridge.session_tools import (
        _maybe_no_effect_prefix, BrowserSessionState,
    )
    from superbrowser_bridge.routing import tactic_penalty_summary

    s = BrowserSessionState()
    s.current_url = "https://decayed.example/"

    # Accumulate.
    for _ in range(3):
        _maybe_no_effect_prefix(
            {"effect": {"url_changed": False, "mutation_delta": 0, "focused_changed": False}},
            "browser_click",
            "x",
            session_state=s,
        )
    # One successful use.
    _maybe_no_effect_prefix(
        {"effect": {"mutation_delta": 2, "url_changed": False, "focused_changed": False}},
        "browser_click",
        "x",
        session_state=s,
    )
    penalties = tactic_penalty_summary("decayed.example", min_count=1)
    tools = dict(penalties)
    assert tools.get("browser_click", 0) == 2, f"expected decay to 2, got {tools}"
    print("✓ test_prefix_decays_on_successful_effect")


def main() -> int:
    tests = [
        test_missing_effect_is_treated_as_had_effect,
        test_zero_delta_is_no_effect,
        test_url_change_alone_counts_as_effect,
        test_mutation_delta_alone_counts_as_effect,
        test_focus_change_alone_counts_as_effect,
        test_prefix_wraps_caption_on_no_effect,
        test_prefix_passthrough_on_effect,
        test_prefix_passthrough_non_cursor_tool_no_tag,
        test_prefix_records_tactic_failure,
        test_prefix_decays_on_successful_effect,
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
