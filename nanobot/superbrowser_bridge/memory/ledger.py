"""Pure-data layer for the memory subsystem.

Every dataclass here is small, JSON-roundtrippable, and has no I/O.
The LLM never reads these dataclasses directly - it reads
``Ledger.render(role)`` which produces a compact text block.

Conventions:
- ``recent`` is a bounded deque (maxlen=3) - older steps are still in
  steps.jsonl for recall, but not in the rendered view.
- ``episodic`` collects per-subgoal summaries (written by subgoal
  compaction in step 10) and free-form notes.
- Each dataclass carries an optional ``subgoal`` tag so the worker
  slice can filter to "what's relevant for this subgoal".
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


Role = Literal["orchestrator", "worker"]


def _fmt_time(ts: float) -> str:
    """Format a unix timestamp as HH:MM:SS for legacy serialization.

    State.py used to write step_history entries with a ``time`` field
    in HH:MM:SS shape. Several callers (telemetry, the deprecated
    export path) read that field. Preserve the contract so downstream
    consumers don't break during the cutover.
    """
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except (ValueError, OSError):
        return ""


_RECENT_MAXLEN = 3
_ACTIVITY_LOG_MAXLEN = 30
_RENDER_MAX_FACTS = 30
_RENDER_MAX_DEAD_ENDS = 12
_RENDER_MAX_CHECKPOINTS = 10
_RENDER_MAX_EPISODIC = 8

# Item 3 — single-line instruction at the top of the rendered ledger
# block. Goes into messages[0]["content"][1] (the dynamic ledger
# block) every turn. Steers the model toward the structured ledger
# when older message-history bulk has been gutted by Item 1's
# auto-archive — the action results in those messages now read as
# "[archived]" markers, so the ledger is the only source of truth
# for what happened earlier in the task.
_TRUST_LEDGER_INSTRUCTION = (
    "NOTE: Trust this Agent Ledger as the authoritative structured "
    "memory of the task. Older message-history details may have been "
    "archived to bounded markers; when the ledger and message history "
    "disagree, the ledger wins."
)

# Tools whose calls naturally come in streaks during a single task
# (form filling, multi-element clicks, scrollable lists). Phase 5
# chunking treats consecutive calls of these tools on the same URL
# as a single conceptual action in the rendered ``recent[]`` view.
_CHUNKABLE_TOOLS = frozenset(
    {
        "browser_type",
        "browser_click",
        "browser_click_at",
        "browser_scroll",
    }
)


def _chunk_label(tool: str, count: int) -> str:
    """Short human-readable phrase for a chunked tool streak."""
    return {
        "browser_type": f"filled {count} fields",
        "browser_click": f"clicked {count} elements",
        "browser_click_at": f"clicked {count} vision targets",
        "browser_scroll": f"scrolled {count} times",
    }.get(tool, f"{tool} x{count}")


def _normalize_url_for_match(url: str) -> tuple[str, str]:
    """Normalize ``url`` to ``(domain, path)`` for state-keyed lookups.

    Strips protocol, ``www.`` prefix, port, and trailing slash (except
    on bare ``/``). Returns ``("", "")`` for empty input.

    The same canonical form is used by ``Ledger`` render grouping and
    ``Memory.dead_ends_for_url`` so cross-task site_model URLs match
    live agent URLs even when www/protocol/trailing-slash differs.
    """
    if not url:
        return ("", "")
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        domain = (parsed.netloc or "").lower().split(":")[0]
        if domain.startswith("www."):
            domain = domain[4:]
        path = parsed.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        return (domain, path)
    except Exception:
        return ("", url)


def _short_url(url: str) -> str:
    """Strip protocol + host so the chunk line stays readable.

    'https://www.wineaccess.com/store/white-wine/' -> '/store/white-wine/'
    """
    if not url:
        return ""
    try:
        if "://" in url:
            url = url.split("://", 1)[1]
        slash = url.find("/")
        return url[slash:] if slash >= 0 else "/"
    except Exception:
        return url


def _render_recent_chunked(recent: list["StepOutcome"]) -> list[str]:
    """Render the bounded recent[] view, collapsing consecutive
    same-tool same-URL streaks into a single chunk line.

    A streak of 5 ``browser_type`` calls on /checkout/ renders as
    ``✓ filled 5 fields on /checkout/`` instead of 5 separate lines.
    Solo calls render via the existing StepOutcome.render_line.

    Non-chunkable tools (navigate, screenshot, get_markdown) always
    render solo; chunking would obscure semantically distinct actions.
    """
    if not recent:
        return []
    lines: list[str] = []
    streak_tool: str | None = None
    streak_url: str | None = None
    streak_count = 0
    streak_first: "StepOutcome | None" = None
    streak_any_failed = False

    def flush() -> None:
        nonlocal streak_first, streak_count, streak_any_failed
        if streak_first is None:
            return
        if streak_count > 1 and streak_tool in _CHUNKABLE_TOOLS:
            marker = "✗" if streak_any_failed else "✓"
            label = _chunk_label(streak_tool or "", streak_count)
            short = _short_url(streak_url or "")
            url_part = f" on {short}" if short else ""
            lines.append(f"  {marker} {label}{url_part}  [{streak_tool}]")
        else:
            lines.append(streak_first.render_line())
        streak_first = None
        streak_count = 0
        streak_any_failed = False

    for step in recent:
        if (
            step.tool == streak_tool
            and step.url == streak_url
            and step.tool in _CHUNKABLE_TOOLS
        ):
            streak_count += 1
            streak_any_failed = streak_any_failed or not step.success
            continue
        flush()
        streak_tool = step.tool
        streak_url = step.url
        streak_count = 1
        streak_first = step
        streak_any_failed = not step.success
    flush()
    return lines


@dataclass
class Fact:
    """A discrete piece of knowledge the agent has gathered.

    Keys are short, human-readable identifiers; values are short
    strings. If a piece of data doesn't fit in one short line, it
    belongs in episodic, not facts.

    ``category`` is a free-form string with documented values rather
    than an enum: ``observation`` (default), ``constraint``,
    ``preference``, ``identity``, ``credential``, ``derived``. The
    render layer doesn't enforce — it groups when callers cooperate.
    ``last_referenced_at`` is bumped by recall / re-writes; MRU
    ordering uses it for the fact-render in long-running tasks.
    """

    key: str
    value: str
    source_step: int = -1
    confidence: float = 1.0
    subgoal: str | None = None
    timestamp: float = field(default_factory=time.time)
    category: str = "observation"
    last_referenced_at: float = 0.0

    def __post_init__(self) -> None:
        # Default last_referenced_at to the creation timestamp so newly
        # added facts naturally sort to the top of MRU views before they
        # have ever been queried.
        if not self.last_referenced_at:
            self.last_referenced_at = self.timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source_step": self.source_step,
            "confidence": self.confidence,
            "subgoal": self.subgoal,
            "timestamp": self.timestamp,
            "category": self.category,
            "last_referenced_at": self.last_referenced_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Fact":
        return cls(
            key=d["key"],
            value=d.get("value", ""),
            source_step=int(d.get("source_step", -1)),
            confidence=float(d.get("confidence", 1.0)),
            subgoal=d.get("subgoal"),
            timestamp=float(d.get("timestamp", 0.0)),
            category=d.get("category", "observation"),
            last_referenced_at=float(d.get("last_referenced_at", 0.0)),
        )


@dataclass
class Checkpoint:
    """A verified-progress marker - a URL the agent reached intentionally.

    Checkpoints survive aggressive context eviction so the agent can
    answer "where was I making progress?" even when older history is
    gone. Used by routing/rewind logic to recover after regressions.

    ``kind`` groups checkpoints semantically: ``navigation`` (default),
    ``landing``, ``login``, ``search_results``, ``form_filled``,
    ``data_extracted``, ``task_complete``. The render layer groups
    by kind so "where did I log in?" reads at a glance.
    """

    url: str
    title: str = ""
    action: str = ""
    subgoal: str | None = None
    timestamp: float = field(default_factory=time.time)
    kind: str = "navigation"

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "action": self.action,
            "subgoal": self.subgoal,
            "timestamp": self.timestamp,
            "kind": self.kind,
            "time": _fmt_time(self.timestamp),  # legacy HH:MM:SS field
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Checkpoint":
        return cls(
            url=d.get("url", ""),
            title=d.get("title", ""),
            action=d.get("action", ""),
            subgoal=d.get("subgoal"),
            timestamp=float(d.get("timestamp", 0.0)),
            kind=d.get("kind", "navigation"),
        )


@dataclass
class StepOutcome:
    """One agent action and what it produced.

    Compact by design: ``args`` and ``result`` are short summaries, not
    the full payloads. The full tool result lives in the message log
    (until compacted) and on disk in steps.jsonl.

    ``caption`` carries the vision pipeline's semantic description of
    the page state when this step ran (e.g., "white wine listing page
    with Region filter expanded"). The render layer leads with
    ``caption`` when present so the LLM reads "✓ clicked Shop nav →
    /store/" rather than "✓ browser_click_at(V2) → success" (Phase 5).
    """

    tool: str
    args: str = ""
    result: str = ""
    url: str = ""
    success: bool = True
    subgoal: str | None = None
    iteration: int = -1
    timestamp: float = field(default_factory=time.time)
    caption: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args": self.args,
            "result": self.result,
            "url": self.url,
            "success": self.success,
            "subgoal": self.subgoal,
            "iteration": self.iteration,
            "timestamp": self.timestamp,
            "caption": self.caption,
            "time": _fmt_time(self.timestamp),  # legacy HH:MM:SS field
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StepOutcome":
        return cls(
            tool=d.get("tool", ""),
            args=d.get("args", ""),
            result=d.get("result", ""),
            url=d.get("url", ""),
            success=bool(d.get("success", True)),
            subgoal=d.get("subgoal"),
            iteration=int(d.get("iteration", -1)),
            timestamp=float(d.get("timestamp", 0.0)),
            caption=d.get("caption", ""),
        )

    def render_line(self) -> str:
        marker = "✓" if self.success else "✗"
        if self.caption:
            # Caption-first: semantic record leads, syntactic tail.
            # Phase 5 design — schema landed here so Phase 5 only
            # changes presentation.
            result_part = f" → {self.result}" if self.result else ""
            return f"  {marker} {self.caption}{result_part}  [{self.tool}]"
        args_part = f"({self.args})" if self.args else ""
        result_part = f" → {self.result}" if self.result else ""
        return f"  {marker} {self.tool}{args_part}{result_part}"


@dataclass
class DeadEnd:
    """A path the agent tried that failed - so it doesn't retry it.

    Sourced from collapsed failures, escalations, regressions. The
    description is intentionally short - one line.

    ``url`` is the page on which the failure happened. State-keyed
    retrieval (``Memory.dead_ends_for_url``) relies on it — without
    URL the agent could only see "this thing failed somewhere" not
    "this thing failed HERE". ``cause`` is the coarse classifier
    output from ``hook._classify_failure`` (network / bot_block /
    stale_selector / postcondition_miss / etc.).
    """

    description: str
    subgoal: str | None = None
    timestamp: float = field(default_factory=time.time)
    url: str = ""
    cause: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "subgoal": self.subgoal,
            "timestamp": self.timestamp,
            "url": self.url,
            "cause": self.cause,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DeadEnd":
        return cls(
            description=d.get("description", ""),
            subgoal=d.get("subgoal"),
            timestamp=float(d.get("timestamp", 0.0)),
            url=d.get("url", ""),
            cause=d.get("cause", "unknown"),
        )


@dataclass
class Ledger:
    """The agent's semantic state - the implicit half of memory.

    Two views into the same data:
    - ``render(role="orchestrator")`` shows everything; this is what
      the orchestrator session reads each turn.
    - ``render(role="worker")`` returns ``slice_for_worker(subgoal)`` -
      the current subgoal, subgoal-tagged facts, recent outcomes only.

    Ledger is plain data - LedgerStore handles all I/O.
    """

    goal: str = ""
    plan: list[str] = field(default_factory=list)
    subgoal: str = ""
    facts: dict[str, Fact] = field(default_factory=dict)
    dead_ends: list[DeadEnd] = field(default_factory=list)
    recent: "deque[StepOutcome]" = field(
        default_factory=lambda: deque(maxlen=_RECENT_MAXLEN)
    )
    checkpoints: list[Checkpoint] = field(default_factory=list)
    episodic: list[str] = field(default_factory=list)
    best_checkpoint: Checkpoint | None = None
    subgoal_message_floor: int = -1
    step_count: int = 0
    # ``all_steps`` is the full in-memory step list, populated as the
    # task runs. It survives across iterations within one process but
    # is not serialized to ledger.json (steps.jsonl is the on-disk
    # ground truth - LedgerStore.load rehydrates this field from it).
    # Callers that need a full step view (worker_hook, telemetry,
    # delegation diagnostics) read from here; the bounded ``recent``
    # exists for the render layer only.
    all_steps: list[StepOutcome] = field(default_factory=list)
    # Tracks the current URL the agent reasons about; navigation
    # mechanics (regression detection, URL visit counts) still live
    # in BrowserSessionState, but the rendered ledger and resumption
    # path need the value here too.
    current_url: str = ""
    # Bounded HH:MM:SS audit trail of ad-hoc actions logged via
    # BrowserSessionState.log_activity. Distinct from ``recent`` (full
    # step outcomes) and ``episodic`` (subgoal summaries / free-form
    # notes). Capped to ``_ACTIVITY_LOG_MAXLEN`` entries; older
    # entries fall off the front.
    activity_log: list[str] = field(default_factory=list)

    # ----- mutation helpers (pure data, no I/O) -----

    def append_step(self, outcome: StepOutcome) -> StepOutcome | None:
        """Push to recent and all_steps; return the displaced StepOutcome.

        The displaced step (from the bounded recent deque) is the
        caller's signal to consider persisting it differently, but
        all steps are always retained in ``all_steps`` regardless.
        """
        displaced: StepOutcome | None = None
        if self.recent.maxlen is not None and len(self.recent) == self.recent.maxlen:
            displaced = self.recent[0]
        self.recent.append(outcome)
        self.all_steps.append(outcome)
        self.step_count += 1
        return displaced

    def add_fact(self, fact: Fact) -> None:
        self.facts[fact.key] = fact

    def remove_fact(self, key: str) -> bool:
        return self.facts.pop(key, None) is not None

    def add_dead_end(self, dead_end: DeadEnd) -> None:
        self.dead_ends.append(dead_end)

    def add_checkpoint(self, checkpoint: Checkpoint) -> None:
        self.checkpoints.append(checkpoint)
        # Best checkpoint defaults to the most recent one. Callers with
        # a richer notion of "best" can overwrite directly.
        self.best_checkpoint = checkpoint

    def add_episode(self, text: str) -> None:
        if text:
            self.episodic.append(text)

    def add_activity(self, entry: str) -> None:
        if not entry:
            return
        self.activity_log.append(entry)
        while len(self.activity_log) > _ACTIVITY_LOG_MAXLEN:
            self.activity_log.pop(0)

    # ----- JSON serialization (round-trip with LedgerStore) -----

    def to_dict(self) -> dict[str, Any]:
        # ``all_steps`` is deliberately omitted - it can be hundreds of
        # entries and is mirrored on disk in steps.jsonl. ``recent`` is
        # included so a process restart preserves the bounded render
        # window without an immediate steps.jsonl read.
        return {
            "goal": self.goal,
            "plan": list(self.plan),
            "subgoal": self.subgoal,
            "facts": {k: v.to_dict() for k, v in self.facts.items()},
            "dead_ends": [d.to_dict() for d in self.dead_ends],
            "recent": [s.to_dict() for s in self.recent],
            "checkpoints": [c.to_dict() for c in self.checkpoints],
            "episodic": list(self.episodic),
            "best_checkpoint": (
                self.best_checkpoint.to_dict() if self.best_checkpoint else None
            ),
            "subgoal_message_floor": self.subgoal_message_floor,
            "step_count": self.step_count,
            "current_url": self.current_url,
            "activity_log": list(self.activity_log),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Ledger":
        recent_data = d.get("recent") or []
        recent: deque[StepOutcome] = deque(
            (StepOutcome.from_dict(s) for s in recent_data),
            maxlen=_RECENT_MAXLEN,
        )
        best_cp_raw = d.get("best_checkpoint")
        return cls(
            goal=d.get("goal", ""),
            plan=list(d.get("plan") or []),
            subgoal=d.get("subgoal", ""),
            facts={
                k: Fact.from_dict(v) for k, v in (d.get("facts") or {}).items()
            },
            dead_ends=[DeadEnd.from_dict(x) for x in (d.get("dead_ends") or [])],
            recent=recent,
            checkpoints=[
                Checkpoint.from_dict(x) for x in (d.get("checkpoints") or [])
            ],
            episodic=list(d.get("episodic") or []),
            best_checkpoint=(
                Checkpoint.from_dict(best_cp_raw) if best_cp_raw else None
            ),
            subgoal_message_floor=int(d.get("subgoal_message_floor", -1)),
            step_count=int(d.get("step_count", 0)),
            # all_steps not in snapshot - LedgerStore.load() rebuilds it
            # from steps.jsonl after from_dict returns.
            all_steps=[],
            current_url=d.get("current_url", ""),
            activity_log=list(d.get("activity_log") or []),
        )

    # ----- rendering -----

    def render(self, role: Role = "orchestrator") -> str:
        if role == "worker":
            return self.slice_for_worker(self.subgoal or None)
        return self._render_full()

    def _render_full(self) -> str:
        parts: list[str] = [_TRUST_LEDGER_INSTRUCTION]

        if self.goal:
            parts.append(f"GOAL: {self.goal}")

        if self.plan:
            parts.append("PLAN:")
            for i, item in enumerate(self.plan, start=1):
                marker = " ← current" if item == self.subgoal else ""
                parts.append(f"  {i}. {item}{marker}")
        elif self.subgoal:
            parts.append(f"SUBGOAL: {self.subgoal}")

        if self.facts:
            parts.append("FACTS:")
            # MRU + current-subgoal-first ordering. Long-running tasks
            # accumulate facts; without sorting we'd keep showing the
            # oldest ones and truncate the freshest into "... and N more".
            sorted_facts = sorted(
                self.facts.values(),
                key=lambda f: (
                    0 if f.subgoal == self.subgoal else 1,
                    -(f.last_referenced_at or f.timestamp),
                ),
            )
            shown = sorted_facts[:_RENDER_MAX_FACTS]
            for f in shown:
                tag = f" [subgoal={f.subgoal}]" if f.subgoal else ""
                cat = f" ({f.category})" if f.category and f.category != "observation" else ""
                parts.append(f"  - {f.key} = {f.value}{cat}{tag}")
            extra = len(self.facts) - len(shown)
            if extra > 0:
                parts.append(f"  ... and {extra} more (use memory_recall to query)")

        if self.dead_ends:
            # State-keyed dead-ends are surfaced FIRST and labeled
            # prominently. They're the most actionable ones: "you're
            # standing on this URL right now, here's what already failed".
            cur_key = _normalize_url_for_match(self.current_url)
            here = [
                d for d in self.dead_ends
                if d.url and _normalize_url_for_match(d.url) == cur_key
            ]
            other = [d for d in self.dead_ends if d not in here]
            if here:
                parts.append(f"DEAD_ENDS ON CURRENT URL ({self.current_url}):")
                for d in here[-_RENDER_MAX_DEAD_ENDS:]:
                    cause = f" [{d.cause}]" if d.cause and d.cause != "unknown" else ""
                    parts.append(f"  - {d.description}{cause}")
            if other:
                parts.append("DEAD_ENDS (other URLs):")
                shown_dead = other[-_RENDER_MAX_DEAD_ENDS:]
                for d in shown_dead:
                    cause = f" [{d.cause}]" if d.cause and d.cause != "unknown" else ""
                    url_part = f" @{d.url}" if d.url else ""
                    tag = f" [subgoal={d.subgoal}]" if d.subgoal else ""
                    parts.append(f"  - {d.description}{cause}{url_part}{tag}")

        if self.checkpoints:
            parts.append("CHECKPOINTS:")
            shown_cp = self.checkpoints[-_RENDER_MAX_CHECKPOINTS:]
            # Group by kind so "where did I log in?" reads at a glance.
            by_kind: dict[str, list[Checkpoint]] = {}
            for c in shown_cp:
                by_kind.setdefault(c.kind or "navigation", []).append(c)
            for kind, cps in by_kind.items():
                if len(by_kind) > 1:
                    parts.append(f"  [{kind}]")
                for c in cps:
                    title = f' "{c.title}"' if c.title else ""
                    parts.append(f"  - {c.url}{title}")

        if self.recent:
            parts.append("RECENT:")
            for line in _render_recent_chunked(list(self.recent)):
                parts.append(line)

        if self.episodic:
            parts.append("EPISODIC:")
            shown_ep = self.episodic[-_RENDER_MAX_EPISODIC:]
            for e in shown_ep:
                parts.append(f"  - {e}")

        return "\n".join(parts) if parts else "(empty ledger)"

    def slice_for_worker(self, subgoal_id: str | None) -> str:
        """A focused view for the worker session.

        Includes only what the worker needs to execute the current
        subgoal: the subgoal itself, the most recent step outcomes,
        subgoal-tagged facts (or all facts if untagged), and recent
        dead-ends in the same subgoal. URL-matched dead-ends are
        surfaced first regardless of subgoal tag — they're what would
        bite the worker on its next click.
        """
        parts: list[str] = [_TRUST_LEDGER_INSTRUCTION]

        if self.goal:
            parts.append(f"GOAL: {self.goal}")
        active_subgoal = subgoal_id or self.subgoal
        if active_subgoal:
            parts.append(f"SUBGOAL: {active_subgoal}")

        relevant_facts = [
            f
            for f in self.facts.values()
            if f.subgoal is None or f.subgoal == active_subgoal
        ]
        if relevant_facts:
            parts.append("FACTS:")
            # MRU-sorted within the worker's relevance window.
            relevant_facts.sort(
                key=lambda f: -(f.last_referenced_at or f.timestamp),
            )
            for f in relevant_facts[: _RENDER_MAX_FACTS // 2]:
                cat = f" ({f.category})" if f.category and f.category != "observation" else ""
                parts.append(f"  - {f.key} = {f.value}{cat}")

        # URL-matched dead-ends first (state-keyed), then subgoal-relevant.
        cur_key = _normalize_url_for_match(self.current_url)
        here = [
            d for d in self.dead_ends
            if d.url and _normalize_url_for_match(d.url) == cur_key
        ]
        if here:
            parts.append(f"DEAD_ENDS ON CURRENT URL ({self.current_url}):")
            for d in here[-_RENDER_MAX_DEAD_ENDS // 2 :]:
                cause = f" [{d.cause}]" if d.cause and d.cause != "unknown" else ""
                parts.append(f"  - {d.description}{cause}")

        relevant_dead = [
            d
            for d in self.dead_ends
            if (d.subgoal is None or d.subgoal == active_subgoal) and d not in here
        ]
        if relevant_dead:
            parts.append("DEAD_ENDS:")
            for d in relevant_dead[-_RENDER_MAX_DEAD_ENDS // 2 :]:
                cause = f" [{d.cause}]" if d.cause and d.cause != "unknown" else ""
                url_part = f" @{d.url}" if d.url else ""
                parts.append(f"  - {d.description}{cause}{url_part}")

        if self.recent:
            parts.append("RECENT:")
            for line in _render_recent_chunked(list(self.recent)):
                parts.append(line)

        return "\n".join(parts) if parts else "(empty ledger - no subgoal in flight)"
