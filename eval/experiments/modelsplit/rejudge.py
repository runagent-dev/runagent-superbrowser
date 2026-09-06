"""Re-judge saved eval runs offline — no browser, no worker-model spend.

When the browser/worker ran fine but only the LLM-judge call failed (e.g. the
judge key was out of quota → 429), the final answer is already saved on disk;
only the verdict is missing. This re-reads each run's `final_answer`, re-runs the
judge (now configured via `SUPERBROWSER_EVAL_JUDGE_*` in .env), and rewrites
`meta.json["judge"]` + `judge.json` in place. Tool-use metrics are unaffected;
only `task_success` is refreshed. Re-run `python -m eval.experiments.modelsplit.analyzer` afterwards.

By default only re-judges runs that NEED it (judge `success` is null, or the
rationale is a judge/quota error) AND whose worker produced a real answer (not a
provider error / empty). `--force` re-judges everything; `--dry-run` shows the
new verdicts without writing.

Usage:
  python -m eval.experiments.modelsplit.rejudge --label openai-gpt-5-4     # one model's runs
  python -m eval.experiments.modelsplit.rejudge                            # all runs under eval/runs
  python -m eval.experiments.modelsplit.rejudge --runs eval/runs_ablation --force
"""
from __future__ import annotations

from eval import _bootstrap  # noqa: F401  (loads .env → judge config)
from eval._bootstrap import REPO_ROOT
from . import oracles
from .tasks import TASKS

import argparse
import asyncio
import json
from pathlib import Path

_TASK_BY_ID = {t.id: t for t in TASKS}


def _needs_rejudge(meta) -> bool:
    j = meta.get("judge") or {}
    if j.get("success") is None:
        return True
    rat = (j.get("rationale") or "").lower()
    return ("judge error" in rat) or ("429" in rat) or oracles.looks_like_api_error(rat)


async def _rejudge_one(meta_path: Path, args) -> str:
    try:
        meta = json.loads(meta_path.read_text())
    except Exception as exc:
        return f"skip  (unreadable meta {meta_path}): {exc}"
    final = meta.get("final_answer") or ""
    task_id = meta.get("task_id")
    tag = f"{meta.get('label', '?')}/{task_id}/seed{meta.get('seed')}"

    if not args.force and not _needs_rejudge(meta):
        cur = (meta.get("judge") or {}).get("success")
        return f"skip  {tag}: already judged (success={cur})"
    # Can't judge a run whose worker itself errored / produced no answer.
    if meta.get("api_error") or oracles.looks_like_api_error(final) or not final.strip():
        return f"skip  {tag}: worker produced no real answer (api_error/empty)"
    task = _TASK_BY_ID.get(task_id)
    if task is None:
        return f"skip  {tag}: no rubric for task id {task_id!r} in eval/experiments/modelsplit/tasks.py"

    verdict = await oracles.judge(task, final)
    if verdict.get("success") is None:
        return f"FAIL  {tag}: judge still errored — {(verdict.get('rationale') or '')[:90]}"
    if args.dry_run:
        return f"DRY   {tag}: success={verdict['success']}  ({verdict['rationale'][:70]})"
    meta["judge"] = verdict
    meta_path.write_text(json.dumps(meta, indent=2))
    (meta_path.parent / "judge.json").write_text(json.dumps(verdict, indent=2))
    return f"wrote {tag}: success={verdict['success']}  ({verdict['rationale'][:70]})"


async def _main():
    ap = argparse.ArgumentParser(description="Re-judge saved runs offline (no browser)")
    ap.add_argument("--runs", default=str(REPO_ROOT / "eval" / "runs"))
    ap.add_argument("--label", default=None, help="only this run label (dir under runs/)")
    ap.add_argument("--tasks", default=None, help="comma-separated task ids to limit to")
    ap.add_argument("--force", action="store_true", help="re-judge even already-judged runs")
    ap.add_argument("--dry-run", action="store_true", help="show verdicts without writing")
    args = ap.parse_args()

    root = Path(args.runs)
    pat = f"{args.label}/*/seed*/meta.json" if args.label else "*/*/seed*/meta.json"
    metas = sorted(root.glob(pat))
    if args.tasks:
        want = {t.strip() for t in args.tasks.split(",")}
        kept = []
        for m in metas:
            try:
                if json.loads(m.read_text()).get("task_id") in want:
                    kept.append(m)
            except Exception:
                pass
        metas = kept
    if not metas:
        print(f"[error] no meta.json under {root} (label={args.label}, tasks={args.tasks})")
        return

    judge_model = oracles._resolve_judge_client()[1]
    print(f"Re-judging {len(metas)} run(s) with judge={judge_model}"
          f"{' [dry-run]' if args.dry_run else ''}\n")
    results = []
    for mp in metas:  # sequential — gentle on the judge's rate limit
        results.append(await _rejudge_one(mp, args))
    for r in results:
        print(" ", r)
    wrote = sum(1 for r in results if r.startswith("wrote"))
    print(f"\nUpdated {wrote} run(s)."
          + (" Re-run `python -m eval.experiments.modelsplit.analyzer` to refresh the CSVs." if wrote else ""))


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()
