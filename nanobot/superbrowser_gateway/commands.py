"""Gateway-level chat commands, answered without an LLM round-trip.

Only exact-token commands are intercepted (plus configured peek aliases), so a
task like "cancel my subscription on example.com" is never swallowed. Built-in
nanobot commands (/stop, /pairing, ...) pass through to the agent loop.
"""

from __future__ import annotations

import time
from typing import Awaitable, Callable

from nanobot.bus.events import InboundMessage, OutboundMessage

from .config import GatewaySettings
from .media import MediaService

Publish = Callable[[OutboundMessage], Awaitable[None]]


class GatewayCommands:
    def __init__(self, *, settings: GatewaySettings, media: MediaService, registry):
        self.settings = settings
        self.media = media
        self.registry = registry

    async def try_handle(self, msg: InboundMessage, publish: Publish) -> bool:
        """True when the message was a gateway command (already answered)."""
        text = (msg.content or "").strip().lower()
        if not text:
            return False
        if text in tuple(alias.lower() for alias in self.settings.peek_aliases):
            await self._peek(msg, publish)
            return True
        if text == "/gateway":
            await self._status(msg, publish)
            return True
        return False

    async def _peek(self, msg: InboundMessage, publish: Publish) -> None:
        shot = await self.media.current_view(self.settings.engine_url)
        if shot is None:
            await publish(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="No browser view available right now (no recent browser task).",
                )
            )
            return
        await publish(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Current browser view:",
                media=[str(shot)],
            )
        )

    async def _status(self, msg: InboundMessage, publish: Publish) -> None:
        turns = self.registry.active()
        if not turns:
            body = "Gateway is up. No task is running."
        else:
            lines = ["Gateway is up. Running:"]
            now = time.time()
            for turn in turns:
                minutes = int((now - turn.started_at) // 60)
                lines.append(f"- {turn.session_key}: {turn.last_inbound or '(task)'} ({minutes} min)")
            body = "\n".join(lines)
        await publish(OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=body))
