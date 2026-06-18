"""Construct a provisioned orchestrator Nanobot wired to SuperBrowser tools.

Mirrors what ``nanobot/test_superbrowser.py`` + ``superbrowser_bridge/cli.py``
do, but as a reusable function. All ``superbrowser_bridge`` imports are
function-level so the SDK can set env vars (``SUPERBROWSER_URL``,
``SUPERBROWSER_WORKSPACE_ROOT``, ``VISION_*``) *before* the bridge is imported
and its module-level path/URL constants freeze.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .modes import Mode, apply_mode


@dataclass(slots=True)
class Orchestrator:
    bot: Any
    memory: Any
    hook: Any
    task_id: str
    session_key: str
    directive: str


def _bot_from_config(workspace: str, model: str | None) -> Any:
    """``Nanobot.from_config`` with a best-effort model override.

    ``from_config`` has no model parameter (model lives in
    ``~/.nanobot/config.json`` under ``agents.defaults.model``). To honour
    ``SuperBrowser(model=...)`` without editing nanobot, we patch that field in
    a temporary copy of the config and point ``from_config`` at it. Any failure
    falls back to the unmodified config — model override is a convenience, not a
    guarantee.
    """
    from nanobot import Nanobot

    if not model:
        return Nanobot.from_config(workspace=workspace)
    import json
    import os
    import tempfile

    try:
        src = os.path.expanduser("~/.nanobot/config.json")
        data = json.load(open(src)) if os.path.exists(src) else {}
        data.setdefault("agents", {}).setdefault("defaults", {})["model"] = model
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="sb-config-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            return Nanobot.from_config(config_path=tmp, workspace=workspace)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:  # noqa: BLE001 - never let model override break construction
        try:
            from loguru import logger

            logger.warning(
                "SuperBrowser(model={!r}) could not be applied; using the model "
                "from ~/.nanobot/config.json instead.",
                model,
            )
        except Exception:  # noqa: BLE001
            pass
        return Nanobot.from_config(workspace=workspace)


def build_orchestrator(
    *,
    mode: Mode,
    task: str,
    model: str | None = None,
    provision_force: bool = False,
) -> Orchestrator:
    """Provision workspaces, build the orchestrator bot, register tools, attach
    memory, and apply the mode. Returns everything the caller needs to run."""
    from superbrowser_bridge.memory import Memory, set_orchestrator_memory
    from superbrowser_bridge.orchestrator_tools import register_orchestrator_tools
    from superbrowser_bridge.workspaces import provision, workspace_for

    provision(force=provision_force)

    bot = _bot_from_config(str(workspace_for("orchestrator")), model)
    register_orchestrator_tools(bot)
    directive = apply_mode(bot, mode)

    short = uuid.uuid4().hex[:8]
    task_id = f"orch-{short}"
    session_key = f"orchestrator:{short}"
    memory = Memory(task_id, session_key=session_key, role="orchestrator")
    # Exposed to delegation.py's finally block so worker findings get promoted
    # into this orchestrator ledger (matches cli.py's wiring).
    set_orchestrator_memory(memory)
    hook = memory.attach(bot)
    if task:
        try:
            memory.set_goal(task[:300])
        except Exception:  # noqa: BLE001 - goal seeding is best effort
            pass

    return Orchestrator(
        bot=bot,
        memory=memory,
        hook=hook,
        task_id=task_id,
        session_key=session_key,
        directive=directive,
    )
