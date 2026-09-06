"""Offline dress rehearsal for the whole eval stack (no browser, no LLM).

    python -m eval.rehearse

1. ``--dry-run`` every experiment's schedule (arms x subset x seeds, per-run env);
2. replay the recorded §7.4 model-split runs into ``modelsplit_replay`` and run
   harvest + every process metric + the standard analyzer + the cost analyzer +
   a trace excerpt on that REAL data (judges stubbed from the recorded verdicts);
3. print the realized cost so the README budget table can be refreshed.

Everything it touches is offline; it is the pre-flight check before any live sweep.
"""
from __future__ import annotations

import argparse
import importlib
from pathlib import Path

from eval.core import analysis, replay, report
from eval.core.experiment import ExperimentSpec, standard_analyze
from eval.core.loaders import DEFAULT_RUNS_ROOT

EXPERIMENTS = ["e1_main", "e2_memory_policy", "e3_memory_pressure", "e4_deadend", "e5_perception_reuse",
               "e7_click_cascade", "e8_topology", "e10_robustness"]


def dry_run_all() -> None:
    print("=" * 70, "\n1. Dry-run every experiment schedule\n" + "=" * 70)
    for name in EXPERIMENTS:
        spec = importlib.import_module(f"eval.experiments.{name}").SPEC
        run = importlib.import_module(f"eval.experiments.{name}.run")
        print(f"\n--- {name} ---")
        try:
            run.main(["--dry-run", "--tasks", spec.default_tasks])
        except SystemExit:
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"  [dry-run error: {exc}]")


def replay_and_analyze(runs_root: Path) -> None:
    print("\n" + "=" * 70, "\n2. Replay recorded §7.4 runs through the pipeline\n" + "=" * 70)
    n = replay.replay_experiment(runs_root, experiment="modelsplit_replay")
    recs = replay.load_replayed(runs_root)
    print(f"adapted {n} legacy runs -> {len(recs)} records")
    if not recs:
        print("  (no legacy runs found; skipping the replay analysis)")
        return
    spec = ExperimentSpec(name="modelsplit_replay", title="Replayed §7.4 model-split (offline rehearsal)",
                          question="pipeline rehearsal on real recorded data", arms=lambda a: [], default_tasks="custom_dev")
    res = standard_analyze(spec, runs_root=runs_root)
    print(f"  standard analyzer: {res['n_records']} records across {len(res.get('arms', {}))} model-arms")
    importlib.import_module("eval.experiments.e11_cost.analyze").main(["--experiments", "modelsplit_replay", "--runs", str(runs_root)])
    # a trace excerpt between the leanest and the heaviest model on petfinder
    arms = analysis.by_arm(recs)
    have = [a for a in ("anthropic-claude-opus-4-8", "moonshotai-kimi-k2-6") if a in arms]
    if len(have) == 2:
        importlib.import_module("eval.experiments.e12_traces.analyze").main(
            ["--experiment", "modelsplit_replay", "--arm-a", have[0], "--arm-b", have[1], "--metric", "tool_calls", "--runs", str(runs_root)])
    print(f"\n  artifacts -> {report.out_dir('modelsplit_replay')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline dress rehearsal")
    ap.add_argument("--runs", default=str(DEFAULT_RUNS_ROOT))
    ap.add_argument("--skip-dry-run", action="store_true")
    ap.add_argument("--skip-replay", action="store_true")
    args = ap.parse_args(argv)
    if not args.skip_dry_run:
        dry_run_all()
    if not args.skip_replay:
        replay_and_analyze(Path(args.runs))
    print("\nRehearsal complete. Nothing above spent API credits or launched a live browser task.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
