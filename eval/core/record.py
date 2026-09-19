"""Build run records for run directories that have none, and freeze results.jsonl.

    python -m eval.core.record --experiment sdk                 # record every un-recorded run
    python -m eval.core.record --experiment sdk --rescue        # + re-harvest runs whose process died
    python -m eval.core.record --rebuild-results --experiment ablate10 \\
        --to eval/artifacts/ablate10_paper/results.jsonl        # frozen copy WITH metrics

The SDK audit trail (``superbrowser_bridge.audit``) writes the same run
directory the harness writes, minus the judge verdicts and ``run_record.json``
(both need the evaluator credentials and the harness). This module closes that
gap without a judge: it assembles ``run_record.json`` from the files on disk and
appends the row to the experiment's ``results.jsonl``. ``python -m
eval.core.judge --experiment <name>`` does the same AND judges; run whichever
the available credentials allow. Both are idempotent: an existing record is
kept unless ``--force``.

``--rebuild-results`` regenerates ``results.jsonl`` from the per-run records,
which is what carries the analyzers' process metrics (CSD, DRR, grounding);
the file the runner appends during a sweep has ``metrics: {}``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from eval._bootstrap import REPO_ROOT
from eval.core.harvest import build_record
from eval.core.records import append_result, rebuild_results, results_path


def _meta(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def rescue_run(run_dir: Path) -> bool:
    """A run whose process died before ``finish`` leaves ``meta.json`` with
    ``stop_reason: running`` (written at start by the SDK recorder). Harvest
    whatever the memory directories still hold and stamp the run as a harness
    error so it is excluded, never scored as a task failure."""
    meta = _meta(run_dir)
    if meta.get("stop_reason") != "running":
        return False
    ids = list(meta.get("role_task_ids") or []) or ([meta["orch_task_id"]] if meta.get("orch_task_id") else [])
    try:
        from superbrowser_bridge.audit import harvest_memory_dirs, index_screenshots

        harvested = harvest_memory_dirs(run_dir, ids)
        index_screenshots(run_dir)
    except Exception as exc:  # noqa: BLE001 - the stamp below still lands
        harvested = []
        print(f"[record] rescue harvest failed for {run_dir}: {exc}")
    meta.update({"stop_reason": "harness_error", "error": meta.get("error") or "process ended before the run finished",
                 "ended_at": meta.get("ended_at") or time.time(), "role_task_ids": sorted(set(ids) | set(harvested)),
                 "rescued_at": time.time()})
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
    return True


def record_experiment(runs_root: Path, experiment: str, *, force: bool = False, rescue: bool = False,
                      dry_run: bool = False) -> list[Path]:
    base = Path(runs_root) / experiment
    done: list[Path] = []
    for spec in sorted(base.glob("*/*/seed*/spec.json")):
        d = spec.parent
        if d.parts and any(part.startswith("_") for part in d.relative_to(base).parts):
            continue  # archived / failed attempts are never re-recorded
        if (d / "run_record.json").exists() and not force:
            continue
        if rescue:
            rescue_run(d)
        if _meta(d).get("stop_reason") == "running":
            print(f"[record] {d}: still marked running (pass --rescue after the process is gone); skipped")
            continue
        if dry_run:
            print(f"would record {d}")
            continue
        rec = build_record(d)
        rec.write(d)
        append_result(results_path(Path(runs_root), experiment), rec)
        print(f"{rec.run_id}: success={rec.success} decided_by={rec.outcome.get('decided_by')} "
              f"stop={rec.outcome.get('stop_reason')} iters={rec.counts.get('worker_iterations')} "
              f"tools={rec.counts.get('tool_calls_executed')}")
        done.append(d)
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--experiment", required=True)
    ap.add_argument("--out", default=str(REPO_ROOT / "eval" / "runs"), help="runs root (default eval/runs)")
    ap.add_argument("--force", action="store_true", help="rebuild records that already exist")
    ap.add_argument("--rescue", action="store_true", help="harvest runs whose process died before finishing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild-results", action="store_true",
                    help="regenerate results.jsonl from the per-run records (carries the analyzers' metrics)")
    ap.add_argument("--to", default=None, help="with --rebuild-results: write the rebuilt file here instead of in place")
    args = ap.parse_args(argv)
    runs_root = Path(args.out)
    if args.rebuild_results:
        out = rebuild_results(runs_root, args.experiment, out=Path(args.to) if args.to else None)
        n = sum(1 for line in out.open(encoding="utf-8") if line.strip())
        print(f"rebuilt {out} ({n} records)")
        return 0
    done = record_experiment(runs_root, args.experiment, force=args.force, rescue=args.rescue, dry_run=args.dry_run)
    print(f"recorded {len(done)} run(s) under {runs_root / args.experiment}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
