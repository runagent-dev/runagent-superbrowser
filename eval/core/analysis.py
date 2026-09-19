"""Shared analysis helpers: pairing runs across arms, paired tables, exclusions.

Every experiment analyzer follows the same recipe:

1. ``load`` the experiment's records (+ recompute process metrics from the
   run directories so a new metric definition applies to old runs);
2. ``pair`` runs of two arms by (task_id, seed); apply the impossible-task rule
   (a task is excluded only if the exclusion marker fires in EVERY arm);
3. ``paired_binary`` / ``paired_metric`` -> McNemar / bootstrap + Wilcoxon;
4. write CSV + TeX + PNG through ``report.py``.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from eval.core import stats
from eval.core.loaders import DEFAULT_RUNS_ROOT, load_records, run_dir_of
from eval.core.metrics import compute_all
from eval.core.metrics.cost import cost_of_record
from eval.core.records import RunRecord
from eval.core.tasks import excluded_task_ids

Key = tuple[str, int]


def load(experiment: str, *, runs_root: Path = DEFAULT_RUNS_ROOT, recompute: bool = True) -> list[RunRecord]:
    recs = load_records(experiment, runs_root=runs_root)
    if recompute:
        for r in recs:
            d = run_dir_of(r)
            if d.exists():
                try:
                    r.cost = cost_of_record(r)
                except Exception:
                    pass
                compute_all(d, r)
    return recs


def by_arm(records: Iterable[RunRecord]) -> dict[str, dict[Key, RunRecord]]:
    out: dict[str, dict[Key, RunRecord]] = defaultdict(dict)
    for r in records:
        out[str(r.ids.get("arm"))][(str(r.ids.get("task_id")), int(r.ids.get("seed") or 0))] = r
    return out


def split_pre_registered(records: Iterable[RunRecord]) -> tuple[list[RunRecord], dict[str, str]]:
    """(kept, dropped) under the pre-registered hand exclusions of
    ``exclusions.json``. A dropped task leaves EVERY primary table, not only
    the paired rows, so per-arm denominators and paired n agree; the all-task
    numbers are still reported as a sensitivity line."""
    pre = excluded_task_ids()
    kept: list[RunRecord] = []
    dropped: dict[str, str] = {}
    for r in records:
        tid = str(r.ids.get("task_id"))
        if tid in pre:
            dropped[tid] = pre[tid]
            continue
        kept.append(r)
    return kept, dropped


@dataclass
class Pairing:
    arm_a: str
    arm_b: str
    keys: list[Key]                  # paired (task, seed) after exclusions
    excluded: dict[Key, str] = field(default_factory=dict)
    unpaired: dict[str, list[Key]] = field(default_factory=dict)

    def rows(self, arms: dict[str, dict[Key, RunRecord]]) -> list[tuple[RunRecord, RunRecord]]:
        return [(arms[self.arm_a][k], arms[self.arm_b][k]) for k in self.keys]


NOT_A_TASK_OUTCOME = ("api_error", "harness_error")   # the provider or the harness failed, not the agent


def is_task_outcome(r: RunRecord) -> bool:
    return r.outcome.get("exclusion_label") not in NOT_A_TASK_OUTCOME


def pair(arms: dict[str, dict[Key, RunRecord]], arm_a: str, arm_b: str, *, apply_exclusions: bool = True) -> Pairing:
    a, b = arms.get(arm_a, {}), arms.get(arm_b, {})
    common = sorted(set(a) & set(b))
    excluded: dict[Key, str] = {}
    pre = excluded_task_ids()
    keys: list[Key] = []
    for k in common:
        ra, rb = a[k], b[k]
        if apply_exclusions:
            if k[0] in pre:
                excluded[k] = f"pre-registered: {pre[k[0]]}"
                continue
            la, lb = ra.outcome.get("exclusion_label"), rb.outcome.get("exclusion_label")
            if la in NOT_A_TASK_OUTCOME or lb in NOT_A_TASK_OUTCOME:
                # a provider refusal / harness crash on EITHER side leaves no
                # task outcome to pair; it must never be scored as a failure
                excluded[k] = la if la in NOT_A_TASK_OUTCOME else lb
                continue
            if la and la == lb:
                excluded[k] = la  # impossible in BOTH arms
                continue
            if ra.outcome.get("api_error") and rb.outcome.get("api_error"):
                excluded[k] = "api_error"
                continue
        keys.append(k)
    return Pairing(arm_a, arm_b, keys, excluded,
                   {arm_a: sorted(set(a) - set(b)), arm_b: sorted(set(b) - set(a))})


def _success(r: RunRecord) -> bool:
    return bool(r.outcome.get("success"))


def paired_binary(arms: dict[str, dict[Key, RunRecord]], arm_a: str, arm_b: str, **kw: Any) -> dict[str, Any]:
    p = pair(arms, arm_a, arm_b, **kw)
    rows = p.rows(arms)
    res = stats.mcnemar_exact([_success(x) for x, _ in rows], [_success(y) for _, y in rows])
    ka = sum(1 for x, _ in rows if _success(x))
    kb = sum(1 for _, y in rows if _success(y))
    n = len(rows)
    return {"arm_a": arm_a, "arm_b": arm_b, "n": n, "k_a": ka, "k_b": kb,
            "tsr_a": ka / n if n else None, "tsr_b": kb / n if n else None,
            "wilson_a": stats.wilson_ci(ka, n) if n else (None, None),
            "wilson_b": stats.wilson_ci(kb, n) if n else (None, None),
            "n_excluded": len(p.excluded), "excluded": {f"{k[0]}:s{k[1]}": v for k, v in p.excluded.items()},
            "unpaired": {k: len(v) for k, v in p.unpaired.items()}, **res.to_dict()}


def paired_metric(arms: dict[str, dict[Key, RunRecord]], arm_a: str, arm_b: str, getter: Callable[[RunRecord], float | None],
                  *, name: str, **kw: Any) -> dict[str, Any]:
    p = pair(arms, arm_a, arm_b, **kw)
    xs, ys = [], []
    for ra, rb in p.rows(arms):
        va, vb = getter(ra), getter(rb)
        if va is None or vb is None:
            continue
        xs.append(float(va))
        ys.append(float(vb))
    res = stats.paired_compare(xs, ys) if xs else stats.PairedResult(0, float("nan"), float("nan"), float("nan"), (float("nan"), float("nan")))
    return {"metric": name, "arm_a": arm_a, "arm_b": arm_b, **res.to_dict()}


SNIP_MARGIN_TOKENS = 1024   # nanobot trims history above context_window - max_tokens - this


def _snip_threshold(r: RunRecord) -> int | None:
    proto = r.protocol or {}
    try:
        cw, mt = int(proto.get("context_window_tokens")), int(proto.get("max_tokens"))
    except (TypeError, ValueError):
        return None
    return cw - mt - SNIP_MARGIN_TOKENS


def _csd_pool(rs: list[RunRecord], scored_key: str, rate_key: str) -> dict[str, Any]:
    """Event-pooled CSD: items present / items scored, summed over runs.

    ``csd.compute`` stores per-run rates; the number present is recovered as
    round(scored * rate). The macro-average over runs (``m(...)`` above) is
    kept alongside because it is what earlier drafts reported; the two differ
    when runs contribute very different numbers of items."""
    events = present = 0
    n_runs = 0
    for r in rs:
        c = r.metrics.get("csd") or {}
        scored = c.get(scored_key)
        rate = c.get(rate_key)
        if not isinstance(scored, (int, float)) or scored <= 0 or not isinstance(rate, (int, float)):
            continue
        n_runs += 1
        events += int(scored)
        present += int(round(scored * rate))
    return {"events": events, "present": present, "lost": events - present,
            "pooled": (present / events) if events else None, "n_runs": n_runs}


def arm_summary(records: Iterable[RunRecord], *, apply_exclusions: bool = True) -> dict[str, dict[str, Any]]:
    """Per-arm descriptive summary (all runs, no pairing).

    With ``apply_exclusions`` (default) the pre-registered task exclusions of
    ``exclusions.json`` are removed first, so an arm's denominator matches the
    paired comparisons' n. Pass ``False`` for the all-task sensitivity line.
    """
    records = list(records)
    dropped: dict[str, str] = {}
    if apply_exclusions:
        records, dropped = split_pre_registered(records)
    out: dict[str, dict[str, Any]] = {}
    for arm, runs in by_arm(records).items():
        all_rs = list(runs.values())
        rs = [r for r in all_rs if is_task_outcome(r)]      # provider/harness errors leave the denominator
        n = len(rs)
        k = sum(1 for r in rs if _success(r))
        excl = sum(1 for r in rs if r.outcome.get("exclusion_label"))
        n_provider = len(all_rs) - n

        def m(get: Callable[[RunRecord], Any]) -> float | None:
            vals = [get(r) for r in rs]
            vals = [float(v) for v in vals if isinstance(v, (int, float))]
            return sum(vals) / len(vals) if vals else None

        def mx(get: Callable[[RunRecord], Any]) -> float | None:
            vals = [get(r) for r in rs]
            vals = [float(v) for v in vals if isinstance(v, (int, float))]
            return max(vals) if vals else None

        def cnt(get: Callable[[RunRecord], Any]) -> int:
            return sum(1 for r in rs if isinstance(get(r), (int, float)))

        peaks = [(r.tokens.get("prompt_tokens_per_iter_peak"), _snip_threshold(r)) for r in rs]
        over_snip = sum(1 for pk, th in peaks if isinstance(pk, (int, float)) and th is not None and pk > th)
        obs = _csd_pool(rs, "observed_scored", "csd_observed")
        tg = _csd_pool(rs, "task_given_scored", "csd_task_given")
        fp_sources = sorted({str((r.metrics.get("grounding") or {}).get("source")) for r in rs
                             if isinstance((r.metrics.get("grounding") or {}).get("first_path_success"), (int, float))})

        out[arm] = {
            "n": n, "k": k, "tsr": k / n if n else None, "wilson": stats.wilson_ci(k, n) if n else (None, None),
            "n_excluded": excl,
            "n_provider_errors": n_provider,
            "n_pre_excluded": sum(1 for _ in dropped),
            "iterations": m(lambda r: r.counts.get("worker_iterations")),
            "tool_calls": m(lambda r: r.counts.get("tool_calls_executed")),
            "vision_calls": m(lambda r: r.counts.get("vision_calls")),
            "prompt_mean": m(lambda r: r.tokens.get("prompt_tokens_per_iter_mean")),
            "prompt_peak": m(lambda r: r.tokens.get("prompt_tokens_per_iter_peak")),
            "prompt_peak_max": mx(lambda r: r.tokens.get("prompt_tokens_per_iter_peak")),
            "ctx_peak": m(lambda r: r.tokens.get("context_est_after_peak")),
            "ctx_peak_max": mx(lambda r: r.tokens.get("context_est_after_peak")),
            "n_over_snip_threshold": over_snip,
            "input_tokens": m(lambda r: r.tokens.get("input_tokens")),
            "wall_s": m(lambda r: r.timing.get("wall_s")),
            "usd": m(lambda r: (r.cost or {}).get("usd")),
            "usd_cached": m(lambda r: (r.cost or {}).get("usd_cached")),
            "usd_judge": m(lambda r: (r.cost or {}).get("usd_judge")),
            "usd_per_success": (sum(float((r.cost or {}).get("usd") or 0) for r in rs) / k) if k else None,
            # CSD: macro mean over runs (legacy) and event-pooled (definition in PROTOCOL.md)
            "csd_observed": m(lambda r: (r.metrics.get("csd") or {}).get("csd_observed")),
            "csd_observed_pooled": obs["pooled"], "csd_events": obs["events"], "csd_lost": obs["lost"],
            "csd_n_runs": obs["n_runs"],
            "csd_task_given": m(lambda r: (r.metrics.get("csd") or {}).get("csd_task_given")),
            "csd_task_given_pooled": tg["pooled"], "csd_task_given_events": tg["events"],
            "csd_task_given_lost": tg["lost"], "csd_task_given_n_runs": tg["n_runs"],
            "drr": m(lambda r: (r.metrics.get("drr") or {}).get("drr")),
            "drr_n_runs": cnt(lambda r: (r.metrics.get("drr") or {}).get("drr")),
            "repeated_actions": m(lambda r: (r.metrics.get("drr") or {}).get("repeated_actions")),
            "rpr": m(lambda r: (r.metrics.get("rpr") or {}).get("rpr")),
            "rpr_n_runs": cnt(lambda r: (r.metrics.get("rpr") or {}).get("rpr")),
            "first_path_success": m(lambda r: (r.metrics.get("grounding") or {}).get("first_path_success")),
            "first_path_n_runs": cnt(lambda r: (r.metrics.get("grounding") or {}).get("first_path_success")),
            "first_path_source": "+".join(fp_sources) if fp_sources else None,
            "recovery_success": m(lambda r: (r.metrics.get("grounding") or {}).get("recovery_success")),
            "recovery_n_runs": cnt(lambda r: (r.metrics.get("grounding") or {}).get("recovery_success")),
            "failure_reasons": _counter(r.outcome.get("failure_reason") for r in rs if not _success(r)),
        }
    return out


def _counter(items: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[str(it)] = out.get(str(it), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


METRIC_GETTERS: dict[str, Callable[[RunRecord], float | None]] = {
    "tool_calls": lambda r: r.counts.get("tool_calls_executed"),
    "worker_iterations": lambda r: r.counts.get("worker_iterations"),
    "vision_calls": lambda r: r.counts.get("vision_calls"),
    "prompt_mean": lambda r: r.tokens.get("prompt_tokens_per_iter_mean"),
    "prompt_peak": lambda r: r.tokens.get("prompt_tokens_per_iter_peak"),
    "input_tokens": lambda r: r.tokens.get("input_tokens"),
    "wall_s": lambda r: r.timing.get("wall_s"),
    "usd": lambda r: (r.cost or {}).get("usd"),
    "usd_cached": lambda r: (r.cost or {}).get("usd_cached"),
    "csd_observed": lambda r: (r.metrics.get("csd") or {}).get("csd_observed"),
    "drr": lambda r: (r.metrics.get("drr") or {}).get("drr"),
    "repeated_actions": lambda r: (r.metrics.get("drr") or {}).get("repeated_actions"),
    "rpr": lambda r: (r.metrics.get("rpr") or {}).get("rpr"),
    "rpr_uncached": lambda r: (r.metrics.get("rpr") or {}).get("rpr_uncached"),
    "first_path_success": lambda r: (r.metrics.get("grounding") or {}).get("first_path_success"),
    "recovery_success": lambda r: (r.metrics.get("grounding") or {}).get("recovery_success"),
}
