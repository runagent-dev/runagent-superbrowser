"""The rich result object returned by :meth:`SuperBrowser.run` / ``arun``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RunResult:
    """Outcome of a single SuperBrowser task.

    Attributes:
        text: The final, user-visible answer. This is what you usually want —
            it's the orchestrator's direct text output, or (when the agent
            answered through its ``message()`` tool, which leaves
            ``raw_content`` empty) the last message captured off the bus.
        success: ``True`` when a non-empty answer came back and no hard error
            was raised.
        task_id: The internal task id (``orch-<hex8>``) — useful for locating
            the on-disk ledger under ``/tmp/superbrowser/<task_id>/``.
        mode: The mode the task actually ran in (``auto`` / ``fetch`` /
            ``browser``).
        data: When ``output_schema`` was supplied and the answer contained
            parseable JSON, the validated/parsed payload (a pydantic model
            instance or a plain dict/list). ``None`` otherwise — parsing is
            best-effort and never raises.
        error: A short message when the run failed (timeout, server
            unavailable, exception). ``None`` on success.
        raw_content: The orchestrator's direct ``result.content`` — often
            empty when the agent answers via the ``message()`` tool. Exposed
            so callers can distinguish direct vs bus-captured answers.
        classification: For ``mode="auto"``, the routing classifier's verdict
            (``{"approach","reason","confidence"}``) — surfaces *why* the agent
            would lean fetch vs browser. ``None`` for forced modes.
    """

    text: str
    success: bool
    task_id: str
    mode: str
    data: Any | None = None
    error: str | None = None
    raw_content: str = ""
    classification: dict[str, Any] | None = field(default=None)

    def __bool__(self) -> bool:  # `if result:` reads as "did it work?"
        return self.success
