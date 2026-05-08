"""Orchestrator tool registration.

`register_orchestrator_tools` wires DelegateBrowserTaskTool +
CheckLearningsTool + SaveLearningTool + the antibot Fetch tools, calls
`register_search_tools`, and unregisters the default `web_search` /
`web_fetch` so all web research has to go through the search worker
(API-based) or the browser worker.
"""

from __future__ import annotations

from .delegation import DelegateBrowserTaskTool
from .learnings_tools import CheckLearningsTool, SaveLearningTool


def register_orchestrator_tools(bot: "Nanobot") -> None:
    """Register orchestrator-specific tools (delegation + learnings + search)."""
    from superbrowser_bridge.search_tools import register_search_tools
    from superbrowser_bridge.antibot import (
        FetchArchiveTool,
        FetchAutoTool,
        FetchImpersonateTool,
        FetchUndetectedTool,
    )

    tools = [
        DelegateBrowserTaskTool(),
        CheckLearningsTool(),
        SaveLearningTool(),
        FetchAutoTool(),
        FetchImpersonateTool(),
        FetchUndetectedTool(),
        FetchArchiveTool(),
    ]
    for tool in tools:
        bot._loop.tools.register(tool)

    # Register the search delegation tool
    register_search_tools(bot)

    # Remove direct web search tools — orchestrator must delegate ALL web
    # research to the search worker (API-based) or browser worker (browser-based).
    for name in ("web_search", "web_fetch"):
        bot._loop.tools.unregister(name)
