"""ChannelManager wiring: WhatsApp swap-in + a light restart supervisor.

Entry-point channel plugins cannot shadow built-in names (nanobot registry
skips duplicates), so the QR-relaying subclass is swapped INTO the manager's
channel dict after normal init — bus routing keys and config sections stay
untouched, and the per-channel send flags the manager resolved are copied over.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from nanobot.channels.manager import ChannelManager

from .login_broker import LoginBroker


class GatewayChannelManager(ChannelManager):
    def __init__(self, config: Any, bus: Any, *, broker: LoginBroker, **kwargs: Any):
        self._broker = broker
        super().__init__(config, bus, **kwargs)

    def _init_channels(self) -> None:
        super()._init_channels()
        original = self.channels.get("whatsapp")
        if original is None:
            return
        try:
            from .wa_channel import GatewayWhatsAppChannel

            section = getattr(self.config.channels, "whatsapp", None)
            replacement = GatewayWhatsAppChannel(section, self.bus, broker=self._broker)
            for flag in ("send_progress", "send_tool_hints", "show_reasoning"):
                setattr(replacement, flag, getattr(original, flag))
            self.channels["whatsapp"] = replacement
            logger.info("WhatsApp channel upgraded with console QR relay")
        except Exception:  # noqa: BLE001 - fall back to the stock channel
            logger.exception("could not swap in GatewayWhatsAppChannel; stock channel kept")


class ChannelSupervisor:
    """Restart channels whose start() returned (e.g. WhatsApp logged out).

    ``ChannelManager.start_all`` runs each channel once and gathers forever; a
    channel that exits stays down. This poller watches ``is_running`` and
    re-spawns ``channel.start()`` with exponential backoff. Status surfacing
    (terminal + console) happens via the LoginBroker's own poller.
    """

    def __init__(self, manager: ChannelManager, *, min_backoff_s: float = 5.0, max_backoff_s: float = 300.0):
        self.manager = manager
        self.min_backoff_s = min_backoff_s
        self.max_backoff_s = max_backoff_s
        self._backoff: dict[str, float] = {}
        self._down_since: dict[str, float] = {}
        self._restarts: dict[str, asyncio.Task] = {}

    async def run(self, interval_s: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(interval_s)
            for name, channel in self.manager.channels.items():
                try:
                    if getattr(channel, "is_running", False):
                        self._down_since.pop(name, None)
                        self._backoff.pop(name, None)
                        continue
                    pending = self._restarts.get(name)
                    if pending is not None and not pending.done():
                        continue
                    first_seen = self._down_since.setdefault(name, loop.time())
                    backoff = self._backoff.get(name, self.min_backoff_s)
                    if (loop.time() - first_seen) < backoff:
                        continue
                    logger.warning("channel {} is down — restarting (backoff {}s)", name, int(backoff))
                    self._backoff[name] = min(backoff * 2, self.max_backoff_s)
                    self._down_since[name] = loop.time()
                    self._restarts[name] = asyncio.create_task(self._restart(name, channel))
                except Exception:  # noqa: BLE001
                    logger.exception("supervisor error for channel {}", name)

    async def _restart(self, name: str, channel: Any) -> None:
        try:
            await channel.start()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("channel {} restart failed", name)
