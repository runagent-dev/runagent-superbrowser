"""Build a stratified human-labelling sheet from finished runs.

    python -m eval.experiments.e9_evaluator_validation.make_sheet \
        --experiments e1_main,e2_memory_policy --n 60 --seed 7

Sampling is stratified over (automatic verdict x arm x site family) so both
successes and failures of every arm are represented. The sheet lists, per
sampled run, the task, the final answer, the action history, the last three
screenshots (paths) and the automatic verdicts; two annotators fill
``label_h1`` / ``label_h2`` (success|failure|cannot_judge) in the CSV, then
``analyze.py`` computes agreement, Cohen's kappa and the FP/FN patterns.
Automatic verdicts are shown in a separate column set so annotators can be
blinded by hiding those columns in the spreadsheet.
"""
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

from eval.core import report
from eval.core.judges.base import action_history
from eval.core.judges.webjudge import list_screenshots
from eval.core.loaders import DEFAULT_RUNS_ROOT, load_records, load_transcripts
from eval.core.tasks import Task, site_type

NAME = "e9_evaluator_validation"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the human-validation sheet")
    ap.add_argument("--experiments", default="e1_main,e2_memory_policy")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--runs", default=str(DEFAULT_RUNS_ROOT))
    args = ap.parse_args(argv)
    recs = []
    for e in [x.strip() for x in args.experiments.split(",") if x.strip()]:
        recs += load_records(e, runs_root=Path(args.runs))
    recs = [r for r in recs if r.outcome.get("success") is not None]
    if not recs:
        print("[e9] no judged records; run experiments first")
        return 0
    strata: dict[tuple, list] = defaultdict(list)
    for r in recs:
        t = Task(task_id=r.ids["task_id"], instruction="", start_url=r.ids.get("website"))
        strata[(bool(r.outcome.get("success")), str(r.ids.get("arm")), site_type(t))].append(r)
    rng = random.Random(args.seed)
    keys = sorted(strata)
    chosen: list = []
    # round-robin over strata until n
    pools = {k: rng.sample(v, len(v)) for k, v in strata.items()}
    while len(chosen) < args.n and any(pools.values()):
        for k in keys:
            if pools[k] and len(chosen) < args.n:
                chosen.append(pools[k].pop())
    rows = []
    for i, r in enumerate(chosen):
        run_dir = Path(r.ids["run_dir"])
        spec = json.loads((run_dir / "spec.json").read_text()) if (run_dir / "spec.json").exists() else {}
        actions = action_history(load_transcripts(run_dir))
        shots = list_screenshots(run_dir)
        rows.append({
            "item": i + 1, "run_id": r.run_id, "experiment": r.ids.get("experiment"), "arm": r.ids.get("arm"),
            "task_id": r.ids.get("task_id"), "website": r.ids.get("website"),
            "instruction": (spec.get("task") or {}).get("instruction", ""),
            "final_answer": (run_dir / "result.txt").read_text(encoding="utf-8")[:2000] if (run_dir / "result.txt").exists() else "",
            "n_actions": len(actions), "actions_tail": " || ".join(actions[-8:]),
            "screenshots_last3": " | ".join(str(p) for p in shots[-3:]),
            "label_h1": "", "label_h2": "", "notes_h1": "", "notes_h2": "",
            "auto_success": r.outcome.get("success"), "auto_decided_by": r.outcome.get("decided_by"),
            "auto_webjudge": r.outcome.get("success_webjudge"), "auto_answer_judge": r.outcome.get("success_answer_judge"),
            "auto_deterministic": r.outcome.get("success_deterministic"),
        })
    out = report.out_dir(NAME)
    path = report.write_csv(out / "labelling_sheet.csv", rows)
    print(f"[e9] wrote {path} with {len(rows)} items across {len(strata)} strata")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
