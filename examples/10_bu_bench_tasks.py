#!/usr/bin/env python3
"""Run a benchmark subset through SuperBrowser, one task at a time.

    --benchmark bu_bench_v1    browser-use BU Bench V1 (default, subset bu25)
    --benchmark scroll_iframe  iframe/widget + long-list scrolling (subset si12)

Each task is a separate `eval.core.runner` invocation on the `ledger` arm, so
it gets its own server, its own log file and its own audit trail under
``eval/runs/bu_bench_v1/ledger/<task_id>/seed0/`` (spec, meta, ledgers,
screenshots, usage, config, run.log) exactly like the paper's sweeps.

Why one at a time: a single sweep hides which task broke and forces a whole
re-run to retry one. Here every task is isolated, its outcome is classified,
and a failure prints the one command that retries just that task.

    # run the whole subset, skipping tasks already done
    python examples/10_bu_bench_tasks.py

    # see what is done / failed / pending without running anything
    python examples/10_bu_bench_tasks.py --status

    # retry a single task after fixing the harness (archives the old attempt)
    python examples/10_bu_bench_tasks.py --redo 5eca85e0-4b4d-418e-ad1a-390dc68910d1

    # run a few specific tasks
    python examples/10_bu_bench_tasks.py --tasks <id>,<id>

NOTE ON WHAT "FAIL" MEANS HERE
This script judges nothing. It reports whether the run *executed*: ok, timeout,
crashed, or an infrastructure failure (proxy, dead session, provider error).
Those are the failures you can fix. Whether the agent actually solved the task
is decided later by the shared judge over all four harnesses:

    cd /root/agentic-browser/harness-bench
    python -m harness_bench.run --harness superbrowser --tasks bu25 --collect-only
    python -m harness_bench.judge_all
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.core.records import run_dir_for  # noqa: E402
from eval.core.tasks import load_benchmark  # noqa: E402

# Task sets this script can drive. The experiment name equals the benchmark
# name, so each set gets its own run tree under eval/runs/<benchmark>/.
# `mode` is the DEFAULT engine pinning for each benchmark, overridable with
# --mode. It is not cosmetic. Under "auto" the orchestrator keeps the search
# worker and may answer a task without opening a browser at all — which is
# reasonable for BU Bench, a third of whose tasks are research questions, and
# useless for a benchmark whose whole point is interacting with a widget.
# Observed: the 401(k) slider task returned an annuity calculation from the
# model's own head in 28s with iters=0 tools=0 vision=0, having announced
# "I can answer directly without needing to browse Chase's calculator."
BENCHMARKS = {
    "bu_bench_v1":   {"subset": "bu25", "mode": "auto",
                      "what": "browser-use BU Bench V1"},
    "scroll_iframe": {"subset": "si12", "mode": "browser",
                      "what": "iframe/widget + long-list scrolling (from Online-Mind2Web)"},
    "hard5":         {"subset": "hi5",  "mode": "browser",
                      "what": "five author-supplied hard-interaction tasks, one obstacle each"},
}
BENCHMARK = os.environ.get("HB_BENCHMARK", "bu_bench_v1")
EXPERIMENT = BENCHMARK
ARM = "ledger"
SUBSET = BENCHMARKS.get(BENCHMARK, {}).get("subset", "all")
RUNS_ROOT = REPO_ROOT / "eval" / "runs"


def log_dir() -> Path:
    return RUNS_ROOT / EXPERIMENT / "_task_logs"


def status_file() -> Path:
    return RUNS_ROOT / EXPERIMENT / "_task_status.json"
HARNESS_BENCH = Path(os.environ.get("HARNESS_BENCH_ROOT", "/root/agentic-browser/harness-bench"))

# Markers that mean the *infrastructure* failed, not the agent. Kept in sync with
# eval.core.audit_sweep so both tools classify a run the same way.
try:
    from eval.core.audit_sweep import INFRA_MARKERS
except Exception:  # pragma: no cover - audit_sweep is optional here
    INFRA_MARKERS = re.compile(
        r"ERR_NO_SUPPORTED_PROXIES|ERR_TUNNEL_CONNECTION_FAILED|ERR_PROXY_CONNECTION_FAILED|"
        r"proxy error|session backend lost|session (?:is )?(?:dead|expired)", re.I)

NEEDS_FIX = ("infra", "crashed", "provider_error")


# --------------------------------------------------------------------- tasks
def subset_ids() -> list[str]:
    """Task ids for the subset, from harness-bench if present (single source of
    truth), else from this repo's own benchmarks/subsets.json."""
    hb_subsets = {"bu_bench_v1": "subsets.json", "scroll_iframe": "subsets_scroll_iframe.json",
                  "hard5": "subsets_hard5.json"}
    for path in (HARNESS_BENCH / "tasks" / hb_subsets.get(BENCHMARK, "subsets.json"),
                 REPO_ROOT / "eval" / "benchmarks" / "subsets.json"):
        if path.exists():
            entry = json.loads(path.read_text()).get(SUBSET)
            if entry:
                ids = entry.get("task_ids", entry) if isinstance(entry, dict) else entry
                if ids:
                    return list(ids)
    raise SystemExit(f"subset {SUBSET!r} not found; run harness_bench.tasks --export-superbrowser first")


def load_tasks(selected: list[str] | None = None) -> list[dict[str, Any]]:
    by_id = {t.task_id: t for t in load_benchmark(BENCHMARK)}
    ids = selected or subset_ids()
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SystemExit(f"unknown task id(s) for benchmark {BENCHMARK!r}: {missing[:5]}\n"
                         f"re-export with: python -m harness_bench.tasks --benchmark {BENCHMARK} --export-superbrowser")
    out = []
    for i in ids:
        t = by_id[i]
        out.append({"task_id": t.task_id, "instruction": t.instruction,
                    "category": (t.extra or {}).get("category", ""), "level": t.level})
    return out


# ------------------------------------------------------------------ outcomes
def run_dir(task_id: str) -> Path:
    return run_dir_for(RUNS_ROOT, EXPERIMENT, ARM, task_id, 0)


def infra_hits(d: Path) -> int:
    """Occurrences of infrastructure markers in this run's logs."""
    n = 0
    for p in [d / "run.log", *sorted(d.glob("workers/*.json"))]:
        if p.exists():
            try:
                n += len(INFRA_MARKERS.findall(p.read_text(errors="replace")))
            except Exception:
                pass
    return n


def classify(task_id: str) -> dict[str, Any]:
    """Execution outcome of the recorded attempt. Says nothing about correctness."""
    d = run_dir(task_id)
    info: dict[str, Any] = {"task_id": task_id, "run_dir": str(d), "status": "pending"}
    if not d.exists():
        return info
    meta_p, rec_p = d / "meta.json", d / "run_record.json"
    if not meta_p.exists():
        info["status"] = "crashed"
        info["detail"] = "no meta.json: the run died before it could be harvested"
        return info
    meta = json.loads(meta_p.read_text())
    stop = meta.get("stop_reason")
    info.update({"stop_reason": stop, "error": meta.get("error"),
                 "duration_s": meta.get("duration_s"), "n_screenshots": meta.get("n_screenshots"),
                 "model": (meta.get("effective_defaults") or {}).get("model"),
                 "mode": ((meta.get("environment") or {}).get("env") or {}).get("SUPERBROWSER_EVAL_MODE"),
                 "started_at": meta.get("started_at")})
    if rec_p.exists():
        rec = json.loads(rec_p.read_text())
        outcome = rec.get("outcome") or {}
        counts = rec.get("counts") or {}
        info.update({"failure_reason": outcome.get("failure_reason"),
                     "iters": counts.get("worker_iterations"), "tools": counts.get("tool_calls_executed"),
                     "vision": counts.get("vision_calls"),
                     "tokens": (rec.get("tokens") or {}).get("total_tokens"),
                     "cost_usd": (rec.get("cost") or {}).get("usd")})
        if outcome.get("failure_reason") in ("api_error", "harness_error"):
            info["status"] = "provider_error" if outcome["failure_reason"] == "api_error" else "crashed"
            info["detail"] = f"{outcome['failure_reason']}: excluded, not a task outcome"
            return info
    hits = infra_hits(d)
    info["infra_markers"] = hits
    if stop == "timeout":
        info["status"] = "timeout"
        info["detail"] = "hit the wall clock; counts as a task failure, not a harness bug"
    elif stop in (None, "running"):
        info["status"] = "crashed"
        info["detail"] = "meta says the run never finished (killed or still in flight)"
    elif hits >= 3:
        info["status"] = "infra"
        info["detail"] = f"{hits} proxy/dead-session markers in the logs: browser layer failed"
    elif not rec_p.exists():
        info["status"] = "crashed"
        info["detail"] = "no run_record.json: harvest did not complete"
    else:
        info["status"] = "ok"
        info["detail"] = "executed cleanly; correctness is decided by the judge later"
    return info


# ---------------------------------------------------------------- execution
def archive_previous(task_id: str, reason: str) -> Path | None:
    """Move an existing attempt aside. Attempts cost money; never delete one."""
    d = run_dir(task_id)
    if not d.exists():
        return None
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(d.stat().st_mtime))
    dest = RUNS_ROOT / EXPERIMENT / "_failed_attempts" / f"{ARM}__{task_id}__seed0__{stamp}__{reason}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(d), str(dest))
    return dest


def run_one(task: dict[str, Any], *, model: str, timeout: int, viewer_port: int | None,
            mode: str = "auto") -> int:
    """One `eval.core.runner` invocation for one task, streamed and logged."""
    log_dir().mkdir(parents=True, exist_ok=True)
    log_path = log_dir() / f"{task['task_id']}__{time.strftime('%Y%m%dT%H%M%S')}.log"
    cmd = [sys.executable, "-m", "eval.core.runner", "--experiment", EXPERIMENT, "--arms", ARM,
           "--benchmark", BENCHMARK, "--tasks", task["task_id"], "--model", model,
           "--manage-server", "--no-judge", "--follow", "--wall-clock", str(timeout)]
    cmd += ["--no-viewer"] if viewer_port is None else ["--viewer-port", str(viewer_port)]
    print(f"    $ {' '.join(cmd[2:])}", flush=True)
    print(f"    log: {log_path}", flush=True)
    with log_path.open("wb") as log:
        log.write(f"# {json.dumps(task, ensure_ascii=False)}\n# {' '.join(cmd)}\n".encode())
        log.flush()
        proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUNBUFFERED": "1",
                                     "SUPERBROWSER_EVAL_MODE": mode})
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.buffer.write(line)
            sys.stdout.flush()
            log.write(line)
        return proc.wait()


def collect_one(task_id: str, model: str) -> str:
    """Import one finished run into harness-bench (unified record + screenshots)
    so all four harnesses sit side by side for judging. Never re-runs anything.

    --benchmark is required, not optional: harness_bench resolves task ids
    against one benchmark at a time and defaults to bu_bench_v1, so omitting
    it made every hard5 / scroll_iframe collect die with
    `KeyError: unknown task id(s)` after the run had already succeeded.
    """
    if not HARNESS_BENCH.exists():
        return "harness-bench not found; skipped"
    r = subprocess.run([sys.executable, "-m", "harness_bench.run", "--harness", "superbrowser",
                        "--benchmark", BENCHMARK,
                        "--model", model, "--tasks", task_id, "--collect-only", "--no-judge"],
                       cwd=str(HARNESS_BENCH), capture_output=True, text=True)
    dest = HARNESS_BENCH / "runs" / "superbrowser" / model.replace("/", "-") / task_id
    if dest.exists():
        return str(dest)
    tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or ["no output"]
    return f"collect failed: {tail[0][:160]}"


# ------------------------------------------------------------------ reporting
def status_row(i: int, n: int, task: dict[str, Any], info: dict[str, Any]) -> str:
    mark = {"ok": "ok     ", "timeout": "TIMEOUT", "infra": "INFRA  ", "crashed": "CRASHED",
            "provider_error": "PROVIDER", "pending": "pending"}.get(info["status"], info["status"])
    bits = []
    for k, fmt in (("iters", "iters={}"), ("tools", "tools={}"), ("vision", "vision={}")):
        if info.get(k) is not None:
            bits.append(fmt.format(info[k]))
    if info.get("duration_s"):
        bits.append(f"{info['duration_s']:.0f}s")
    if info.get("cost_usd") is not None:
        bits.append(f"${info['cost_usd']:.3f}")
    return (f"[{i}/{n}] {task['task_id'][:8]} {task.get('category',''):16} {mark} " + " ".join(bits))


def print_status(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    print(f"{'#':>3}  {'task':8}  {'category':16} {'status':9} {'stop':8} {'iters':>5} {'wall':>7} {'cost':>7}")
    for i, t in enumerate(tasks, 1):
        info = classify(t["task_id"])
        counts[info["status"]] = counts.get(info["status"], 0) + 1
        print(f"{i:>3}  {t['task_id'][:8]}  {t.get('category',''):16} {info['status']:9} "
              f"{str(info.get('stop_reason') or '-'):8} {str(info.get('iters') or '-'):>5} "
              f"{(str(round(info['duration_s'])) + 's') if info.get('duration_s') else '-':>7} "
              f"{('$%.3f' % info['cost_usd']) if info.get('cost_usd') is not None else '-':>7}")
    print("\n  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return counts


def save_status(tasks: list[dict[str, Any]]) -> None:
    status_file().parent.mkdir(parents=True, exist_ok=True)
    status_file().write_text(json.dumps(
        {"experiment": EXPERIMENT, "arm": ARM, "subset": SUBSET, "written_at": time.time(),
         "tasks": [classify(t["task_id"]) | {"category": t.get("category")} for t in tasks]}, indent=2))


def main(argv: list[str] | None = None) -> int:
    global BENCHMARK, EXPERIMENT, SUBSET
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--benchmark", default=BENCHMARK, choices=sorted(BENCHMARKS),
                    help="task set to run: " + "; ".join(f"{k} = {v['what']}" for k, v in BENCHMARKS.items()))
    ap.add_argument("--model", default=None,
                    help="brain model id. Default: whatever agents.defaults.model says in "
                         "~/.nanobot/config.json, so changing the model there changes the runs. "
                         "The resolved id is passed to eval.core.runner explicitly and recorded "
                         "per run.")
    ap.add_argument("--timeout", type=int, default=1800, help="per-task wall clock (s); BU Bench uses 1800")
    ap.add_argument("--mode", choices=("auto", "browser", "fetch"), default=None,
                    help="auto: orchestrator picks the browser worker or the search worker per task, like the "
                         "other harnesses which also have fetch/search tools. browser: pin the engine (what the "
                         "paper's ablation uses) — the orchestrator cannot answer from its own knowledge. "
                         "Default is per benchmark: "
                         + ", ".join(f"{k}={v['mode']}" for k, v in sorted(BENCHMARKS.items()))
                         + ". Recorded in each run's env snapshot.")
    ap.add_argument("--tasks", default=None, help="comma-separated task ids (default: the whole subset)")
    ap.add_argument("--redo", default=None, help="comma-separated task ids to re-run; the old attempt is archived")
    ap.add_argument("--from-index", type=int, default=1, help="start at the Nth task of the subset")
    ap.add_argument("--all", action="store_true", help="re-run tasks that already executed cleanly")
    ap.add_argument("--status", action="store_true", help="print the table and exit")
    ap.add_argument("--keep-going", action="store_true",
                    help="continue after an infrastructure failure instead of stopping to be fixed")
    ap.add_argument("--viewer-port", type=int, default=None, help="live viewer port (default: no viewer)")
    ap.add_argument("--dry-run", action="store_true", help="print the per-task commands without running them")
    ap.add_argument("--no-collect", action="store_true",
                    help="do not import each finished run into harness-bench (collect later instead)")
    args = ap.parse_args(argv)
    BENCHMARK = args.benchmark
    EXPERIMENT = BENCHMARK
    SUBSET = BENCHMARKS[BENCHMARK]["subset"]
    # The model used to be hardcoded here, so editing ~/.nanobot/config.json
    # had no effect on a run and the id in the banner was a constant rather
    # than a fact. Resolve it from the same place the runner would.
    model_source = "--model"
    if not args.model:
        args.model = os.environ.get("MODEL") or ""
        model_source = "$MODEL"
    if not args.model:
        from eval._bootstrap import read_active_model
        args.model = read_active_model().get("model") or ""
        model_source = "~/.nanobot/config.json (agents.defaults.model)"
    if not args.model or args.model == "unknown":
        print("no model: pass --model, set $MODEL, or set agents.defaults.model "
              "in ~/.nanobot/config.json")
        return 2
    # Resolve the engine pinning only once the benchmark is known, so the
    # default tracks the task set rather than a single hardcoded value.
    if args.mode is None:
        args.mode = BENCHMARKS[BENCHMARK]["mode"]

    selected = [s.strip() for s in (args.redo or args.tasks or "").split(",") if s.strip()] or None
    tasks = load_tasks(selected)

    if args.status:
        print_status(tasks)
        save_status(tasks)
        print(f"\nstatus file: {status_file()}")
        return 0

    redo = {s.strip() for s in (args.redo or "").split(",") if s.strip()}
    queue = tasks[args.from_index - 1:] if not selected else tasks
    print(f"benchmark={BENCHMARK} ({BENCHMARKS[BENCHMARK]['what']})")
    print(f"experiment={EXPERIMENT} arm={ARM} model={args.model} mode={args.mode} "
          f"timeout={args.timeout}s tasks={len(queue)} runs_root={RUNS_ROOT}")
    print(f"  model from {model_source}")
    # A benchmark compares harnesses under ONE pinned model. Silently mixing
    # two across a task set makes the column meaningless, and the mix is
    # invisible once the runs are collected, so say it now.
    others = sorted({m for m in (classify(t["task_id"]).get("model") for t in tasks)
                     if m and m != args.model})
    if others:
        print(f"  WARNING: finished runs in {EXPERIMENT} already used {', '.join(others)}. "
              f"Mixing models across one task set invalidates the comparison — "
              f"re-run the others with --model {args.model}, or switch back.")
    if args.mode == "auto":
        print("  mode=auto: the orchestrator may answer a task WITHOUT opening a browser. "
              "Use --mode browser to require the engine.")
    print(f"judging is NOT run here; score all four harnesses later with harness_bench.judge_all\n")

    if args.dry_run:
        for i, task in enumerate(queue, args.from_index if not selected else 1):
            info = classify(task["task_id"])
            print(f"[{i}/{len(tasks)}] {task['task_id']} {task.get('category',''):16} {info['status']}")
            print(f"    $ python -m eval.core.runner --experiment {EXPERIMENT} --arms {ARM} "
                  f"--benchmark {BENCHMARK} --tasks {task['task_id']} --model {args.model} "
                  f"--manage-server --no-judge --follow --wall-clock {args.timeout}   "
                  f"(SUPERBROWSER_EVAL_MODE={args.mode})")
        return 0

    stopped = None
    for i, task in enumerate(queue, args.from_index if not selected else 1):
        info = classify(task["task_id"])
        if task["task_id"] in redo or args.all:
            if info["status"] != "pending":
                dest = archive_previous(task["task_id"], "redo")
                print(f"[{i}] archived previous attempt -> {dest}")
        elif info["status"] == "ok":
            print(f"[{i}/{len(tasks)}] {task['task_id'][:8]} {task.get('category','')} — skip (already executed)")
            continue
        elif info["status"] != "pending":
            dest = archive_previous(task["task_id"], info["status"])
            print(f"[{i}] previous attempt was {info['status']}; archived -> {dest}")

        print(f"\n[{i}/{len(tasks)}] {task['task_id']}  {task.get('category','')}")
        print(f"    {task['instruction'][:160]}")
        t0 = time.time()
        rc = run_one(task, model=args.model, timeout=args.timeout, viewer_port=args.viewer_port,
                     mode=args.mode)
        info = classify(task["task_id"])
        print("  " + status_row(i, len(tasks), task, info))
        if info.get("detail"):
            print(f"    {info['detail']}")
        print(f"    artifacts: {info['run_dir']}")
        if not args.no_collect and info["status"] in ("ok", "timeout"):
            print(f"    collected: {collect_one(task['task_id'], args.model)}")
        save_status(tasks)

        if info["status"] in NEEDS_FIX and not args.keep_going:
            stopped = (task, info, rc)
            break

    if stopped:
        task, info, rc = stopped
        print("\n" + "=" * 78)
        print(f"STOPPED: {task['task_id']} failed to execute ({info['status']}) — runner exit {rc}")
        print(f"  {info.get('detail','')}")
        print(f"  logs:      {log_dir()}")
        print(f"  run dir:   {info['run_dir']}")
        print(f"  server log: {RUNS_ROOT / EXPERIMENT / '_logs'}")
        print("\nThis is a harness problem, not a task result. Fix it, then retry just this task:")
        print(f"  python examples/10_bu_bench_tasks.py --redo {task['task_id']}")
        print("Then continue the rest:")
        print("  python examples/10_bu_bench_tasks.py")
        print("Or run past it without stopping:  --keep-going")
        print("=" * 78)
        return 1

    print("\n" + "=" * 78)
    counts = print_status(tasks)
    save_status(tasks)
    if not args.no_collect and HARNESS_BENCH.exists():
        print("\nre-syncing every finished run into harness-bench ...")
        rs = subprocess.run([sys.executable, "-m", "harness_bench.run", "--harness", "superbrowser",
                             "--benchmark", BENCHMARK,
                             "--model", args.model, "--tasks", SUBSET, "--collect-only", "--no-judge"],
                            cwd=str(HARNESS_BENCH), check=False, capture_output=True, text=True)
        if rs.returncode == 0:
            print(f"  -> {HARNESS_BENCH / 'runs' / 'superbrowser' / args.model.replace('/', '-')}")
        else:
            # Printing the destination unconditionally hid real failures —
            # the path is where the runs WOULD go, not proof any arrived.
            tail = (rs.stdout + rs.stderr).strip().splitlines()[-1:] or ["no output"]
            print(f"  -> re-sync FAILED: {tail[0][:200]}")
    pending = counts.get("pending", 0) + sum(counts.get(k, 0) for k in NEEDS_FIX)
    if pending:
        print(f"\n{pending} task(s) still need attention; re-run this script to continue.")
    else:
        print("\nAll tasks executed and collected. Score every harness together:")
        print(f"  cd {HARNESS_BENCH} && python -m harness_bench.judge_all")
        print(f"  cd {HARNESS_BENCH} && python -m harness_bench.aggregate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
