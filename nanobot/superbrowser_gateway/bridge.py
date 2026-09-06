"""GatewayBridge — middleware between the channel edge and the orchestrator.

Two independent MessageBus instances:

    channels ⇄ edge_bus ⇄ GatewayBridge ⇄ agent_bus ⇄ orchestrator AgentLoop

The bridge is pure middleware; both sides keep their native behavior:
- inbound pump (edge→agent): intercepts gateway commands (/peek, /status),
  records turn starts, sends a one-time "queued" notice when other chats are
  mid-task, then forwards untouched.
- outbound pump (agent→edge): throttles progress messages per chat, attaches
  the final result screenshot for browser turns, and forwards.
- keepalive: "still working" notes when a turn is silent too long.

Turn state persists to <data_dir>/active_turns.json so a restart mid-task can
tell the affected chats instead of leaving them hanging.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus

from .commands import GatewayCommands
from .config import GatewaySettings
from .media import MediaService

# nanobot tags every progress variant (tool hints, reasoning, tool events) with
# `_progress` (see nanobot/bus/progress.py:33), so that one marker identifies a
# discrete progress ping. Token-level streaming deltas are tagged
# `_stream_delta`/`_stream_end` instead and are coalesced into channel edits by
# nanobot's own ChannelManager on the edge bus — the bridge must forward those
# UNTOUCHED (never throttle them). An untagged content message is the final
# answer.
def _classify(msg: OutboundMessage) -> str:
    metadata = msg.metadata or {}
    if metadata.get("_stream_delta") or metadata.get("_stream_end"):
        return "stream"
    if metadata.get("_progress"):
        return "progress"
    return "final"


@dataclass
class TurnState:
    session_key: str
    channel: str
    chat_id: str
    started_at: float
    last_inbound: str = ""
    last_outbound_at: float = 0.0
    progress_sent: int = 0
    queued_notice_sent: bool = False

    def to_dict(self) -> dict:
        return {
            "session_key": self.session_key,
            "channel": self.channel,
            "chat_id": self.chat_id,
            "started_at": self.started_at,
            "last_inbound": self.last_inbound,
        }


class ActiveTurnRegistry:
    """In-memory turn tracking + restart markers on disk."""

    def __init__(self, marker_path: Path):
        self._marker_path = marker_path
        self._turns: dict[str, TurnState] = {}

    def turn_started(self, msg: InboundMessage) -> TurnState:
        turn = self._turns.get(msg.session_key)
        if turn is None:
            turn = TurnState(
                session_key=msg.session_key,
                channel=msg.channel,
                chat_id=msg.chat_id,
                started_at=time.time(),
            )
            self._turns[msg.session_key] = turn
        turn.last_inbound = (msg.content or "")[:120]
        self._persist()
        return turn

    def outbound_seen(self, session_key: str, *, final: bool) -> None:
        turn = self._turns.get(session_key)
        if turn is None:
            return
        turn.last_outbound_at = time.time()
        if final:
            del self._turns[session_key]
            self._persist()

    def get(self, session_key: str) -> TurnState | None:
        return self._turns.get(session_key)

    def active(self) -> list[TurnState]:
        return list(self._turns.values())

    def load_stale_markers(self) -> list[dict]:
        """Turns that were active when the previous process died."""
        try:
            data = json.loads(self._marker_path.read_text())
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def clear_markers(self) -> None:
        self._persist()

    def _persist(self) -> None:
        try:
            self._marker_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps([t.to_dict() for t in self._turns.values()], indent=2)
            self._marker_path.write_text(payload)
        except OSError:  # noqa: PERF203 - marker is best-effort
            pass


class GatewayBridge:
    def __init__(
        self,
        *,
        agent_bus: MessageBus,
        edge_bus: MessageBus,
        settings: GatewaySettings,
        media: MediaService,
        registry: ActiveTurnRegistry,
    ):
        self.agent_bus = agent_bus
        self.edge_bus = edge_bus
        self.settings = settings
        self.media = media
        self.registry = registry
        self.commands = GatewayCommands(settings=settings, media=media, registry=registry)
        # session_key of chats we already told "queued"; reset when idle
        self._last_progress_at: dict[str, float] = {}

    # ----- pumps -----

    async def run_inbound_pump(self) -> None:
        while True:
            msg = await self.edge_bus.consume_inbound()
            try:
                handled = await self.commands.try_handle(msg, publish=self.edge_bus.publish_outbound)
                if handled:
                    continue
                others_active = any(t.session_key != msg.session_key for t in self.registry.active())
                turn = self.registry.turn_started(msg)
                if (
                    others_active
                    and self.settings.max_concurrent_tasks <= 1
                    and not turn.queued_notice_sent
                ):
                    turn.queued_notice_sent = True
                    await self.edge_bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="Another task is running — yours is queued and starts right after.",
                            metadata={"_progress": True},
                        )
                    )
                await self.agent_bus.publish_inbound(msg)
            except Exception:  # noqa: BLE001 - one bad message must not kill the pump
                logger.exception("inbound pump error")

    async def run_outbound_pump(self) -> None:
        while True:
            msg = await self.agent_bus.consume_outbound()
            try:
                await self._handle_outbound(msg)
            except Exception:  # noqa: BLE001
                logger.exception("outbound pump error; forwarding message untouched")
                try:
                    await self.edge_bus.publish_outbound(msg)
                except Exception:  # noqa: BLE001
                    pass

    async def _handle_outbound(self, msg: OutboundMessage) -> None:
        session_key = f"{msg.channel}:{msg.chat_id}"
        kind = _classify(msg)

        if kind == "stream":
            # Token-level streaming — hand straight to the ChannelManager, which
            # coalesces deltas into edits (or drops them for channels that can't
            # stream). Throttling here would shred the stream.
            self.registry.outbound_seen(session_key, final=False)
            await self.edge_bus.publish_outbound(msg)
            return

        if kind == "progress":
            self.registry.outbound_seen(session_key, final=False)
            if not self._progress_allowed(session_key):
                return  # throttled discrete ping
            await self.edge_bus.publish_outbound(msg)
            return

        # Final answer for this turn.
        turn = self.registry.get(session_key)
        if (
            self.settings.screenshots.attach_final
            and turn is not None
            and not msg.media
            and (msg.content or "").strip()
        ):
            shot = self.media.newest_screenshot(since_ts=turn.started_at - 2.0)
            if shot is not None:
                copied = self.media.copy_and_fit(shot)
                if copied is not None:
                    msg.media = [str(copied)]
        self.registry.outbound_seen(session_key, final=True)
        self._last_progress_at.pop(session_key, None)
        await self.edge_bus.publish_outbound(msg)

    def _progress_allowed(self, session_key: str) -> bool:
        now = time.time()
        last = self._last_progress_at.get(session_key)
        if last is not None and (now - last) < self.settings.progress.min_interval_s:
            return False
        self._last_progress_at[session_key] = now
        return True

    # ----- keepalive -----

    async def run_keepalive(self) -> None:
        interval = 15.0
        while True:
            await asyncio.sleep(interval)
            try:
                await self._keepalive_tick()
            except Exception:  # noqa: BLE001
                logger.exception("keepalive tick error")

    async def _keepalive_tick(self) -> None:
        now = time.time()
        for turn in self.registry.active():
            silent_since = max(turn.last_outbound_at, turn.started_at)
            if (now - silent_since) < self.settings.progress.keepalive_s:
                continue
            elapsed_min = int((now - turn.started_at) // 60)
            media: list[str] = []
            if self.settings.progress.keepalive_screenshot:
                shot = await self.media.current_view(self.settings.engine_url)
                if shot is not None:
                    media = [str(shot)]
            turn.last_outbound_at = now
            await self.edge_bus.publish_outbound(
                OutboundMessage(
                    channel=turn.channel,
                    chat_id=turn.chat_id,
                    content=(
                        f"Still working on it — {elapsed_min} min elapsed."
                        if elapsed_min
                        else "Still working on it."
                    ),
                    media=media,
                    metadata={"_progress": True},
                )
            )

    # ----- restart notices -----

    async def notify_restarted_turns(self) -> None:
        """Tell chats whose turn died with the previous process."""
        if not self.settings.notify_on_restart:
            self.registry.clear_markers()
            return
        for marker in self.registry.load_stale_markers():
            channel = marker.get("channel")
            chat_id = marker.get("chat_id")
            if not channel or not chat_id:
                continue
            await self.edge_bus.publish_outbound(
                OutboundMessage(
                    channel=str(channel),
                    chat_id=str(chat_id),
                    content=(
                        "I was restarted while working on your last request — "
                        "say 'continue' to pick it back up."
                    ),
                )
            )
        self.registry.clear_markers()
