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

    Arch v3 additions:
      - `task_brief` — full TaskBrief (constraints, plan_of_attack, CoT
        trail). Replaces the truncated `task_instruction` as the primary
        memory carrier. The `task_instruction` field stays for backward
        compatibility but is now the FULL query, not [:500].
      - `step_history_summary` — last 30 steps compressed to
        {tool, args_summary, result_outcome, url, t}. Today's handoff
        drops step history entirely; successor worker flies blind.
      - `vision_state_history` — last 5 PageState dicts. Lets the
        successor know how the page evolved without re-screenshotting.
      - `failed_tactics` — short strings naming tactics already tried
        and failed (e.g. "selector_lookup_for_login_button",
        "url_guess_for_search_results").
      - `interaction_ledger` — last 20 bbox-by-V_n references the brain
        used + outcomes (clicked / typed / verify_succeeded). Helps
        successor avoid re-trying dead bboxes.
    """

    session_id: str = ""
    current_url: str = ""
    pinned_domain: str = ""
    task_instruction: str = ""
    task_target_url: str = ""
    task_plan: Any = None  # task_plan.TaskPlan (preserves cursor)
    task_brief: Any = None  # task_brief.TaskBrief (arch v3)
    step_history_summary: list[dict] = field(default_factory=list)
    vision_state_history: list[dict] = field(default_factory=list)
    failed_tactics: list[str] = field(default_factory=list)
    interaction_ledger: list[dict] = field(default_factory=list)
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
        brief_bit = ""
        if self.task_brief is not None:
            try:
                constraints = getattr(self.task_brief, "constraints", []) or []
                sat = sum(
                    1 for c in constraints
                    if getattr(c, "status", "") == "satisfied"
                )
                brief_bit = (
                    f" brief={sat}/{len(constraints)} constraints satisfied"
                )
            except Exception:
                brief_bit = ""
        url_bit = self.current_url[:120] if self.current_url else "(no url)"
        return (
            f"session={self.session_id} backend={self.backend} "
            f"url={url_bit}{plan_bit}{brief_bit}"
        )


def save(task_id: str, state: Any) -> None:
    """Capture the live state of a dying worker.

    `state` is a `BrowserSessionState`; we only read attributes (no
    mutation). Idempotent — re-saving the same task_id replaces the
    previous entry.
    """
    if not task_id:
        return
    try:
        # Arch v3: prefer task_brief.original_query as the canonical
        # task_instruction (full text, never truncated). Fall back to
        # state.task_instruction (which is the [:500] truncated form
        # in legacy code, full text after the v3 session_tools edit).
        brief = getattr(state, "task_brief", None)
        instruction = ""
        if brief is not None:
            instruction = (
                getattr(brief, "original_query", "")
                or getattr(state, "task_instruction", "")
                or ""
            )
        else:
            instruction = getattr(state, "task_instruction", "") or ""

        # Compress step_history (if present) to a small summary list.
        raw_history = (
            getattr(state, "step_history", None)
            or getattr(state, "_step_history", None)
            or []
        )
        step_summary: list[dict] = []
        if isinstance(raw_history, list):
            for item in raw_history[-30:]:
                if not isinstance(item, dict):
                    continue
                step_summary.append({
                    "tool": str(item.get("tool", ""))[:48],
                    "args_summary": str(item.get("args_summary", ""))[:120],
                    "result_outcome": str(item.get("result_outcome", ""))[:60],
                    "url": str(item.get("url", ""))[:200],
                    "t": item.get("t"),
                })

        # Vision state history — last 5 PageState dicts if state stores them.
        raw_vsh = getattr(state, "vision_state_history", None) or []
        vsh: list[dict] = []
        if isinstance(raw_vsh, list):
            for ps in raw_vsh[-5:]:
                if isinstance(ps, dict):
                    vsh.append(ps)
                elif hasattr(ps, "model_dump"):
                    try:
                        vsh.append(ps.model_dump())
                    except Exception:
                        pass

        failed_tactics = list(
            getattr(state, "failed_tactics", []) or []
        )[-20:]
        interaction_ledger = list(
            getattr(state, "interaction_ledger", []) or []
        )[-20:]

        h = WorkerHandoff(
            session_id=getattr(state, "session_id", "") or "",
            current_url=getattr(state, "current_url", "") or "",
            pinned_domain=getattr(state, "pinned_domain", "") or "",
            task_instruction=instruction,
            task_target_url=getattr(state, "task_target_url", "") or "",
            task_plan=getattr(state, "task_plan", None),
            task_brief=brief,
            step_history_summary=step_summary,
            vision_state_history=vsh,
            failed_tactics=failed_tactics,
            interaction_ledger=interaction_ledger,
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


def take_recent_for_domain(
    domain: str,
    *,
    max_age_s: float = 1800.0,
) -> WorkerHandoff | None:
    """Arch v3 fix H — domain-fallback handoff lookup.

    The orchestrator's LLM often crafts NEW retry instructions
    ("Retry with a different tactic. Hypothesis: ...") rather than
    re-using the original. The dedup key changes (sha1 of instructions),
    so `take(_dedup_key)` returns None and the new worker fresh-starts
    with no progress carryover — even though the same domain has a
    very-recent saved entry from a worker that just exited.

    This helper finds the MOST RECENT non-expired handoff for `domain`
    and pops it (one-shot, like `take`). Used by delegate_browser_task
    as a fallback after the exact-key lookup misses. Matching is on
    `pinned_domain` first, then on the domain extracted from
    `current_url`. Empty `domain` returns None.
    """
    if not domain:
        return None
    domain = domain.lower().lstrip("www.")
    with _lock:
        now = time.monotonic()
        # Drop expired first so we don't return stale entries.
        _gc_locked()
        # Find candidates whose pinned_domain or current_url-host matches.
        candidates: list[tuple[str, WorkerHandoff]] = []
        for k, v in _store.items():
            if (now - v.captured_at) > max_age_s:
                continue
            host = ""
            pin = (v.pinned_domain or "").lower().lstrip("www.")
            if pin and (pin == domain or pin.endswith("." + domain) or domain.endswith("." + pin)):
                candidates.append((k, v))
                continue
            try:
                from urllib.parse import urlsplit
                host = (urlsplit(v.current_url or "").hostname or "").lower().lstrip("www.")
            except Exception:
                host = ""
            if host and (host == domain or host.endswith("." + domain) or domain.endswith("." + host)):
                candidates.append((k, v))
        if not candidates:
            return None
        # Most recent wins.
        candidates.sort(key=lambda kv: kv[1].captured_at, reverse=True)
        key_to_pop, hit = candidates[0]
        _store.pop(key_to_pop, None)
        return hit


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


__all__ = [
    "WorkerHandoff", "save", "take", "peek", "clear",
    "take_recent_for_domain",
]
