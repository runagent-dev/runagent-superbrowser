"""Arm registry: what an experimental condition changes, as env toggles.

Every arm is a *diff against the full system*: an empty env dict is the
production configuration. Arms are pure data so that a run's env is
reproducible from its RunRecord alone.

``side`` tells the runner where the toggle is read:

* ``python`` — read in-process by the bridge; effective per subprocess run
* ``ts``     — read by the long-running browser server; the runner has to
  restart the server with the env baked in (``core/server.py``)
* ``both``   — both halves

Toggle semantics are documented next to the code that reads them
(``docs/CONFIG.md`` lists them all). Nothing in this file has any effect
unless the runner injects the env into a run.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

Side = Literal["python", "ts", "both"]


@dataclass(frozen=True)
class Arm:
    name: str
    env: dict[str, str] = field(default_factory=dict)
    side: Side = "python"
    description: str = ""
    family: str = "system"

    @property
    def ts_env(self) -> dict[str, str]:
        """The subset of env the browser server must be (re)started with."""
        return {k: v for k, v in self.env.items() if k in TS_KEYS}

    def with_env(self, extra: dict[str, str], *, suffix: str) -> "Arm":
        """Derive a variant (e.g. a budget level) without losing provenance."""
        merged = dict(self.env)
        merged.update(extra)
        side: Side = self.side
        if any(k in TS_KEYS for k in extra) and side == "python":
            side = "both"
        return replace(self, name=f"{self.name}__{suffix}", env=merged, side=side,
                       description=f"{self.description} [+{suffix}]")


# Env keys the TypeScript server reads (needs a restart to change).
TS_KEYS = frozenset({
    "SUPERBROWSER_CLICK_TIERS",
    "MOTOR_HUMANIZATION",
    "SUPERBROWSER_SNAP_STRATEGY",
    "SUPERBROWSER_HUMANIZE_ALL_CLICKS",
    "SUPERBROWSER_PRECLICK_VALIDATE",
})

_A = Arm  # brevity

ARMS: dict[str, Arm] = {a.name: a for a in [
    # ---- the full system ---------------------------------------------------
    _A("ledger", {}, "python", "Full system: structured Ledger + six-phase eviction", "memory"),
    # ---- E2 matched memory policies (LRE-style) ---------------------------
    _A("full_history", {"SUPERBROWSER_MEMORY_POLICY": "full"}, "python",
       "No eviction, no Ledger: keep the entire history verbatim (upper-bound context)", "memory"),
    _A("fifo", {"SUPERBROWSER_MEMORY_POLICY": "fifo"}, "python",
       "Recency only: last K turns verbatim, older turns archived, no Ledger", "memory"),
    _A("summary", {"SUPERBROWSER_MEMORY_POLICY": "summary"}, "python",
       "LLM compression: last K turns verbatim + one regenerated structured summary of older turns", "memory"),
    _A("ledger_noevict", {"SUPERBROWSER_MEMORY_POLICY": "ledger_noevict"}, "python",
       "Diagnostic: Ledger injected but nothing evicted (separates Ledger from eviction)", "memory"),
    # ---- E4 dead-end memory --------------------------------------------------
    _A("no_deadend", {"ABLATE_DEAD_END_MEMORY": "1"}, "python",
       "Failures are not remembered as dead-ends (no ledger section, no [DEAD_ENDS_HERE])", "memory"),
    # ---- E5 perception reuse ---------------------------------------------------
    _A("fresh_vision", {"ABLATE_VISION_REUSE": "1"}, "python",
       "No perception reuse: no prefetch, no vision cache, epoch expires every turn, no piggyback", "perception"),
    _A("no_prefetch", {"VISION_ASYNC_PREFETCH": "0"}, "python",
       "Legacy Table-1 row: background vision prefetch off only", "perception"),
    # ---- E6 sub-element targeting (TS snapper) ----------------------------------
    _A("snap_chevron", {}, "ts", "Default snapper: label-weighted grid scan with the chevron tiebreaker", "grounding"),
    _A("snap_center", {"SUPERBROWSER_SNAP_STRATEGY": "center"}, "ts",
       "Naive snapper: click the raw bbox centre / largest-area candidate", "grounding"),
    _A("snap_dom_alt", {"SUPERBROWSER_SNAP_STRATEGY": "dom_alt"}, "ts",
       "DOM-aware alternative: pinpoint + nearest interactive ancestor, no chevron/label weights", "grounding"),
    _A("no_chevron_hints", {"WORKER_CHEVRON_FOCUS": "0", "BBOX_COMPOUND_ROW_SPLIT": "0"}, "python",
       "Legacy Table-1 row: Python chevron hint + compound-row split off (TS tiebreaker unchanged)", "grounding"),
    # ---- E7 click ladder ------------------------------------------------------------
    _A("no_ladder", {"ABLATE_CLICK_LADDER": "1", "CLICK_LADDER_AUTO": "0", "SUPERBROWSER_CLICK_TIERS": "tier1"}, "both",
       "First click path only: no js/keyboard escalation, no Tier-2/3 selector cascade", "execution"),
    _A("no_humanize", {"MOTOR_HUMANIZATION": "off"}, "ts",
       "Legacy Table-1 row: motor humanization off (teleport + raw dispatch)", "execution"),
    # ---- E8 topology ----------------------------------------------------------------
    _A("flat", {"SUPERBROWSER_TOPOLOGY": "flat"}, "python",
       "Single agent: browser tools registered directly, same memory hook and budgets, no orchestrator", "topology"),
]}


def get(name: str) -> Arm:
    try:
        return ARMS[name]
    except KeyError:
        raise KeyError(f"unknown arm {name!r}; known: {sorted(ARMS)}") from None


def resolve(names: list[str] | str) -> list[Arm]:
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    return [get(n) for n in names]


# ---- derived arms used by E3 / E10 ------------------------------------------
def pressure_arm(base: str, tokens_per_step: int) -> Arm:
    """E3: same policy under a memory-pressure level (distractor tokens/step)."""
    arm = get(base)
    if tokens_per_step <= 0:
        return arm.with_env({"SUPERBROWSER_EVAL_DISTRACTOR_TOKENS": "0"}, suffix="p0")
    return arm.with_env({"SUPERBROWSER_EVAL_DISTRACTOR_TOKENS": str(tokens_per_step)},
                        suffix=f"p{tokens_per_step}")


def budget_arm(base: str, *, budget_tokens: int | None = None, recent_k: int | None = None) -> Arm:
    """E10: same policy under a different history budget / recent window."""
    extra: dict[str, str] = {}
    parts: list[str] = []
    if budget_tokens is not None:
        extra["SUPERBROWSER_MEMORY_BUDGET_TOKENS"] = str(budget_tokens)
        parts.append(f"B{budget_tokens}")
    if recent_k is not None:
        extra["SUPERBROWSER_MEMORY_RECENT_K"] = str(recent_k)
        parts.append(f"K{recent_k}")
    return get(base).with_env(extra, suffix="_".join(parts) or "base")


def ts_signature(arm: Arm) -> tuple[tuple[str, str], ...]:
    """Hashable key of the TS-side env; equal keys share one server instance."""
    return tuple(sorted(arm.ts_env.items()))
