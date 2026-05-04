"""DEPRECATED in Arch v4 — pending removal.

Superseded by ``TaskBrief.checklist`` (constraints + completed_log +
focus_id) in ``task_brief.py``. The whole-task decomposition is now
done at ``build_task_brief()`` time via the constraint extractor; the
checklist is rendered each step via ``render_checklist_block()`` and
mid-task replanning fires through ``redecompose()`` rather than via a
``browser_set_task_plan`` tool call. Keep this module loaded so legacy
callers don't break, but do NOT extend it. Scheduled for deletion once
all callsites have migrated.

────────────────────────────────────────────────────────────────────
Original docstring follows.
────────────────────────────────────────────────────────────────────

Persistent multi-step task plan for the browser worker.

The existing per-screenshot `[PLAN]` block (action_planner.py) ranks
*one* dismiss-blocker action and *one* main-goal action against the
current scene. That works when the task is "click a button on this
page" but breaks down on multi-constraint queries like "apply 4
filters then read the top result" — by the time the worker has clicked
two filters the original goal has fallen out of working memory and the
brain drifts to URL guessing.

This module adds a *persistent* TaskPlan that lives across iterations
on `BrowserSessionState`. It's:
  • opt-in (the brain calls `browser_set_task_plan` once per task)
  • validated (every step needs an observable `success_criteria`)
  • lifecycle-tracked (pending → in_progress → satisfied | unsatisfiable)
  • bounded (after MAX_STEP_ATTEMPTS failures a step is marked
    unsatisfiable so the worker stops looping on it)

`form_session` (form_session.py) is a *leaf* sub-machine: a TaskPlan
step can declare `delegate={kind: "form_session", payload: {...}}` and
the form-fill flow runs inside that step. Worker_hook renders only one
checklist at a time — the form one when active, the plan one otherwise.

The success_criteria reuses the `Postcondition` shape from
`action_planner.py` so the same verify_action probes work unchanged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from .action_planner import Postcondition


StepStatus = Literal["pending", "in_progress", "satisfied", "unsatisfiable"]
DelegateKind = Literal["form_session", "navigation", "extraction", "manual"]

# Two consecutive failures of the same step's success_criteria flips it
# to `unsatisfiable`. Lower numbers cause premature give-ups (one
# transient network hiccup ends a step); higher numbers reintroduce the
# loop the plan is supposed to prevent. 2 is the sweet spot.
MAX_STEP_ATTEMPTS = 2


@dataclass
class StepDelegate:
    """Optional sub-machine that executes within a TaskStep.

    `kind="form_session"` — the brain is expected to call
    `browser_form_begin` with `payload["fields"]` before the next click.
    `kind="navigation"` — the step's success_criteria typically asserts
    `url_matches` against `payload["url_pattern"]`.
    `kind="extraction"` — terminal step that reads data with
    `browser_get_markdown` / `browser_eval`.
    `kind="manual"` — no enforced sub-machine; verify_action just checks
    the success_criteria after each state-change tool.
    """

    kind: DelegateKind
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "payload": dict(self.payload)}


@dataclass
class TaskStep:
    name: str
    success_criteria: Postcondition
    delegate: Optional[StepDelegate] = None
    status: StepStatus = "pending"
    attempts: int = 0
    last_failure_reason: str = ""

    def mark_attempt(self, satisfied: bool, reason: str = "") -> None:
        """Record one verification attempt against this step.

        On success → status=satisfied. On failure → bump attempts; once
        the count reaches MAX_STEP_ATTEMPTS the step flips to
        `unsatisfiable` and the brain must explicitly skip or replan
        before the plan advances.
        """
        self.attempts += 1
        if satisfied:
            self.status = "satisfied"
            self.last_failure_reason = ""
            return
        self.last_failure_reason = (reason or "criterion_not_satisfied")[:160]
        if self.attempts >= MAX_STEP_ATTEMPTS:
            self.status = "unsatisfiable"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "success_criteria": self.success_criteria.to_dict(),
            "delegate": self.delegate.to_dict() if self.delegate else None,
            "status": self.status,
            "attempts": self.attempts,
            "last_failure_reason": self.last_failure_reason,
        }


_STATUS_MARKER = {
    "satisfied": "[done]",
    "unsatisfiable": "[fail]",
    "in_progress": "[->]  ",
    "pending": "[ ]   ",
}


@dataclass
class TaskPlan:
    steps: list[TaskStep]
    created_at: float = 0.0

    @property
    def is_complete(self) -> bool:
        """All steps either satisfied or unsatisfiable."""
        return all(
            s.status in ("satisfied", "unsatisfiable") for s in self.steps
        )

    def active_step(self) -> Optional[TaskStep]:
        """Return the first non-resolved step, marking it in_progress.

        Side effect: bumps the first `pending` step to `in_progress` so
        verify_action probes know which criterion to check after the
        next state-change tool. Returns None if every step is resolved.
        """
        for s in self.steps:
            if s.status == "in_progress":
                return s
            if s.status == "pending":
                s.status = "in_progress"
                return s
        return None

    def peek_active(self) -> Optional[TaskStep]:
        """Like active_step() but without the pending→in_progress flip."""
        for s in self.steps:
            if s.status in ("in_progress", "pending"):
                return s
        return None

    def active_index(self) -> Optional[int]:
        for i, s in enumerate(self.steps):
            if s.status in ("pending", "in_progress"):
                return i
        return None

    def skip_active(self, reason: str = "") -> Optional[TaskStep]:
        """Mark the active step unsatisfiable and return it. None if no active."""
        s = self.peek_active()
        if s is None:
            return None
        s.status = "unsatisfiable"
        s.last_failure_reason = (reason or "explicitly_skipped")[:160]
        return s

    def to_brain_text(self, *, compact: bool = False) -> str:
        """Render the plan for inclusion in tool result captions.

        compact=True returns a single-line cursor (used when a
        form_session is active and its own checklist is the primary
        view). compact=False returns the full multi-line checklist.
        """
        if not self.steps:
            return ""
        if compact:
            ai = self.active_index()
            if ai is None:
                return (
                    f"[PLAN] all {len(self.steps)} steps resolved "
                    f"({sum(1 for s in self.steps if s.status == 'satisfied')} satisfied)"
                )
            s = self.steps[ai]
            in_what = (
                f" (in {s.delegate.kind})" if s.delegate else ""
            )
            return (
                f"[PLAN] step {ai + 1}/{len(self.steps)}: {s.name}{in_what}"
            )
        n = len(self.steps)
        done = sum(1 for s in self.steps if s.status == "satisfied")
        lines = [f"[TASK PLAN] {done}/{n} satisfied"]
        for i, s in enumerate(self.steps, start=1):
            marker = _STATUS_MARKER.get(s.status, "[?]   ")
            extra = ""
            if s.status == "in_progress" and s.attempts > 0:
                extra = (
                    f"  (attempt {s.attempts}/{MAX_STEP_ATTEMPTS} — "
                    f"prev: {s.last_failure_reason[:60]})"
                )
            elif s.status == "unsatisfiable":
                extra = f"  (gave up: {s.last_failure_reason[:60]})"
            elif s.status == "satisfied" and s.attempts > 1:
                extra = f"  (verified after {s.attempts} attempts)"
            d = ""
            if s.delegate:
                d = f"  <{s.delegate.kind}>"
            lines.append(f"  {marker} {i}. {s.name}{d}{extra}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "is_complete": self.is_complete,
        }


# ── Validator ────────────────────────────────────────────────────────


class TaskPlanValidationError(ValueError):
    """Raised by validate_steps when the LLM-supplied plan is unusable."""


def validate_steps(raw_steps: Any) -> list[TaskStep]:
    """Validate raw step dicts from the LLM into TaskStep instances.

    Rejects:
      • empty plan / non-list input
      • single-step plan (use no-plan path; planner overhead only pays
        off when the task has ≥2 distinct sub-goals)
      • step with empty `name`
      • step whose `success_criteria.kind` is `"none"` (no observable
        check would mean the plan can never auto-advance, defeating
        the point)
    """
    if not isinstance(raw_steps, list) or not raw_steps:
        raise TaskPlanValidationError("plan is empty")
    if len(raw_steps) < 2:
        raise TaskPlanValidationError(
            "plan has only 1 step — use the no-plan path for single-step tasks"
        )
    out: list[TaskStep] = []
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise TaskPlanValidationError(
                f"step {i + 1}: must be an object, got {type(raw).__name__}"
            )
        name = str(raw.get("name") or "").strip()
        if not name:
            raise TaskPlanValidationError(f"step {i + 1}: empty name")
        sc_raw = raw.get("success_criteria") or {}
        if not isinstance(sc_raw, dict):
            raise TaskPlanValidationError(
                f"step {i + 1} ({name!r}): success_criteria must be an object"
            )
        kind = str(sc_raw.get("kind") or "none")
        if kind == "none":
            raise TaskPlanValidationError(
                f"step {i + 1} ({name!r}): success_criteria.kind cannot be "
                "'none' — every step must be observably verifiable. Pick one "
                "of: url_changed, url_matches, text_visible, text_hidden, "
                "bbox_disappeared, focus_on_role, dom_mutated, flag_cleared."
            )
        criteria = Postcondition(
            kind=kind,  # type: ignore[arg-type]
            payload=dict(sc_raw.get("payload") or {}),
            timeout_ms=int(sc_raw.get("timeout_ms") or 2500),
        )
        delegate: Optional[StepDelegate] = None
        d_raw = raw.get("delegate")
        if isinstance(d_raw, dict) and d_raw.get("kind"):
            delegate = StepDelegate(
                kind=str(d_raw["kind"]),  # type: ignore[arg-type]
                payload=dict(d_raw.get("payload") or {}),
            )
        out.append(
            TaskStep(name=name, success_criteria=criteria, delegate=delegate)
        )
    return out


def make_plan(raw_steps: Any) -> TaskPlan:
    """Build a TaskPlan from LLM-supplied raw step dicts."""
    return TaskPlan(steps=validate_steps(raw_steps), created_at=time.monotonic())


__all__ = [
    "DelegateKind",
    "MAX_STEP_ATTEMPTS",
    "StepDelegate",
    "StepStatus",
    "TaskPlan",
    "TaskPlanValidationError",
    "TaskStep",
    "make_plan",
    "validate_steps",
]
