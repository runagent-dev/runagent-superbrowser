"""Single-component ablation runner for Table 1 (tab1_ablations.tex).

Runs the full system and each one-mechanism-removed configuration, holding the
LLM / vision model / budgets fixed (paper: "same LLM, same vision model, same
budgets"). Each config is one row of Table 1; success + tokens/iter are filled
in by ``eval/figures/make_ablation_table.py`` afterwards.

Each config is launched as a SUBPROCESS of ``python -m eval.run_eval`` with the
config's toggle env vars merged in, so:
  * module-level env reads in superbrowser_bridge see the value, and
  * configs are fully isolated from one another.
The fixed model is whatever ``~/.nanobot/config.json`` currently selects.

Two kinds of toggle:
  * Python-side (memory eviction, structured ledger, chevron, async prefetch)
    are read by the in-process Worker, so setting them on the run_eval
    subprocess is enough — the standing TS server can stay up in default state.
  * TS-side (click cascade Tier-2/3, motor humanization) are read by the
    long-running browser server, a SEPARATE process Python env vars can't
    reach. Those configs need the server rebuilt + restarted with the toggle
    baked in. By default this script prints the exact manual sequence; pass
    ``--manage-server`` to let it stop/start the server itself.

Usage (from the repo root, venv active, TS server running in DEFAULT state):
    python -m eval.run_ablations --tasks "petfinder_rabbits,bestbuy_qled_240hz_monitor" --seeds 2
    python -m eval.run_ablations --list
    python -m eval.run_ablations --only no_eviction --tasks ... --seeds 2
    python -m eval.run_ablations --manage-server --tasks ... --seeds 2   # auto-manage server
Then:  python -m eval.figures.make_ablation_table
"""
from __future__ import annotations

from . import _bootstrap  # noqa: F401  (sets sys.path, loads .env into os.environ)
from ._bootstrap import REPO_ROOT, read_active_model

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Each row of Table 1: (config name, {toggle env vars}). Defaults preserve the
# full system; a config only ablates when its env vars are explicitly set.
ABLATIONS: list[tuple[str, dict]] = [
    ("full",        {}),
    ("no_eviction", {"ABLATE_MEMORY_EVICTION": "1"}),
    ("no_ledger",   {"ABLATE_STRUCTURED_LEDGER": "1"}),
    ("no_chevron",  {"WORKER_CHEVRON_FOCUS": "0", "BBOX_COMPOUND_ROW_SPLIT": "0"}),
    ("no_prefetch", {"VISION_ASYNC_PREFETCH": "0"}),
    ("tier1_only",  {"SUPERBROWSER_CLICK_TIERS": "tier1"}),   # TS-side
    ("no_humanize", {"MOTOR_HUMANIZATION": "off"}),           # TS-side
]
# Configs whose toggle is read by the TS server (need a server restart).
TS_SIDE = {"tier1_only", "no_humanize"}

PORT = int(os.environ.get("PORT", "3100"))
HEALTH_URL = f"http://localhost:{PORT}/health"


# --- run one config ----------------------------------------------------------
def _run_one_config(name: str, cfg_env: dict, args) -> int:
    env = {**os.environ, **cfg_env}
    cmd = [
        sys.executable, "-m", "eval.run_eval",
        "--tasks", args.tasks,
        "--seeds", str(args.seeds),
        "--label", f"ablation__{name}",
        "--out", args.out,
        "--timeout", str(args.timeout),
    ]
    if args.no_judge:
        cmd.append("--no-judge")
    side = "TS-side" if name in TS_SIDE else "python-side"
    print(f"\n===== ablation config: {name} ({side})  env={cfg_env or '{}'} =====")
    r = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env)
    if r.returncode != 0:
        print(f"[warn] config {name} exited with code {r.returncode}")
    return r.returncode


# --- TS server lifecycle (only used with --manage-server) --------------------
def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _kill_port(port: int = PORT) -> None:
    try:
        out = subprocess.run(["lsof", f"-ti:{port}"], capture_output=True, text=True)
        pids = [int(x) for x in out.stdout.split()]
    except Exception:
        pids = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    for _ in range(30):  # wait up to ~15s for the port to free
        if not _http_ok(HEALTH_URL):
            return
        time.sleep(0.5)


def _restart_server(cfg_env: dict):
    """Stop whatever holds :PORT, launch a fresh `node build/index.js` with
    cfg_env baked in, and wait for /health. Returns the Popen handle."""
    _kill_port()
    env = {**os.environ, **cfg_env}
    print(f"[manage-server] launching node build/index.js  env={cfg_env or 'default'} ...")
    handle = subprocess.Popen(
        ["node", "build/index.js"], cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(120):  # up to ~60s
        if _http_ok(HEALTH_URL):
            print("[manage-server] server healthy.")
            return handle
        if handle.poll() is not None:
            raise RuntimeError("TS server exited before becoming healthy "
                               "(did you run `npm run build`?)")
        time.sleep(0.5)
    raise RuntimeError("TS server did not become healthy within 60s")


def _stop_server(handle) -> None:
    if handle is not None:
        try:
            handle.terminate()
            handle.wait(timeout=10)
        except Exception:
            try:
                handle.kill()
            except Exception:
                pass
    _kill_port()


# --- modes -------------------------------------------------------------------
def _print_ts_instructions(ts: list[tuple[str, dict]], args) -> None:
    print("\n================= TS-side ablations (manual) =================")
    print("The browser server is a separate long-running process, so Python env")
    print("vars don't reach it. For EACH config, restart the server with the")
    print("toggle baked in, then run that one config here:\n")
    extra = " --no-judge" if args.no_judge else ""
    for name, env in ts:
        envstr = " ".join(f"{k}={v}" for k, v in env.items())
        print(f"  # --- {name} ---")
        print(f"  # 1) in the server shell (repo root):")
        print(f"  npm run build && {envstr} node build/index.js")
        print(f"  # 2) in this shell:")
        print(f"  python -m eval.run_ablations --only {name} "
              f"--tasks {args.tasks!r} --seeds {args.seeds} --out {args.out}{extra}")
        print(f"  # 3) afterwards restore the default server:  npm start\n")
    print("Or re-run the whole sweep with --manage-server to automate the restarts.")


def _run_manual(selected: list[tuple[str, dict]], args) -> None:
    py = [(n, e) for (n, e) in selected if n not in TS_SIDE]
    ts = [(n, e) for (n, e) in selected if n in TS_SIDE]
    only_ts = args.only is not None and all(n in TS_SIDE for (n, _e) in selected)

    if py:
        print("Running python-side configs against the standing server.")
        print("  NB: the TS server must be in DEFAULT state (plain `npm start`) for these.")
        for name, env in py:
            _run_one_config(name, env, args)

    if ts and only_ts:
        print("Running TS-side config(s) against the CURRENT server — assuming you")
        print("restarted it with the matching ablation env (see --list for the env).")
        for name, env in ts:
            _run_one_config(name, env, args)
    elif ts:
        _print_ts_instructions(ts, args)


def _run_managed(selected: list[tuple[str, dict]], args) -> None:
    py = [(n, e) for (n, e) in selected if n not in TS_SIDE]
    ts = [(n, e) for (n, e) in selected if n in TS_SIDE]
    server = None
    try:
        if py:
            server = _restart_server({})  # default state for python-side configs
            for name, env in py:
                _run_one_config(name, env, args)
            _stop_server(server)
            server = None
        for name, env in ts:
            server = _restart_server(env)
            _run_one_config(name, env, args)
            _stop_server(server)
            server = None
    finally:
        if server is not None:
            _stop_server(server)
    print("\n[manage-server] done. Start your default server again (`npm start`) for normal use.")


def main():
    ap = argparse.ArgumentParser(description="Run Table 1 single-component ablations")
    ap.add_argument("--tasks", default="all", help="'all' or comma-separated task ids")
    ap.add_argument("--seeds", type=int, default=2, help="runs per task per config (default 2)")
    ap.add_argument("--out", default=str(REPO_ROOT / "eval" / "runs_ablation"))
    ap.add_argument("--only", default=None, help="run only this config (comma-separated ok)")
    ap.add_argument("--list", action="store_true", help="list configs + their toggles and exit")
    ap.add_argument("--manage-server", action="store_true",
                    help="let this script stop/start the TS server per config (exclusive use)")
    ap.add_argument("--no-judge", action="store_true", help="heuristic-only scoring (no judge)")
    ap.add_argument("--timeout", type=int, default=1800, help="per-run wall-clock budget (s)")
    args = ap.parse_args()

    if args.list:
        for name, env in ABLATIONS:
            tag = "  [TS — needs server restart]" if name in TS_SIDE else ""
            print(f"  {name:14s} {env or '{}'}{tag}")
        return

    selected = ABLATIONS
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        selected = [(n, e) for (n, e) in ABLATIONS if n in want]
        if not selected:
            print(f"[error] no configs match --only {args.only!r}; see --list")
            return

    model = read_active_model()
    print(f"Ablation sweep — fixed model (from ~/.nanobot/config.json): "
          f"{model.get('model')} [{model.get('provider')}]")
    print(f"  configs={[n for n, _ in selected]}  tasks={args.tasks}  seeds={args.seeds}")
    print(f"  out={args.out}  judge={'off' if args.no_judge else 'on'}  "
          f"manage_server={args.manage_server}")

    if args.manage_server:
        _run_managed(selected, args)
    else:
        _run_manual(selected, args)

    print("\nNext: python -m eval.figures.make_ablation_table   "
          "(fills paper/tables/tab1_ablations.tex)")


if __name__ == "__main__":
    main()
