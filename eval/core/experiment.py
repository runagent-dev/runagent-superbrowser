"""Common shape of an experiment: a spec + generic run/analyze entry points.

Each ``eval/experiments/<name>/`` package declares a ``SPEC`` and gets, for
free, a ``run.py`` (fixed arms/subset, shared runner flags) and a standard
``analyze.py`` that writes per-run rows, per-arm summaries, the
pre-registered paired comparisons and the accuracy–cost plane. Experiment
packages add their own analysis on top (ladders, strata, kappa, ...).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from eval.core import analysis, report, stats
from eval.core.arms import Arm
from eval.core.loaders import DEFAULT_RUNS_ROOT, records_frame
from eval.core.records import RunRecord
from eval.core.runner import add_common_args, run_from_args


@dataclass
class ExperimentSpec:
    name: str
    title: str
    question: str
    arms: Callable[[argparse.Namespace], Sequence[Arm]]
    default_tasks: str = "ablate24"
    default_seeds: int = 1
    confirmatory: Sequence[tuple[str, str]] = ()      # (arm_a, arm_b) pairs tested first
    secondary: Sequence[tuple[str, str]] = ()
    metrics: Sequence[str] = ("tool_calls", "worker_iterations", "vision_calls", "prompt_peak", "input_tokens", "wall_s", "usd")
    extra_args: Callable[[argparse.ArgumentParser], None] | None = None
    notes: Sequence[str] = field(default_factory=list)

    @property
    def pairs(self) -> list[tuple[str, str]]:
        return list(self.confirmatory) + [p for p in self.secondary if p not in self.confirmatory]


# ------------------------------------------------------------------- run
def run_main(spec: ExperimentSpec) -> Callable[[list[str] | None], int]:
    def main(argv: list[str] | None = None) -> int:
        ap = argparse.ArgumentParser(description=f"{spec.name}: {spec.title}")
        add_common_args(ap)
        ap.set_defaults(tasks=spec.default_tasks, seeds=spec.default_seeds)
        if spec.extra_args:
            spec.extra_args(ap)
        args = ap.parse_args(argv)
        run_from_args(args, experiment=spec.name, arms=list(spec.arms(args)))
        return 0
    return main


# --------------------------------------------------------------- analyze
def per_run_rows(records: Sequence[RunRecord]) -> list[dict[str, Any]]:
    df = records_frame(list(records))
    return df.to_dict(orient="records") if len(df) else []


_SUMMARY_COLUMNS = ("n", "k", "tsr", "n_excluded", "n_provider_errors", "n_pre_excluded", "iterations", "tool_calls", "vision_calls",
                    "prompt_mean", "prompt_peak", "prompt_peak_max", "ctx_peak", "ctx_peak_max", "n_over_snip_threshold",
                    "input_tokens", "wall_s", "usd", "usd_cached", "usd_judge", "usd_per_success",
                    "csd_observed", "csd_observed_pooled", "csd_events", "csd_lost", "csd_n_runs",
                    "csd_task_given", "csd_task_given_pooled", "csd_task_given_events", "csd_task_given_lost", "csd_task_given_n_runs",
                    "drr", "drr_n_runs", "repeated_actions", "rpr", "rpr_n_runs",
                    "first_path_success", "first_path_n_runs", "first_path_source",
                    "recovery_success", "recovery_n_runs", "failure_reasons")


def _summary_rows(summary: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for arm, s in summary.items():
        row = {"arm": arm}
        for c in _SUMMARY_COLUMNS:
            if c == "tsr":
                row["tsr"] = s["tsr"]
                row["wilson_low"], row["wilson_high"] = s["wilson"]
            else:
                row[c] = s.get(c)
        rows.append(row)
    return rows


def standard_analyze(spec: ExperimentSpec, *, runs_root: Path = DEFAULT_RUNS_ROOT, recompute: bool = True,
                     records: Sequence[RunRecord] | None = None) -> dict[str, Any]:
    all_recs = list(records) if records is not None else analysis.load(spec.name, runs_root=runs_root, recompute=recompute)
    out = report.out_dir(spec.name)
    result: dict[str, Any] = {"experiment": spec.name, "n_records": len(all_recs)}
    if not all_recs:
        print(f"[{spec.name}] no records under {runs_root / spec.name}")
        return result
    # persist recomputed metrics back into the run dirs so results.jsonl stays the source of truth
    for r in all_recs:
        try:
            r.write(Path(r.ids["run_dir"]))
        except Exception:
            pass
    # pre-registered hand exclusions leave EVERY primary table (per-arm rows
    # included), so denominators agree with the paired n; the all-task summary
    # is kept as a sensitivity artifact
    recs, dropped = analysis.split_pre_registered(all_recs)
    result["n_records_primary"] = len(recs)
    result["pre_registered_exclusions"] = dropped
    if dropped:
        print(f"[{spec.name}] pre-registered exclusions applied: " + "; ".join(f"{t}: {why}" for t, why in dropped.items()))
    report.write_csv(out / "per_run.csv", per_run_rows(all_recs))
    arms = analysis.by_arm(recs)
    summary = analysis.arm_summary(recs, apply_exclusions=False)
    result["arms"] = summary
    report.write_json(out / "arm_summary.json", summary)
    if dropped:
        summary_all = analysis.arm_summary(all_recs, apply_exclusions=False)
        result["arms_all_tasks"] = summary_all
        report.write_json(out / "arm_summary_all_tasks.json", summary_all)
        report.write_csv(out / "arm_summary_all_tasks.csv", _summary_rows(summary_all))
    rows = _summary_rows(summary)
    report.write_csv(out / "arm_summary.csv", rows)
    tex_rows = [[report.tex_escape(r["arm"]), f"{r['k']}/{r['n']}", report.fmt(r["tsr"], "pct"),
                 report.fmt(r["iterations"]), report.fmt(r["tool_calls"]), report.fmt(r["vision_calls"]),
                 report.fmt(r["prompt_peak"], "k"), report.fmt(r["usd"], "usd"), report.fmt(r["usd_per_success"], "usd")]
                for r in rows]
    report.write_tex_table(out / "arm_summary.tex",
                           ["Arm", "Success", "TSR (\\%)", "Iters", "Tool calls", "Vision calls", "Peak prompt", "\\$/task", "\\$/success"],
                           tex_rows, note="TSR = successes / evaluated runs (Wilson 95% CI in arm_summary.csv)")
    # paired comparisons
    paired_bin, paired_met = [], []
    for a, b in spec.pairs:
        if a in arms and b in arms:
            pb = analysis.paired_binary(arms, a, b)
            pb["confirmatory"] = (a, b) in spec.confirmatory
            paired_bin.append(pb)
            for m in spec.metrics + ("csd_observed", "drr", "rpr", "repeated_actions", "first_path_success", "recovery_success"):
                getter = analysis.METRIC_GETTERS.get(m)
                if getter is None:
                    continue
                pm = analysis.paired_metric(arms, a, b, getter, name=m)
                if pm["n"]:
                    paired_met.append(pm)
    # Holm step-down over this experiment's SECONDARY comparisons (the
    # confirmatory pairs were pre-registered and are reported unadjusted)
    secondary = [pb for pb in paired_bin if not pb["confirmatory"]]
    for pb, adj in zip(secondary, stats.holm([pb["p_value"] for pb in secondary])):
        pb["p_holm"] = adj
    for pb in paired_bin:
        pb.setdefault("p_holm", None)
        pb["holm_family"] = f"{spec.name}:secondary" if not pb["confirmatory"] else None
    result["paired_binary"] = paired_bin
    result["paired_metrics"] = paired_met
    if paired_bin:
        report.write_json(out / "paired_binary.json", paired_bin)
        report.write_csv(out / "paired_binary.csv", [{k: v for k, v in pb.items() if k not in ("excluded", "unpaired")} for pb in paired_bin])
        report.write_tex_table(out / "paired_binary.tex",
                               ["Comparison", "n", "TSR A", "TSR B", "$\\Delta$ (pp)", "95\\% CI", "discordant", "McNemar p", "Holm p"],
                               [[f"{report.tex_escape(pb['arm_a'])} vs {report.tex_escape(pb['arm_b'])}" + (" $^\\dagger$" if pb["confirmatory"] else ""),
                                 str(pb["n"]), f"{pb['k_a']}/{pb['n']}", f"{pb['k_b']}/{pb['n']}",
                                 report.fmt(100 * pb["diff"]), f"[{report.fmt(100 * pb['diff_ci_low'])}, {report.fmt(100 * pb['diff_ci_high'])}]",
                                 f"{pb['a_only']}/{pb['b_only']}", report.fmt(pb["p_value"], "auto"),
                                 report.fmt(pb["p_holm"], "auto") if pb["p_holm"] is not None else "--"] for pb in paired_bin],
                               note="dagger = pre-registered confirmatory comparison (unadjusted); Holm p = step-down adjustment over the secondary comparisons of this experiment; discordant = A-only/B-only successes")
    if paired_met:
        report.write_csv(out / "paired_metrics.csv", paired_met)
    # figures
    labels = list(summary)
    report.bar_png(out / "tsr.png", labels, [s["tsr"] or 0 for s in summary.values()],
                   errors=[s["wilson"] for s in summary.values()], ylabel="Task success rate", title=spec.title, ylim=(0, 1))
    pts = {a: (s["usd"] or 0.0, s["tsr"] or 0.0) for a, s in summary.items() if s["usd"] is not None}
    if len(pts) >= 2:
        report.scatter_png(out / "accuracy_cost.png", pts, xlabel="USD per task (list price)", ylabel="TSR",
                           title=f"{spec.name}: accuracy–cost plane",
                           yerr={a: s["wilson"] for a, s in summary.items() if a in pts})
    report.write_json(out / "summary.json", result)
    print(f"[{spec.name}] {len(recs)} runs; arms: " + ", ".join(f"{a} {s['k']}/{s['n']}" for a, s in summary.items()))
    for pb in paired_bin:
        print(f"    {pb['arm_a']} vs {pb['arm_b']}: Δ={100 * pb['diff']:+.1f}pp CI[{100 * pb['diff_ci_low']:+.1f}, {100 * pb['diff_ci_high']:+.1f}] "
              f"McNemar p={pb['p_value']:.3f} (n={pb['n']}, excluded {pb['n_excluded']})")
    print(f"    artifacts -> {out}")
    return result


def analyze_main(spec: ExperimentSpec, extra: Callable[[argparse.Namespace, list[RunRecord], dict[str, Any]], None] | None = None
                 ) -> Callable[[list[str] | None], int]:
    def main(argv: list[str] | None = None) -> int:
        ap = argparse.ArgumentParser(description=f"analyze {spec.name}")
        ap.add_argument("--runs", default=str(DEFAULT_RUNS_ROOT))
        ap.add_argument("--experiment", default=None,
                        help="read runs from this experiment instead of %r — use it when several "
                             "ablations were swept together as one combined experiment so they share "
                             "one baseline arm; only this experiment's arms are analysed" % spec.name)
        ap.add_argument("--no-recompute", action="store_true", help="use stored metrics instead of recomputing from run dirs")
        if spec.extra_args:
            spec.extra_args(ap)
        args = ap.parse_args(argv)
        source = args.experiment or spec.name
        recs = analysis.load(source, runs_root=Path(args.runs), recompute=not args.no_recompute)
        if args.experiment:
            try:                                   # the arms this run would have used
                wanted = {a.name for a in spec.arms(args)}
            except Exception:                      # fall back to the arms it compares
                wanted = {n for pair in spec.pairs for n in pair}
            kept = [r for r in recs if r.ids.get("arm") in wanted]
            missing = wanted - {r.ids.get("arm") for r in kept}
            print(f"[{spec.name}] reading {source!r}: {len(kept)}/{len(recs)} records match this "
                  f"experiment's arms {sorted(wanted)}"
                  + (f"; MISSING arm(s): {sorted(missing)}" if missing else ""))
            recs = kept
        result = standard_analyze(spec, runs_root=Path(args.runs), records=recs)
        if extra is not None and recs:
            extra(args, recs, result)
        return 0
    return main


def write_readme(spec: ExperimentSpec, path: Path, *, arms_text: str, metrics_text: str, extra: str = "") -> None:
    """Helper used once to scaffold READMEs (kept for regeneration)."""
    body = f"""# {spec.name} — {spec.title}

**Question.** {spec.question}

**Arms.** {arms_text}

**Task set / seeds.** `{spec.default_tasks}` (see `eval/benchmarks/subsets.json`), {spec.default_seeds} seed(s) by default.

**Metrics.** {metrics_text}

**Confirmatory comparisons.** {', '.join(f'`{a}` vs `{b}`' for a, b in spec.confirmatory) or 'none (descriptive)'}.

```bash
python -m eval.experiments.{spec.name}.run --dry-run            # schedule + env
python -m eval.experiments.{spec.name}.run --model <id> [--seeds N] [--tasks all|ablation24|ids]
python -m eval.experiments.{spec.name}.analyze                  # -> eval/artifacts/{spec.name}/
```
{extra}
"""
    path.write_text(body)
