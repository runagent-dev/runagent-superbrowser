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


async def _run_and_capture(
    orchestrator,
    task: str,
    session_key: str,
    *,
    hooks: list | None = None,
) -> str:
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

    ``hooks`` is the MemoryHook list. Passing it through here means the
    orchestrator's per-iteration screenshot back-patch / failure-collapse
    / ledger injection actually fires on every turn.
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
        result = await orchestrator.run(task, session_key=session_key, hooks=hooks)
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
    from superbrowser_bridge.memory import Memory, set_orchestrator_memory
    from superbrowser_bridge.orchestrator_tools import register_orchestrator_tools

    # Create the orchestrator with its own workspace
    orchestrator = Nanobot.from_config(
        workspace=str(Path(__file__).parent / "workspace_orchestrator")
    )
    register_orchestrator_tools(orchestrator)

    # Each task gets a unique session key — no stale history.
    # Generate it first so the orchestrator's Memory can bind to it.
    task_id = uuid.uuid4().hex[:8]
    session_key = f"orchestrator:{task_id}"

    # Wire orchestrator-side Memory. Registers the four memory_* recall
    # tools on the orchestrator and returns the MemoryHook the run loop
    # composes with each iteration (screenshot back-patch, failure
    # collapse, ledger injection into the system message). The
    # orchestrator's ledger lives at
    # /tmp/superbrowser/orch-{task_id}/memory/; each delegated worker
    # writes to its own /tmp/superbrowser/{worker_task_id}/memory/.
    orch_task_id = f"orch-{task_id}"
    orch_memory = Memory(orch_task_id, session_key=session_key, role="orchestrator")
    # Phase 3 — expose orchestrator memory to delegation.py's finally
    # block so worker exit can promote findings into this ledger.
    set_orchestrator_memory(orch_memory)
    orch_hook = orch_memory.attach(orchestrator)

    print("Two-agent SuperBrowser system ready")
    print("  Orchestrator: plans tasks, checks learnings, delegates")
    print("  Browser Worker: fresh instance per task, executes scripts")
    print(f"  Memory: task_id={orch_task_id} role=orchestrator")
    print("=" * 60)

    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
        """ must go to this site https://www.chess.com/play/computer/chessbacca-BOT . Try to win Please. Make sure to drag properly with your tools."""
        # """ Go to trip.com and find me the cheapest flight from dhaka to bangkok on 30th April 2026 and return on 5th May 2026."""
    )

    print(f"Task: {task}")
    print("-" * 60)

    # Seed the orchestrator's ledger with the user task so render_for_llm
    # has a goal to anchor on from the first turn.
    orch_memory.set_goal(task[:300])

    # First turn — send the task
    content = await _run_and_capture(
        orchestrator, task, session_key, hooks=[orch_hook]
    )
    print(f"\nAgent: {content}")

    # Multi-turn loop
    try:
        while True:
            user_input = input("\nYou: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                break

            content = await _run_and_capture(
                orchestrator, user_input, session_key, hooks=[orch_hook]
            )
            print(f"\nAgent: {content}")
    finally:
        # Phase 6 — distill the session's URL-tagged dead-ends and
        # constraint/derived facts into per-domain site_models so
        # repeat runs benefit from this session's lessons.
        try:
            orch_memory.write_task_summary(success=True)
        except Exception as exc:
            print(f">> task summary write failed: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
