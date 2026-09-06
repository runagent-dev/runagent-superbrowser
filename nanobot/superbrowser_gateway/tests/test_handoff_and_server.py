"""Handoff routing + HTTP surface tests (aiohttp TestClient, no external services)."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus

from superbrowser_gateway.bridge import ActiveTurnRegistry, GatewayBridge
from superbrowser_gateway.config import GatewaySettings
from superbrowser_gateway.handoff import HandoffInbox, HandoffRouter
from superbrowser_gateway.login_broker import LoginBroker
from superbrowser_gateway.media import MediaService
from superbrowser_gateway.webhook_server import build_app

_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _wiring(tmp_path: Path, settings: GatewaySettings | None = None):
    settings = settings or GatewaySettings()
    agent_bus, edge_bus = MessageBus(), MessageBus()
    media = MediaService(tmp_path / "media")
    registry = ActiveTurnRegistry(tmp_path / "turns.json")
    inbox = HandoffInbox()
    broker = LoginBroker()
    router = HandoffRouter(
        inbox=inbox, registry=registry, edge_bus=edge_bus, settings=settings, media=media, broker=broker
    )
    bridge = GatewayBridge(
        agent_bus=agent_bus, edge_bus=edge_bus, settings=settings, media=media, registry=registry
    )
    app = build_app(
        settings=settings,
        broker=broker,
        registry=registry,
        inbox=inbox,
        router=router,
        bridge=bridge,
        agent_bus=agent_bus,
    )
    return settings, agent_bus, edge_bus, registry, inbox, broker, router, app


def _inbound(content: str, chat: str = "111") -> InboundMessage:
    return InboundMessage(channel="test", sender_id=chat, chat_id=chat, content=content)


# ----- router unit behavior -----


def test_router_targets_active_turn_and_saves_screenshot(tmp_path: Path) -> None:
    async def main():
        _, _, edge_bus, registry, inbox, _, router, _ = _wiring(tmp_path)
        registry.turn_started(_inbound("buy tickets"))
        payload = {
            "event": "human_handoff_required",  # legacy TS event name
            "url": "http://localhost:3100/session/s1/view?token=x",
            "caption": "Captcha needs a human.",
            "screenshot": base64.b64encode(_PNG_1PX).decode(),
            "screenshotMimeType": "image/png",
            "sessionId": "s1",
            "taskId": "orch-1",
        }
        event = await router.route(payload)
        assert event.assist_type == "captcha"
        assert event.screenshot_path and Path(event.screenshot_path).exists()
        delivered = await edge_bus.consume_outbound()
        assert delivered.chat_id == "111"
        assert "http://localhost:3100/session/s1/view" in delivered.content
        assert delivered.media == [event.screenshot_path]
        assert inbox.list()[0]["id"] == event.id

    asyncio.run(main())


def test_router_falls_back_to_owners(tmp_path: Path) -> None:
    async def main():
        settings = GatewaySettings()
        settings.owners = {"test": ["777"]}
        _, _, edge_bus, _, _, _, router, _ = _wiring(tmp_path, settings)
        event = await router.route({"event": "human_assist_required", "assistType": "login", "url": "http://x"})
        delivered = await edge_bus.consume_outbound()
        assert delivered.chat_id == "777"
        assert event.delivered_to == ["test:777"]

    asyncio.run(main())


# ----- HTTP surface -----


def test_http_surface_end_to_end(tmp_path: Path) -> None:
    async def main():
        settings, agent_bus, edge_bus, registry, inbox, broker, router, app = _wiring(tmp_path)
        registry.turn_started(_inbound("long task"))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            # health
            resp = await client.get("/api/health")
            assert resp.status == 200
            body = await resp.json()
            assert body["gateway"] == "ok"
            assert body["activeTurns"][0]["session_key"] == "test:111"

            # handoff sink (loopback → allowed)
            resp = await client.post(
                "/api/hooks/handoff",
                json={"event": "human_assist_required", "url": "http://v", "caption": "help"},
            )
            assert resp.status == 200
            posted = await resp.json()
            assert posted["ok"] and posted["deliveredTo"] == ["test:111"]
            await edge_bus.consume_outbound()  # the routed chat message

            # inbox + ack
            resp = await client.get("/api/handoffs")
            listed = (await resp.json())["handoffs"]
            assert listed and listed[0]["id"] == posted["id"]
            resp = await client.post(f"/api/handoffs/{posted['id']}/ack")
            assert (await resp.json())["ok"] is True

            # QR: none yet → 404; publish → PNG
            resp = await client.get("/api/channels/whatsapp/qr.png")
            assert resp.status == 404
            broker.publish_qr(b"2@fakeqrpayload")
            resp = await client.get("/api/channels/whatsapp/qr.png")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "image/png"

            # tasks list + stop (injects /stop inbound for the agent loop)
            resp = await client.get("/api/tasks")
            assert (await resp.json())["turns"][0]["session_key"] == "test:111"
            resp = await client.post("/api/tasks/test:111/stop")
            assert (await resp.json())["ok"] is True
            stop_msg = await agent_bus.consume_inbound()
            assert stop_msg.content == "/stop" and stop_msg.chat_id == "111"
        finally:
            await client.close()

    asyncio.run(main())


def test_http_auth_token_gate(tmp_path: Path) -> None:
    async def main():
        settings = GatewaySettings()
        settings.token = "sb-test-token"
        settings.loopback_bypass = False  # force the token check even from 127.0.0.1
        *_, app = _wiring(tmp_path, settings)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/health")
            assert resp.status == 401
            resp = await client.get("/api/health", headers={"Authorization": "Bearer sb-test-token"})
            assert resp.status == 200
            resp = await client.get("/api/health?token=sb-test-token")
            assert resp.status == 200
        finally:
            await client.close()

    asyncio.run(main())


def test_hooks_handoff_rejects_bad_json(tmp_path: Path) -> None:
    async def main():
        *_, app = _wiring(tmp_path)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/hooks/handoff", data=b"{not json", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400
        finally:
            await client.close()

    asyncio.run(main())


def test_broker_qr_and_events() -> None:
    async def main():
        broker = LoginBroker()
        queue = broker.subscribe()
        broker.publish_qr(b"2@payload")
        event = queue.get_nowait()
        assert event["topic"] == "qr.whatsapp"
        assert broker.qr_fresh()
        assert broker.qr_png_b64()
        snapshot = broker.snapshot()
        assert snapshot["qrFresh"] is True

    asyncio.run(main())
