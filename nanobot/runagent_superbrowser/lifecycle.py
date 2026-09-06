"""Process-local task lifecycle registry (thread-safe).

Tracks every SuperBrowser task started in this process under a *handle*
(client-supplied or generated ``task-<hex8>``), so tasks can be listed and
cooperatively cancelled. It also works across the ``runagent serve`` boundary:
the Docker entrypoints (deploy/main.py) register the SDK's ``client_task_id``
here, and the ``cancel`` / ``tasks`` entrypoints consult the same module state
— one server process, one shared registry.

Cancellation is COOPERATIVE: :func:`request_cancel` flips a
``threading.Event``; the run loop checks :func:`cancelled` between stream
events (events flow every LLM step, so latency is seconds, not minutes) and
closes its generator, which unwinds the underlying task through the existing
``_capture`` cancel chain. Nothing here interrupts a thread mid-flight.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

TERMINAL_STATES = ("done", "cancelled", "error")

_PRUNE_TERMINAL_AFTER_S = 3600.0


@dataclass
class TaskRecord:
    handle: str
    task_text: str
    started_at: float
    state: str = "running"  # running | cancelling | done | cancelled | error
    orch_task_id: str = ""
    transport: str = ""
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "task": self.task_text,
            "state": self.state,
            "orch_task_id": self.orch_task_id,
            "transport": self.transport,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 1),
        }


_lock = threading.Lock()
_records: dict[str, TaskRecord] = {}


def new_handle() -> str:
    return f"task-{uuid.uuid4().hex[:8]}"


def register(handle: str | None, task_text: str, *, transport: str = "") -> TaskRecord:
    """Create (or return the live record for) ``handle``.

    Re-registering a handle whose record is still running returns the existing
    record instead of clobbering it — the cancel event must stay the same
    object for a cancel issued between transport retries to stick.
    """
    resolved = handle or new_handle()
    with _lock:
        _prune_locked()
        existing = _records.get(resolved)
        if existing is not None and existing.state not in TERMINAL_STATES:
            return existing
        record = TaskRecord(
            handle=resolved,
            task_text=(task_text or "")[:120],
            started_at=time.time(),
            transport=transport,
        )
        _records[resolved] = record
        return record


def get(handle: str) -> TaskRecord | None:
    with _lock:
        return _records.get(handle)


def note_orch_task_id(handle: str, orch_task_id: str) -> None:
    with _lock:
        record = _records.get(handle)
        if record is not None:
            record.orch_task_id = orch_task_id


def request_cancel(handle: str) -> bool:
    """Flip the cancel event. True when a live task matched."""
    with _lock:
        record = _records.get(handle)
        if record is None or record.state in TERMINAL_STATES:
            return False
        record.state = "cancelling"
        record.cancel_event.set()
        return True


def cancelled(handle: str) -> bool:
    with _lock:
        record = _records.get(handle)
    return record is not None and record.cancel_event.is_set()


def finish(handle: str, state: str = "done") -> None:
    """Mark a task terminal. No-op if it already reached a terminal state."""
    if state not in TERMINAL_STATES:
        state = "done"
    with _lock:
        record = _records.get(handle)
        if record is not None and record.state not in TERMINAL_STATES:
            record.state = state
            record.finished_at = time.time()


def list_tasks() -> list[dict[str, Any]]:
    """All known tasks, running first, most recent first within each group."""
    with _lock:
        _prune_locked()
        records = list(_records.values())
    records.sort(key=lambda r: (r.state in TERMINAL_STATES, -r.started_at))
    return [record.to_dict() for record in records]


def _prune_locked() -> None:
    cutoff = time.time() - _PRUNE_TERMINAL_AFTER_S
    stale = [
        handle
        for handle, record in _records.items()
        if record.state in TERMINAL_STATES and (record.finished_at or record.started_at) < cutoff
    ]
    for handle in stale:
        del _records[handle]
