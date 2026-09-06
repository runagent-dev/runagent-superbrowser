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

from eval.core import analysis, report
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
    default_tasks: str = "ablation24"
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


def standard_analyze(spec: ExperimentSpec, *, runs_root: Path = DEFAULT_RUNS_ROOT, recompute: bool = True,
                     records: Sequence[RunRecord] | None = None) -> dict[str, Any]:
    recs = list(records) if records is not None else analysis.load(spec.name, runs_root=runs_root, recompute=recompute)
    out = report.out_dir(spec.name)
    result: dict[str, Any] = {"experiment": spec.name, "n_records": len(recs)}
    if not recs:
        print(f"[{spec.name}] no records under {runs_root / spec.name}")
        return result
    # persist recomputed metrics back into the run dirs so results.jsonl stays the source of truth
    for r in recs:
        try:
            r.write(Path(r.ids["run_dir"]))
        except Exception:
            pass
    report.write_csv(out / "per_run.csv", per_run_rows(recs))
    arms = analysis.by_arm(recs)
    summary = analysis.arm_summary(recs)
    result["arms"] = summary
    report.write_json(out / "arm_summary.json", summary)
    rows = []
    for arm, s in summary.items():
        rows.append({"arm": arm, "n": s["n"], "k": s["k"], "tsr": s["tsr"], "wilson_low": s["wilson"][0], "wilson_high": s["wilson"][1],
                     "n_excluded": s["n_excluded"], "iterations": s["iterations"], "tool_calls": s["tool_calls"],
                     "vision_calls": s["vision_calls"], "prompt_mean": s["prompt_mean"], "prompt_peak": s["prompt_peak"],
                     "input_tokens": s["input_tokens"], "wall_s": s["wall_s"], "usd": s["usd"], "usd_cached": s["usd_cached"],
                     "usd_per_success": s["usd_per_success"], "csd_observed": s["csd_observed"], "drr": s["drr"],
                     "repeated_actions": s["repeated_actions"], "rpr": s["rpr"], "first_path_success": s["first_path_success"],
                     "recovery_success": s["recovery_success"], "failure_reasons": s["failure_reasons"]})
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
    result["paired_binary"] = paired_bin
    result["paired_metrics"] = paired_met
    if paired_bin:
        report.write_json(out / "paired_binary.json", paired_bin)
        report.write_csv(out / "paired_binary.csv", [{k: v for k, v in pb.items() if k not in ("excluded", "unpaired")} for pb in paired_bin])
        report.write_tex_table(out / "paired_binary.tex",
                               ["Comparison", "n", "TSR A", "TSR B", "$\\Delta$ (pp)", "95\\% CI", "discordant", "McNemar p"],
                               [[f"{report.tex_escape(pb['arm_a'])} vs {report.tex_escape(pb['arm_b'])}" + (" $^\\dagger$" if pb["confirmatory"] else ""),
                                 str(pb["n"]), f"{pb['k_a']}/{pb['n']}", f"{pb['k_b']}/{pb['n']}",
                                 report.fmt(100 * pb["diff"]), f"[{report.fmt(100 * pb['diff_ci_low'])}, {report.fmt(100 * pb['diff_ci_high'])}]",
                                 f"{pb['a_only']}/{pb['b_only']}", report.fmt(pb["p_value"], "auto")] for pb in paired_bin],
                               note="dagger = pre-registered confirmatory comparison; discordant = A-only/B-only successes")
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
        ap.add_argument("--no-recompute", action="store_true", help="use stored metrics instead of recomputing from run dirs")
        if spec.extra_args:
            spec.extra_args(ap)
        args = ap.parse_args(argv)
        recs = analysis.load(spec.name, runs_root=Path(args.runs), recompute=not args.no_recompute)
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
