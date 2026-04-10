"""
Test SuperBrowser with nanobot — multi-turn interactive session.

Prerequisites:
  1. Start SuperBrowser server:  cd .. && npm start
  2. nanobot onboard (set up API keys)
  3. Run:  python3 test_superbrowser.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


async def main():
    from nanobot import Nanobot
    from superbrowser_bridge.tools import register_all_tools

    bot = Nanobot.from_config(workspace=str(Path(__file__).parent / "workspace"))
    register_all_tools(bot)

    print("SuperBrowser tools registered with nanobot")
    print("=" * 60)

    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
        "can you go to gozayaan and search for Dhaka to Chattogram flight ticket on 14th April 2026 if found any? find from gozayaan only. and list me the price of air astra flight only"
    )


    print(f"Task: {task}")
    print("-" * 60)

    session_key = "test:superbrowser"

    # First turn — send the task
    result = await bot.run(task, session_key=session_key)
    print(f"\nAgent: {result.content}")

    # Multi-turn loop — keep talking until agent is done
    while True:
        user_input = input("\nYou: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ('quit', 'exit', 'q'):
            break

        result = await bot.run(user_input, session_key=session_key)
        print(f"\nAgent: {result.content}")


if __name__ == "__main__":
    asyncio.run(main())
