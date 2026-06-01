"""
Run the SuperBrowser agent via nanobot library.

Usage:
    # Start the SuperBrowser HTTP server first:
    #   cd /root/agentic-browser/runagent-superbrowser && npm start
    #
    # Then run this script:
    #   python nanobot/run.py "Search for latest AI news and summarize"
    #   python nanobot/run.py "Go to github.com and find trending repos"

This uses nanobot's library directly (not MCP) to register
SuperBrowser tools and run tasks.
"""

import asyncio
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

# Load the project-root .env BEFORE any module reads os.environ. The TS
# server picks these up via node dotenv; Python was previously missing
# the same step, which meant VISION_ENABLED / VISION_API_KEY / etc. never
# reached the Python vision preprocessor. Result: every browser tool call
# fell through to the legacy image-blocks path and nanobot's brain paid
# image tokens on every screenshot.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # dotenv optional; env can still be set in the shell.


async def main():
    import uuid

    from nanobot import Nanobot
    from superbrowser_bridge.memory import Memory, set_orchestrator_memory
    from superbrowser_bridge.tools import register_all_tools

    # Get task from command line
    if len(sys.argv) < 2:
        print("Usage: python run.py <task>")
        print('Example: python run.py "Search for the latest AI news"')
        sys.exit(1)

    task = " ".join(sys.argv[1:])

    # Uses ~/.nanobot/config.json (set up via `nanobot onboard`)
    bot = Nanobot.from_config(workspace=str(Path(__file__).parent / "workspace"))

    # Attach orchestrator-side Memory FIRST so the BrowserSessionState
    # created during tool registration is bound to it. Each worker
    # delegation generates its own task_id under /tmp/superbrowser/;
    # cross-task fact promotion is a Phase-2 concern.
    orch_task_id = f"orch-{uuid.uuid4().hex[:8]}"
    memory = Memory(orch_task_id, session_key="superbrowser:cli", role="orchestrator")
    # Phase 3 — expose the orchestrator's Memory to delegation.py's
    # finally block so worker exit can promote findings into this ledger
    # without threading the memory through every delegation call site.
    set_orchestrator_memory(memory)

    # Register SuperBrowser tools (uses library, not MCP)
    register_all_tools(bot, memory=memory)
    print(f"Registered SuperBrowser tools with nanobot")

    memory_hook = memory.attach(bot)
    print(f"Memory attached: task_id={orch_task_id} role=orchestrator")
    print(f"Task: {task}")
    print("---")

    # Run the task
    task_success = False
    try:
        result = await bot.run(task, session_key="superbrowser:cli", hooks=[memory_hook])
        print("\n=== Result ===")
        print(result.content)
        task_success = bool(result.content)
    finally:
        # Phase 6 — distill any URL-tagged dead-ends and constraint/
        # preference facts into per-domain site models so the next
        # run on the same site benefits from this run's lessons.
        # Safe even on crash: write_task_summary swallows its errors.
        try:
            memory.write_task_summary(success=task_success)
        except Exception as exc:
            print(f">> task summary write failed: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
