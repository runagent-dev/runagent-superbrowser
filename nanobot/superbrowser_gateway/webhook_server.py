"""The gateway's HTTP surface (aiohttp, one port).

Phase-3 scope: health, the HANDOFF_WEBHOOK_URL sink, WhatsApp QR (PNG + SSE
event feed), pairing approvals, and chat-turn listing/stop. The web console
SPA and the full config API mount on this same app in a later phase.

Auth mirrors the engine's semantics (src/server/auth.ts): no token configured
→ open; token configured → Bearer/?token required, loopback callers exempt
unless ``gateway.loopbackBypass`` is false. ``POST /api/hooks/handoff`` is
loopback-or-token always (the engine posts from the same host/container).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import web
from loguru import logger

from .bridge import ActiveTurnRegistry, GatewayBridge
from .config import GatewaySettings
from .handoff import HandoffInbox, HandoffRouter
from .login_broker import LoginBroker

_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

SETTINGS_KEY = web.AppKey("settings", GatewaySettings)


def _is_loopback(request: web.Request) -> bool:
    peer = request.remote or ""
    return peer in _LOOPBACK_HOSTS or peer.startswith("127.")


def _request_token(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return request.query.get("token")


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    settings: GatewaySettings = request.app[SETTINGS_KEY]
    # The static SPA shell (non-/api GETs) loads without a token — it then
    # authenticates its API calls. Only /api routes are gated.
    gated = request.path.startswith("/api/")
    if settings.token and gated:
        allowed = (
            _request_token(request) == settings.token
            or (settings.loopback_bypass and _is_loopback(request))
        )
        if not allowed:
            return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


def build_app(
    *,
    settings: GatewaySettings,
    broker: LoginBroker,
    registry: ActiveTurnRegistry,
    inbox: HandoffInbox,
    router: HandoffRouter,
    bridge: GatewayBridge,
    agent_bus,
) -> web.Application:
    app = web.Application(middlewares=[_auth_middleware])
    app[SETTINGS_KEY] = settings

    async def health(_request: web.Request) -> web.Response:
        engine_ok = await _engine_health(settings.engine_url)
        return web.json_response(
            {
                "gateway": "ok",
                "engine": engine_ok,
                "channels": broker.snapshot()["channels"],
                "activeTurns": [t.to_dict() for t in registry.active()],
            }
        )

    async def hooks_handoff(request: web.Request) -> web.Response:
        # The engine posts from the same host/container: loopback or token only.
        if not (_is_loopback(request) or (settings.token and _request_token(request) == settings.token)):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        event = await router.route(payload if isinstance(payload, dict) else {})
        return web.json_response({"ok": True, "id": event.id, "deliveredTo": event.delivered_to})

    async def handoffs_list(_request: web.Request) -> web.Response:
        return web.json_response({"handoffs": inbox.list()})

    async def handoffs_ack(request: web.Request) -> web.Response:
        ok = inbox.ack(request.match_info["id"])
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    async def channels_status(_request: web.Request) -> web.Response:
        return web.json_response(broker.snapshot())

    async def whatsapp_qr(_request: web.Request) -> web.Response:
        png = broker.qr_png()
        if png is None:
            return web.json_response({"error": "no fresh QR — is the WhatsApp channel waiting for login?"}, status=404)
        return web.Response(body=png, content_type="image/png")

    async def events_sse(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await resp.prepare(request)
        await resp.write(b": connected\n\n")
        queue = broker.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                    body = json.dumps(event).encode()
                    await resp.write(b"data: " + body + b"\n\n")
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            broker.unsubscribe(queue)
        return resp

    async def pairing_list(_request: web.Request) -> web.Response:
        try:
            from nanobot import pairing

            return web.json_response({"pending": pairing.list_pending()})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=500)

    async def pairing_approve(request: web.Request) -> web.Response:
        body = await _json_body(request)
        code = str(body.get("code") or "").strip()
        try:
            from nanobot import pairing

            resolved = pairing.approve_code(code)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=500)
        if not resolved:
            return web.json_response({"ok": False, "error": "unknown or expired code"}, status=404)
        channel, sender_id = resolved
        return web.json_response({"ok": True, "channel": channel, "senderId": sender_id})

    async def pairing_deny(request: web.Request) -> web.Response:
        body = await _json_body(request)
        code = str(body.get("code") or "").strip()
        try:
            from nanobot import pairing

            ok = pairing.deny_code(code)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"ok": bool(ok)})

    async def tasks_list(_request: web.Request) -> web.Response:
        return web.json_response({"turns": [t.to_dict() for t in registry.active()]})

    async def tasks_stop(request: web.Request) -> web.Response:
        """Stop a chat turn by injecting nanobot's priority /stop command."""
        session_key = request.match_info["session_key"]
        turn = registry.get(session_key)
        if turn is None:
            return web.json_response({"ok": False, "error": "no active turn"}, status=404)
        from nanobot.bus.events import InboundMessage

        await agent_bus.publish_inbound(
            InboundMessage(
                channel=turn.channel,
                sender_id="gateway-console",
                chat_id=turn.chat_id,
                content="/stop",
            )
        )
        return web.json_response({"ok": True})

    app.router.add_get("/api/health", health)
    app.router.add_post("/api/hooks/handoff", hooks_handoff)
    app.router.add_get("/api/handoffs", handoffs_list)
    app.router.add_post("/api/handoffs/{id}/ack", handoffs_ack)
    app.router.add_get("/api/channels", channels_status)
    app.router.add_get("/api/channels/whatsapp/qr.png", whatsapp_qr)
    app.router.add_get("/api/events", events_sse)
    app.router.add_get("/api/pairing", pairing_list)
    app.router.add_post("/api/pairing/approve", pairing_approve)
    app.router.add_post("/api/pairing/deny", pairing_deny)
    app.router.add_get("/api/tasks", tasks_list)
    app.router.add_post("/api/tasks/{session_key}/stop", tasks_stop)

    # Console REST + static SPA (must be added last: its catch-all "/{path}"
    # route would otherwise shadow the /api routes above).
    from .console_api import mount_console

    mount_console(app, settings)
    return app


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


async def _engine_health(engine_url: str) -> bool:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{engine_url.rstrip('/')}/health")
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


async def start_server(app: web.Application, *, bind: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, bind, port)
    await site.start()
    logger.info("gateway HTTP surface on http://{}:{}", bind, port)
    return runner
