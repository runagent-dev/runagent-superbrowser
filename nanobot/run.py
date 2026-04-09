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


async def main():
    from nanobot import Nanobot
    from superbrowser_bridge.tools import register_all_tools

    # Get task from command line
    if len(sys.argv) < 2:
        print("Usage: python run.py <task>")
        print('Example: python run.py "Search for the latest AI news"')
        sys.exit(1)

    task = " ".join(sys.argv[1:])

    # Uses ~/.nanobot/config.json (set up via `nanobot onboard`)
    bot = Nanobot.from_config(workspace=str(Path(__file__).parent / "workspace"))

    # Register SuperBrowser tools (uses library, not MCP)
    register_all_tools(bot)
    print(f"Registered SuperBrowser tools with nanobot")
    print(f"Task: {task}")
    print("---")

    # Run the task
    result = await bot.run(task, session_key="superbrowser:cli")
    print("\n=== Result ===")
    print(result.content)


if __name__ == "__main__":
    asyncio.run(main())
