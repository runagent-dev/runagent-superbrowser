"""Gateway composition root.

Startup order matters:
1. channels merge into ~/.nanobot/config.json (AFTER the LLM bootstrap, so the
   B64 wholesale-replace path can't wipe the channels section),
2. brain (orchestrator + its bus),
3. edge bus + ChannelManager (with the QR-relaying WhatsApp swap),
4. bridge pumps, brokers, supervisor, HTTP surface,
then run everything under one asyncio.gather with signal-driven shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path

from loguru import logger

from .bridge import ActiveTurnRegistry, GatewayBridge
from .brain import OrchestratorBrain
from .channels_setup import ChannelSupervisor, GatewayChannelManager
from .config import GatewaySettings
from .handoff import HandoffInbox, HandoffRouter
from .login_broker import LoginBroker
from .media import MediaService
from .nanobot_channels import sync_channels_into_nanobot_config
from .webhook_server import build_app, start_server


async def run_gateway(settings: GatewaySettings, *, homed: bool = False) -> None:
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import load_config

    config_path = None
    if homed and settings.home is not None:
        from nanobot.config.loader import get_config_path

        config_path = get_config_path()  # already pointed at <home>/config.json by the CLI
    sync_channels_into_nanobot_config(settings, config_path=config_path, homed=homed)

    brain = OrchestratorBrain(settings).build()

    edge_bus = MessageBus()
    broker = LoginBroker()
    nanobot_config = load_config()
    manager = GatewayChannelManager(nanobot_config, edge_bus, broker=broker)
    if not manager.channels:
        logger.warning(
            "no channels enabled — enable one in ~/.superbrowser/config.json "
            "(gateway.channels.telegram/whatsapp/discord) and run `superbrowser-gateway setup`"
        )

    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    media = MediaService(
        data_dir / "media",
        max_side=settings.screenshots.max_side,
        jpeg_quality=settings.screenshots.jpeg_quality,
    )
    registry = ActiveTurnRegistry(data_dir / "active_turns.json")
    bridge = GatewayBridge(
        agent_bus=brain.bus,
        edge_bus=edge_bus,
        settings=settings,
        media=media,
        registry=registry,
    )
    inbox = HandoffInbox()
    router = HandoffRouter(
        inbox=inbox,
        registry=registry,
        edge_bus=edge_bus,
        settings=settings,
        media=media,
        broker=broker,
    )
    supervisor = ChannelSupervisor(manager)

    app = build_app(
        settings=settings,
        broker=broker,
        registry=registry,
        inbox=inbox,
        router=router,
        bridge=bridge,
        agent_bus=brain.bus,
    )
    runner = await start_server(app, bind=settings.bind, port=settings.port)

    await bridge.notify_restarted_turns()

    async def _owner_pairing_notify(entry: dict) -> None:
        from nanobot.bus.events import OutboundMessage

        for channel, ids in (settings.owners or {}).items():
            for chat_id in ids:
                await edge_bus.publish_outbound(
                    OutboundMessage(
                        channel=channel,
                        chat_id=str(chat_id),
                        content=(
                            f"New pairing request on {entry.get('channel')}: sender "
                            f"{entry.get('sender_id')} — approve with /pairing approve {entry.get('code')}"
                        ),
                    )
                )

    tasks = [
        asyncio.create_task(brain.loop.run(), name="agent-loop"),
        asyncio.create_task(manager.start_all(), name="channels"),
        asyncio.create_task(bridge.run_inbound_pump(), name="bridge-in"),
        asyncio.create_task(bridge.run_outbound_pump(), name="bridge-out"),
        asyncio.create_task(bridge.run_keepalive(), name="bridge-keepalive"),
        asyncio.create_task(broker.run_status_poller(manager.channels), name="status-poller"),
        asyncio.create_task(broker.run_pairing_poller(on_new=_owner_pairing_notify), name="pairing-poller"),
        asyncio.create_task(supervisor.run(), name="channel-supervisor"),
    ]

    stop_event = asyncio.Event()

    def _on_signal() -> None:
        logger.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, _on_signal)

    try:
        # The gateway exits on a signal or on a CRASHED component. Benign task
        # completion parks instead: `start_all` returns immediately with zero
        # channels (the console must stay up so channels can be configured),
        # and it also returns when a channel exits while the supervisor is
        # restarting it.
        stop_waiter = asyncio.create_task(stop_event.wait(), name="stop-waiter")
        waiting = set(tasks)
        while True:
            done, _pending = await asyncio.wait(
                [stop_waiter, *waiting], return_when=asyncio.FIRST_COMPLETED
            )
            if stop_waiter in done:
                break
            crashed = False
            for finished in done:
                waiting.discard(finished)
                if finished.exception() is not None:
                    logger.error(
                        "gateway task {} crashed: {}", finished.get_name(), finished.exception()
                    )
                    crashed = True
                else:
                    logger.info("gateway task {} completed", finished.get_name())
            if crashed:
                break
            if not waiting:
                logger.warning("all gateway tasks completed — idling until a signal arrives")
                await stop_event.wait()
                break
    finally:
        stop_waiter.cancel()
        for task in tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            brain.loop.stop()
        with contextlib.suppress(Exception):
            await manager.stop_all()
        with contextlib.suppress(Exception):
            await runner.cleanup()
        logger.info("gateway stopped")


def main_async_entry(settings: GatewaySettings, *, homed: bool = False) -> None:
    asyncio.run(run_gateway(settings, homed=homed))
