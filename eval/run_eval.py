"""Batch full-orchestrator eval runner.

Runs the task suite (eval/tasks.py) through the SAME orchestrator+worker pipeline
as nanobot/test_superbrowser.py, once per (task, seed), capturing every worker's
full transcript (via the gated tap in delegation.py) plus telemetry and a judge
verdict. The candidate model is whatever you've set in ~/.nanobot/config.json —
swap it there, re-run, repeat.

Usage (from the repo root, with the TS server running: `cd .. && npm start`):
    source venv/bin/activate
    python -m eval.run_eval                       # auto-labels by active model
    python -m eval.run_eval --seeds 3 --tasks all
    SUPERBROWSER_EVAL_SCHEMA_REMINDER=1 python -m eval.run_eval --label kimi_rescue

Output: eval/runs/<label>/<task_id>/seed<k>/{workers/*.json, ledgers/, result.txt,
judge.json, meta.json}
"""
from __future__ import annotations

# Bootstrap (sys.path + .env) runs via the package __init__ on import.
from . import _bootstrap  # noqa: F401
from ._bootstrap import NANOBOT_TREE, REPO_ROOT, read_active_model, slugify

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from . import oracles
from .tasks import Task, active_tasks


# --- bus-capture helper (verbatim from test_superbrowser.py) ----------------
async def _run_and_capture(orchestrator, task: str, session_key: str, *, hooks=None) -> str:
    """Run the orchestrator and return the user-visible content, falling back to
    bus-captured content when the answer is delivered via the message() tool."""
    bus = orchestrator._loop.bus
    captured: list[str] = []
    stop = asyncio.Event()

    async def _pump() -> None:
        while not stop.is_set():
            try:
                msg = await asyncio.wait_for(bus.consume_outbound(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            md = msg.metadata or {}
            if md.get("_progress") or md.get("_stream_delta") or md.get("_stream_end"):
                continue
            if msg.content:
                captured.append(msg.content)

    pump = asyncio.create_task(_pump())
    try:
        result = await orchestrator.run(task, session_key=session_key, hooks=hooks)
    finally:
        await asyncio.sleep(0.05)
        stop.set()
        pump.cancel()
        try:
            await pump
        except (asyncio.CancelledError, Exception):
            pass

    direct = (result.content or "").strip() if result else ""
    if direct:
        return direct
    if captured:
        return captured[-1]
    return ""


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO_ROOT), text=True
        ).strip()
    except Exception:
        return "unknown"


def _make_orchestrator():
    """Fresh orchestrator with browser-delegation tools (mirrors test_superbrowser)."""
    from nanobot import Nanobot
    from superbrowser_bridge.orchestrator_tools import register_orchestrator_tools

    orch = Nanobot.from_config(workspace=str(NANOBOT_TREE / "workspace_orchestrator"))
    register_orchestrator_tools(orch)
    return orch


def _harvest_ledgers(cap_dir: Path, dest: Path) -> list[str]:
    """Copy each captured worker's steps.jsonl / step_history.json (keyed by the
    transcript filename = worker task_id) for cross-checking telemetry."""
    worker_ids: list[str] = []
    for tf in sorted(cap_dir.glob("*.json")):
        wid = tf.stem
        worker_ids.append(wid)
        src_mem = Path("/tmp/superbrowser") / wid / "memory" / "steps.jsonl"
        src_evt = Path("/tmp/superbrowser") / wid / "memory" / "events.jsonl"
        src_hist = Path("/tmp/superbrowser") / wid / "step_history.json"
        if src_mem.exists() or src_evt.exists() or src_hist.exists():
            wd = dest / wid
            wd.mkdir(parents=True, exist_ok=True)
            for src in (src_mem, src_evt, src_hist):
                if src.exists():
                    try:
                        shutil.copy2(src, wd / src.name)
                    except OSError:
                        pass
    return worker_ids


async def _run_one(task: Task, seed: int, args, label: str, model_info: dict) -> dict:
    run_dir = Path(args.out) / label / task.id / f"seed{seed}"
    cap_dir = run_dir / "workers"
    cap_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "ledgers").mkdir(parents=True, exist_ok=True)

    # Route this run's worker transcripts to cap_dir (the delegation.py tap reads it).
    os.environ["SUPERBROWSER_EVAL_CAPTURE_DIR"] = str(cap_dir.resolve())

    from superbrowser_bridge.memory import Memory, set_orchestrator_memory

    orch = _make_orchestrator()
    task_uid = uuid.uuid4().hex[:8]
    session_key = f"orchestrator:{task_uid}"
    orch_task_id = f"orch-{task_uid}"
    orch_memory = Memory(orch_task_id, session_key=session_key, role="orchestrator")
    set_orchestrator_memory(orch_memory)
    orch_hook = orch_memory.attach(orch)
    orch_memory.set_goal(task.instruction[:300])

    print(f"\n=== [{label}] {task.id} seed{seed} ===")
    print(f"    {task.instruction[:110]}")

    from superbrowser_bridge.usage import (
        UsageHook,
        pop,
        snapshot,
        track_task,
        write_usage_json,
    )

    start = time.time()
    stop_reason, err, content = "ok", None, ""
    try:
        with track_task(orch_task_id):
            content = await asyncio.wait_for(
                _run_and_capture(
                    orch,
                    task.instruction,
                    session_key,
                    hooks=[orch_hook, UsageHook("orchestrator")],
                ),
                timeout=args.timeout,
            )
    except asyncio.TimeoutError:
        stop_reason = "timeout"
    except Exception as exc:  # keep going; record the failure
        stop_reason, err = "error", f"{type(exc).__name__}: {exc}"
    duration = round(time.time() - start, 2)

    try:
        orch_memory.write_task_summary(success=(stop_reason == "ok"))
    except Exception:
        pass

    worker_ids = _harvest_ledgers(cap_dir, run_dir / "ledgers")

    # Per-task token usage across all roles (orchestrator + worker(s) + vision).
    usage = snapshot(orch_task_id)
    pop(orch_task_id)
    if usage is not None:
        (run_dir / "usage.json").write_text(
            json.dumps(usage.to_dict(), indent=2, default=str)
        )
        write_usage_json(usage)

    # Score: LLM judge (unless --no-judge) + cheap heuristic.
    verdict = {"success": None, "rationale": "judge skipped", "judge_model": None}
    if not args.no_judge:
        verdict = await oracles.judge(task, content)
    heur = oracles.heuristic_success(content)
    api_error = oracles.looks_like_api_error(content)
    if api_error:
        print("    !! API/billing error (not a task result) — the brain model never ran. "
              "Fix provider quota/billing or switch providers; this run holds no real data.")

    (run_dir / "result.txt").write_text(content or "")
    (run_dir / "judge.json").write_text(json.dumps(verdict, indent=2))

    meta = {
        "label": label,
        "task_id": task.id,
        "orch_task_id": orch_task_id,
        "seed": seed,
        "model": model_info,
        "schema_reminder": bool(os.environ.get("SUPERBROWSER_EVAL_SCHEMA_REMINDER")),
        "vision_model": os.environ.get("VISION_MODEL", ""),
        "headless_mode": os.environ.get("SUPERBROWSER_HEADLESS_MODE", "new"),
        "instruction": task.instruction,
        "url": task.url,
        "final_answer": content,
        "judge": verdict,
        "heuristic_success": heur,
        "api_error": api_error,
        "stop_reason": stop_reason,
        "error": err,
        "duration_sec": duration,
        "timestamp": time.time(),
        "n_worker_transcripts": len(worker_ids),
        "worker_ids": worker_ids,
        "usage": (usage.to_dict() if usage is not None else None),
        "harness_git_sha": _git_sha(),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str))

    js = verdict.get("success")
    print(
        f"    -> judge={js} heuristic={heur} workers={len(worker_ids)} "
        f"{duration}s stop={stop_reason}"
    )
    return meta


async def _main_async(args) -> None:
    model_info = read_active_model(args.config)
    label = args.label or slugify(model_info["model"])
    if os.environ.get("SUPERBROWSER_EVAL_SCHEMA_REMINDER") and not args.label:
        label = f"{label}_rescue"

    if args.tasks == "all":
        tasks = active_tasks()
    else:
        wanted = {t.strip() for t in args.tasks.split(",")}
        tasks = [t for t in active_tasks() if t.id in wanted]

    skipped = [t.id for t in active_tasks(include_placeholders=True) if t.is_placeholder]
    if skipped:
        print(f"[warn] skipping placeholder tasks (edit eval/tasks.py): {skipped}")
    if not tasks:
        print("[error] no runnable tasks. Edit eval/tasks.py and add your five tasks.")
        return

    print(
        f"Model: {model_info['model']} (provider={model_info['provider']}) | "
        f"label={label} | tasks={[t.id for t in tasks]} | seeds={args.seeds}"
    )
    print("Reminder: the TS SuperBrowser server must be running (cd .. && npm start).")

    metas = []
    for task in tasks:
        for seed in range(args.seeds):
            metas.append(await _run_one(task, seed, args, label, model_info))

    judged = [m for m in metas if m["judge"].get("success") is not None]
    n_ok = sum(1 for m in judged if m["judge"]["success"])
    print(f"\nDone: {len(metas)} runs. Judge success {n_ok}/{len(judged) or 0} "
          f"(heuristic {sum(m['heuristic_success'] for m in metas)}/{len(metas)}).")
    print(f"Artifacts under {Path(args.out) / label}/")
    print("Next: python -m eval.analyzer")


def main() -> None:
    p = argparse.ArgumentParser(description="SuperBrowser §7.4 batch eval runner")
    p.add_argument("--seeds", type=int, default=3, help="runs per task (default 3)")
    p.add_argument("--tasks", default="all", help="'all' or comma-separated task ids")
    p.add_argument("--label", default=None, help="run label (default = slug of active model)")
    p.add_argument("--timeout", type=int, default=1800, help="per-run wall-clock budget (s)")
    p.add_argument("--out", default=str(REPO_ROOT / "eval" / "runs"), help="output dir")
    p.add_argument("--config", default=None, help="config.json path for reading active model")
    p.add_argument("--no-judge", action="store_true", help="skip the LLM judge (heuristic only)")
    args = p.parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
