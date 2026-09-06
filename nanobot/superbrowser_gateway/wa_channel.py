"""WhatsApp channel subclass that mirrors QR frames to the LoginBroker.

neonize's ``client.qr`` decorator REPLACES the registered callback (verified:
``neonize.aioze.events`` stores a single ``self._qr``), so re-registering after
``super()._register_handlers`` swaps in our handler. It replicates the base
class's terminal QR (segno) — the terminal UX is unchanged — and additionally
publishes the raw QR payload to the broker for the web console.

Only the ``qr`` callback is re-registered. Re-registering the Connected/
Disconnected/PairStatus events would REPLACE the base handlers and break login
bookkeeping — the broker gets connection status by polling instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nanobot.channels.whatsapp import WhatsAppChannel

from .login_broker import LoginBroker


class GatewayWhatsAppChannel(WhatsAppChannel):
    def __init__(self, config: Any, bus: Any, *, broker: LoginBroker):
        super().__init__(config, bus)
        self._broker = broker

    def _register_handlers(
        self,
        client: Any,
        *,
        login_result: "asyncio.Future[None] | None" = None,
        handle_messages: bool,
    ) -> None:
        super()._register_handlers(client, login_result=login_result, handle_messages=handle_messages)
        broker = self._broker

        @client.qr
        async def _on_qr(_: Any, qr_data: bytes) -> None:
            import segno

            self.logger.info("Scan the WhatsApp QR code with Linked Devices")
            segno.make_qr(qr_data).terminal(compact=True)
            try:
                broker.publish_qr(qr_data)
            except Exception:  # noqa: BLE001 - console mirror is best-effort
                self.logger.exception("QR broker publish failed")
