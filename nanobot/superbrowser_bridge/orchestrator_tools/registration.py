"""Tool-registration entry point for the orchestrator agent.

Mirrors the legacy ``register_orchestrator_tools`` byte-for-byte —
same tools registered, in the same order, plus the same ``web_search``
/ ``web_fetch`` unregistration. Exposed via the package
``__init__`` so `from superbrowser_bridge.orchestrator_tools import
register_orchestrator_tools` keeps working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .delegate_browser import DelegateBrowserTaskTool
from .learnings_tools import CheckLearningsTool, SaveLearningTool

if TYPE_CHECKING:
    from nanobot import Nanobot  # noqa: F401


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
