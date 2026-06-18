"""The ``intelligence`` / ``mode`` switch.

Three modes map onto the existing orchestrator topology:

* ``auto``    — full topology; the orchestrator decides fetch vs browser using
                its SOUL routing rubric (this is the "intelligence" the user
                asked for). Nothing is removed.
* ``fetch``   — read-only. ``delegate_browser_task`` is removed, leaving the
                search worker + in-process ``fetch_*`` tools. Needs no TS engine.
* ``browser`` — interactive. ``delegate_search_task`` is removed so the agent
                drives a real browser.

We *layer* on top of ``register_orchestrator_tools`` by mutating the tool
registry after it runs, rather than forking it — so the shared registration
function (and the existing test harness / CLI) stay untouched.
"""

from __future__ import annotations

from typing import Any, Literal

Mode = Literal["auto", "fetch", "browser"]

MODES: tuple[Mode, ...] = ("auto", "fetch", "browser")

_FETCH_DIRECTIVE = (
    "MODE: read-only fetch. The interactive browser is unavailable — use "
    "delegate_search_task and the fetch_* tools to read and aggregate public "
    "information. If the task fundamentally requires interaction (logging in, "
    "submitting a form, completing a booking/purchase), say so plainly instead "
    "of guessing or fabricating a result."
)

_BROWSER_DIRECTIVE = (
    "MODE: interactive browser. Search delegation is unavailable — drive the "
    "real browser via delegate_browser_task to complete the task. When you call "
    "delegate_browser_task, pass force=True (the user explicitly asked for the "
    "browser, so the search-vs-browser classifier should not override that)."
)


def _safe_unregister(bot: Any, name: str) -> None:
    try:
        bot._loop.tools.unregister(name)
    except Exception:  # noqa: BLE001 - tool may already be absent; best effort
        pass


def apply_mode(bot: Any, mode: Mode) -> str:
    """Adjust ``bot``'s tool registry for ``mode`` and return a system directive.

    Must be called *after* ``register_orchestrator_tools(bot)``. Returns a short
    directive string to prepend to the task (empty for ``auto``).
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    if mode == "fetch":
        _safe_unregister(bot, "delegate_browser_task")
        return _FETCH_DIRECTIVE
    if mode == "browser":
        _safe_unregister(bot, "delegate_search_task")
        return _BROWSER_DIRECTIVE
    return ""  # auto — leave the full topology in place
