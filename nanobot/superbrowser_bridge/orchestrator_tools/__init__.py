"""Orchestrator tools for the two-agent architecture.

The orchestrator delegates browser work to a fresh browser worker instance,
manages site-specific learnings, and never touches browser tools directly.

This package re-exports the public surface of the previous
`orchestrator_tools.py` module so existing imports keep working unchanged.
The `_classify_task` / `_record_routing_outcome` / `_domain_from_url`
re-exports are part of the contract — `search_tools.py` reaches for them
here rather than from `routing` directly.
"""

from __future__ import annotations

from superbrowser_bridge.routing import (
    _classify_task,
    _domain_from_url,
    _record_routing_outcome,
)

from .constants import BROWSER_WORKSPACE
from .delegation import DelegateBrowserTaskTool
from .learnings_tools import CheckLearningsTool, SaveLearningTool
from .registry import register_orchestrator_tools
