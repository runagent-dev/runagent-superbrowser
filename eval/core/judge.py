"""(Re)judge finished runs offline and refresh their records.

    python -m eval.core.judge --experiment e1_main [--judges webjudge,answer_judge] [--force]
    python -m eval.core.judge --run-dir eval/runs/e1_main/ledger/<task>/seed0

Useful when a judge key was missing/out of quota during the run, when the
judge model is changed (the verdict records its model), or to add the
deterministic checks after ``checks.json`` gained entries. Browser and brain
model are never touched; only ``judges/*.json``, ``run_record.json`` and the
experiment's ``results.jsonl`` change.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from eval._bootstrap import REPO_ROOT
from eval.core.harvest import build_record
from eval.core.judges import JUDGE_NAMES, judge_run_async
from eval.core.records import append_result, iter_run_dirs, results_path
from eval.core.tasks import Task


def _task_of(run_dir: Path) -> Task:
    spec = json.loads((run_dir / "spec.json").read_text())
    return Task.from_row(spec["task"])


async def _judge_dirs(dirs: list[Path], *, judges: list[str], force: bool, dry_run: bool) -> int:
    n = 0
    for d in dirs:
        task = _task_of(d)
        if dry_run:
            print(f"would judge {d} with {judges}")
            continue
        verdicts = await judge_run_async(d, task, which=judges, force=force)
        rec = build_record(d)
        rec.write(d)
        exp = rec.ids.get("experiment")
        runs_root = d.parents[3]
        append_result(results_path(runs_root, exp), rec)
        print(f"{rec.run_id}: " + ", ".join(f"{k}={v.success}" for k, v in verdicts.items()))
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Re-judge runs offline")
    ap.add_argument("--experiment", default=None)
    ap.add_argument("--run-dir", action="append", default=[])
    ap.add_argument("--out", default=str(REPO_ROOT / "eval" / "runs"))
    ap.add_argument("--judges", default=",".join(JUDGE_NAMES))
    ap.add_argument("--force", action="store_true", help="re-run judges that already have a verdict")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    dirs = [Path(d) for d in args.run_dir]
    if args.experiment:
        dirs += list(iter_run_dirs(Path(args.out), args.experiment))
        # runs that never got a record (crashed before harvest)
        for spec in sorted((Path(args.out) / args.experiment).glob("*/*/seed*/spec.json")):
            if spec.parent not in dirs:
                dirs.append(spec.parent)
        # runs whose directories live elsewhere (e.g. replayed legacy runs): follow results.jsonl
        from eval.core.records import read_results

        for rec in read_results(results_path(Path(args.out), args.experiment)):
            d = Path(rec.ids.get("run_dir", ""))
            if d.exists() and d not in dirs:
                dirs.append(d)
    if not dirs:
        print("nothing to judge (pass --experiment or --run-dir)")
        return 1
    judges = [j.strip() for j in args.judges.split(",") if j.strip()]
    n = asyncio.run(_judge_dirs(dirs, judges=judges, force=args.force, dry_run=args.dry_run))
    print(f"judged {n} run(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
