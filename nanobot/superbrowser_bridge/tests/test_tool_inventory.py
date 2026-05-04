"""Unit tests for Arch v4 Phase G: brain tool inventory shrink.

Verifies that register_session_tools registers exactly the tools
expected for v4: vision-bbox click is the only click variant. Raw-
coords click, DOM-selector click, and untargeted scroll are
unregistered by default — they're available behind
REGISTER_LEGACY_TOOLS=1 for debug.

Run:
    source venv/bin/activate && \
        python3 -m superbrowser_bridge.tests.test_tool_inventory
"""

from __future__ import annotations

import os
import sys


# Tools the brain MUST be able to call in arch v4.
_REQUIRED_TOOLS = {
    # Lifecycle
    # Arch v4.4: browser_navigate is NOT registered by default.
    # browser_open handles cold-start; in-task navigation goes via
    # browser_click_at(V_n). Re-enable with REGISTER_BROWSER_NAVIGATE=1.
    "browser_open", "browser_close",
    # Vision
    "browser_screenshot",
    "browser_image_region",
    # The single click family
    "browser_click_at",
    # Typing — both variants kept per user direction
    "browser_type_at", "browser_type", "browser_fix_text_at",
    "browser_keys",
    # Scroll — targeted only
    "browser_scroll_until",
    # Forms / selects / sliders
    "browser_select", "browser_select_option",
    "browser_form_plan", "browser_form_begin", "browser_form_status",
    "browser_form_commit",
    "browser_set_slider", "browser_set_slider_at",
    "browser_list_slider_handles", "browser_drag_slider_until",
    "browser_drag", "browser_drag_path", "browser_drag_selectors",
    "browser_get_rect",
    # Last-resort tier-3 / tier-4
    "browser_eval", "browser_run_script",
    # Hierarchical planning (still in default surface).
    # Arch v4.1 (Fix 2a): browser_set_task_plan, browser_plan_skip_step,
    # and browser_plan_replan removed — TaskBrief.checklist is the
    # single source of truth.
    # Arch v4.2: browser_look_again, browser_state_check,
    # browser_verify_action, browser_update_task_brief, browser_preplan
    # also removed from default surface — they were meta-vision bloat
    # making the brain take 5 tool calls per click. OLD lightweight
    # architecture used only screenshot + get_markdown for observation.
    "browser_plan_next_steps",
    "browser_wait_for",
    # Captcha + escalate
    "browser_detect_captcha", "browser_solve_captcha",
    "browser_captcha_screenshot", "browser_solve_puzzle",
    # Helpers + exits
    "browser_get_markdown", "browser_dialog",
    "browser_ask_user", "browser_request_help", "browser_verify_fact",
    "browser_escalate", "browser_rewind_to_checkpoint",
}

# Tools that MUST NOT be on the brain's inventory by default. These
# stay as classes for tests / legacy paths but are unregistered to
# stop the brain from drifting to them.
_REMOVED_TOOLS = {
    "browser_click",                # raw coordinates — use click_at
    "browser_click_selector",       # DOM selector — use click_at
    "browser_scroll",               # untargeted — use scroll_until
    "browser_inventory_filters",    # large dump triggers hallucination
}


class _FakeBot:
    """Minimal Nanobot stand-in: only needs ._loop.tools.register."""
    def __init__(self):
        from nanobot.agent.tools.registry import ToolRegistry

        class _Loop:
            tools = ToolRegistry()
        self._loop = _Loop()


def _registered_names(register_legacy: bool = False) -> set[str]:
    from superbrowser_bridge.session_tools import register_session_tools

    if register_legacy:
        os.environ["REGISTER_LEGACY_TOOLS"] = "1"
    else:
        os.environ.pop("REGISTER_LEGACY_TOOLS", None)
    try:
        bot = _FakeBot()
        register_session_tools(bot)
        return set(bot._loop.tools._tools.keys())
    finally:
        os.environ.pop("REGISTER_LEGACY_TOOLS", None)


def test_required_tools_all_registered() -> None:
    names = _registered_names()
    missing = _REQUIRED_TOOLS - names
    assert not missing, f"required tools missing from inventory: {missing}"


def test_removed_tools_not_registered_by_default() -> None:
    names = _registered_names()
    leaked = _REMOVED_TOOLS & names
    assert not leaked, (
        f"unwanted tools still on brain's inventory: {leaked}. "
        f"Per arch v4 Phase G, click_at is the only click; selector "
        f"click and untargeted scroll are removed."
    )


def test_legacy_flag_restores_removed_tools() -> None:
    """REGISTER_LEGACY_TOOLS=1 brings back the removed tools (debug)."""
    names = _registered_names(register_legacy=True)
    for tool in _REMOVED_TOOLS:
        assert tool in names, (
            f"REGISTER_LEGACY_TOOLS=1 should re-register {tool!r}"
        )


def test_no_orphan_tools_in_default_inventory() -> None:
    """Sanity: every default tool name is either required or a
    registered legacy tool — no surprises in the default inventory.
    Catches accidental adds during refactors."""
    names = _registered_names()
    expected = _REQUIRED_TOOLS  # legacy is opt-in only
    unexpected = names - expected
    assert not unexpected, (
        f"unexpected tools in default inventory (not in _REQUIRED_TOOLS): "
        f"{sorted(unexpected)}. If these are legitimate, add them to "
        f"_REQUIRED_TOOLS in this test."
    )


def main() -> int:
    tests = [
        test_required_tools_all_registered,
        test_removed_tools_not_registered_by_default,
        test_legacy_flag_restores_removed_tools,
        test_no_orphan_tools_in_default_inventory,
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
