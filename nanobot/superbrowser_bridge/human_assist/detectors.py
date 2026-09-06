"""Completion detectors — "did the human finish?" polled every few seconds.

LoginDetector resolution order (strongest first):
1. explicit Done — the engine's pending human-input request got answered
   (``GET /session/:id/human-input`` flips ``pending`` non-null → null);
2. heuristic, debounced over 2 consecutive polls: the URL moved off the
   login page AND no password field is present (probed via the loopback
   ``/session/:id/script`` endpoint on T1, or the T3 page handle).

Everything is injected (url getters, probes) so the decision table is unit
testable without an engine.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

LOGIN_URL_PATTERN = re.compile(r"/log[-_]?in|/sign[-_]?in|/auth|/verify|sso|account/login", re.I)

POLL_INTERVAL_S = float(os.environ.get("SUPERBROWSER_ASSIST_POLL_S", "5"))


@dataclass
class LoginDetector:
    start_url: str
    get_url: Callable[[], Awaitable[str | None]]
    has_password_field: Callable[[], Awaitable[bool | None]]
    pending_done: Callable[[], Awaitable[bool | None]] | None = None
    _saw_pending: bool = False
    _positive_streak: int = field(default=0)

    async def poll(self) -> dict[str, Any] | None:
        """None = keep waiting; dict = resolved {"how": ..., ...}."""
        if self.pending_done is not None:
            done = await self.pending_done()
            if done is True:
                return {"how": "user_done"}

        url = await self.get_url()
        if url is None:
            self._positive_streak = 0
            return None
        moved_off_login = bool(url) and url != self.start_url and not LOGIN_URL_PATTERN.search(url)
        if not moved_off_login:
            self._positive_streak = 0
            return None

        password_present = await self.has_password_field()
        if password_present is True:
            self._positive_streak = 0
            return None
        # password probe None (unavailable) still counts once URL moved —
        # but only via the 2-poll debounce below.
        self._positive_streak += 1
        if self._positive_streak >= 2:
            return {"how": "heuristic", "url": url}
        return None


def make_t1_probes(session_id: str) -> dict[str, Callable]:
    """Engine-backed probes for a T1 (puppeteer) session."""
    from ..session_tools.http_client import SUPERBROWSER_URL, _request_with_backoff

    async def get_url() -> str | None:
        try:
            resp = await _request_with_backoff(
                "GET", f"{SUPERBROWSER_URL}/session/{session_id}/state", params={"vision": "false"}, timeout=10.0
            )
            resp.raise_for_status()
            return str(resp.json().get("url") or "") or None
        except Exception:  # noqa: BLE001
            return None

    async def has_password_field() -> bool | None:
        try:
            resp = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/script",
                json={"code": "return !!document.querySelector('input[type=password]');", "mutates": False},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result", data.get("data"))
            if isinstance(result, bool):
                return result
            return None
        except Exception:  # noqa: BLE001 - /script may be token-gated off-loopback
            return None

    async def pending_done() -> bool | None:
        """True once a previously-seen pending human-input request resolves."""
        try:
            resp = await _request_with_backoff(
                "GET", f"{SUPERBROWSER_URL}/session/{session_id}/human-input", timeout=10.0
            )
            resp.raise_for_status()
            pending = resp.json().get("pending")
        except Exception:  # noqa: BLE001
            return None
        state = _pending_state.setdefault(session_id, {"seen": False})
        if pending:
            state["seen"] = True
            return False
        if state["seen"]:
            state["seen"] = False
            return True
        return False

    return {"get_url": get_url, "has_password_field": has_password_field, "pending_done": pending_done}


_pending_state: dict[str, dict[str, bool]] = {}


def make_t3_probes(session_id: str) -> dict[str, Callable]:
    """Best-effort probes for a T3 (patchright) session via the live page handle."""

    async def get_url() -> str | None:
        try:
            from ..antibot import interactive_session as t3

            managed = t3.default()._sessions.get(session_id)  # noqa: SLF001 - same-repo bridge internals
            if managed is None:
                return None
            return str(managed.page.url or "") or None
        except Exception:  # noqa: BLE001
            return None

    async def has_password_field() -> bool | None:
        try:
            from ..antibot import interactive_session as t3

            managed = t3.default()._sessions.get(session_id)  # noqa: SLF001
            if managed is None:
                return None
            return bool(await managed.page.evaluate("() => !!document.querySelector('input[type=password]')"))
        except Exception:  # noqa: BLE001
            return None

    return {"get_url": get_url, "has_password_field": has_password_field, "pending_done": None}
