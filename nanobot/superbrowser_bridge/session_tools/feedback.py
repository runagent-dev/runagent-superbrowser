"""Cross-process feedback gate.

Reads the FeedbackBus state the TS server publishes at `/feedback` and
returns a deferred-result string when another subsystem (the captcha
solver) currently owns the browser. Mutating tools call `_feedback_gate`
at the top of their `execute` so they yield instead of racing the solver.
"""

from __future__ import annotations

from typing import Any

import httpx

from .http_client import SUPERBROWSER_URL


async def _fetch_feedback_state() -> dict[str, Any]:
    """Read the TS-side FeedbackBus snapshot over HTTP.

    Non-fatal on any failure — returns {} so callers fall through to the
    normal dispatch path (caller stays the same when the signal is down).
    """
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/feedback")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


async def _feedback_gate(tool_name: str) -> str | None:
    """Return a deferred-result string when another subsystem owns the
    browser right now (active captcha solve). None means `proceed`.

    Used at the top of mutating tools (click/type/scroll/navigate) to
    keep nanobot from racing the captcha solver — if the gate fires,
    nanobot gets an observation saying "captcha active, retry after 2s"
    and yields instead of firing a click that lands on a solved-then-
    reloaded page.
    """
    state = await _fetch_feedback_state()
    if state.get("captchaActive"):
        strategy = state.get("captchaStrategy") or "unknown"
        msg = (
            f"[feedback] {tool_name} deferred: captcha solve in progress "
            f"(strategy={strategy}). Retry after ~2000ms; do not issue "
            f"more actions until you see the captcha_done signal."
        )
        print(f"  {msg}")
        return msg
    return None
