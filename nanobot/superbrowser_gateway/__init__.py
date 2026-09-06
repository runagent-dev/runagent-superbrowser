"""superbrowser_gateway — chat channels for SuperBrowser (WhatsApp/Telegram/Discord).

Runs the existing SuperBrowser orchestrator directly on a nanobot MessageBus,
fronted by nanobot's ChannelManager, with a GatewayBridge in between for
progress throttling, screenshot attachment, human-handoff routing, and a small
HTTP surface (health, handoff webhook sink, WhatsApp QR, pairing) on :8460.

Entry point: ``superbrowser-gateway`` (see cli.py). Requires the ``gateway``
extra for WhatsApp/Discord: ``pip install "runagent-superbrowser[gateway]"``
(Telegram works with the base install).
"""

from .config import GatewaySettings, load_settings

__all__ = ["GatewaySettings", "load_settings"]
