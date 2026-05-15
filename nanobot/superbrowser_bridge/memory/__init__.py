"""In-task semantic memory for superbrowser.

This package composes nanobot's primitives (Consolidator, AutoCompact,
AgentHook, session.metadata) into a coherent "what does the agent know"
layer. It is the answer to context bloat: the message log carries
recent verbatim turns; the ledger carries everything else as compact
structured state.

Roles:
- Orchestrator-side Memory holds the full ledger and exposes recall tools.
- Worker-side Memory shares the same on-disk store but renders a
  subgoal-scoped slice into the LLM context.

Persistence layout under /tmp/superbrowser/{task_id}/memory/:
- ledger.json     - single JSON snapshot of in-memory state
- steps.jsonl     - per-action append log
- episodic.jsonl  - compacted subgoal summaries + free-form notes
- facts.jsonl     - audit trail of remember/forget actions
- events.jsonl    - observability log (token usage, evictions, compactions)
"""

from __future__ import annotations

from .hook import MemoryHook
from .ledger import Checkpoint, DeadEnd, Fact, Ledger, StepOutcome
from .memory import Memory, Role
from .registry import (
    clear_orchestrator_memory,
    get_orchestrator_memory,
    set_orchestrator_memory,
)
from .store import EventLog, LedgerStore
from .tools import (
    MemoryForgetTool,
    MemoryNoteTool,
    MemoryRecallTool,
    MemoryRememberTool,
)

__all__ = [
    "Checkpoint",
    "DeadEnd",
    "EventLog",
    "Fact",
    "Ledger",
    "LedgerStore",
    "Memory",
    "MemoryForgetTool",
    "MemoryHook",
    "MemoryNoteTool",
    "MemoryRecallTool",
    "MemoryRememberTool",
    "Role",
    "StepOutcome",
    "clear_orchestrator_memory",
    "get_orchestrator_memory",
    "set_orchestrator_memory",
]
