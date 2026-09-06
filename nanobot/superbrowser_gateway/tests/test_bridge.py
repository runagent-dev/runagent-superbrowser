"""Bridge middleware tests — two real MessageBus instances, no LLM, no channels."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus

from superbrowser_gateway.bridge import ActiveTurnRegistry, GatewayBridge
from superbrowser_gateway.config import GatewaySettings
from superbrowser_gateway.media import MediaService


def _settings(**overrides) -> GatewaySettings:
    settings = GatewaySettings()
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _make_bridge(tmp_path: Path, settings: GatewaySettings | None = None):
    agent_bus, edge_bus = MessageBus(), MessageBus()
    media = MediaService(tmp_path / "media")
    registry = ActiveTurnRegistry(tmp_path / "active_turns.json")
    bridge = GatewayBridge(
        agent_bus=agent_bus,
        edge_bus=edge_bus,
        settings=settings or _settings(),
        media=media,
        registry=registry,
    )
    return bridge, agent_bus, edge_bus, registry, media


def _inbound(content: str, chat: str = "111") -> InboundMessage:
    return InboundMessage(channel="test", sender_id=chat, chat_id=chat, content=content)


def _fake_screenshot(tmp_path: Path, name: str = "001-final.jpg") -> Path:
    from PIL import Image

    directory = tmp_path / "shots"
    directory.mkdir(exist_ok=True)
    path = directory / name
    Image.new("RGB", (32, 32), (255, 77, 0)).save(path, "JPEG")
    return path


async def _pump_once(pump_coro) -> asyncio.Task:
    task = asyncio.create_task(pump_coro)
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return task


def test_inbound_forwarded_and_turn_registered(tmp_path: Path) -> None:
    async def main():
        bridge, agent_bus, edge_bus, registry, _ = _make_bridge(tmp_path)
        await edge_bus.publish_inbound(_inbound("book a flight"))
        await _pump_once(bridge.run_inbound_pump())
        assert agent_bus.inbound_size == 1
        forwarded = await agent_bus.consume_inbound()
        assert forwarded.content == "book a flight"
        assert registry.get("test:111") is not None

    asyncio.run(main())


def test_peek_intercepted_no_agent_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def main():
        bridge, agent_bus, edge_bus, registry, _ = _make_bridge(tmp_path)
        shot = _fake_screenshot(tmp_path)
        monkeypatch.setenv("SUPERBROWSER_SCREENSHOT_DIR", str(shot.parent))
        await edge_bus.publish_inbound(_inbound("/peek"))
        await _pump_once(bridge.run_inbound_pump())
        assert agent_bus.inbound_size == 0  # never reached the agent
        reply = await edge_bus.consume_outbound()
        assert reply.media and Path(reply.media[0]).exists()
        assert registry.get("test:111") is None  # commands do not open turns

    asyncio.run(main())


def test_queued_notice_once_when_other_turn_active(tmp_path: Path) -> None:
    async def main():
        bridge, agent_bus, edge_bus, _, _ = _make_bridge(tmp_path)
        await edge_bus.publish_inbound(_inbound("task one", chat="111"))
        await edge_bus.publish_inbound(_inbound("task two", chat="222"))
        await edge_bus.publish_inbound(_inbound("follow-up", chat="222"))
        await _pump_once(bridge.run_inbound_pump())
        assert agent_bus.inbound_size == 3  # all forwarded regardless
        notices = []
        while edge_bus.outbound_size:
            notices.append(await edge_bus.consume_outbound())
        assert len(notices) == 1  # only chat 222 got it, only once
        assert notices[0].chat_id == "222"
        assert "queued" in notices[0].content

    asyncio.run(main())


def test_progress_throttled_and_final_attaches_screenshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main():
        settings = _settings()
        settings.progress.min_interval_s = 3600  # only the first progress passes
        bridge, agent_bus, edge_bus, registry, _ = _make_bridge(tmp_path, settings)

        await edge_bus.publish_inbound(_inbound("browse something"))
        await _pump_once(bridge.run_inbound_pump())
        await agent_bus.consume_inbound()

        shot = _fake_screenshot(tmp_path)
        monkeypatch.setenv("SUPERBROWSER_SCREENSHOT_DIR", str(shot.parent))

        for n in range(3):
            await agent_bus.publish_outbound(
                OutboundMessage(channel="test", chat_id="111", content=f"step {n}", metadata={"_progress": True})
            )
        await agent_bus.publish_outbound(
            OutboundMessage(channel="test", chat_id="111", content="All done — the answer is 42.")
        )
        await _pump_once(bridge.run_outbound_pump())

        delivered = []
        while edge_bus.outbound_size:
            delivered.append(await edge_bus.consume_outbound())
        progress = [m for m in delivered if m.metadata.get("_progress")]
        finals = [m for m in delivered if not m.metadata.get("_progress")]
        assert len(progress) == 1  # throttled to the first
        assert len(finals) == 1
        assert finals[0].media, "final reply should carry the newest screenshot"
        assert Path(finals[0].media[0]).exists()
        assert registry.get("test:111") is None  # turn closed

    asyncio.run(main())


def test_stream_deltas_pass_through_untouched(tmp_path: Path) -> None:
    async def main():
        settings = _settings()
        settings.progress.min_interval_s = 3600  # throttle would block if misclassified
        bridge, agent_bus, edge_bus, registry, _ = _make_bridge(tmp_path, settings)
        await edge_bus.publish_inbound(_inbound("stream me"))
        await _pump_once(bridge.run_inbound_pump())
        await agent_bus.consume_inbound()

        # 5 token deltas — nanobot's ChannelManager coalesces these; the bridge
        # must forward ALL of them, never throttle like a discrete progress ping.
        for n in range(5):
            await agent_bus.publish_outbound(
                OutboundMessage(channel="test", chat_id="111", content=f"tok{n}", metadata={"_stream_delta": True})
            )
        await agent_bus.publish_outbound(
            OutboundMessage(channel="test", chat_id="111", content="", metadata={"_stream_end": True})
        )
        await _pump_once(bridge.run_outbound_pump())

        delivered = []
        while edge_bus.outbound_size:
            delivered.append(await edge_bus.consume_outbound())
        deltas = [m for m in delivered if m.metadata.get("_stream_delta")]
        ends = [m for m in delivered if m.metadata.get("_stream_end")]
        assert len(deltas) == 5, "all stream deltas must pass through (not throttled)"
        assert len(ends) == 1

    asyncio.run(main())


def test_final_without_turn_or_content_untouched(tmp_path: Path) -> None:
    async def main():
        bridge, agent_bus, edge_bus, _, _ = _make_bridge(tmp_path)
        await agent_bus.publish_outbound(OutboundMessage(channel="test", chat_id="999", content="hello"))
        await _pump_once(bridge.run_outbound_pump())
        out = await edge_bus.consume_outbound()
        assert out.content == "hello" and not out.media

    asyncio.run(main())


def test_keepalive_fires_for_silent_turn(tmp_path: Path) -> None:
    async def main():
        settings = _settings()
        settings.progress.keepalive_s = 0.0  # any silence triggers
        bridge, _, edge_bus, registry, _ = _make_bridge(tmp_path, settings)
        turn = registry.turn_started(_inbound("slow task"))
        turn.started_at = time.time() - 120
        await bridge._keepalive_tick()
        note = await edge_bus.consume_outbound()
        assert "Still working" in note.content
        assert note.metadata.get("_progress")

    asyncio.run(main())


def test_restart_markers_notify_and_clear(tmp_path: Path) -> None:
    async def main():
        bridge, _, edge_bus, registry, _ = _make_bridge(tmp_path)
        registry.turn_started(_inbound("interrupted task"))

        # simulate a fresh process picking up the old marker file
        bridge2, _, edge_bus2, registry2, _ = _make_bridge(tmp_path)
        await bridge2.notify_restarted_turns()
        notice = await edge_bus2.consume_outbound()
        assert "restarted" in notice.content
        assert registry2.load_stale_markers() == []  # cleared

    asyncio.run(main())
