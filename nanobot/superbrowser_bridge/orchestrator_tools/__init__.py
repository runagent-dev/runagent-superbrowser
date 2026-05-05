"""Orchestrator-tools package — split out of the legacy
orchestrator_tools.py monolith.

This ``__init__`` re-exports every public AND private symbol that the
old orchestrator_tools.py exposed. Critically, it also re-exports the
routing helpers (``_classify_task``, ``_record_routing_outcome``,
``_domain_from_url``) that the old module pulled in from
``superbrowser_bridge.routing`` and that ``search_tools.py`` imports
through this module's namespace (see search_tools.py:368).
"""

from __future__ import annotations

# Re-exports from the routing module — search_tools.py imports these
# transitively via this namespace (lines 368, 565). Preserve the
# pass-through so search_tools doesn't have to change.
from superbrowser_bridge.routing import (
    LEARNINGS_DIR,
    TACTIC_ALTERNATIVES,
    _captcha_learnings_path,
    _classify_task,
    _domain_from_url,
    _learnings_path,
    _looks_blocked,
    _preferred_approach,
    _record_routing_outcome,
    _rewrite_for_search,
    _routing_path,
    learning_reads_enabled,
    tactic_penalty_summary,
)

from .captcha_learnings import (
    _domain_needs_human_handoff,
    _update_captcha_learnings,
)
from .constants import (
    BROWSER_WORKSPACE,
    _BASE,
    _DELEGATION_ATTEMPTS,
    _DELEGATION_MAX_ATTEMPTS,
    _DELEGATION_WINDOW_SEC,
    _SUBSTANTIVE_KEYWORDS,
    _SUBSTANTIVE_PRICE_RE,
)
from .delegate_browser import DelegateBrowserTaskTool
from .delegation_registry import _result_is_substantive, _task_fingerprint
from .learnings_tools import CheckLearningsTool, SaveLearningTool
from .registration import register_orchestrator_tools
from .url_probe import _probe_url

__all__ = [
    # Public API
    "register_orchestrator_tools",
    # Tool classes
    "CheckLearningsTool",
    "DelegateBrowserTaskTool",
    "SaveLearningTool",
    # Public constants
    "BROWSER_WORKSPACE",
    # Routing pass-throughs (kept on the orchestrator namespace for
    # backwards compatibility with search_tools.py:368/565).
    "LEARNINGS_DIR",
    "TACTIC_ALTERNATIVES",
    "_captcha_learnings_path",
    "_classify_task",
    "_domain_from_url",
    "_learnings_path",
    "_looks_blocked",
    "_preferred_approach",
    "_record_routing_outcome",
    "_rewrite_for_search",
    "_routing_path",
    "learning_reads_enabled",
    "tactic_penalty_summary",
    # Private helpers
    "_BASE",
    "_DELEGATION_ATTEMPTS",
    "_DELEGATION_MAX_ATTEMPTS",
    "_DELEGATION_WINDOW_SEC",
    "_SUBSTANTIVE_KEYWORDS",
    "_SUBSTANTIVE_PRICE_RE",
    "_domain_needs_human_handoff",
    "_probe_url",
    "_result_is_substantive",
    "_task_fingerprint",
    "_update_captcha_learnings",
]
