"""Per-task token accounting driver (via the SuperBrowser SDK).

Run a handful of tasks through the public ``SuperBrowser`` facade and report the
**total input / output tokens** each consumed — summed across every role
(orchestrator + worker delegations + the separate vision model). Screenshot
tokens are included: brain screenshots are already inside ``input_tokens``, and
the dedicated vision model is surfaced as ``vision_tokens``. Each task's full
breakdown is saved to ``meta.json`` and a roll-up to ``summary.csv``.

Usage (from the repo root, with the TS server running: ``cd .. && npm start``):
    source venv/bin/activate
    python -m eval.experiments.modelsplit.run_tokens --tasks-json eval/experiments/modelsplit/token_tasks.json          # whole file
    python -m eval.experiments.modelsplit.run_tokens --tasks-json eval/experiments/modelsplit/token_tasks.json --tasks petfinder_rabbits   # ONE task
    python -m eval.experiments.modelsplit.run_tokens --tasks-json eval/experiments/modelsplit/token_tasks.json --tasks petfinder_rabbits,trip_flight_dac_bkk
    python -m eval.experiments.modelsplit.run_tokens                          # eval/experiments/modelsplit/tasks.py suite, mode=auto
    python -m eval.experiments.modelsplit.run_tokens --tasks petfinder_rabbits --mode browser

Run tasks one at a time or in subsets with ``--tasks <id[,id...]>`` (works with
both --tasks-json and eval/experiments/modelsplit/tasks.py). ``summary.csv`` ACCUMULATES across runs —
each run adds/updates its tasks' rows, so task-by-task builds one combined table;
re-running a task overwrites its own row. Per-task ``meta.json`` is always kept.

``--tasks-json`` points at a JSON list of ``{"id","instruction","url","mode"}``
objects, so you can drop in your own 5-10 tasks WITHOUT editing source. Each
task's ``mode`` is optional (defaults to ``--mode``, itself ``auto``); a task the
classifier routes to ``fetch`` legitimately reports 0 screenshot/vision tokens.

The candidate model is whatever you've wired in ``~/.nanobot/config.json`` —
same as ``eval/run_eval.py``. Output: ``eval/token_runs/<label>/<id>/meta.json``
plus ``eval/token_runs/<label>/summary.csv``.
"""
from __future__ import annotations

# Bootstrap (sys.path + .env) runs via the package __init__ on import.
from eval import _bootstrap  # noqa: F401
from eval._bootstrap import REPO_ROOT, read_active_model, slugify

import argparse
import csv
import json
import time
from dataclasses import dataclass
from pathlib import Path

from .tasks import active_tasks

VALID_MODES = ("auto", "fetch", "browser")

_SUMMARY_FIELDS = [
    "task_id",
    "mode",
    "success",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "vision_tokens",
    "vision_calls",
    "duration_sec",
]


@dataclass
class TokenTask:
    id: str
    instruction: str
    url: str | None = None
    mode: str = "auto"


def _norm_mode(mode: str | None) -> str:
    m = (mode or "auto").strip().lower()
    if m not in VALID_MODES:
        raise SystemExit(f"invalid mode {mode!r}; expected one of {VALID_MODES}")
    return m


def _select(tasks: list[TokenTask], tasks_arg: str | None) -> list[TokenTask]:
    """Filter by a comma-separated id selection ('all'/empty = keep all).

    Applies to both task sources, so ``--tasks <id>`` runs exactly one task
    (or ``--tasks id1,id2`` a subset) whether tasks came from --tasks-json or
    eval/experiments/modelsplit/tasks.py.
    """
    if not tasks_arg or tasks_arg == "all":
        return tasks
    wanted = {t.strip() for t in tasks_arg.split(",") if t.strip()}
    chosen = [t for t in tasks if t.id in wanted]
    missing = wanted - {t.id for t in chosen}
    if missing:
        known = ", ".join(t.id for t in tasks)
        raise SystemExit(f"unknown task id(s): {', '.join(sorted(missing))}. Available: {known}")
    return chosen


def _load_tasks(args) -> list[TokenTask]:
    """Tasks from --tasks-json (your own file) or the eval/experiments/modelsplit/tasks.py suite,
    then narrowed by the --tasks id filter."""
    if args.tasks_json:
        raw = json.loads(Path(args.tasks_json).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise SystemExit(
                f"--tasks-json must be a JSON list of task objects, got {type(raw).__name__}"
            )
        out: list[TokenTask] = []
        for i, obj in enumerate(raw):
            if not isinstance(obj, dict) or not obj.get("instruction"):
                raise SystemExit(f"--tasks-json[{i}] needs at least an 'instruction' field")
            out.append(
                TokenTask(
                    id=str(obj.get("id") or f"task{i + 1}"),
                    instruction=str(obj["instruction"]),
                    url=obj.get("url"),
                    mode=_norm_mode(obj.get("mode") or args.mode),
                )
            )
    else:
        out = [
            TokenTask(id=t.id, instruction=t.instruction, url=t.url, mode=_norm_mode(args.mode))
            for t in active_tasks()
        ]
    return _select(out, args.tasks)


_NUMERIC_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "vision_tokens", "vision_calls")


def _coerce(row: dict) -> dict:
    """Coerce a CSV-read row back to ints/bool so re-loaded rows sum + print
    like freshly-produced ones."""
    out = dict(row)
    for k in _NUMERIC_FIELDS:
        try:
            out[k] = int(float(out.get(k) or 0))
        except (TypeError, ValueError):
            out[k] = 0
    try:
        out["duration_sec"] = float(out.get("duration_sec") or 0)
    except (TypeError, ValueError):
        out["duration_sec"] = 0.0
    out["success"] = str(out.get("success")).strip().lower() in ("true", "1", "yes")
    return out


def _read_summary(path: Path) -> list[dict]:
    """Load an existing summary.csv (empty list if absent) so task-by-task runs
    accumulate into one file instead of overwriting."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [_coerce(r) for r in csv.DictReader(f)]


def _write_summary(path: Path, rows: list[dict]) -> None:
    """(Re)write the roll-up CSV. Called after every task so a crash mid-batch
    keeps the rows collected so far."""
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in _SUMMARY_FIELDS})


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Per-task input/output token accounting via the SuperBrowser SDK."
    )
    ap.add_argument(
        "--tasks",
        default="all",
        help="comma-separated task ids from eval/experiments/modelsplit/tasks.py, or 'all' (default)",
    )
    ap.add_argument(
        "--tasks-json",
        default=None,
        help="path to a JSON list of {id,instruction,url,mode} tasks (overrides --tasks)",
    )
    ap.add_argument(
        "--mode",
        default="auto",
        help="default per-task mode: auto|fetch|browser (a task's own 'mode' wins)",
    )
    ap.add_argument(
        "--label",
        default=None,
        help="output subdir under eval/token_runs/ (default: active model slug)",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "per-task wall-clock timeout in seconds. Default: NONE (no timeout) "
            "— a task runs until it finishes or hits the worker/orchestrator "
            "iteration caps (config maxToolIterations) or the consecutive-script "
            "abort valve (SUPERBROWSER_MAX_CONSEC_SCRIPTS). Pass a positive number "
            "to re-impose a wall-clock cap; 0 or negative also means no timeout."
        ),
    )
    ap.add_argument(
        "--auto-start",
        action="store_true",
        help="auto-start the browser engine if it isn't already running",
    )
    ap.add_argument(
        "--out", default=None, help="output root (default: eval/token_runs)"
    )
    args = ap.parse_args()

    # None / 0 / negative → NO per-task wall-clock (the 900s barrier is removed).
    # The task is then bounded only by the worker/orchestrator iteration caps
    # (config maxToolIterations) and the consecutive-script abort valve
    # (SUPERBROWSER_MAX_CONSEC_SCRIPTS), so a slow-but-progressing model can
    # finish instead of being killed mid-step. sb.run(timeout=None) skips the
    # asyncio.wait_for wrap in _capture.py entirely.
    task_timeout = args.timeout if (args.timeout and args.timeout > 0) else None

    _norm_mode(args.mode)  # validate early
    tasks = _load_tasks(args)
    if not tasks:
        raise SystemExit("no tasks to run (check --tasks / --tasks-json)")

    model_info = read_active_model()
    label = args.label or slugify(model_info.get("model", "unknown"))
    out_root = Path(args.out) if args.out else (REPO_ROOT / "eval" / "token_runs")
    outdir = out_root / label
    outdir.mkdir(parents=True, exist_ok=True)
    summary_path = outdir / "summary.csv"

    from runagent_superbrowser import SuperBrowser

    # Carry over prior rows for OTHER tasks so running task-by-task accumulates
    # into one summary.csv (re-running a task replaces its own row).
    current_ids = {t.id for t in tasks}
    rows: list[dict] = [r for r in _read_summary(summary_path) if r.get("task_id") not in current_ids]
    carried = len(rows)

    print(
        f"\n[run_tokens] model={model_info.get('model')} label={label} tasks={len(tasks)}"
        + (f" (+{carried} carried over in summary.csv)" if carried else "")
        + f"  timeout={'none (iteration-capped)' if task_timeout is None else f'{task_timeout:g}s'}"
    )

    with SuperBrowser(auto_start_server=args.auto_start) as sb:
        for t in tasks:
            print(f"\n=== [{label}] {t.id} (mode={t.mode}) ===")
            print(f"    {t.instruction[:110]}")
            start = time.time()
            try:
                res = sb.run(t.instruction, url=t.url, mode=t.mode, timeout=task_timeout)
                err = res.error
            except Exception as exc:  # never let one task kill the batch
                res = None
                err = f"{type(exc).__name__}: {exc}"
            duration = round(time.time() - start, 2)

            usage = (res.usage if res is not None else None) or {}
            row = {
                "task_id": t.id,
                "mode": t.mode,
                "success": bool(res.success) if res is not None else False,
                "input_tokens": res.input_tokens if res is not None else 0,
                "output_tokens": res.output_tokens if res is not None else 0,
                "total_tokens": res.total_tokens if res is not None else 0,
                "vision_tokens": usage.get("vision_tokens", 0),
                "vision_calls": usage.get("vision_calls", 0),
                "duration_sec": duration,
            }
            rows.append(row)

            task_dir = outdir / t.id
            task_dir.mkdir(parents=True, exist_ok=True)
            meta = {
                "label": label,
                "task_id": t.id,
                "orch_task_id": res.task_id if res is not None else "",
                "mode": t.mode,
                "model": model_info,
                "instruction": t.instruction,
                "url": t.url,
                "final_answer": res.text if res is not None else "",
                "success": row["success"],
                "error": err,
                "duration_sec": duration,
                "timestamp": time.time(),
                "usage": usage or None,
            }
            (task_dir / "meta.json").write_text(
                json.dumps(meta, indent=2, default=str), encoding="utf-8"
            )
            _write_summary(summary_path, rows)  # incremental — survives a mid-batch crash

            print(
                f"    -> in={row['input_tokens']} out={row['output_tokens']} "
                f"total={row['total_tokens']} vision={row['vision_tokens']} "
                f"({row['vision_calls']} calls) success={row['success']} {duration}s"
            )
            if err:
                print(f"       note: {err}")

    # Final summary table — everything now in summary.csv (this run + carried).
    grand = {k: sum(int(r.get(k, 0) or 0) for r in rows) for k in _NUMERIC_FIELDS}
    print("\n" + "=" * 84)
    print(f"TOKEN SUMMARY  [{label}]  ({len(rows)} tasks)")
    print("-" * 84)
    print(
        f"{'task_id':<28}{'mode':<8}{'in':>11}{'out':>10}{'total':>11}{'vision':>10}{'ok':>6}"
    )
    for row in rows:
        print(
            f"{row['task_id'][:27]:<28}{row['mode']:<8}{row['input_tokens']:>11}"
            f"{row['output_tokens']:>10}{row['total_tokens']:>11}{row['vision_tokens']:>10}"
            f"{str(row['success']):>6}"
        )
    print("-" * 84)
    print(
        f"{'TOTAL':<28}{'':<8}{grand['input_tokens']:>11}{grand['output_tokens']:>10}"
        f"{grand['total_tokens']:>11}{grand['vision_tokens']:>10}"
    )
    print("=" * 84)
    print(f"\nPer-task metadata ({len(rows)}):")
    for r in rows[:8]:
        print(f"    {outdir / r['task_id'] / 'meta.json'}")
    if len(rows) > 8:
        print(f"    … and {len(rows) - 8} more under {outdir}/")
    print(f"Summary CSV:       {summary_path}")


if __name__ == "__main__":
    main()
