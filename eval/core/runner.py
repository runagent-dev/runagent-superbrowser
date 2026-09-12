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
import atexit
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
            # which browser server this run used: the managed one gets a fresh
            # free port each sweep, so the live viewer cannot guess it
            "server_url": self.server_url,
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


# Every run is started in its own process group so a timeout can kill the whole
# tree (Chrome included). The side effect is that Ctrl-C on the sweep never
# reaches the running child, so the parent would die and the run keep going as an
# orphan -- observed live: three orphaned runs fighting over one Tier-3 Chrome
# profile, producing TargetClosedError and burning credit on work no parent would
# ever harvest. These hooks make the parent reap its child on any exit path.
_LIVE_CHILDREN: set[subprocess.Popen] = set()


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=20)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
    finally:
        _LIVE_CHILDREN.discard(proc)


def reap_children(note: str = "") -> int:
    """Kill any run subprocess this sweep still owns. Safe to call twice."""
    n = 0
    for proc in list(_LIVE_CHILDREN):
        if proc.poll() is None:
            _kill_tree(proc)
            n += 1
        else:
            _LIVE_CHILDREN.discard(proc)
    if n:
        print(f"\n  [stopped {n} in-flight run(s){' — ' + note if note else ''}; "
              f"their work is not harvested, so --resume will redo them]", flush=True)
    return n


def _install_signal_handlers() -> None:
    """Reap the child, then die of the same signal (so the exit code is honest)."""
    def handler(signum, _frame):
        reap_children(f"signal {signal.Signals(signum).name}")
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):   # not the main thread, or unsupported
            pass
    atexit.register(reap_children, "process exit")


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


# Lines worth surfacing when following a run in the terminal. The full log is
# always written to the run's run.log either way; this only decides what is
# echoed live, so a sweep does not look frozen for minutes at a time.
_FOLLOW_PATTERNS = ("Tool call:", "[vision-agent]", "ERROR", "WARNING", "Traceback",
                    "[run_one]", "browser_", "LLM returned error", "Starting agent loop iteration")


def _echo_worthy(line: str) -> str | None:
    """Condense a nanobot log line to something readable, or drop it."""
    if not any(p in line for p in _FOLLOW_PATTERNS):
        return None
    if "Starting agent loop iteration" in line:
        n = line.rsplit("iteration", 1)[-1].split("for")[0].strip()
        return f"      iteration {n}"
    if "Tool call:" in line:
        call = line.split("Tool call:", 1)[1].strip()
        return f"      -> {call[:110]}"
    if "[vision-agent]" in line:
        seg = line.split("[vision-agent]", 1)[1].strip()
        return f"      vision {seg[:100]}"
    if "ERROR" in line or "Traceback" in line or "LLM returned error" in line:
        return f"      ! {line.strip()[:140]}"
    return None


def launch_run(spec: RunSpec, *, log_to: Path, follow: bool = False) -> tuple[int | None, str]:
    """Run ``eval.core.run_one`` for ``spec``; returns (returncode|None, stop_note).

    ``follow`` echoes a condensed live view to the terminal as well as writing
    the full log; without it a 40-run sweep prints one line per run and looks
    frozen for minutes at a time.
    """
    stale = archive_failed_attempt(spec)
    if stale is not None:
        print(f"    [archived unfinished attempt -> {stale.relative_to(stale.parents[2])}]", flush=True)
    spec.run_dir.mkdir(parents=True, exist_ok=True)
    (spec.run_dir / "spec.json").write_text(json.dumps(spec.to_json(), indent=2, ensure_ascii=False))
    env = {**os.environ, **spec.env()}
    cmd = [sys.executable, "-m", "eval.core.run_one", "--spec", str(spec.run_dir / "spec.json")]
    hard_timeout = spec.protocol.wall_clock_s + 180
    if not follow:
        with log_to.open("ab") as log:
            proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,  # noqa: S603
                                    start_new_session=True)
            _LIVE_CHILDREN.add(proc)
            try:
                rc = proc.wait(timeout=hard_timeout)
                return rc, "ok" if rc == 0 else f"exit {rc}"
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                return None, "killed: hard wall-clock exceeded"
            finally:
                _LIVE_CHILDREN.discard(proc)

    deadline = time.time() + hard_timeout
    with log_to.open("ab") as log:
        proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE,  # noqa: S603
                                stderr=subprocess.STDOUT, start_new_session=True)
        _LIVE_CHILDREN.add(proc)
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                log.write(raw)
                if time.time() > deadline:
                    _kill_tree(proc)
                    return None, "killed: hard wall-clock exceeded"
                shown = _echo_worthy(raw.decode("utf-8", "replace").rstrip())
                if shown:
                    print(shown, flush=True)
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
        rc = proc.wait()
        _LIVE_CHILDREN.discard(proc)
        return rc, "ok" if rc == 0 else f"exit {rc}"


def _ensure_meta_after_kill(spec: RunSpec, note: str, *, stop_reason: str = "timeout") -> None:
    """Record WHY no meta.json exists.

    A subprocess that died without writing one produced no trajectory at all, so
    grading it as a task would invent a failure: the answer judge is handed an
    empty answer and dutifully returns success=False, indistinguishable from an
    agent that genuinely gave up. ``harness_error`` keeps such runs out of the
    denominator (see harvest.EXCLUDABLE).
    """
    meta_path = spec.run_dir / "meta.json"
    if meta_path.exists():
        return
    meta_path.write_text(json.dumps({
        "run_id": spec.run_id, "experiment": spec.experiment, "arm": spec.arm.name,
        "task_id": spec.task.task_id, "seed": spec.seed, "topology": spec.topology,
        "stop_reason": stop_reason, "error": note, "final_answer": "", "started_at": None,
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
            log_dir: Path, server_port: int | None = None,
            viewer_port: int | None = None, follow: bool = False,
            viewer_host: str = "127.0.0.1") -> list[RunResultSummary]:
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
    _install_signal_handlers()
    viewer = None
    if viewer_port:
        # Read-only, and deliberately not the TS server's /session/:id/view: that
        # route only knows Tier-1 sessions it holds itself, so Tier-3 runs are
        # invisible through it, and its port changes every sweep. Both tiers write
        # frames into the run dir, so serving those follows the sweep either way.
        try:
            from eval.viewer import serve as _serve

            tok = None
            if viewer_host not in ("127.0.0.1", "localhost"):
                import secrets

                tok = secrets.token_urlsafe(12)
            viewer = _serve(viewer_port, Path(specs[0].run_dir).parents[3] if specs else DEFAULT_RUNS_ROOT,
                            None, host=viewer_host, token=tok)
            url = f"http://{viewer_host}:{viewer_port}" + (f"/?t={tok}" if tok else "")
            print(f"\n  live viewer: {url}   (Tier-1 and Tier-3; follows the active run)")
            if viewer_host == "127.0.0.1":
                print("  loopback only. From a laptop, run this ON THE LAPTOP (not on the server):")
                print(f"      ssh -L {viewer_port}:127.0.0.1:{viewer_port} <user>@<host>")
                print(f"  or bind it directly with:  --viewer-host 0.0.0.0   (adds a token to the URL)")
            print()
        except OSError as exc:
            print(f"  [viewer not started on :{viewer_port} — {exc}; pass --viewer-port N or --no-viewer]")
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
            rc, note = launch_run(s, log_to=s.run_dir / "run.log", follow=follow)
            if rc is None:
                _ensure_meta_after_kill(s, note)
            elif rc != 0 and not (s.run_dir / "meta.json").exists():
                # the subprocess crashed before producing anything to grade
                _ensure_meta_after_kill(s, f"run_one exited {rc} without writing meta.json; see run.log",
                                        stop_reason="harness_error")
            try:
                rec = asyncio.run(finish_run(s, judges=judges, no_judge=no_judge))
            except Exception as exc:  # noqa: BLE001 - one bad run must not end the sweep
                print(f"    !! harvest/judge failed: {type(exc).__name__}: {str(exc)[:200]}")
                _ensure_meta_after_kill(s, f"{type(exc).__name__}: {exc}"[:300], stop_reason="harness_error")
                try:
                    rec = asyncio.run(finish_run(s, judges=[], no_judge=True))
                except Exception as exc2:  # noqa: BLE001
                    print(f"    !! could not record this run at all: {type(exc2).__name__}")
                    out.append(RunResultSummary(s.run_id, "failed", None, round(time.time() - t0, 1), "unrecorded"))
                    continue
            wall = round(time.time() - t0, 1)
            flag = ""
            if rec.outcome.get("failure_reason") in ("api_error", "harness_error"):
                consecutive_api_errors += 1
                flag = (f"  [{rec.outcome.get('failure_reason').upper()} {consecutive_api_errors}/{max_api_errors} — excluded, not a task failure]")
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
        reap_children("sweep finished")
        servers.close()
        if viewer is not None:
            viewer.shutdown()
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
    ap.add_argument("--viewer-port", type=int, default=8700,
                    help="live viewer port; it follows whichever run is writing frames (any browser tier)")
    ap.add_argument("--no-viewer", action="store_true", help="do not start the live viewer")
    ap.add_argument("--viewer-host", default="127.0.0.1",
                    help="viewer bind address (loopback by default; it shows the agent's screen)")
    ap.add_argument("--follow", "-f", action="store_true",
                    help="echo a condensed live log (iterations, tool calls, vision, errors) to the "
                         "terminal; the full log always goes to each run's run.log regardless")


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
                      log_dir=Path(args.out) / experiment / "_logs", server_port=args.server_port,
                      viewer_port=None if args.no_viewer else args.viewer_port, follow=args.follow,
                      viewer_host=args.viewer_host)
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
