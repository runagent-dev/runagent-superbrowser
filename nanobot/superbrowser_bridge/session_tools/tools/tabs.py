"""Tab management for site-opened new tabs.

The TS server auto-switches observation to a popup tab the moment the
page opens one (mirrors real browser focus) and reports it via
``[NEW_TAB ...]`` notices in tool results. This tool is the explicit
control surface on top of that: list the session's tabs, switch focus
back to a previous tab, or close one.

Deliberately minimal — there is no "open tab" action. Tab creation is a
page-driven event (the site opens them); deliberate URL changes go
through browser_navigate on the current tab.

T3 (patchright) sessions are single-page; tab tracking is a Tier-1
feature for now, so t3 session ids get a graceful unsupported note.
"""

from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)

from ..formatting import _format_state
from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        action=StringSchema(
            "'list' — show all tabs with their URLs; "
            "'switch' — focus tab `index` (observation moves there); "
            "'close' — close tab `index` (default: active; focus falls "
            "back to the most recent remaining tab).",
            enum=("list", "switch", "close"),
        ),
        index=IntegerSchema(
            description="0-based tab index (required for 'switch'; optional for 'close').",
            nullable=True,
        ),
        required=["session_id", "action"],
    )
)
class BrowserTabsTool(Tool):
    name = "browser_tabs"
    description = (
        "Manage browser tabs within the current session. When a click "
        "opens a new tab, the system AUTO-SWITCHES to it and tells you "
        "via [NEW_TAB ...] — you rarely need this tool for that case. "
        "Use action='list' to see all open tabs, 'switch' to go back to "
        "a previous tab, 'close' to close one. After a switch, take a "
        "fresh browser_screenshot before clicking — V_n bboxes never "
        "survive a tab change."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        action: str,
        index: int | None = None,
        **kw: Any,
    ) -> str:
        if session_id.startswith("t3-"):
            return (
                "[tabs_unsupported_tier] t3 sessions are single-page — tab "
                "tracking is a Tier-1 feature. If a link must open in a new "
                "tab, navigate to its href directly instead."
            )

        action = (action or "").strip().lower()
        if action == "list":
            return await self._list(session_id)
        if action == "switch":
            if index is None:
                return "[tabs_failed:index_required] action='switch' needs a 0-based `index` — call browser_tabs(action='list') first."
            return await self._switch(session_id, index)
        if action == "close":
            return await self._close(session_id, index)
        return f"[tabs_failed:bad_action] Unknown action '{action}' — use list | switch | close."

    async def _list(self, session_id: str) -> str:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/tabs",
            timeout=10.0,
        )
        if r.status_code != 200:
            return f"[tabs_failed:http_{r.status_code}] {r.text[:200]}"
        data = r.json() or {}
        tabs = data.get("tabs") or []
        if len(tabs) <= 1:
            only = tabs[0] if tabs else {}
            return (
                f"1 tab open: {only.get('url', self.s.current_url or '?')} "
                "(no other tabs — nothing to switch to)"
            )
        lines = [f"{len(tabs)} tabs open (active marked with ←):"]
        for t in tabs:
            marker = "  ← active" if t.get("active") else ""
            title = (t.get("title") or "").strip()
            title_part = f" — {title[:60]!r}" if title else ""
            lines.append(f"  [{t.get('index')}] {t.get('url', '?')}{title_part}{marker}")
        lines.append(
            "browser_tabs(session_id, action='switch', index=N) to focus one."
        )
        self.s.log_activity("browser_tabs(list)", f"count={len(tabs)}")
        return "\n".join(lines)

    async def _switch(self, session_id: str, index: int) -> str:
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/tabs/activate",
            json={"index": index},
            timeout=15.0,
        )
        if r.status_code != 200:
            body = {}
            try:
                body = r.json() or {}
            except Exception:
                pass
            return (
                f"[tabs_failed:{body.get('reason', f'http_{r.status_code}')}] "
                f"{body.get('error', r.text[:200])}"
            )
        data = r.json() or {}
        self._sync_state_after_focus_change(data)
        self.s.log_activity(
            f"browser_tabs(switch {index})", f"url={(data.get('url') or '?')[:50]}"
        )
        self.s.record_step(
            "browser_tabs", f"switch index={index}",
            f"url={(data.get('url') or '?')[:60]}",
        )
        header = (
            f"Switched to tab {index + 1} — {data.get('url', '?')}\n"
            "V_n bboxes from before the switch are invalid: take a fresh "
            "browser_screenshot before clicking."
        )
        return f"{header}\n{_format_state(data, self.s)}"

    async def _close(self, session_id: str, index: int | None) -> str:
        payload: dict[str, Any] = {}
        if index is not None:
            payload["index"] = index
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/tabs/close",
            json=payload,
            timeout=15.0,
        )
        if r.status_code != 200:
            body = {}
            try:
                body = r.json() or {}
            except Exception:
                pass
            if body.get("reason") == "last_tab":
                return (
                    "[tabs_failed:last_tab] Cannot close the only tab — use "
                    "browser_close(session_id) to end the whole session."
                )
            return (
                f"[tabs_failed:{body.get('reason', f'http_{r.status_code}')}] "
                f"{body.get('error', r.text[:200])}"
            )
        data = r.json() or {}
        self._sync_state_after_focus_change(data)
        closed = data.get("closed") or {}
        self.s.log_activity(
            f"browser_tabs(close {closed.get('index', '?')})",
            f"now={(data.get('url') or '?')[:50]}",
        )
        header = (
            f"Closed tab {closed.get('index', '?')} ({closed.get('url', '?')}). "
            f"Now on: {data.get('url', '?')}"
        )
        return f"{header}\n{_format_state(data, self.s)}"

    def _sync_state_after_focus_change(self, data: dict) -> None:
        """Focus moved to a different tab → different document. Record the
        new URL and drop the frozen vision epoch + cached-vision piggyback
        so the next click_at can't resolve against the old tab's bboxes.
        Same invalidation idiom as click.py's EPOCH_DIRTY path."""
        url = data.get("url") or ""
        if url:
            self.s.record_url(url)
        self.s._vision_epoch_response = None
        self.s._last_vision_ts = 0.0
