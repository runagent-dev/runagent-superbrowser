"""antibot: tiered anti-bot fetch pipeline.

See /root/.claude/plans/okay-in-runagent-browser-shiny-swan.md for the
design. Mechanisms ported from crawl4ai's `antibot_detector` and
crawlee-python's `SessionPool` / `ProxyConfiguration` / `HeaderGenerator`;
all orchestration, detection, session, proxy, warmup, and routing logic
is our own.
"""

from .tools import (
    FetchArchiveTool,
    FetchAutoTool,
    FetchImpersonateTool,
    FetchUndetectedTool,
)

__all__ = [
    "FetchArchiveTool",
    "FetchAutoTool",
    "FetchImpersonateTool",
    "FetchUndetectedTool",
]
