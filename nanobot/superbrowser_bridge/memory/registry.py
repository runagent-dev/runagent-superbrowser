"""Process-wide registry for the orchestrator's Memory.

Worker spawn paths in delegation.py need to debrief into the
orchestrator's ledger after worker.run returns. Threading the
orchestrator memory through every delegation call would touch
every call site of delegate_browser_task and complicate signatures
the worker would otherwise be agnostic to.

Instead, run.py sets the orchestrator Memory once at startup via
``set_orchestrator_memory``. delegation.py's finally block calls
``get_orchestrator_memory()`` after worker.run returns and absorbs
the worker's high-confidence facts and URL-tagged dead-ends.

Single-orchestrator-per-process assumption is correct for
superbrowser today (run.py owns one Memory). If that ever changes
— say, an HTTP server hosting concurrent agents — switch to a
``contextvars.ContextVar`` keyed per-request so concurrent
orchestrators don't trample each other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .memory import Memory


_orchestrator_memory: "Memory | None" = None


def set_orchestrator_memory(memory: "Memory") -> None:
    """Register the current process's orchestrator Memory.

    Called once by run.py right after constructing the orchestrator
    Memory. Re-registering replaces the prior reference; callers
    should not rely on accessing more than the most-recently-set one.
    """
    global _orchestrator_memory
    _orchestrator_memory = memory


def get_orchestrator_memory() -> "Memory | None":
    """Return the registered orchestrator Memory, or None if unset.

    Worker-spawn paths use this to absorb worker findings on exit.
    Returns None in test contexts that don't run the full run.py
    bootstrap; callers must tolerate that.
    """
    return _orchestrator_memory


def clear_orchestrator_memory() -> None:
    """Reset the registry. Test-only helper."""
    global _orchestrator_memory
    _orchestrator_memory = None
