"""TaskBrief — the brain's persistent working memory for one task.

Background
----------
Arch v2 lost the user's full query at every restart: `WorkerHandoff`
truncated `task_instruction` to 500 chars, and multi-condition queries
("hotel with WiFi AND parking under $100") survived only as TaskPlan
step names — the conditions themselves dropped out.

`TaskBrief` is a first-class object that:
  - Holds the FULL original query verbatim (no truncation, ever).
  - Tracks structured `Constraint`s with status (unverified | satisfied
    | failed | not_applicable). A constraint flips to `satisfied` when
    vision sees the corresponding active filter / target attribute.
  - Holds a high-level `plan_of_attack` and a running
    `chain_of_thought` trail so the brain can self-orient across many
    turns without re-reading the prompt.
  - Survives session restart with full fidelity via handoff_store.

Population strategy
-------------------
Always-LLM extraction at delegation time: a single Gemini Flash text
call against the original query produces the constraint list. The call
is cached on `sha256(original_query)` so retries cost nothing. A
heuristic regex pass runs as a sanity check after the LLM call and any
constraint the regex finds that the LLM missed is appended.

When extraction fails (network blip, malformed JSON), the brief still
populates with `original_query` and an empty constraints list — the
brain can still reason from the verbatim text.

Reconciliation
--------------
`reconcile_from_page_state(brief, page_state)` runs after every
screenshot. It fuzzy-matches `Constraint.canonical_value` against
`PageState.active_filters[*].label` and flips matched constraints to
`satisfied` with vision-derived evidence. The brain doesn't have to
remember to update statuses — vision does it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal, Optional


# ── Schema ───────────────────────────────────────────────────────────


ConstraintKind = Literal[
    "filter", "attribute", "negative", "numeric", "ordering"
]
ConstraintStatus = Literal[
    "unverified", "satisfied", "failed", "not_applicable"
]


@dataclass
class Constraint:
    text: str = ""
    kind: ConstraintKind = "filter"
    canonical_value: str = ""
    operator: str = ""  # "eq" | "lte" | "gte" | "contains" | "not"
    threshold: str = ""
    unit: str = ""
    status: ConstraintStatus = "unverified"
    evidence: str = ""
    last_checked_url: str = ""
    # Arch v4 Move 2: index of a constraint that must be satisfied first
    # ("Portland" before "WiFi" on a hotel-search funnel: enter destination
    # → results render → filters appear). -1 = no prerequisite. The LLM
    # extractor populates this when the query has dependencies; the focus
    # picker skips a constraint whose prerequisite is unverified.
    prerequisite_idx: int = -1
    # Arch v4: stable id (slug). Survives reorderings; used by focus_id
    # and the [CHECKLIST] render. Lazily populated by ensure_id().
    id: str = ""
    # Arch v4: written ONCE on terminal flip (satisfied|failed|na).
    # ≤120 chars. Replaces accumulating per-turn evidence as the
    # canonical post-completion summary line in [CHECKLIST].
    outcome: str = ""

    def ensure_id(self, fallback_idx: int = 0) -> str:
        """Lazily compute a stable slug id from canonical_value/text."""
        if self.id:
            return self.id
        seed = (self.canonical_value or self.text or "").strip().lower()
        slug = re.sub(r"[^a-z0-9]+", "_", seed).strip("_")
        if not slug:
            slug = f"item{fallback_idx + 1}"
        self.id = slug[:24]
        return self.id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Constraint":
        return cls(
            text=str(d.get("text") or "")[:200],
            kind=_coerce_kind(d.get("kind")),
            canonical_value=str(d.get("canonical_value") or "")[:80].lower(),
            operator=str(d.get("operator") or "")[:16],
            threshold=str(d.get("threshold") or "")[:32],
            unit=str(d.get("unit") or "")[:16],
            status=_coerce_status(d.get("status")),
            evidence=str(d.get("evidence") or "")[:160],
            last_checked_url=str(d.get("last_checked_url") or "")[:240],
            prerequisite_idx=int(d.get("prerequisite_idx") if d.get("prerequisite_idx") is not None else -1),
            id=str(d.get("id") or "")[:24],
            outcome=str(d.get("outcome") or "")[:120],
        )


def _coerce_kind(v: Any) -> ConstraintKind:
    s = str(v or "").strip().lower()
    if s in {"filter", "attribute", "negative", "numeric", "ordering"}:
        return s  # type: ignore[return-value]
    aliases = {
        "amenity": "filter", "feature": "filter", "option": "filter",
        "property": "attribute", "trait": "attribute",
        "exclude": "negative", "without": "negative", "no": "negative",
        "price": "numeric", "rating": "numeric", "count": "numeric",
        "sort": "ordering", "order": "ordering", "rank": "ordering",
    }
    return aliases.get(s, "filter")  # type: ignore[return-value]


def _coerce_status(v: Any) -> ConstraintStatus:
    s = str(v or "").strip().lower()
    if s in {"unverified", "satisfied", "failed", "not_applicable"}:
        return s  # type: ignore[return-value]
    return "unverified"


@dataclass
class ChainOfThoughtNote:
    turn: int
    summary: str = ""
    decision: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChainOfThoughtNote":
        return cls(
            turn=int(d.get("turn") or 0),
            summary=str(d.get("summary") or "")[:200],
            decision=str(d.get("decision") or "")[:200],
        )


@dataclass
class TaskBrief:
    original_query: str = ""
    target_url: str = ""
    domain: str = ""
    constraints: list[Constraint] = field(default_factory=list)
    # Legacy (Arch v3) fields. Kept as dataclass fields so existing
    # serialized briefs still deserialize, but Arch v4 no longer
    # renders them. Will be removed after callers migrate.
    plan_of_attack: str = ""
    cot_trail: list[ChainOfThoughtNote] = field(default_factory=list)
    extracted_at: float = 0.0
    extraction_model: str = ""
    version: int = 1
    # Arch v4 Move 2: which constraint the system recommends working on
    # next. Updated by `compute_focus()` after every status flip and by
    # vision/URL reconciliation. The brain can override via
    # browser_update_task_brief; the override sticks until the next
    # auto-recompute fires (e.g. another constraint flips). -1 means
    # "no constraints / all done / no recommendation".
    current_focus_idx: int = -1
    # Arch v4 (sub-goal compression): id-based mirror of
    # current_focus_idx. Updated together with current_focus_idx via
    # _sync_focus_id(); reorder-safe.
    focus_id: str = ""
    # Arch v4: one-line summaries of items closed via mark_constraint
    # (terminal status). Capped at MAX_COMPLETED_LOG. Replaces the
    # accumulating cot_trail with a bounded set of "<text> → <outcome>"
    # entries the brain reads at a glance.
    completed_log: list[str] = field(default_factory=list)
    # Arch v4: turns since last progress signal. Reset on terminal
    # constraint flip; bumped by callers when an attempt yields no
    # status change. Drives the redecompose-on-stuck trigger.
    stuck_counter: int = 0
    # Arch v4: how many times redecompose() has been called for this
    # brief. Capped at MAX_REDECOMPOSE.
    redecompose_count: int = 0

    # Maximum CoT notes retained — legacy. Kept so old code paths
    # writing to cot_trail don't OOM, but cot_trail is no longer
    # rendered into the brain prompt.
    MAX_COT_NOTES: int = 20
    # Arch v4: maximum entries kept in completed_log before old
    # entries roll off. Mirrors the max checklist size.
    MAX_COMPLETED_LOG: int = 12
    # Arch v4: redecompose() refuses to fire more than this many
    # times per task — protects against pathological replanning loops.
    MAX_REDECOMPOSE: int = 2

    def _sync_focus_id(self) -> None:
        """Mirror current_focus_idx onto focus_id (id-based slug)."""
        idx = self.current_focus_idx
        if 0 <= idx < len(self.constraints):
            self.focus_id = self.constraints[idx].ensure_id(idx)
        else:
            self.focus_id = ""

    def bump_stuck(self) -> int:
        """Increment stuck_counter; return the new value."""
        self.stuck_counter += 1
        return self.stuck_counter

    def add_cot_note(self, turn: int, summary: str, decision: str = "") -> None:
        """Legacy: append a CoT note. Arch v4 no longer renders these
        but the field is preserved so older callsites don't crash.
        """
        note = ChainOfThoughtNote(
            turn=int(turn),
            summary=(summary or "").strip()[:200],
            decision=(decision or "").strip()[:200],
        )
        self.cot_trail.append(note)
        if len(self.cot_trail) > self.MAX_COT_NOTES:
            # Keep most recent notes — older context falls off.
            self.cot_trail = self.cot_trail[-self.MAX_COT_NOTES :]
        self.version += 1

    def mark_constraint(
        self,
        index: int,
        status: ConstraintStatus,
        evidence: str = "",
        url: str = "",
        outcome: str = "",
    ) -> bool:
        """Update a constraint's status. Returns True if status changed.

        Arch v4 Move 2: also recomputes `current_focus_idx` after a flip
        so the brain's next-iteration FOCUS pointer reflects the change.

        Arch v4 (sub-goal compression): on a TERMINAL flip
        (satisfied|failed|not_applicable), also writes
        constraint.outcome (≤120 chars), appends a one-line entry to
        completed_log (bounded), and resets stuck_counter.
        """
        if not (0 <= index < len(self.constraints)):
            return False
        c = self.constraints[index]
        if c.status == status:
            return False
        c.status = status
        if evidence:
            c.evidence = evidence[:160]
        if url:
            c.last_checked_url = url[:240]

        if status in {"satisfied", "failed", "not_applicable"}:
            # Pick the most informative source for the outcome line:
            # explicit outcome arg > evidence arg > prior c.evidence.
            out_clean = (outcome or evidence or c.evidence or "").strip()
            if out_clean:
                c.outcome = out_clean[:120]
            label = c.text or c.canonical_value or c.ensure_id(index)
            log_line = f"{label} → {c.outcome}" if c.outcome else label
            self.completed_log.append(log_line[:200])
            if len(self.completed_log) > self.MAX_COMPLETED_LOG:
                self.completed_log = self.completed_log[-self.MAX_COMPLETED_LOG :]
            # A terminal flip is progress — clear stuck counter.
            self.stuck_counter = 0

        self.version += 1
        # Recompute focus on every status change. Cheap (linear in
        # constraint count, typically <10).
        self.current_focus_idx = compute_focus(self)
        self._sync_focus_id()
        return True

    def find_constraint_by_canonical(self, canonical: str) -> int:
        """Return index of the first constraint whose canonical_value
        fuzzy-matches `canonical`, or -1.
        """
        needle = (canonical or "").strip().lower()
        if not needle:
            return -1
        for i, c in enumerate(self.constraints):
            cv = (c.canonical_value or "").strip().lower()
            if not cv:
                continue
            if cv == needle or cv in needle or needle in cv:
                return i
        return -1

    def counts(self) -> tuple[int, int, int]:
        """Return (total, satisfied, failed)."""
        total = len(self.constraints)
        sat = sum(1 for c in self.constraints if c.status == "satisfied")
        fail = sum(1 for c in self.constraints if c.status == "failed")
        return total, sat, fail

    def is_complete(self) -> bool:
        """True when every constraint is satisfied."""
        return bool(self.constraints) and all(
            c.status == "satisfied" for c in self.constraints
        )

    def focus_line(self) -> str:
        """Render the [FOCUS] line for inclusion in iteration prompts.

        Arch v4 Move 2: surfaces the system-recommended next constraint
        so the brain isn't free-floating. Returns an empty string when
        focus is unset, out of range, or the brief has no constraints.
        """
        idx = self.current_focus_idx
        if not (0 <= idx < len(self.constraints)):
            return ""
        c = self.constraints[idx]
        label = c.canonical_value or c.text or f"#{idx + 1}"
        return (
            f"[FOCUS] #{idx + 1} {label!r} "
            f"({c.kind}, {c.status}) — system recommends attacking next"
        )

    def to_brain_text(self, *, compact: bool = False) -> str:
        """Render the brief for inclusion in tool result captions.

        Arch v4 — fixed-shape render:
          - compact=True: one-line `[BRIEF v=N] checklist=sat/total ...`
          - compact=False: `[TASK_BRIEF v=N]`, original query, [CHECKLIST]
            block, optional [FOCUS] line, optional last 3 completed_log.

        cot_trail and plan_of_attack are still stored for back-compat
        (deserialization of old saved briefs) but are no longer rendered.
        """
        total, sat, fail = self.counts()
        if compact:
            bits = [
                f"[BRIEF v={self.version}]",
                f"checklist={sat}/{total}",
            ]
            if fail:
                bits.append(f"failed={fail}")
            if self.focus_id:
                bits.append(f"focus={self.focus_id}")
            return " ".join(bits)

        lines: list[str] = [f"[TASK_BRIEF v={self.version}]"]
        # Original query — verbatim, never truncated. Brain re-reads
        # exact phrasing here.
        if self.original_query:
            lines.append(f"Original: {self.original_query}")
        if self.constraints:
            lines.append(self.render_checklist_block())
        focus_line = self.focus_line()
        if focus_line:
            lines.append(focus_line)
        if self.completed_log:
            tail = self.completed_log[-3:]
            lines.append("Recently closed:")
            for entry in tail:
                lines.append(f"  · {entry}")
        return "\n".join(lines)

    def render_checklist_block(self) -> str:
        """Render the `[CHECKLIST]` block per Arch v4 §B template.

        Format:
            [CHECKLIST] N items, S done, F failed
              - [done]    1) <text>   → <outcome>
              - [active]  2) <text>   ← focus
              - [open]    3) <text>
              - [blocked] 4) <text>   → <reason>

        Each line ≤120 chars; max 12 items rendered (the checklist is
        already capped at extraction time so this is a guard, not a cut).
        """
        total, sat, fail = self.counts()
        header = f"[CHECKLIST] {total} items, {sat} done"
        if fail:
            header += f", {fail} failed"
        lines: list[str] = [header]
        focus_idx = self.current_focus_idx
        for i, c in enumerate(self.constraints[:12]):
            label_text = (c.text or c.canonical_value or c.ensure_id(i))[:60]
            if c.status == "satisfied":
                marker = "[done]   "
                tail = f" → {c.outcome}" if c.outcome else ""
            elif c.status == "failed":
                marker = "[blocked]"
                tail = f" → {c.outcome or 'failed'}"
            elif c.status == "not_applicable":
                marker = "[na]     "
                tail = f" → {c.outcome or 'not applicable'}"
            elif i == focus_idx:
                marker = "[active] "
                tail = "  ← focus"
            else:
                marker = "[open]   "
                tail = ""
            line = f"  - {marker} {i + 1}) {label_text}{tail}"
            lines.append(line[:120])
        return "\n".join(lines)

    def render_query_block(self) -> str:
        """Render the `[QUERY]` block per Arch v4 §B template (≤800 chars).

        Brain pulls the original wording from here on every step — this
        is the persistent task memory humans rely on.
        """
        q = (self.original_query or "").strip()
        if not q:
            return "[QUERY] (no original query recorded)"
        if len(q) > 800:
            q = q[:797] + "..."
        return f"[QUERY] {q}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "target_url": self.target_url,
            "domain": self.domain,
            "constraints": [c.to_dict() for c in self.constraints],
            "plan_of_attack": self.plan_of_attack,
            "cot_trail": [n.to_dict() for n in self.cot_trail],
            "extracted_at": self.extracted_at,
            "extraction_model": self.extraction_model,
            "version": self.version,
            "current_focus_idx": self.current_focus_idx,
            "focus_id": self.focus_id,
            "completed_log": list(self.completed_log),
            "stuck_counter": self.stuck_counter,
            "redecompose_count": self.redecompose_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskBrief":
        focus_raw = d.get("current_focus_idx")
        focus = int(focus_raw) if focus_raw is not None else -1
        return cls(
            original_query=str(d.get("original_query") or ""),
            target_url=str(d.get("target_url") or ""),
            domain=str(d.get("domain") or ""),
            constraints=[
                Constraint.from_dict(c)
                for c in (d.get("constraints") or [])
                if isinstance(c, dict)
            ],
            plan_of_attack=str(d.get("plan_of_attack") or ""),
            cot_trail=[
                ChainOfThoughtNote.from_dict(n)
                for n in (d.get("cot_trail") or [])
                if isinstance(n, dict)
            ],
            extracted_at=float(d.get("extracted_at") or 0.0),
            extraction_model=str(d.get("extraction_model") or ""),
            version=int(d.get("version") or 1),
            current_focus_idx=focus,
            focus_id=str(d.get("focus_id") or "")[:24],
            completed_log=[
                str(s)[:200] for s in (d.get("completed_log") or [])
                if isinstance(s, str)
            ],
            stuck_counter=int(d.get("stuck_counter") or 0),
            redecompose_count=int(d.get("redecompose_count") or 0),
        )


def compute_focus(brief: "TaskBrief", page_state: Any = None) -> int:
    """Pick the next constraint the system recommends working on.

    Selection layers (in order — earlier signals override later ones):

    1. **Prerequisite chain**: skip any constraint whose `prerequisite_idx`
       points to another constraint that isn't `satisfied`. Forces the
       brain to handle "Portland" before "WiFi" on hotel-search funnels
       where filters don't render until the destination is set.

    2. **Funnel-aware** (when `page_state` is supplied with a populated
       `funnel` field): prefer kinds that match the current funnel step
       (e.g. on `destination_input` prefer `attribute`; on `results_list`
       prefer `filter` and `numeric`). Maps via `_FUNNEL_KIND_PREFERENCE`.

    3. **Kind ordering fallback** (no prerequisite + no funnel signal):
       filter > attribute > numeric > negative > ordering. Hotel-search-
       biased default; harmless when prerequisite/funnel signals fire.

    Returns the index of the recommended constraint, or -1 when no
    unverified constraint is reachable (all satisfied/failed/n/a, or
    every unverified one is blocked by an unsatisfied prerequisite).
    """
    if not brief.constraints:
        return -1

    # Determine which constraints are eligible (unverified + prerequisite
    # satisfied or no prerequisite).
    eligible: list[int] = []
    for i, c in enumerate(brief.constraints):
        if c.status != "unverified":
            continue
        pre = getattr(c, "prerequisite_idx", -1)
        if isinstance(pre, int) and 0 <= pre < len(brief.constraints):
            if brief.constraints[pre].status != "satisfied":
                continue  # blocked
        eligible.append(i)
    if not eligible:
        return -1

    # Funnel-aware preference. PageState may be a dataclass or a dict;
    # tolerate both. Empty funnel → fall through.
    funnel = ""
    if page_state is not None:
        funnel = (
            getattr(page_state, "funnel", None)
            if not isinstance(page_state, dict)
            else page_state.get("funnel")
        ) or ""
    funnel = str(funnel).strip().lower()
    if funnel:
        preferred = _FUNNEL_KIND_PREFERENCE.get(funnel)
        if preferred:
            for kind in preferred:
                for i in eligible:
                    if brief.constraints[i].kind == kind:
                        return i
            # Funnel matched but no eligible constraint of preferred
            # kind — fall through to kind-ordering fallback rather than
            # returning -1 (still pick *something* from the eligibles).

    # Kind-ordering fallback.
    for kind in _KIND_FALLBACK_ORDER:
        for i in eligible:
            if brief.constraints[i].kind == kind:
                return i

    # Defensive: every eligible has an unrecognized kind. Pick first.
    return eligible[0]


# Funnel → ordered list of preferred constraint kinds. When PageState
# reports the page is on a particular funnel step, focus picks an
# eligible constraint whose kind matches the head of this list first.
# Keys are lowercased; the funnel taxonomy comes from
# vision_agent.schemas.PageState.funnel.
_FUNNEL_KIND_PREFERENCE: dict[str, tuple[ConstraintKind, ...]] = {
    "destination_input":  ("attribute", "filter"),
    "search_input":       ("attribute", "filter"),
    "results_list":       ("filter", "numeric", "ordering", "negative", "attribute"),
    "filter_panel":       ("filter", "numeric", "negative", "ordering", "attribute"),
    "booking_form":       ("attribute", "negative", "filter"),
    "checkout":           ("attribute", "filter"),
    "product_detail":     ("attribute", "negative"),
    "comparison":         ("ordering", "filter", "numeric"),
}

# Default kind-ordering when no funnel/prereq signal applies. Hotel-
# search-biased: filters are cheapest to satisfy via UI clicks; numeric
# (sliders) and ordering (sort dropdowns) come last.
_KIND_FALLBACK_ORDER: tuple[ConstraintKind, ...] = (
    "filter", "attribute", "numeric", "negative", "ordering",
)


def _short_url(u: str) -> str:
    """Trim long URLs to host+path for brief evidence rendering."""
    if not u:
        return ""
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(u)
        path = parts.path or "/"
        if len(path) > 40:
            path = path[:37] + "..."
        return path
    except Exception:
        return u[:50]


# ── Heuristic regex sanity-check ─────────────────────────────────────


_NUMERIC_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    # "under $100", "below $100", "less than 100"
    (
        re.compile(
            r"(?:under|below|less\s+than|cheaper\s+than|max(?:imum)?|up\s+to|<=?)\s*\$?\s*(\d+(?:\.\d+)?)\s*(\w+)?",
            re.I,
        ),
        "lte",
        "price",
    ),
    # "over $100", "above $100", "more than 100"
    (
        re.compile(
            r"(?:over|above|more\s+than|at\s+least|min(?:imum)?|>=?)\s*\$?\s*(\d+(?:\.\d+)?)\s*(\w+)?",
            re.I,
        ),
        "gte",
        "price",
    ),
    # "4 stars", "rating 4+", "4-star"
    (
        re.compile(r"(\d(?:\.\d)?)\s*(?:\+|plus)?\s*(?:-)?\s*star", re.I),
        "gte",
        "rating",
    ),
]

_NEGATIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:no|without|exclude|exclusive\s+of|not?\s+include)\s+([a-z][a-z0-9\s\-]{1,40})", re.I),
]

_FILTER_LIST_PATTERN = re.compile(
    r"\b(?:with|including|that\s+(?:has|have|offer|provides?))\s+([^.;,]{2,120})",
    re.I,
)

_ORDERING_PATTERNS = [
    (re.compile(r"\b(?:cheapest|lowest\s+price|least\s+expensive)\b", re.I), "ascending", "price"),
    (re.compile(r"\b(?:most\s+expensive|highest\s+price|priciest)\b", re.I), "descending", "price"),
    (re.compile(r"\b(?:top[\-\s]rated|highest[\-\s]rated|best[\-\s]rated)\b", re.I), "descending", "rating"),
    (re.compile(r"\b(?:newest|most\s+recent|latest)\b", re.I), "descending", "date"),
]


def extract_constraints_heuristic(query: str) -> list[Constraint]:
    """Cheap regex pass — captures common shapes without an LLM call.

    Used as a sanity check after the LLM extraction; constraints found
    here that the LLM missed are appended.
    """
    if not query:
        return []
    out: list[Constraint] = []
    seen_canonical: set[str] = set()

    def _add(c: Constraint) -> None:
        key = (c.canonical_value or c.text).lower()
        if not key or key in seen_canonical:
            return
        seen_canonical.add(key)
        out.append(c)

    # Numeric thresholds
    for pat, op, default_unit in _NUMERIC_PATTERNS:
        for m in pat.finditer(query):
            threshold = m.group(1)
            unit = (m.group(2) or "").strip() if m.lastindex and m.lastindex >= 2 else ""
            unit = unit if unit else default_unit
            _add(Constraint(
                text=m.group(0).strip(),
                kind="numeric",
                canonical_value=default_unit,
                operator=op,
                threshold=threshold,
                unit=unit if unit != "price" else "USD",
            ))

    # Ordering hints
    for pat, direction, axis in _ORDERING_PATTERNS:
        m = pat.search(query)
        if m:
            _add(Constraint(
                text=m.group(0).strip(),
                kind="ordering",
                canonical_value=axis,
                operator=direction,
            ))

    # Negatives — "no pets", "without parking"
    for pat in _NEGATIVE_PATTERNS:
        for m in pat.finditer(query):
            value = m.group(1).strip().rstrip(".,;")
            _add(Constraint(
                text=m.group(0).strip(),
                kind="negative",
                canonical_value=value.lower(),
                operator="not",
            ))

    # Filter lists — "with WiFi and parking and breakfast"
    for m in _FILTER_LIST_PATTERN.finditer(query):
        chunk = m.group(1)
        # Split on " and " / commas / " plus "
        parts = re.split(r"\s+and\s+|,\s*|\s+plus\s+", chunk, flags=re.I)
        for p in parts:
            cleaned = p.strip().rstrip(".,;:")
            if not cleaned or len(cleaned) > 50:
                continue
            # Skip if it already matches a numeric/ordering
            if any(cleaned.lower() in seen for seen in seen_canonical):
                continue
            _add(Constraint(
                text=cleaned,
                kind="filter",
                canonical_value=cleaned.lower(),
                operator="contains",
            ))

    return out


# ── LLM extraction ───────────────────────────────────────────────────


_EXTRACTION_CACHE: dict[str, list[Constraint]] = {}
_EXTRACTION_LOCK = asyncio.Lock()


_EXTRACTOR_SYSTEM_PROMPT = """\
You decompose a user's web-task query into structured CONSTRAINTS that
must be verified during the task.

A constraint is one filter, attribute, negative, numeric threshold, or
ordering preference the user has expressed. Examples:
  - "under $100"               -> {kind:numeric, canonical_value:price, operator:lte, threshold:100, unit:USD}
  - "with WiFi"                -> {kind:filter, canonical_value:wifi, operator:contains}
  - "no pets"                  -> {kind:negative, canonical_value:pets, operator:not}
  - "4+ star rating"           -> {kind:numeric, canonical_value:rating, operator:gte, threshold:4}
  - "cheapest first"           -> {kind:ordering, canonical_value:price, operator:ascending}
  - "near downtown"            -> {kind:attribute, canonical_value:downtown, operator:contains}

OUTPUT — return ONLY this JSON object, no commentary:
{
  "plan_of_attack": "<3-5 sentence high-level approach>",
  "constraints": [
    {
      "text":"<short verbatim quote>",
      "kind":"filter|attribute|negative|numeric|ordering",
      "canonical_value":"<lowercase normalized value>",
      "operator":"eq|lte|gte|contains|not|ascending|descending",
      "threshold":"<numeric threshold or empty>",
      "unit":"<USD|stars|miles|empty>"
    }
  ]
}

Rules:
- Extract ONLY constraints the user explicitly stated. Do not invent.
- Decompose compound phrases ("WiFi AND parking" -> two constraints).
- For "around $100" or "near $100" — kind:numeric, operator:eq, threshold:100.
- Keep canonical_value short (one to three words, lowercase).
- 1 to 12 constraints. Empty array is valid for queries with no constraints.
"""


def _repair_truncated_json(content: str) -> Optional[dict]:
    """Best-effort recovery of constraints from a truncated/malformed
    LLM JSON response.

    Strategy:
      1. Strip ```json fences if present.
      2. Try to locate the `constraints` array and extract complete
         object entries by counting braces.
      3. If a complete `plan_of_attack` string is parseable, keep it.

    Returns a dict with keys `plan_of_attack`, `constraints` on partial
    success, or None on total failure.
    """
    if not content:
        return None
    # Strip code fences.
    s = content.strip()
    if s.startswith("```"):
        # Drop first fence line and trailing fence.
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    # Try parsing the cleaned content directly first.
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Walk the string and extract complete top-level constraint objects.
    constraints_idx = s.find('"constraints"')
    if constraints_idx == -1:
        return None
    array_start = s.find("[", constraints_idx)
    if array_start == -1:
        return None
    objects: list[dict] = []
    i = array_start + 1
    n = len(s)
    while i < n:
        # Skip whitespace + commas.
        while i < n and s[i] in " \t\n\r,":
            i += 1
        if i >= n or s[i] == "]":
            break
        if s[i] != "{":
            break
        # Find balanced closing brace, tracking strings.
        depth = 0
        j = i
        in_str = False
        esc = False
        while j < n:
            ch = s[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        # Got a complete object.
                        candidate = s[i : j + 1]
                        try:
                            obj = json.loads(candidate)
                            if isinstance(obj, dict):
                                objects.append(obj)
                        except json.JSONDecodeError:
                            pass
                        i = j + 1
                        break
            j += 1
        else:
            # Reached end of string mid-object — truncated. Stop.
            break
    if not objects:
        return None
    # Try to recover plan_of_attack string too.
    plan = ""
    plan_match = re.search(
        r'"plan_of_attack"\s*:\s*"((?:\\.|[^"\\])*)"', s
    )
    if plan_match:
        try:
            plan = json.loads(f'"{plan_match.group(1)}"')
        except json.JSONDecodeError:
            plan = plan_match.group(1)
    return {"plan_of_attack": plan, "constraints": objects}


async def extract_constraints_llm(query: str) -> tuple[str, list[Constraint], str]:
    """Call Gemini Flash to extract constraints + plan_of_attack.

    Returns (plan_of_attack, constraints, model_name). On failure or
    timeout, returns ("", [], "fallback").
    """
    if not query.strip():
        return ("", [], "")

    cache_key = hashlib.sha256(query.encode("utf-8", errors="ignore")).hexdigest()
    async with _EXTRACTION_LOCK:
        cached = _EXTRACTION_CACHE.get(cache_key)
    if cached is not None:
        return ("", list(cached), "cached")

    api_key = (
        os.environ.get("VISION_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    )
    if not api_key:
        return ("", [], "no-key")

    model = os.environ.get("TASK_BRIEF_MODEL") or "gemini-2.5-flash"
    base_url = os.environ.get(
        "TASK_BRIEF_BASE_URL"
    ) or "https://generativelanguage.googleapis.com/v1beta/openai"

    try:
        from openai import AsyncOpenAI  # type: ignore
    except Exception:
        return ("", [], "no-sdk")

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=10.0)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _EXTRACTOR_SYSTEM_PROMPT},
                {"role": "user", "content": query[:4000]},
            ],
            response_format={"type": "json_object"},
            max_tokens=1500,
            temperature=0.1,
        )
    except Exception as exc:
        # Network/quota failures are non-fatal — brain can still work
        # off the original_query alone.
        print(f"[task_brief] LLM extraction failed: {exc}")
        return ("", [], "error")
    finally:
        # AsyncOpenAI client is short-lived per call; close to release
        # connections promptly.
        try:
            await client.close()
        except Exception:
            pass

    try:
        content = (resp.choices[0].message.content or "").strip()
    except (AttributeError, IndexError) as exc:
        print(f"[task_brief] extraction response unreadable: {exc}")
        return ("", [], "no-response")
    data: Any = None
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        # Recovery: LLM responses are sometimes truncated mid-string
        # (max_tokens hit) or wrapped in ``` fences. Try to repair.
        recovered = _repair_truncated_json(content)
        if recovered is not None:
            data = recovered
            print(
                f"[task_brief] extraction JSON repair succeeded "
                f"(recovered {len(recovered.get('constraints') or [])} constraints "
                f"from truncated response)"
            )
        else:
            print(f"[task_brief] extraction JSON parse failed: {exc}")
            return ("", [], "parse-error")

    plan = str(data.get("plan_of_attack") or "")[:600]
    raw_constraints = data.get("constraints") or []
    constraints: list[Constraint] = []
    if isinstance(raw_constraints, list):
        for raw in raw_constraints[:12]:
            if not isinstance(raw, dict):
                continue
            constraints.append(Constraint.from_dict(raw))

    async with _EXTRACTION_LOCK:
        _EXTRACTION_CACHE[cache_key] = list(constraints)

    return (plan, constraints, model)


async def build_task_brief(
    query: str,
    *,
    target_url: str = "",
    domain: str = "",
) -> TaskBrief:
    """Construct a TaskBrief from a raw user query.

    1. Calls LLM extractor (Gemini Flash) for constraints + plan.
    2. Runs heuristic regex pass; appends any constraints the LLM missed.
    3. Returns TaskBrief with original_query verbatim.
    """
    if not query:
        return TaskBrief(extracted_at=time.time())

    plan, constraints, model_name = await extract_constraints_llm(query)

    # Append regex-detected constraints the LLM missed.
    heuristic = extract_constraints_heuristic(query)
    seen = {(c.canonical_value or c.text).strip().lower() for c in constraints}
    for c in heuristic:
        key = (c.canonical_value or c.text).strip().lower()
        if key and key not in seen:
            constraints.append(c)
            seen.add(key)

    # Arch v4: cap at MAX_COMPLETED_LOG to align with the per-step
    # checklist render. Extractor already requests 1..12; this is a
    # belt-and-braces guard.
    if len(constraints) > TaskBrief.MAX_COMPLETED_LOG:
        constraints = constraints[: TaskBrief.MAX_COMPLETED_LOG]
    # Arch v4: ensure each constraint has a stable id slug before the
    # brief is exposed to renderers / focus_id mirroring.
    for i, c in enumerate(constraints):
        c.ensure_id(i)

    brief = TaskBrief(
        original_query=query,
        target_url=target_url,
        domain=domain,
        constraints=constraints,
        plan_of_attack=plan,
        cot_trail=[],
        extracted_at=time.time(),
        extraction_model=model_name or "regex-only",
        version=1,
    )
    # Arch v4 Move 2: seed focus on construction so the first iteration
    # already has a system recommendation in [FOCUS].
    brief.current_focus_idx = compute_focus(brief)
    brief._sync_focus_id()
    return brief


def build_task_brief_sync(
    query: str,
    *,
    target_url: str = "",
    domain: str = "",
) -> TaskBrief:
    """Sync wrapper for callers outside an event loop.

    Used by orchestrator_tools.delegate_browser_task which builds the
    brief during a sync nanobot tool call.
    """
    if not query:
        return TaskBrief(extracted_at=time.time())
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        return asyncio.run(
            build_task_brief(query, target_url=target_url, domain=domain)
        )
    # We're inside a running loop — must thread off; this should not
    # happen in the orchestrator path but guards against hangs.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(
            asyncio.run,
            build_task_brief(query, target_url=target_url, domain=domain),
        )
        return fut.result(timeout=15.0)


# ── Reconciliation from PageState ────────────────────────────────────


def reconcile_from_page_state(
    brief: Optional[TaskBrief],
    page_state: Any,
    *,
    current_url: str = "",
) -> int:
    """Inspect page_state.active_filters and flip matching constraints
    to `satisfied`. Returns the number of constraints transitioned.

    `page_state` is a `vision_agent.schemas.PageState` (or a duck-typed
    object). Uses fuzzy match on `canonical_value`.
    """
    if brief is None or page_state is None:
        return 0
    active_filters = getattr(page_state, "active_filters", None) or []
    if not active_filters:
        return 0

    transitions = 0
    for af in active_filters:
        state = getattr(af, "state", "") or ""
        if state not in {"on", "partial"}:
            continue
        label = (getattr(af, "label", "") or "").strip().lower()
        value = (getattr(af, "value", "") or "").strip().lower()
        if not label and not value:
            continue
        # Try label first, then value
        idx = brief.find_constraint_by_canonical(label)
        if idx < 0 and value:
            idx = brief.find_constraint_by_canonical(value)
        if idx < 0:
            continue
        c = brief.constraints[idx]
        if c.status == "satisfied":
            continue
        evidence = f"vision saw '{label}' = {state}"
        if value:
            evidence += f" ({value})"
        if brief.mark_constraint(idx, "satisfied", evidence, current_url):
            transitions += 1
    return transitions


_URL_RECONCILE_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "this", "that",
    "wine", "wines",  # generic noun on wineaccess-like sites — too broad
    "usa", "us", "united", "states",  # country tokens that match generic URLs
    "type", "types", "kind", "kinds", "category", "categories",
    "brand", "make", "model", "name",
    "filter", "filters", "option", "options", "value", "values",
})


def reconcile_from_url(
    brief: Optional[TaskBrief],
    current_url: str,
    *,
    debug: Optional[bool] = None,
) -> int:
    """URL-based constraint reconciliation.

    Many sites encode filter state directly in the URL — a navigate to
    `/store/regions/oregon/`, `/cars/?make=ford`, or
    `/?food_pairings=fish%2Csweets` is itself evidence that the
    corresponding filter is applied. Vision can't always confirm this
    (no visible chip on the listing page until results render), so
    PageState reconciliation alone leaves the constraint unverified.

    Arch v3 fix B (post-trace): the LLM extractor sometimes produces
    multi-word `canonical_value`s with modifier words ("oregon, USA",
    "white wine grape", "willamette valley region"). The previous
    "all tokens present" rule missed these. Now we require either:
      (a) the LONGEST significant token (≥4 chars, non-stopword) hits
          the URL haystack — the "anchor" is usually the place name
          or distinctive feature; OR
      (b) at least ceil(N/2) of the significant tokens hit.
    Stopwords like "wine", "USA", "type" are stripped to avoid
    over-matching. Set `DEBUG_URL_RECONCILE=1` to log per-constraint
    decisions.

    Negative constraints are NOT flipped here — a URL absence isn't
    proof of absence (the user might just not have applied that filter
    yet). Negative reconciliation continues to live in
    `reconcile_negative_constraints` and only fires on observation.
    """
    if brief is None or not current_url:
        return 0
    if debug is None:
        debug = os.environ.get("DEBUG_URL_RECONCILE", "0") == "1"
    try:
        from urllib.parse import urlsplit, parse_qsl, unquote
        parts = urlsplit(current_url)
        path_lower = unquote(parts.path or "").lower()
        qs_pairs = parse_qsl(parts.query or "", keep_blank_values=True)
        qs_values = " ".join(unquote(v) for _, v in qs_pairs).lower()
        # Also include qs KEYS (e.g. ?food_pairings=...) — sometimes
        # the constraint canonical matches the param NAME rather than
        # the value (filter is engaged by mere param presence).
        qs_keys = " ".join(unquote(k) for k, _ in qs_pairs).lower()
        haystack = f"{path_lower} {qs_keys} {qs_values}"
    except Exception as exc:
        if debug:
            print(f"[url_reconcile] urlsplit failed: {exc}")
        return 0
    if not haystack.strip():
        return 0
    # Normalize separators so "white-wine" matches "white wine" tokens.
    normalized = haystack.replace("-", " ").replace("_", " ").replace(",", " ")

    if debug:
        print(f"[url_reconcile] url={current_url!r}")
        print(f"[url_reconcile] haystack={normalized[:240]!r}")

    transitions = 0
    for i, c in enumerate(brief.constraints):
        if c.kind not in {"filter", "attribute", "ordering"}:
            continue
        if c.status == "satisfied":
            continue
        cv = (c.canonical_value or "").strip().lower()
        if not cv or len(cv) < 3:
            if debug:
                print(f"[url_reconcile] skip [{i}] cv={cv!r} (too short)")
            continue
        # Tokenize multi-word canonical values; drop stopwords.
        raw_tokens = [
            t for t in cv.replace("-", " ").replace("_", " ").replace(",", " ").split()
            if len(t) >= 3
        ]
        tokens = [t for t in raw_tokens if t not in _URL_RECONCILE_STOPWORDS]
        if not tokens:
            # All tokens were stopwords — fall back to the raw set so
            # a lone constraint like "wine" still has a chance.
            tokens = raw_tokens
        if not tokens:
            if debug:
                print(f"[url_reconcile] skip [{i}] cv={cv!r} (no tokens after filter)")
            continue

        # The "anchor" is the longest content token (≥4 chars) — typically
        # the place name / distinctive attribute (e.g. "oregon",
        # "willamette", "ford"). Falls back to the longest token when
        # nothing is ≥4 chars.
        anchor_candidates = [t for t in tokens if len(t) >= 4] or list(tokens)
        anchor = max(anchor_candidates, key=len)
        anchor_hit = anchor in normalized
        token_hits = sum(1 for t in tokens if t in normalized)
        threshold = (len(tokens) + 1) // 2  # ceil(N/2)

        matched = anchor_hit or token_hits >= threshold

        if debug:
            print(
                f"[url_reconcile] [{i}] cv={cv!r} kind={c.kind} "
                f"tokens={tokens} anchor={anchor!r} "
                f"anchor_hit={anchor_hit} token_hits={token_hits}/{len(tokens)} "
                f"threshold={threshold} -> {'MATCH' if matched else 'miss'}"
            )

        if matched:
            evidence = f"URL contains '{anchor}'" + (
                f" + {token_hits - 1} tokens" if token_hits > 1 and not anchor_hit else ""
            )
            if brief.mark_constraint(i, "satisfied", evidence, current_url):
                transitions += 1
    return transitions


def reconcile_negative_constraints(
    brief: Optional[TaskBrief],
    page_state: Any,
    *,
    current_url: str = "",
) -> int:
    """Negative constraints flip to `failed` when their canonical value
    is OBSERVED active on the page (e.g. "no pets" + page shows
    "pets allowed: yes").

    Returns the number of transitions.
    """
    if brief is None or page_state is None:
        return 0
    active_filters = getattr(page_state, "active_filters", None) or []
    if not active_filters:
        return 0
    transitions = 0
    for af in active_filters:
        state = getattr(af, "state", "") or ""
        if state != "on":
            continue
        label = (getattr(af, "label", "") or "").strip().lower()
        if not label:
            continue
        for i, c in enumerate(brief.constraints):
            if c.kind != "negative":
                continue
            cv = (c.canonical_value or "").strip().lower()
            if cv and (cv == label or cv in label or label in cv):
                if c.status == "failed":
                    continue
                if brief.mark_constraint(
                    i,
                    "failed",
                    f"vision saw '{label}' active despite negative constraint",
                    current_url,
                ):
                    transitions += 1
    return transitions


def merge_brief_progress(
    old: Optional[TaskBrief],
    new: Optional[TaskBrief],
) -> int:
    """Arch v3 fix I — copy `satisfied`/`failed`/`not_applicable` statuses
    from `old` brief onto matching constraints in `new` brief.

    Used at delegation time when a handoff brief carries over from a prior
    worker (which had progress) but the new delegation built a fresh brief
    from the new (often retry-enriched) instructions. We want the new
    brief's structure (matches current instructions / LLM extraction) but
    the old brief's progress.

    Matching is fuzzy by `canonical_value`:
      1. Exact lowercase canonical match.
      2. Any-token-overlap (≥3 chars) on canonical_value.

    Returns the number of constraints whose status was transferred.
    Does not modify `old`. Modifies `new` in place.
    """
    if old is None or new is None:
        return 0
    def _tokenize(cv: str) -> set[str]:
        """Lowercase + strip separators (-_ ,/.;:) before splitting."""
        s = cv.lower()
        for ch in "-_,/.;:":
            s = s.replace(ch, " ")
        return {t for t in s.split() if len(t) >= 3}

    transferred = 0
    # Build a normalized lookup over old constraints.
    old_by_cv: dict[str, Constraint] = {}
    old_tokenized: list[tuple[set[str], Constraint]] = []
    for c in old.constraints:
        cv = (c.canonical_value or "").strip().lower()
        if not cv:
            continue
        old_by_cv[cv] = c
        toks = _tokenize(cv)
        if toks:
            old_tokenized.append((toks, c))

    for nc in new.constraints:
        if nc.status != "unverified":
            continue  # don't overwrite a status the new extraction set
        ncv = (nc.canonical_value or "").strip().lower()
        if not ncv:
            continue
        match: Optional[Constraint] = old_by_cv.get(ncv)
        if match is None:
            new_toks = _tokenize(ncv)
            if new_toks:
                # Find the old constraint with the most token overlap.
                best_overlap = 0
                best_match: Optional[Constraint] = None
                for old_toks, oc in old_tokenized:
                    overlap = len(new_toks & old_toks)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_match = oc
                if best_overlap > 0:
                    match = best_match
        if match is None:
            continue
        if match.status in ("satisfied", "failed", "not_applicable"):
            nc.status = match.status
            if match.evidence:
                nc.evidence = match.evidence
            if match.last_checked_url:
                nc.last_checked_url = match.last_checked_url
            # Arch v4: also carry over outcome (one-line summary) so the
            # successor brief renders the closed item identically.
            if getattr(match, "outcome", ""):
                nc.outcome = match.outcome
            transferred += 1
    if transferred > 0:
        new.version += 1
    # Arch v4: carry over completed_log + redecompose_count so the
    # successor brief preserves history of what's already been closed
    # and respects the redecompose budget across handoffs.
    if old.completed_log and not new.completed_log:
        new.completed_log = list(old.completed_log[-TaskBrief.MAX_COMPLETED_LOG :])
    new.redecompose_count = max(new.redecompose_count, old.redecompose_count)
    # Arch v4 Move 2: re-seed focus on the new brief whether or not any
    # progress transferred — the new constraints may have shuffled
    # ordering or added prerequisites that change the recommendation.
    new.current_focus_idx = compute_focus(new)
    new._sync_focus_id()
    return transferred


# ── Redecompose (Arch v4) ────────────────────────────────────────────


_REDECOMPOSE_SYSTEM_PROMPT = """\
You re-extract structured constraints for a browser task that has gotten
stuck mid-execution. Some constraints have already been closed. Your job
is to produce the REMAINING checklist tail using:
  - the original user query (unchanged source of truth),
  - the recently_closed items (do NOT re-emit any of these),
  - the current_url_path (to bias toward the current funnel step).

OUTPUT — return ONLY this JSON object, no commentary:
{
  "constraints": [
    {
      "text":"<short verbatim quote>",
      "kind":"filter|attribute|negative|numeric|ordering",
      "canonical_value":"<lowercase normalized value>",
      "operator":"eq|lte|gte|contains|not|ascending|descending",
      "threshold":"<numeric threshold or empty>",
      "unit":"<USD|stars|miles|empty>"
    }
  ]
}

Rules:
- Emit ONLY constraints still open. Skip anything in recently_closed.
- 0..8 constraints. Empty array is valid (task is essentially done).
- Keep canonical_value short and lowercase.
"""


async def redecompose(
    brief: Optional[TaskBrief],
    *,
    current_url_path: str = "",
) -> int:
    """Replace the still-open/active tail of ``brief.constraints`` with a
    freshly-extracted set, using ``original_query`` + ``completed_log`` +
    ``current_url_path`` as context.

    Refuses if ``brief.redecompose_count >= TaskBrief.MAX_REDECOMPOSE``.
    Closed (satisfied/failed/not_applicable) constraints are preserved
    in place; only ``unverified`` constraints are pruned and replaced.

    Returns the number of new tail constraints added (or 0 if refused
    / no change). Increments ``brief.redecompose_count`` on success.

    LLM call uses the same Gemini-Flash plumbing as
    ``extract_constraints_llm``. On any failure, returns 0 without
    mutating the brief — callers can keep the existing tail.
    """
    if brief is None:
        return 0
    if brief.redecompose_count >= TaskBrief.MAX_REDECOMPOSE:
        return 0
    if not brief.original_query:
        return 0

    api_key = (
        os.environ.get("VISION_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    )
    if not api_key:
        return 0
    try:
        from openai import AsyncOpenAI  # type: ignore
    except Exception:
        return 0

    model = os.environ.get("TASK_BRIEF_MODEL") or "gemini-2.5-flash"
    base_url = os.environ.get(
        "TASK_BRIEF_BASE_URL"
    ) or "https://generativelanguage.googleapis.com/v1beta/openai"

    closed_lines = "\n".join(f"- {s}" for s in brief.completed_log[-12:])
    user_payload = (
        f"original_query:\n  {brief.original_query[:2000]}\n\n"
        f"current_url_path:\n  {current_url_path or '(unknown)'}\n\n"
        f"recently_closed:\n{closed_lines or '  (none)'}"
    )
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=10.0)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _REDECOMPOSE_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload[:6000]},
            ],
            response_format={"type": "json_object"},
            max_tokens=1000,
            temperature=0.1,
        )
    except Exception as exc:
        print(f"[task_brief] redecompose LLM call failed: {exc}")
        return 0
    finally:
        try:
            await client.close()
        except Exception:
            pass

    try:
        content = (resp.choices[0].message.content or "").strip()
    except (AttributeError, IndexError):
        return 0
    data: Any = None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = _repair_truncated_json(content)
    if not isinstance(data, dict):
        return 0
    raw_list = data.get("constraints") or []
    if not isinstance(raw_list, list):
        return 0
    new_tail: list[Constraint] = []
    for raw in raw_list[: TaskBrief.MAX_COMPLETED_LOG]:
        if isinstance(raw, dict):
            new_tail.append(Constraint.from_dict(raw))
    # Drop any constraint whose canonical_value already appears in the
    # closed set — the LLM is asked to skip these, but be defensive.
    closed_canonicals = {
        (c.canonical_value or c.text).strip().lower()
        for c in brief.constraints
        if c.status in {"satisfied", "failed", "not_applicable"}
    }
    new_tail = [
        c for c in new_tail
        if (c.canonical_value or c.text).strip().lower() not in closed_canonicals
    ]
    if not new_tail:
        return 0

    # Replace open/active constraints with the new tail. Closed ones
    # stay in place (front of the list).
    closed = [
        c for c in brief.constraints
        if c.status in {"satisfied", "failed", "not_applicable"}
    ]
    brief.constraints = closed + new_tail
    for i, c in enumerate(brief.constraints):
        c.ensure_id(i)
    brief.redecompose_count += 1
    brief.stuck_counter = 0
    brief.version += 1
    brief.current_focus_idx = compute_focus(brief)
    brief._sync_focus_id()
    return len(new_tail)


__all__ = [
    "Constraint",
    "ConstraintKind",
    "ConstraintStatus",
    "ChainOfThoughtNote",
    "TaskBrief",
    "build_task_brief",
    "build_task_brief_sync",
    "compute_focus",
    "extract_constraints_heuristic",
    "extract_constraints_llm",
    "merge_brief_progress",
    "reconcile_from_page_state",
    "reconcile_from_url",
    "reconcile_negative_constraints",
    "redecompose",
]
