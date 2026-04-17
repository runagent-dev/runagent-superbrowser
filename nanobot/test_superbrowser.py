"""
Two-agent SuperBrowser test — Orchestrator + Browser Worker.

Architecture:
  Orchestrator (this agent):
    - Receives user task
    - Checks site learnings
    - Delegates specific instructions to browser worker
    - Saves learnings from results
    - Reports to user

  Browser Worker (created fresh per task by delegate_browser_task tool):
    - Gets specific instructions
    - Opens browser, writes scripts, extracts data
    - Returns results
    - Fresh session every time — no history pollution

Prerequisites:
  1. Start SuperBrowser server:  cd .. && npm start
  2. nanobot onboard (API keys configured)
  3. Run:  python3 test_superbrowser.py "your task here"
"""

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


async def main():
    from nanobot import Nanobot
    from superbrowser_bridge.orchestrator_tools import register_orchestrator_tools

    # Create the orchestrator with its own workspace
    orchestrator = Nanobot.from_config(
        workspace=str(Path(__file__).parent / "workspace_orchestrator")
    )
    register_orchestrator_tools(orchestrator)

    print("Two-agent SuperBrowser system ready")
    print("  Orchestrator: plans tasks, checks learnings, delegates")
    print("  Browser Worker: fresh instance per task, executes scripts")
    print("=" * 60)

    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
        """ Task: Browse the women's new arrivals section and list the names and prices of the first 5 items displayed.\\nwebsite: https://zara.com"""
        # """ Go to trip.com and find me the cheapest flight from dhaka to bangkok on 30th April 2026 and return on 5th May 2026."""
    )

    print(f"Task: {task}")
    print("-" * 60)

    # Each task gets a unique session key — no stale history
    task_id = uuid.uuid4().hex[:8]
    session_key = f"orchestrator:{task_id}"

    # First turn — send the task
    result = await orchestrator.run(task, session_key=session_key)
    print(f"\nAgent: {result.content}")

    # Multi-turn loop
    while True:
        user_input = input("\nYou: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            break

        result = await orchestrator.run(user_input, session_key=session_key)
        print(f"\nAgent: {result.content}")


if __name__ == "__main__":
    asyncio.run(main())
