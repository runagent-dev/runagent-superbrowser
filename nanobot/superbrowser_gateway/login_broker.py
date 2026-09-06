"""LoginBroker — channel login/pairing state for terminals AND the console.

Holds the latest WhatsApp QR frame (raw payload + PNG), tracks per-channel
connection status by polling the live channel objects, and watches nanobot's
pairing store for pending approval codes. Everything is published to
subscribers as small JSON events (the console's SSE/WS feed), and pending
pairings can be forwarded to owner chats.
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from typing import Any, AsyncIterator

from loguru import logger

QR_FRESH_S = 60.0  # neonize rotates QR frames well inside this window


class LoginBroker:
    def __init__(self) -> None:
        self._qr_data: bytes | None = None
        self._qr_at: float = 0.0
        self._statuses: dict[str, str] = {}
        self._pending_pairings: list[dict[str, Any]] = []
        self._subscribers: list[asyncio.Queue] = []

    # ----- QR -----

    def publish_qr(self, qr_data: bytes) -> None:
        self._qr_data = qr_data
        self._qr_at = time.time()
        self._emit({"topic": "qr.whatsapp", "data": {"qr": qr_data.decode("utf-8", "replace"), "at": self._qr_at}})

    def qr_fresh(self) -> bool:
        return self._qr_data is not None and (time.time() - self._qr_at) < QR_FRESH_S

    def qr_png(self) -> bytes | None:
        if not self.qr_fresh() or self._qr_data is None:
            return None
        try:
            import segno
        except ImportError:  # gateway extra not installed — console shows raw payload instead
            logger.warning("segno not installed; QR PNG unavailable (pip install 'runagent-superbrowser[gateway]')")
            return None

        buf = io.BytesIO()
        segno.make_qr(self._qr_data).save(buf, kind="png", scale=6, border=2)
        return buf.getvalue()

    def qr_png_b64(self) -> str | None:
        png = self.qr_png()
        return base64.b64encode(png).decode() if png else None

    # ----- status -----

    def snapshot(self) -> dict[str, Any]:
        return {
            "channels": dict(self._statuses),
            "qrFresh": self.qr_fresh(),
            "qrAgeS": round(time.time() - self._qr_at, 1) if self._qr_data else None,
            "pendingPairings": list(self._pending_pairings),
        }

    async def run_status_poller(self, channels: dict[str, Any], interval_s: float = 1.0) -> None:
        """Derive per-channel status transitions from the live channel objects."""
        while True:
            await asyncio.sleep(interval_s)
            try:
                for name, channel in channels.items():
                    status = self._derive_status(name, channel)
                    if self._statuses.get(name) != status:
                        self._statuses[name] = status
                        logger.info("channel {} -> {}", name, status)
                        self._emit({"topic": "channel.status", "data": {"channel": name, "status": status}})
            except Exception:  # noqa: BLE001
                logger.exception("status poller error")

    def _derive_status(self, name: str, channel: Any) -> str:
        running = bool(getattr(channel, "is_running", False))
        connected = bool(getattr(channel, "_connected", running))
        if name == "whatsapp":
            if connected:
                return "connected"
            if self.qr_fresh():
                return "waiting_qr"
            return "connecting" if running else "disconnected"
        return "connected" if running and connected else ("connecting" if running else "disconnected")

    # ----- pairing -----

    async def run_pairing_poller(self, interval_s: float = 5.0, on_new=None) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                from nanobot import pairing

                pending = pairing.list_pending()
            except Exception:  # noqa: BLE001 - pairing store unavailable
                continue
            try:
                known = {(p.get("channel"), p.get("sender_id"), p.get("code")) for p in self._pending_pairings}
                fresh = [
                    p for p in pending if (p.get("channel"), p.get("sender_id"), p.get("code")) not in known
                ]
                self._pending_pairings = list(pending)
                for entry in fresh:
                    self._emit({"topic": "pairing.new", "data": entry})
                    if on_new is not None:
                        await on_new(entry)
            except Exception:  # noqa: BLE001
                logger.exception("pairing poller error")

    # ----- subscriptions (console SSE/WS) -----

    def subscribe(self) -> "asyncio.Queue[dict]":
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[dict]") -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    async def events(self) -> AsyncIterator[dict]:
        queue = self.subscribe()
        try:
            while True:
                yield await queue.get()
        finally:
            self.unsubscribe(queue)

    def _emit(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # a stalled console subscriber must not block logins
                self.unsubscribe(queue)
