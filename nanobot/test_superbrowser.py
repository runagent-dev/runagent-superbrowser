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
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))


async def _run_and_capture(orchestrator, task: str, session_key: str) -> str:
    """Run the orchestrator and return the user-visible content.

    `Nanobot.run()` returns `result.content`, which is the LLM's direct
    text output. When the orchestrator (or any nested agent) delivers
    its answer via the `message()` tool instead of direct text, the
    content goes out through the bus and `result.content` is an empty
    string — so this test harness would print a blank "Agent:" line.

    Fix: subscribe to the orchestrator's outbound bus during the turn,
    collect any non-progress / non-streaming messages, and fall back to
    the bus-captured content when `result.content` is empty. First
    non-empty content wins; matches what the CLI channel renders.
    """
    bus = orchestrator._loop.bus
    captured: list[str] = []
    stop = asyncio.Event()

    async def _pump() -> None:
        while not stop.is_set():
            try:
                msg = await asyncio.wait_for(bus.consume_outbound(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            md = msg.metadata or {}
            # Skip progress pings, streaming deltas/ends, and the empty
            # "turn complete" marker — those aren't the final answer.
            if md.get("_progress") or md.get("_stream_delta") or md.get("_stream_end"):
                continue
            if msg.content:
                captured.append(msg.content)

    pump = asyncio.create_task(_pump())
    try:
        result = await orchestrator.run(task, session_key=session_key)
    finally:
        # Give the pump a beat to drain any still-in-flight message.
        await asyncio.sleep(0.05)
        stop.set()
        pump.cancel()
        try:
            await pump
        except (asyncio.CancelledError, Exception):
            pass

    direct = (result.content or "").strip() if result else ""
    if direct:
        return direct
    if captured:
        # Last capture wins — the final message tool call is usually the
        # definitive answer (earlier ones may be progress breadcrumbs).
        return captured[-1]
    return ""


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
        """ must go to this site https://spothero.com/ Find parking near the San Francisco Museum of Modern Art for next Sunday from 1:00 PM to 5:00 PM. I'm driving a Ford F-150 and need a garage that allows in-and-out privileges. If there are multiple options, show me the details of the one with the lowest price. Use browser tools. """
        # """ Go to trip.com and find me the cheapest flight from dhaka to bangkok on 30th April 2026 and return on 5th May 2026."""
    )

    print(f"Task: {task}")
    print("-" * 60)

    # Each task gets a unique session key — no stale history
    task_id = uuid.uuid4().hex[:8]
    session_key = f"orchestrator:{task_id}"

    # First turn — send the task
    content = await _run_and_capture(orchestrator, task, session_key)
    print(f"\nAgent: {content}")

    # Multi-turn loop
    while True:
        user_input = input("\nYou: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            break

        content = await _run_and_capture(orchestrator, user_input, session_key)
        print(f"\nAgent: {content}")


if __name__ == "__main__":
    asyncio.run(main())
