"""OrchestratorBrain — the existing SuperBrowser orchestrator, bus-attached.

``build_orchestrator()`` (runagent_superbrowser._runtime) constructs the same
provisioned nanobot agent the SDK uses (SOUL prompts, orchestrator tools,
memory). Its AgentLoop natively consumes a MessageBus — running ``loop.run()``
turns every chat into a persistent conversation (session key
``<channel>:<chat_id>``) with mid-turn follow-up injection, ``/stop``,
``/pairing`` and progress publication, all from nanobot.

Flagged private accesses (pinned dep ``nanobot-ai>=0.2.2,<0.3``):
``orch.bot._loop`` (the Nanobot facade's AgentLoop) and
``loop._extra_hooks`` (per-run hooks list; the memory hook must apply to bus
turns, not only SDK runs).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from .config import GatewaySettings
from .prompts import GATEWAY_AGENTS_MD


@dataclass
class BrainHandle:
    loop: Any  # nanobot AgentLoop
    bus: Any  # its MessageBus (the gateway's agent_bus)
    orch: Any  # runagent_superbrowser._runtime.Orchestrator


class OrchestratorBrain:
    def __init__(self, settings: GatewaySettings):
        self.settings = settings

    def build(self) -> BrainHandle:
        # All three env vars are read at construction time downstream:
        # NANOBOT_MAX_CONCURRENT_REQUESTS in AgentLoop.__init__; SUPERBROWSER_URL
        # and SUPERBROWSER_WORKSPACE_ROOT by bridge modules that freeze them at
        # import. Explicit env still wins (setdefault).
        #
        # The workspace root is ISOLATED under the gateway data dir on purpose:
        # the gateway's AGENTS.md adds chat framing that must never leak into
        # plain SDK runs sharing the default ~/.superbrowser/workspaces.
        os.environ.setdefault("NANOBOT_MAX_CONCURRENT_REQUESTS", str(self.settings.max_concurrent_tasks))
        os.environ.setdefault("SUPERBROWSER_WORKSPACE_ROOT", str(self.settings.data_dir / "workspaces"))
        os.environ["SUPERBROWSER_URL"] = self.settings.engine_url

        from runagent_superbrowser._runtime import build_orchestrator

        orch = build_orchestrator(mode="auto", task="")
        loop = orch.bot._loop  # noqa: SLF001 - flagged private access (see module docstring)
        loop._extra_hooks.append(orch.hook)  # noqa: SLF001 - memory hook on every bus turn

        self._ensure_gateway_agents_md()
        logger.info("orchestrator brain ready (sessions keyed per chat)")
        return BrainHandle(loop=loop, bus=loop.bus, orch=orch)

    def _ensure_gateway_agents_md(self) -> None:
        try:
            from superbrowser_bridge.workspaces import workspace_for

            path = Path(workspace_for("orchestrator")) / "AGENTS.md"
            if not path.exists() or path.read_text() != GATEWAY_AGENTS_MD:
                path.write_text(GATEWAY_AGENTS_MD)
                logger.info("gateway AGENTS.md written to {}", path)
        except Exception:  # noqa: BLE001 - prompt add-on is not load-bearing
            logger.exception("could not write gateway AGENTS.md")
