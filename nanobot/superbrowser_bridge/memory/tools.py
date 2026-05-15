"""Orchestrator-only recall surface.

Four tools registered by ``Memory.attach`` when ``role == "orchestrator"``.
They are the explicit half of memory: anything that fell out of the
rendered ledger is reachable via ``memory_recall``; the orchestrator
can also write new facts / notes and revoke prior ones.

Workers never see these tools - they're not registered on the worker
bot. As a safety latch each tool's ``execute`` re-checks
``memory.role`` and returns an error string if invoked from a
non-orchestrator context (would only happen if a future refactor
broke role isolation).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)

if TYPE_CHECKING:
    from .memory import Memory


def _role_guard(memory: "Memory", tool_name: str) -> str | None:
    if memory.role != "orchestrator":
        return f"[{tool_name} is orchestrator-only; current role={memory.role}]"
    return None


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema(
            "Substring (case-insensitive) to search across facts, "
            "episodes, and the full step history."
        ),
        limit=IntegerSchema(
            "Maximum number of matches. Default 10, max 50.",
            nullable=True,
        ),
        required=["query"],
    )
)
class MemoryRecallTool(Tool):
    name = "memory_recall"
    description = (
        "Search prior facts, subgoal summaries, and step history for "
        "anything matching `query`. Use when the rendered ledger doesn't "
        "show what you need - older context that has been compacted is "
        "still reachable here. Results are ranked fact > episodic > step."
    )

    def __init__(self, memory: "Memory") -> None:
        self.memory = memory

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self, query: str, limit: int | None = None, **_kw: Any
    ) -> str:
        err = _role_guard(self.memory, self.name)
        if err:
            return err
        cap = min(max(limit or 10, 1), 50)
        matches = self.memory.recall(query, limit=cap)
        if not matches:
            return f"[memory_recall: no matches for '{query}']"
        lines = [f"[memory_recall: {len(matches)} match(es) for '{query}']"]
        for m in matches:
            src = m.get("source", "?")
            text = m.get("text", "")
            extras: list[str] = []
            if m.get("subgoal"):
                extras.append(f"subgoal={m['subgoal']}")
            if m.get("kind"):
                extras.append(f"kind={m['kind']}")
            if m.get("iteration", -1) >= 0:
                extras.append(f"iter={m['iteration']}")
            tail = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"- [{src}] {text}{tail}")
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        key=StringSchema(
            "Short identifier for the fact (e.g. 'login_method', "
            "'preferred_currency'). Reusing a key overwrites the value."
        ),
        value=StringSchema(
            "Short value for the fact (a single line - if it doesn't "
            "fit, use memory_note instead)."
        ),
        confidence=IntegerSchema(
            "0-100 confidence in the fact. Default 100.",
            nullable=True,
        ),
        required=["key", "value"],
    )
)
class MemoryRememberTool(Tool):
    name = "memory_remember"
    description = (
        "Store a key/value fact in the agent's structured memory. "
        "Facts persist across iterations and surface in every turn's "
        "rendered ledger. Reuse a key to update its value."
    )

    def __init__(self, memory: "Memory") -> None:
        self.memory = memory

    async def execute(
        self,
        key: str,
        value: str,
        confidence: int | None = None,
        **_kw: Any,
    ) -> str:
        err = _role_guard(self.memory, self.name)
        if err:
            return err
        if not key:
            return "[memory_remember: 'key' cannot be empty]"
        conf = 1.0
        if confidence is not None:
            try:
                conf = max(0.0, min(1.0, int(confidence) / 100.0))
            except (TypeError, ValueError):
                conf = 1.0
        self.memory.remember(key, value, confidence=conf)
        return json.dumps({"stored": True, "key": key, "value": value})


@tool_parameters(
    tool_parameters_schema(
        text=StringSchema(
            "Free-form observation - longer than a fact, shorter than a "
            "subgoal summary. Appended to the episodic log."
        ),
        required=["text"],
    )
)
class MemoryNoteTool(Tool):
    name = "memory_note"
    description = (
        "Append a free-form note to episodic memory. Notes survive "
        "across iterations and are searchable via memory_recall. Use "
        "for observations that don't fit a fact's key/value shape."
    )

    def __init__(self, memory: "Memory") -> None:
        self.memory = memory

    async def execute(self, text: str, **_kw: Any) -> str:
        err = _role_guard(self.memory, self.name)
        if err:
            return err
        if not text:
            return "[memory_note: 'text' cannot be empty]"
        self.memory.note(text)
        return json.dumps({"noted": True, "chars": len(text)})


@tool_parameters(
    tool_parameters_schema(
        key=StringSchema("Key of the fact to remove."),
        required=["key"],
    )
)
class MemoryForgetTool(Tool):
    name = "memory_forget"
    description = (
        "Remove a previously-stored fact by key. Use when a prior "
        "assumption was wrong (\"the form does NOT need SSN\") so the "
        "stale fact stops biasing future iterations."
    )

    def __init__(self, memory: "Memory") -> None:
        self.memory = memory

    async def execute(self, key: str, **_kw: Any) -> str:
        err = _role_guard(self.memory, self.name)
        if err:
            return err
        removed = self.memory.forget(key)
        return json.dumps({"removed": removed, "key": key})


def recall_tool_classes() -> list[type[Tool]]:
    """Return the four recall tool classes - used by Memory.attach()."""
    return [
        MemoryRecallTool,
        MemoryRememberTool,
        MemoryNoteTool,
        MemoryForgetTool,
    ]
