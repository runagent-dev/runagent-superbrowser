"""Cross-worker state handoff for orchestrator-spawned browser workers.

Background
----------
`delegate_browser_task` (orchestrator_tools.py) historically spawned each
browser worker with a fresh `BrowserSessionState`. When the first worker
gave up via `browser_request_help` and the orchestrator spawned a second
worker to retry, the second worker started with:
  • a brand-new browser session (the first session's URL with filters
    applied was thrown away — observed on wineaccess.com, where worker 1
    reached `?food_pairings=fish%2Csweets&ordering=-score` and worker 2
    started back at the homepage)
  • an empty `task_plan`
  • an empty `cursor_failure_strategies` ledger (so the script-lockout
    gate didn't fire and worker 2 reflexively reached for
    `browser_run_script` and burned the screenshot budget on JS-shape
    syntax errors)

This module is a process-local, one-shot store keyed by orchestrator
task id. The dying worker calls `save(task_id, state)` on its way out;
the next worker calls `take(task_id)` once and hydrates from it. After
`take` the entry is removed — no double-resumes.

Scope
-----
Process-local intentionally. Cross-process handoff would need pickling +
shared storage (Redis, sqlite); none of the state we want to preserve is
pickle-friendly (httpx clients, asyncio Tasks). All workers spawned
by one orchestrator share the parent process, so a dict is enough.

The store is bounded — entries older than `_MAX_AGE_S` are dropped on
`save` to keep memory flat across long orchestrator sessions.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


_MAX_AGE_S = 1800.0  # 30 minutes — covers any realistic retry window
_lock = threading.Lock()
_store: "dict[str, WorkerHandoff]" = {}


@dataclass
class WorkerHandoff:
    """Snapshot of a worker's session state, captured on worker exit.

    Only fields that meaningfully change retry behavior are captured.
    Per-iteration counters (`step_counter`, `vision_calls`, etc.) are
    deliberately omitted — they're noise to the next worker.
    """

    session_id: str = ""
    current_url: str = ""
    pinned_domain: str = ""
    task_instruction: str = ""
    task_target_url: str = ""
    task_plan: Any = None  # task_plan.TaskPlan (preserves cursor)
    cursor_failure_strategies: set[str] = field(default_factory=set)
    cursor_failure_records: list[dict] = field(default_factory=list)
    observed_anchor_urls: set[str] = field(default_factory=set)
    last_filter_manifest: dict | None = None
    backend: str = "t1"  # "t1" or "t3" — for the resume prompt
    captured_at: float = 0.0

    def short_summary(self) -> str:
        """One-line summary used in the resume prompt."""
        plan_bit = ""
        if self.task_plan is not None:
            try:
                steps = getattr(self.task_plan, "steps", []) or []
                done = sum(
                    1 for s in steps if getattr(s, "status", "") == "satisfied"
                )
                plan_bit = f" plan={done}/{len(steps)} steps satisfied"
            except Exception:
                plan_bit = ""
        url_bit = self.current_url[:120] if self.current_url else "(no url)"
        return f"session={self.session_id} backend={self.backend} url={url_bit}{plan_bit}"


def save(task_id: str, state: Any) -> None:
    """Capture the live state of a dying worker.

    `state` is a `BrowserSessionState`; we only read attributes (no
    mutation). Idempotent — re-saving the same task_id replaces the
    previous entry.
    """
    if not task_id:
        return
    try:
        h = WorkerHandoff(
            session_id=getattr(state, "session_id", "") or "",
            current_url=getattr(state, "current_url", "") or "",
            pinned_domain=getattr(state, "pinned_domain", "") or "",
            task_instruction=getattr(state, "task_instruction", "") or "",
            task_target_url=getattr(state, "task_target_url", "") or "",
            task_plan=getattr(state, "task_plan", None),
            cursor_failure_strategies=set(
                getattr(state, "cursor_failure_strategies", set()) or set()
            ),
            cursor_failure_records=list(
                getattr(state, "cursor_failure_records", []) or []
            ),
            observed_anchor_urls=set(
                getattr(state, "observed_anchor_urls", set()) or set()
            ),
            last_filter_manifest=getattr(state, "last_filter_manifest", None),
            backend=(
                "t3" if (getattr(state, "session_id", "") or "").startswith("t3-")
                else "t1"
            ),
            captured_at=time.monotonic(),
        )
    except Exception as exc:
        # Saving is opportunistic — never raise into the worker shutdown path.
        print(f"  [handoff_store.save: skipped — {exc}]")
        return
    with _lock:
        _store[task_id] = h
        _gc_locked()


def take(task_id: str) -> WorkerHandoff | None:
    """One-shot pop. Returns None if no entry exists or it has expired.

    A second `take(task_id)` call returns None — the entry is removed
    after the first read so two workers spawned from the same task id
    can't both claim the resume.
    """
    if not task_id:
        return None
    with _lock:
        h = _store.pop(task_id, None)
        if h is None:
            return None
        if (time.monotonic() - h.captured_at) > _MAX_AGE_S:
            return None
        return h


def peek(task_id: str) -> WorkerHandoff | None:
    """Read without removing. Used by the orchestrator to decide whether
    to set `resume_from_task_id` on the next delegate_browser_task call.
    """
    if not task_id:
        return None
    with _lock:
        h = _store.get(task_id)
        if h is None:
            return None
        if (time.monotonic() - h.captured_at) > _MAX_AGE_S:
            _store.pop(task_id, None)
            return None
        return h


def clear() -> None:
    """For tests."""
    with _lock:
        _store.clear()


def _gc_locked() -> None:
    """Drop entries older than _MAX_AGE_S. Caller must hold _lock."""
    now = time.monotonic()
    expired = [
        k for k, v in _store.items() if (now - v.captured_at) > _MAX_AGE_S
    ]
    for k in expired:
        _store.pop(k, None)


__all__ = ["WorkerHandoff", "save", "take", "peek", "clear"]
