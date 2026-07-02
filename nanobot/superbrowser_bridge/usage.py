"""Per-task token accounting across all SuperBrowser agent roles.

A single task fans LLM work across three *disjoint* token sources:

- **orchestrator** — the top-level ``bot.run()`` loop,
- **worker**       — one or more delegated ``worker.run()`` loops
                     (``orchestrator_tools/delegation.py``),
- **vision**       — the separate screenshot-analysis model
                     (``vision_agent``), which never flows through a nanobot
                     runner and so appears in no ``RunResult.usage``.

Each source feeds this accountant independently, keyed by the orchestrator
task id, and never overlaps another — so the per-role totals simply sum to the
task total with no double counting.

Brain roles (orchestrator/worker) report via :class:`UsageHook`, an
``AgentHook`` that banks each completed iteration's usage in ``after_iteration``
(so tokens survive a cancellation — the runner skips ``after_run`` on the 900s
``asyncio.wait_for`` timeout) and reconciles to the authoritative cumulative in
``after_run`` on the success path. Vision reports via a direct
:func:`record_vision` call at its provider chokepoint. The active task is
carried in a :class:`~contextvars.ContextVar` (copied into nested worker /
``create_task``-scheduled vision coroutines), so concurrent tasks never cross
talk; a fallback to ``get_orchestrator_memory().task_id`` covers the rare case
the contextvar is lost across a thread boundary.

Embedded screenshots sent to the brain are *already inside* the brain's
``input_tokens`` (the model API counts image blocks server-side); this module
additionally surfaces ``image_blocks`` and a coarse
``estimated_embedded_image_tokens`` for observability. The dedicated vision
model is the separate, measured ``vision`` bucket.

Module-top imports are kept to stdlib + ``nanobot.agent.hook`` (already a hard
bridge dependency). ``superbrowser_bridge.memory`` / ``vision_agent`` are
imported at call time to avoid import cycles — ``vision_agent`` is a lower
layer that imports *this* module function-level.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from loguru import logger

from nanobot.agent.hook import AgentHook

_DEFAULT_BASE_DIR = "/tmp/superbrowser"
_DEFAULT_IMAGE_TOKEN_EST = 1200

# The task currently being accounted. Set at the orchestrator boundary by
# track_task(); copied into nested worker/vision coroutines automatically.
_current_task: ContextVar[str | None] = ContextVar("superbrowser_usage_task", default=None)

# task_id -> TaskUsage. Lives until the SDK/eval caller pops it.
_registry: dict[str, "TaskUsage"] = {}

# Guards _registry mutations. Within the asyncio loop mutations are already
# atomic between awaits; the lock defends against any run_in_executor caller.
_lock = threading.Lock()


def _image_token_est() -> int:
    """Per-image token estimate for embedded brain screenshots (env-tunable)."""
    try:
        return int(os.environ.get("SUPERBROWSER_IMAGE_TOKEN_EST", _DEFAULT_IMAGE_TOKEN_EST))
    except (TypeError, ValueError):
        return _DEFAULT_IMAGE_TOKEN_EST


@dataclass
class RoleUsage:
    """Cumulative token usage attributed to one role within a task."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    calls: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "calls": self.calls,
        }


@dataclass
class TaskUsage:
    """Aggregated token usage for a single task across all roles.

    ``total_tokens`` is the headline grand total. The identity holds:
    ``total_tokens == input_tokens + output_tokens`` when every role reports an
    input/output split, and ``== input_tokens + output_tokens + vision_tokens``
    when the vision provider reports only a combined total (vision then lands in
    ``total_tokens`` alone, not in input/output).
    """

    task_id: str
    by_role: dict[str, RoleUsage] = field(default_factory=dict)
    image_blocks: int = 0  # peak brain image blocks seen at end-of-run (observability)

    def role(self, name: str) -> RoleUsage:
        ru = self.by_role.get(name)
        if ru is None:
            ru = RoleUsage()
            self.by_role[name] = ru
        return ru

    @property
    def input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.by_role.values())

    @property
    def output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.by_role.values())

    @property
    def total_tokens(self) -> int:
        return sum(r.total_tokens for r in self.by_role.values())

    @property
    def cache_read_tokens(self) -> int:
        return sum(r.cache_read_tokens for r in self.by_role.values())

    @property
    def cache_creation_tokens(self) -> int:
        return sum(r.cache_creation_tokens for r in self.by_role.values())

    @property
    def vision_tokens(self) -> int:
        v = self.by_role.get("vision")
        return v.total_tokens if v else 0

    @property
    def vision_calls(self) -> int:
        v = self.by_role.get("vision")
        return v.calls if v else 0

    @property
    def estimated_embedded_image_tokens(self) -> int:
        """Coarse, labeled estimate of the screenshot tokens embedded in the
        brain prompt. These are *already* counted inside ``input_tokens``; this
        is a lower-bound observability figure (the count is the post-prune peak).
        """
        return self.image_blocks * _image_token_est()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "vision_tokens": self.vision_tokens,
            "vision_calls": self.vision_calls,
            "image_blocks": self.image_blocks,
            "estimated_embedded_image_tokens": self.estimated_embedded_image_tokens,
            "by_role": {name: ru.to_dict() for name, ru in self.by_role.items()},
        }


def _normalize(usage: dict[str, Any] | None) -> tuple[int, int, int, int, int]:
    """Map a provider/runner usage dict to (input, output, total, cache_read,
    cache_creation), accepting both Anthropic (``prompt_tokens`` already
    includes cache) and OpenAI (``prompt_tokens``/``completion_tokens``) key
    conventions — matching ``worker_hook.py``'s defensive reads.
    """
    if not usage:
        return (0, 0, 0, 0, 0)
    inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or 0) or (inp + out)
    cache_read = int(usage.get("cache_read_input_tokens") or usage.get("cached_tokens") or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    return (inp, out, total, cache_read, cache_creation)


def _resolve(task_id: str | None = None) -> str | None:
    """Resolve the task id to account against: explicit arg, else the active
    contextvar, else the registered orchestrator memory's task id."""
    if task_id:
        return task_id
    cur = _current_task.get()
    if cur:
        return cur
    try:
        from superbrowser_bridge.memory import get_orchestrator_memory

        mem = get_orchestrator_memory()
        tid = getattr(mem, "task_id", None) if mem is not None else None
        if tid:
            return tid
    except Exception:  # noqa: BLE001 - resolution is best-effort
        pass
    return None


def _entry(task_id: str) -> "TaskUsage":
    """Get-or-create the registry entry. Used only by :func:`track_task` — the
    record functions deliberately do NOT create, so a late vision call that
    lands after the caller's snapshot/pop can't resurrect an orphan entry."""
    tu = _registry.get(task_id)
    if tu is None:
        tu = TaskUsage(task_id=task_id)
        _registry[task_id] = tu
    return tu


@contextmanager
def track_task(task_id: str) -> Iterator["TaskUsage"]:
    """Mark ``task_id`` as the active task for the duration of the block.

    Sets the contextvar (so nested worker/vision calls attribute correctly) and
    ensures a registry entry exists. The entry is *not* removed on exit — call
    :func:`snapshot` then :func:`pop` after the run completes.
    """
    token = _current_task.set(task_id)
    with _lock:
        tu = _entry(task_id)
    try:
        yield tu
    finally:
        _current_task.reset(token)


def record_brain(
    role: str,
    usage: dict[str, Any] | None,
    *,
    messages: list[dict[str, Any]] | None = None,
    task_id: str | None = None,
) -> None:
    """Record one brain (orchestrator/worker) run's cumulative usage.

    No-op when no task can be resolved, so it is safe to call from anywhere.
    """
    tid = _resolve(task_id)
    if tid is None:
        return
    inp, out, total, cache_read, cache_creation = _normalize(usage)
    n_images = 0
    if messages:
        try:
            from superbrowser_bridge.memory.store import count_image_blocks

            n_images = count_image_blocks(messages)
        except Exception:  # noqa: BLE001 - observability only
            n_images = 0
    with _lock:
        tu = _registry.get(tid)
        if tu is None:  # only account against an explicitly tracked task
            return
        ru = tu.role(role)
        ru.input_tokens += inp
        ru.output_tokens += out
        ru.total_tokens += total
        ru.cache_read_tokens += cache_read
        ru.cache_creation_tokens += cache_creation
        ru.calls += 1
        if n_images > tu.image_blocks:
            tu.image_blocks = n_images


def reconcile_brain(
    role: str,
    usage: dict[str, Any] | None,
    *,
    banked: dict[str, int],
    messages: list[dict[str, Any]] | None = None,
    task_id: str | None = None,
) -> None:
    """Reconcile a brain role to its authoritative end-of-run cumulative usage.

    :func:`UsageHook.after_iteration` banks each completed iteration's usage so
    brain tokens survive a cancellation (the 900s wall-clock cancels the run and
    ``after_run`` never fires). This adds only the DRIFT between the authoritative
    cumulative and what was already banked per-iteration — WITHOUT bumping
    ``calls`` — so a successful run reflects the provider's exact totals while a
    timed-out run keeps its per-iteration sum. ``banked`` is the caller's running
    tally of what it has already recorded for this role.

    No-op when no task resolves, so it is safe to call from anywhere.
    """
    tid = _resolve(task_id)
    if tid is None:
        return
    inp, out, total, cache_read, cache_creation = _normalize(usage)
    d_in = inp - int(banked.get("input_tokens", 0))
    d_out = out - int(banked.get("output_tokens", 0))
    d_total = total - int(banked.get("total_tokens", 0))
    d_cr = cache_read - int(banked.get("cache_read_tokens", 0))
    d_cc = cache_creation - int(banked.get("cache_creation_tokens", 0))
    n_images = 0
    if messages:
        try:
            from superbrowser_bridge.memory.store import count_image_blocks

            n_images = count_image_blocks(messages)
        except Exception:  # noqa: BLE001 - observability only
            n_images = 0
    with _lock:
        tu = _registry.get(tid)
        if tu is None:  # only account against an explicitly tracked task
            return
        ru = tu.role(role)
        ru.input_tokens += d_in
        ru.output_tokens += d_out
        ru.total_tokens += d_total
        ru.cache_read_tokens += d_cr
        ru.cache_creation_tokens += d_cc
        # NB: no ru.calls bump — after_iteration already counted each iteration.
        if n_images > tu.image_blocks:
            tu.image_blocks = n_images


def record_vision(
    tokens_used: int | None,
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    model: str = "",
    task_id: str | None = None,
) -> None:
    """Record one vision-model round-trip. ``calls`` is bumped even when the
    provider returned no token count (the round-trip still happened). Uses the
    input/output split when available, else lands the value in ``total`` only.
    """
    tid = _resolve(task_id)
    if tid is None:
        return
    inp = int(prompt_tokens or 0)
    out = int(completion_tokens or 0)
    total = int(tokens_used or 0) or (inp + out)
    with _lock:
        tu = _registry.get(tid)
        if tu is None:  # only account against an explicitly tracked task
            return
        ru = tu.role("vision")
        ru.input_tokens += inp
        ru.output_tokens += out
        ru.total_tokens += total
        ru.calls += 1


def snapshot(task_id: str | None = None) -> "TaskUsage | None":
    """Return a deep copy of the current accumulation for ``task_id``."""
    tid = _resolve(task_id)
    if tid is None:
        return None
    with _lock:
        tu = _registry.get(tid)
        if tu is None:
            return None
        copy = TaskUsage(task_id=tu.task_id, image_blocks=tu.image_blocks)
        for name, ru in tu.by_role.items():
            copy.by_role[name] = RoleUsage(**ru.to_dict())
        return copy


def pop(task_id: str | None = None) -> "TaskUsage | None":
    """Remove and return the registry entry for ``task_id`` (cleanup)."""
    tid = _resolve(task_id)
    if tid is None:
        return None
    with _lock:
        return _registry.pop(tid, None)


def write_usage_json(
    tu: "TaskUsage | None", *, base_dir: str = _DEFAULT_BASE_DIR
) -> Path | None:
    """Persist ``tu`` to ``<base_dir>/<task_id>/usage.json``. Best-effort."""
    if tu is None:
        return None
    try:
        from superbrowser_bridge.memory.store import memory_dir

        task_dir = memory_dir(tu.task_id, base_dir=base_dir).parent
        path = task_dir / "usage.json"
        path.write_text(json.dumps(tu.to_dict(), indent=2, default=str), encoding="utf-8")
        return path
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        logger.debug("usage.json write failed for {}: {}", getattr(tu, "task_id", "?"), exc)
        return None


class UsageHook(AgentHook):
    """Account a brain run's token usage to the active task, cancellation-safe.

    Appended alongside existing hooks on the orchestrator and worker ``run()``
    calls. The runtime nanobot runner only calls ``after_run`` on the clean
    success path — a ``CancelledError`` (e.g. the eval's 900s ``asyncio.wait_for``
    wall-clock) re-raises WITHOUT it. Relying on ``after_run`` alone therefore
    loses ALL orchestrator/worker tokens on every timed-out run, leaving
    ``vision`` (recorded inline) as the only role, i.e. ``total == vision``.

    Fix: ``after_iteration`` banks each completed iteration's usage immediately —
    the runner calls it at the end of every iteration, before the next model call
    a timeout would cancel — so the tokens of every completed iteration survive.
    ``after_run`` then reconciles to the provider's authoritative cumulative on
    the success path. Net: success runs are exact; cancelled runs keep the sum of
    the iterations that finished.

    Note the two ``context.usage`` shapes: ``after_iteration``'s is *per
    iteration* (``runner.py``: ``context.usage = dict(raw_usage)``), while
    ``after_run``'s is *cumulative* (``dict(result.usage)``).
    """

    def __init__(self, role: str) -> None:
        super().__init__()
        self.role = role
        # Running tally of what THIS hook has already recorded, so after_run
        # can add only the drift vs the authoritative cumulative.
        self._banked: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }

    async def after_iteration(self, context: Any) -> None:
        try:
            usage = dict(getattr(context, "usage", None) or {})
            if not usage:
                return
            record_brain(self.role, usage)  # adds tokens + bumps calls
            inp, out, total, cr, cc = _normalize(usage)
            self._banked["input_tokens"] += inp
            self._banked["output_tokens"] += out
            self._banked["total_tokens"] += total
            self._banked["cache_read_tokens"] += cr
            self._banked["cache_creation_tokens"] += cc
        except Exception:  # noqa: BLE001 - accounting must never break a run
            pass

    async def after_run(self, context: Any) -> None:
        try:
            reconcile_brain(
                self.role,
                dict(getattr(context, "usage", None) or {}),
                banked=self._banked,
                messages=getattr(context, "messages", None),
            )
        except Exception:  # noqa: BLE001 - accounting must never break a run
            pass
