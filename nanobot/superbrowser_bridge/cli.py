"""Console entry point for the SuperBrowser nanobot agent.

This is the real implementation behind the ``superbrowser-agent`` console
script (see ``[project.scripts]`` in ``pyproject.toml``). It registers the
SuperBrowser tools with a nanobot agent and runs a task against the TS engine
(which must already be listening on ``http://localhost:3100`` — start it with
``superbrowser`` / ``npm start``).

In a source checkout, ``nanobot/run.py`` is a thin shim that calls ``main()``
here. Unlike the old ``run.py``, this module does **not** mutate ``sys.path``:
once the package is installed, ``superbrowser_bridge`` and ``vision_agent``
resolve as ordinary top-level imports, and ``from nanobot import Nanobot``
resolves to the released ``nanobot-ai`` distribution.

Usage:
    superbrowser-agent "Search for the latest AI news and summarize"

    # Pick a workspace dir (defaults to ./workspace, or $SUPERBROWSER_WORKSPACE):
    SUPERBROWSER_WORKSPACE=~/.superbrowser/workspace superbrowser-agent "..."
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path


def _load_env() -> None:
    """Load a ``.env`` (walking up from CWD) before any module reads os.environ.

    The TS server picks these up via node's dotenv; the Python side needs the
    same so VISION_ENABLED / VISION_API_KEY / etc. reach the vision
    preprocessor instead of falling through to the costly image-blocks path.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return  # dotenv optional; env can still be set in the shell.
    # find_dotenv walks up from the CWD, so this finds the repo-root .env when
    # invoked from a checkout, and a project-local .env when installed.
    load_dotenv(find_dotenv(usecwd=True))


def _resolve_workspace(workspace: str | None) -> str:
    """Pick the nanobot workspace dir, creating it if needed."""
    chosen = workspace or os.environ.get("SUPERBROWSER_WORKSPACE") or str(Path.cwd() / "workspace")
    Path(chosen).mkdir(parents=True, exist_ok=True)
    return chosen


async def run_task(task: str, workspace: str | None = None) -> bool:
    """Run a single task through a nanobot agent wired to SuperBrowser tools."""
    from nanobot import Nanobot

    from .memory import Memory, set_orchestrator_memory
    from .tools import register_all_tools
    from .workspaces import provision

    workspace = _resolve_workspace(workspace)

    # Materialize the per-role worker workspaces and their bundled SOUL
    # prompts. In a source checkout this is a no-op; once installed it's what
    # keeps the agents from silently falling back to nanobot's default prompt.
    provision()

    # Uses ~/.nanobot/config.json (set up via `nanobot onboard`).
    bot = Nanobot.from_config(workspace=workspace)

    # Attach orchestrator-side Memory FIRST so the BrowserSessionState created
    # during tool registration is bound to it. Each worker delegation gets its
    # own task_id under /tmp/superbrowser/; cross-task fact promotion is a
    # Phase-2 concern.
    orch_task_id = f"orch-{uuid.uuid4().hex[:8]}"
    memory = Memory(orch_task_id, session_key="superbrowser:cli", role="orchestrator")
    # Expose the orchestrator's Memory to delegation.py's finally block so
    # worker exit can promote findings into this ledger without threading the
    # memory through every delegation call site.
    set_orchestrator_memory(memory)

    register_all_tools(bot, memory=memory)
    print("Registered SuperBrowser tools with nanobot")

    memory_hook = memory.attach(bot)
    print(f"Memory attached: task_id={orch_task_id} role=orchestrator")
    print(f"Task: {task}")
    print("---")

    task_success = False
    try:
        result = await bot.run(task, session_key="superbrowser:cli", hooks=[memory_hook])
        print("\n=== Result ===")
        print(result.content)
        task_success = bool(result.content)
    finally:
        # Distill URL-tagged dead-ends and constraint/preference facts into
        # per-domain site models so the next run on the same site benefits.
        # Safe even on crash: write_task_summary swallows its own errors.
        try:
            memory.write_task_summary(success=task_success)
        except Exception as exc:  # noqa: BLE001 - best-effort summary
            print(f">> task summary write failed: {exc}")
    return task_success


async def _amain(argv: list[str] | None = None, workspace: str | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print('Usage: superbrowser-agent "<task>"')
        print('Example: superbrowser-agent "Search for the latest AI news"')
        return 1
    task = " ".join(argv)
    ok = await run_task(task, workspace=workspace)
    return 0 if ok else 1


def main(argv: list[str] | None = None, workspace: str | None = None) -> None:
    """Sync console-script entry point (console_scripts cannot be coroutines)."""
    _load_env()
    raise SystemExit(asyncio.run(_amain(argv, workspace)))


if __name__ == "__main__":
    main()
