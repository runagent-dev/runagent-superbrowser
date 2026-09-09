"""Schedule and execute runs for an experiment.

Loop order is ``seed -> task -> arm`` so the arms of a paired comparison run
on the same task back-to-back (live sites drift; the pairing has to be close
in time). Within a task, arms that share the same TypeScript-side env are
grouped so a server restart happens at most once per group.

Every run is a subprocess (``eval.core.run_one``) with the arm's env merged
over the protocol pins and per-run paths; the parent enforces the wall-clock
budget, then judges, harvests and records. ``--dry-run`` prints the schedule
and per-run env without launching anything; ``--resume`` skips runs that
already have a ``run_record.json``.

Usage (from the repo root, venv active, TS server running):
    python -m eval.core.runner --experiment e2_memory_policy \
        --arms ledger,fifo,summary,full_history --tasks ablation24 --seeds 1 --model <id>
Experiments wrap this with their own ``run.py`` that fixes arms/subset.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from eval._bootstrap import REPO_ROOT, read_active_model
from eval.core import arms as arms_mod
from eval.core.arms import Arm, ts_signature
from eval.core.harvest import build_record
from eval.core.judges import JUDGE_NAMES, judge_run_async
from eval.core.protocol import Protocol, protocol_from_args
from eval.core.records import RunRecord, append_result, make_run_id, results_path, run_dir_for
from eval.core.server import ServerManager
from eval.core.tasks import Task, resolve_tasks

DEFAULT_RUNS_ROOT = REPO_ROOT / "eval" / "runs"


@dataclass
class RunSpec:
    experiment: str
    arm: Arm
    task: Task
    seed: int
    run_dir: Path
    protocol: Protocol
    model: str | None
    topology: str

    @property
    def run_id(self) -> str:
        return make_run_id(self.experiment, self.arm.name, self.task.task_id, self.seed)

    server_url: str | None = None

    def env(self) -> dict[str, str]:
        env = dict(self.protocol.env())
        env.update(self.arm.env)
        if self.server_url:
            env["SUPERBROWSER_URL"] = self.server_url
        env.update({
            "SUPERBROWSER_EVAL_CAPTURE_DIR": str(self.run_dir / "workers"),
            "SUPERBROWSER_SCREENSHOT_DIR": str(self.run_dir / "screenshots"),
            "SUPERBROWSER_EVAL_RUN_ID": self.run_id,
            "SUPERBROWSER_EVAL_SEED": str(self.seed),
            "SUPERBROWSER_EVAL_TASK_ID": self.task.task_id,
        })
        return env

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "experiment": self.experiment, "seed": self.seed,
            "arm": {"name": self.arm.name, "env": self.arm.env, "side": self.arm.side,
                    "family": self.arm.family, "description": self.arm.description},
            "task": self.task.to_dict(), "benchmark": self.task.benchmark,
            "run_dir": str(self.run_dir), "model": self.model, "topology": self.topology,
            "protocol": {"hash": self.protocol.hash(), **self.protocol.to_dict()},
            "nanobot_overrides": self.protocol.nanobot_config_overrides(),
            "internal_timeout_s": max(60, self.protocol.wall_clock_s - 90),
        }


@dataclass
class RunResultSummary:
    run_id: str
    status: str                 # ok | skipped | failed
    success: bool | None = None
    wall_s: float | None = None
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def build_schedule(*, experiment: str, arms: Sequence[Arm], tasks: Sequence[Task], seeds: Sequence[int],
                   protocol: Protocol, model: str | None, runs_root: Path) -> list[RunSpec]:
    specs: list[RunSpec] = []
    for seed in seeds:
        for task in tasks:
            # python-side arms first (share the default server), then TS groups
            ordered = sorted(arms, key=lambda a: (a.side != "python", ts_signature(a)))  # stable
            for arm in ordered:
                topology = arm.env.get("SUPERBROWSER_TOPOLOGY", "orchestrator")
                specs.append(RunSpec(experiment, arm, task, seed,
                                     run_dir_for(runs_root, experiment, arm.name, task.task_id, seed),
                                     protocol, model, topology))
    return specs


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=20)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass


def archive_failed_attempt(spec: RunSpec) -> Path | None:
    """Move a previous, unfinished attempt out of the way before re-running.

    A run killed by Ctrl-C or a timeout leaves screenshots, traces and ledgers
    behind but no ``run_record.json``. Re-running into that directory only
    overwrites the steps the retry reaches, so screenshots from the longer dead
    attempt survive and WebJudge would score a trajectory that never happened.
    The attempt is archived rather than deleted (these runs cost real money) and
    lands outside the ``<arm>/<task>/seed*`` glob the analysis walks.
    """
    d = spec.run_dir
    if not d.exists() or (d / "run_record.json").exists():
        return None
    if not any(c.name != "spec.json" for c in d.iterdir()):
        return None                      # nothing but the spec we wrote last time
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(d.stat().st_mtime))
    dest = d.parents[2] / "_failed_attempts" / f"{spec.arm.name}__{spec.task.task_id}__seed{spec.seed}__{stamp}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(d), str(dest))
    return dest


def launch_run(spec: RunSpec, *, log_to: Path) -> tuple[int | None, str]:
    """Run ``eval.core.run_one`` for ``spec``; returns (returncode|None, stop_note)."""
    stale = archive_failed_attempt(spec)
    if stale is not None:
        print(f"    [archived unfinished attempt -> {stale.relative_to(stale.parents[2])}]", flush=True)
    spec.run_dir.mkdir(parents=True, exist_ok=True)
    (spec.run_dir / "spec.json").write_text(json.dumps(spec.to_json(), indent=2, ensure_ascii=False))
    env = {**os.environ, **spec.env()}
    cmd = [sys.executable, "-m", "eval.core.run_one", "--spec", str(spec.run_dir / "spec.json")]
    hard_timeout = spec.protocol.wall_clock_s + 180
    with log_to.open("ab") as log:
        proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,  # noqa: S603
                                start_new_session=True)
        try:
            rc = proc.wait(timeout=hard_timeout)
            return rc, "ok" if rc == 0 else f"exit {rc}"
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return None, "killed: hard wall-clock exceeded"


def _ensure_meta_after_kill(spec: RunSpec, note: str) -> None:
    meta_path = spec.run_dir / "meta.json"
    if meta_path.exists():
        return
    meta_path.write_text(json.dumps({
        "run_id": spec.run_id, "experiment": spec.experiment, "arm": spec.arm.name,
        "task_id": spec.task.task_id, "seed": spec.seed, "topology": spec.topology,
        "stop_reason": "timeout", "error": note, "final_answer": "", "started_at": None,
        "ended_at": time.time(), "duration_s": None, "role_task_ids": [], "n_screenshots": 0,
    }, indent=2))


async def finish_run(spec: RunSpec, *, judges: Sequence[str], no_judge: bool) -> RunRecord:
    if not no_judge and judges:
        await judge_run_async(spec.run_dir, spec.task, which=judges)
    rec = build_record(spec.run_dir)
    rec.write(spec.run_dir)
    append_result(results_path(spec.run_dir.parents[3], spec.experiment), rec)
    return rec


def execute(specs: list[RunSpec], *, manage_server: bool, assumed_server_env: dict[str, str] | None,
            resume: bool, no_judge: bool, judges: Sequence[str], dry_run: bool,
            log_dir: Path, server_port: int | None = None) -> list[RunResultSummary]:
    out: list[RunResultSummary] = []
    if dry_run:
        for i, s in enumerate(specs, 1):
            flag = " (skip: done)" if resume and (s.run_dir / "run_record.json").exists() else ""
            print(f"{i:4d}. seed{s.seed}  {s.task.task_id[:12]}  {s.arm.name:18s} side={s.arm.side:6s}"
                  f" topo={s.topology}{flag}")
            extra = {k: v for k, v in s.arm.env.items()}
            if extra:
                print(f"       arm env: {extra}")
        print(f"\n{len(specs)} runs; protocol hash {specs[0].protocol.hash() if specs else '-'}; "
              f"pins: {specs[0].protocol.env() if specs else {}}")
        return out
    log_dir.mkdir(parents=True, exist_ok=True)
    consecutive_api_errors, max_api_errors = 0, 3
    servers = ServerManager(manage=manage_server, log_dir=log_dir, assumed_env=assumed_server_env, port=server_port)
    try:
        for i, s in enumerate(specs, 1):
            if resume and (s.run_dir / "run_record.json").exists():
                rec = RunRecord.read(s.run_dir)
                out.append(RunResultSummary(s.run_id, "skipped", rec.success, rec.timing.get("wall_s"), "resume"))
                continue
            s.server_url = servers.ensure(s.arm.ts_env)
            print(f"\n=== [{i}/{len(specs)}] {s.run_id} ===  server={s.server_url}")
            t0 = time.time()
            rc, note = launch_run(s, log_to=s.run_dir / "run.log")
            if rc is None:
                _ensure_meta_after_kill(s, note)
            rec = asyncio.run(finish_run(s, judges=judges, no_judge=no_judge))
            wall = round(time.time() - t0, 1)
            flag = ""
            if rec.outcome.get("failure_reason") == "api_error":
                consecutive_api_errors += 1
                flag = f"  [PROVIDER ERROR {consecutive_api_errors}/{max_api_errors} — excluded, not a task failure]"
            else:
                consecutive_api_errors = 0
            print(f"    -> success={rec.success} ({rec.outcome.get('decided_by')}) stop={rec.outcome.get('stop_reason')}"
                  f" iters={rec.counts.get('worker_iterations')} tools={rec.counts.get('tool_calls_executed')}"
                  f" vision={rec.counts.get('vision_calls')} {wall}s{flag}")
            out.append(RunResultSummary(s.run_id, "ok" if rc == 0 else "failed", rec.success, wall, note))
            if consecutive_api_errors >= max_api_errors:
                # Every further run would finish in a second and be recorded as a
                # failure, so the sweep would spend its remaining budget writing
                # a success rate made of billing errors. Stop and say so.
                print(f"\n!! ABORTING after {consecutive_api_errors} consecutive provider errors "
                      f"({len(out)}/{len(specs)} runs done).\n"
                      f"   The model provider is refusing requests (quota, billing or auth) — this is not a\n"
                      f"   property of the agent, and the runs above are marked api_error and excluded.\n"
                      f"   Fix the key, then re-run the SAME command with --resume to continue where it stopped.")
                break
    finally:
        servers.close()
    return out


def summarize(results: list[RunResultSummary]) -> str:
    n = len(results)
    ok = sum(1 for r in results if r.success)
    judged = sum(1 for r in results if r.success is not None)
    failed = sum(1 for r in results if r.status == "failed")
    return f"{n} runs, judged {judged}, success {ok}/{judged or 0}, harness failures {failed}"


# ------------------------------------------------------------------------ CLI
def add_common_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--benchmark", default="online_mind2web_hard")
    ap.add_argument("--tasks", default="all", help="'all', a subset name from benchmarks/subsets.json, or ids")
    ap.add_argument("--seeds", type=int, default=1, help="repetitions per (task, arm)")
    ap.add_argument("--seed-offset", type=int, default=0)
    ap.add_argument("--model", default=None, help="brain model id override (recorded per run)")
    ap.add_argument("--out", default=str(DEFAULT_RUNS_ROOT), help="runs root (default eval/runs)")
    ap.add_argument("--max-iterations", type=int, default=None)
    ap.add_argument("--wall-clock", type=int, default=None, help="per-run budget in seconds")
    ap.add_argument("--context-window-tokens", type=int, default=None)
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--judges", default=",".join(JUDGE_NAMES))
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--manage-server", action="store_true",
                    help="let the harness run its OWN TS server (free port unless --server-port) and restart it per arm env")
    ap.add_argument("--server-port", type=int, default=None, help="port for the harness-managed server")
    ap.add_argument("--assume-server-env", default="", help="k=v,k=v the running server was started with")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--dry-run", action="store_true")


def run_from_args(args: argparse.Namespace, *, experiment: str, arms: Sequence[Arm]) -> list[RunResultSummary]:
    protocol = protocol_from_args(
        benchmark=args.benchmark, max_iterations=args.max_iterations, wall_clock_s=args.wall_clock,
        context_window_tokens=args.context_window_tokens, max_tokens=args.max_tokens)
    tasks = resolve_tasks(args.tasks, benchmark=args.benchmark)
    seeds = list(range(args.seed_offset, args.seed_offset + args.seeds))
    model = args.model or read_active_model().get("model")
    specs = build_schedule(experiment=experiment, arms=arms, tasks=tasks, seeds=seeds, protocol=protocol,
                           model=model, runs_root=Path(args.out))
    assumed = dict(kv.split("=", 1) for kv in args.assume_server_env.split(",") if "=" in kv)
    judges = [j.strip() for j in args.judges.split(",") if j.strip()]
    if protocol.host_snip_active():
        budget = protocol.context_window_tokens - protocol.max_tokens - 1024
        print(f"[info] nanobot's own history snip engages only above ~{budget} prompt tokens "
              f"(window {protocol.context_window_tokens} - max_tokens {protocol.max_tokens} - 1024); "
              f"a run that crosses it is flagged by the context_size events.")
    print(f"experiment={experiment} arms={[a.name for a in arms]} tasks={len(tasks)} seeds={seeds} "
          f"model={model} out={args.out} protocol={protocol.hash()}")
    results = execute(specs, manage_server=args.manage_server, assumed_server_env=assumed, resume=args.resume,
                      no_judge=args.no_judge, judges=judges, dry_run=args.dry_run,
                      log_dir=Path(args.out) / experiment / "_logs", server_port=args.server_port)
    if not args.dry_run:
        print("\n" + summarize(results))
        print(f"results: {results_path(Path(args.out), experiment)}")
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run an experiment schedule (generic entry point)")
    ap.add_argument("--experiment", required=True)
    ap.add_argument("--arms", required=True, help="comma-separated arm names (see eval.core.arms)")
    add_common_args(ap)
    args = ap.parse_args(argv)
    run_from_args(args, experiment=args.experiment, arms=arms_mod.resolve(args.arms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
