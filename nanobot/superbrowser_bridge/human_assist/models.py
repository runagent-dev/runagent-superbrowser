"""HumanAssistRequest — the typed unit of "a human must act here"."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

AssistType = Literal["captcha", "login", "otp", "approval", "text"]
AssistState = Literal["pending", "notified", "active", "resolved", "expired", "cancelled"]

TERMINAL_STATES: tuple[str, ...] = ("resolved", "expired", "cancelled")

# state machine: pending -> notified -> active -> resolved | expired | cancelled
_ALLOWED: dict[str, tuple[str, ...]] = {
    "pending": ("notified", "active", "resolved", "expired", "cancelled"),
    "notified": ("active", "resolved", "expired", "cancelled"),
    "active": ("resolved", "expired", "cancelled"),
    "resolved": (),
    "expired": (),
    "cancelled": (),
}


@dataclass
class HumanAssistRequest:
    type: str  # AssistType
    session_id: str
    view_url: str
    question: str
    domain: str = ""
    task_id: str = ""
    tier: str = "t1"
    page_url: str = ""
    timeout_s: float = 600.0
    id: str = field(default_factory=lambda: f"assist-{uuid.uuid4().hex[:8]}")
    state: str = "pending"  # AssistState
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    notify_count: int = 0
    resolution: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.expires_at:
            self.expires_at = self.created_at + self.timeout_s

    def transition(self, new_state: str) -> bool:
        """Apply a state transition; returns False when disallowed (terminal
        states are sticky)."""
        if new_state == self.state:
            return True
        if new_state not in _ALLOWED.get(self.state, ()):
            return False
        self.state = new_state
        return True

    def resolve(self, how: str, data: dict[str, Any] | None = None) -> bool:
        if not self.transition("resolved"):
            return False
        self.resolution = {"how": how, "data": data or {}, "at": time.time()}
        return True

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "state": self.state,
            "sessionId": self.session_id,
            "taskId": self.task_id,
            "tier": self.tier,
            "domain": self.domain,
            "viewUrl": self.view_url,
            "pageUrl": self.page_url,
            "question": self.question,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "notifyCount": self.notify_count,
            "resolution": self.resolution,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HumanAssistRequest":
        request = cls(
            type=str(data.get("type") or "text"),
            session_id=str(data.get("sessionId") or ""),
            view_url=str(data.get("viewUrl") or ""),
            question=str(data.get("question") or ""),
            domain=str(data.get("domain") or ""),
            task_id=str(data.get("taskId") or ""),
            tier=str(data.get("tier") or "t1"),
            page_url=str(data.get("pageUrl") or ""),
            id=str(data.get("id") or f"assist-{uuid.uuid4().hex[:8]}"),
        )
        request.state = str(data.get("state") or "pending")
        request.created_at = float(data.get("createdAt") or time.time())
        request.expires_at = float(data.get("expiresAt") or request.created_at + 600)
        request.notify_count = int(data.get("notifyCount") or 0)
        request.resolution = data.get("resolution")
        return request
