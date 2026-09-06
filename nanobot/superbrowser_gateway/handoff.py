"""Handoff inbox + routing: HANDOFF_WEBHOOK_URL events → user chats.

The engine's captcha ladder (src/browser/captcha/strategies/human-handoff.ts)
and the Python human-assist emitter both POST the same payload family here:
``{event, url, caption, screenshot(b64), taskId, sessionId, pageUrl, ...}``.
Legacy ``human_handoff_*`` events (today's TS builds) are treated as
assistType=captcha.

Routing: the chat whose turn is active gets the link; with several active
turns all of them do (better a duplicate ping than a stranded captcha); with
none, the configured owners are notified.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus

from .bridge import ActiveTurnRegistry
from .config import GatewaySettings
from .media import MediaService

_MAX_INBOX = 50


@dataclass
class HandoffEvent:
    id: str
    event: str
    assist_type: str
    url: str
    caption: str
    page_url: str
    page_title: str
    session_id: str
    task_id: str
    received_at: float
    screenshot_path: str | None = None
    acked: bool = False
    delivered_to: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event": self.event,
            "assistType": self.assist_type,
            "url": self.url,
            "caption": self.caption,
            "pageUrl": self.page_url,
            "pageTitle": self.page_title,
            "sessionId": self.session_id,
            "taskId": self.task_id,
            "receivedAt": self.received_at,
            "screenshotPath": self.screenshot_path,
            "acked": self.acked,
            "deliveredTo": self.delivered_to,
        }


class HandoffInbox:
    def __init__(self) -> None:
        self._events: list[HandoffEvent] = []

    def add(self, event: HandoffEvent) -> None:
        self._events.append(event)
        del self._events[:-_MAX_INBOX]

    def list(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in reversed(self._events)]

    def ack(self, event_id: str) -> bool:
        for event in self._events:
            if event.id == event_id:
                event.acked = True
                return True
        return False


class HandoffRouter:
    def __init__(
        self,
        *,
        inbox: HandoffInbox,
        registry: ActiveTurnRegistry,
        edge_bus: MessageBus,
        settings: GatewaySettings,
        media: MediaService,
        broker=None,
    ):
        self.inbox = inbox
        self.registry = registry
        self.edge_bus = edge_bus
        self.settings = settings
        self.media = media
        self.broker = broker

    async def route(self, payload: dict[str, Any]) -> HandoffEvent:
        event_name = str(payload.get("event") or "human_assist_required")
        assist_type = str(payload.get("assistType") or "captcha")

        screenshot_path: str | None = None
        b64 = payload.get("screenshot")
        if isinstance(b64, str) and b64:
            try:
                import base64

                suffix = ".png" if "png" in str(payload.get("screenshotMimeType") or "") else ".jpg"
                screenshot_path = str(self.media.save_bytes(base64.b64decode(b64), suffix=suffix))
            except Exception:  # noqa: BLE001 - deliver the link even without the shot
                logger.warning("handoff screenshot decode failed")

        event = HandoffEvent(
            id=f"assist-{uuid.uuid4().hex[:8]}",
            event=event_name,
            assist_type=assist_type,
            url=str(payload.get("url") or ""),
            caption=str(payload.get("caption") or "A human is needed in the browser."),
            page_url=str(payload.get("pageUrl") or ""),
            page_title=str(payload.get("pageTitle") or ""),
            session_id=str(payload.get("sessionId") or ""),
            task_id=str(payload.get("taskId") or ""),
            received_at=time.time(),
        )
        event.screenshot_path = screenshot_path
        self.inbox.add(event)
        if self.broker is not None:
            try:
                self.broker._emit({"topic": "handoff.new", "data": event.to_dict()})
            except Exception:  # noqa: BLE001
                pass

        targets = self._targets()

        content = event.caption
        if event.url:
            content = f"{content}\n{event.url}"
        media = [event.screenshot_path] if event.screenshot_path else []

        for channel, chat_id in targets:
            await self.edge_bus.publish_outbound(
                OutboundMessage(channel=channel, chat_id=chat_id, content=content, media=list(media))
            )
            event.delivered_to.append(f"{channel}:{chat_id}")
        if not targets:
            logger.warning("handoff event {} had no delivery targets (no active turn, no owners)", event.id)
        return event

    def _targets(self) -> list[tuple[str, str]]:
        active = [(t.channel, t.chat_id) for t in self.registry.active()]
        if active:
            return active
        owners: list[tuple[str, str]] = []
        for channel, ids in (self.settings.owners or {}).items():
            owners.extend((channel, str(chat_id)) for chat_id in ids)
        return owners
