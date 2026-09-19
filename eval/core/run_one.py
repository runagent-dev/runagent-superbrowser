"""Execute ONE (task, arm, seed) evaluation run — always in a fresh process.

Why a process per run: the bridge freezes several settings at import time
(``SUPERBROWSER_URL``, ``SUPERBROWSER_SCREENSHOT_DIR``, workspace paths) and
the arms are env toggles, so the runner launches
``python -m eval.core.run_one --spec <run_dir>/spec.json`` with the arm's env
already in the environment. This module then:

1. builds a per-run nanobot config (the user's ``~/.nanobot/config.json``
   with the protocol's context/iteration pins and the ``--model`` override
   patched in), points nanobot at it via ``set_config_path`` so BOTH the
   orchestrator and every delegated worker use it, and stores a
   secret-redacted copy in the run dir for provenance;
2. runs the task through the requested topology
   (``orchestrator`` = production Orchestrator -> delegate_browser_task ->
   Worker; ``flat`` = one agent with the browser tools registered directly,
   same memory hook / worker hook / budgets);
3. writes ``result.txt``, ``meta.json``, ``usage.json``, ``workers/*.json``
   (transcripts), harvests every memory directory the run produced into
   ``ledgers/<id>/`` and indexes ``screenshots/``.

Judging and RunRecord assembly happen in the parent (``runner.py`` /
``harvest.py``) so a crash here still leaves auditable artefacts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from eval import _bootstrap  # noqa: F401  (sys.path + .env)
from eval._bootstrap import DEFAULT_CONFIG_PATH, NANOBOT_TREE

from superbrowser_bridge import audit as _audit
from superbrowser_bridge.audit import LEDGER_FILES, SECRET_KEYS, TASK_LEVEL_FILES  # noqa: F401  (re-exported)

# Module global on purpose: tests monkeypatch ``run_one.MEMORY_BASE`` and the
# wrappers below read it at call time.
MEMORY_BASE = _audit.MEMORY_BASE


# --------------------------------------------------------------------- config
def _redact(obj: Any) -> Any:
    return _audit.redact(obj)


def prepare_nanobot_config(run_dir: Path, *, overrides: dict[str, Any], model: str | None) -> tuple[Path, dict[str, Any]]:
    """Patch agents.defaults with the protocol pins (+ model) into a private
    temp config, return (path, redacted_effective_config). The writer lives in
    ``superbrowser_bridge.audit`` so the SDK's audit trail shares it."""
    src = Path(os.environ.get("NANOBOT_CONFIG", str(DEFAULT_CONFIG_PATH)))
    tmp, effective = _audit.redact_config(run_dir, overrides=dict(overrides or {}), model=model, config_path=src)
    assert tmp is not None
    return tmp, effective


# ------------------------------------------------------------------ topologies
async def _run_orchestrator(spec: dict[str, Any], framed_task: str, timeout: float | None) -> dict[str, Any]:
    from nanobot import Nanobot
    from runagent_superbrowser._capture import run_and_capture
    from runagent_superbrowser.modes import apply_mode
    from superbrowser_bridge.audit import ROSTER_NAME, AuditHook
    from superbrowser_bridge.memory import Memory, set_orchestrator_memory
    from superbrowser_bridge.orchestrator_tools import register_orchestrator_tools
    from superbrowser_bridge.usage import UsageHook, pop, snapshot, track_task
    from superbrowser_bridge.workspaces import provision, workspace_for

    provision()
    bot = Nanobot.from_config(workspace=str(workspace_for("orchestrator")))
    register_orchestrator_tools(bot)
    # "browser" is the protocol default (the paper's sweeps pin it so every task
    # drives the engine). SUPERBROWSER_EVAL_MODE=auto leaves the full topology in
    # place — the orchestrator may answer via the search worker instead — which is
    # what the cross-harness benchmark uses, since the other harnesses also have
    # fetch/search tools. The value is recorded in the run's env snapshot.
    mode = os.environ.get("SUPERBROWSER_EVAL_MODE", "browser").strip() or "browser"
    directive = apply_mode(bot, mode)
    strip_non_browser_tools(bot)

    short = uuid.uuid4().hex[:8]
    task_id, session_key = f"orch-{short}", f"orchestrator:{short}"
    memory = Memory(task_id, session_key=session_key, role="orchestrator")
    set_orchestrator_memory(memory)
    hook = memory.attach(bot)
    memory.set_goal(spec["task"]["instruction"][:300])
    cap = os.environ.get("SUPERBROWSER_EVAL_CAPTURE_DIR")
    audit_hook = AuditHook("orchestrator", memory=memory, task_id=task_id,
                           roster_path=Path(cap) / ROSTER_NAME if cap else None)

    prompt = f"{directive}\n\n{framed_task}" if directive else framed_task
    out: dict[str, Any] = {"orch_task_id": task_id, "stop_reason": "ok", "error": None, "final_answer": "",
                           "raw_content": "", "role_task_ids": [task_id]}
    start = time.time()
    try:
        with track_task(task_id):
            text, raw = await run_and_capture(bot, prompt, session_key,
                                              hooks=[hook, UsageHook("orchestrator"), audit_hook], timeout=timeout)
        out["final_answer"], out["raw_content"] = text, raw
    except asyncio.TimeoutError:
        out["stop_reason"] = "timeout"
    except Exception as exc:  # noqa: BLE001 - record and keep the artefacts
        out["stop_reason"], out["error"] = "error", f"{type(exc).__name__}: {exc}"
    out["duration_s"] = round(time.time() - start, 2)
    try:
        memory.write_task_summary(success=(out["stop_reason"] == "ok"))
    except Exception:
        pass
    usage = snapshot(task_id)
    pop(task_id)
    out["usage"] = usage.to_dict() if usage is not None else None
    return out


async def _run_flat(spec: dict[str, Any], framed_task: str, timeout: float | None) -> dict[str, Any]:
    """Single agent: the browser Worker driven directly (no orchestrator).

    Mirrors the worker construction in
    ``superbrowser_bridge/orchestrator_tools/delegation.py`` (from_config on
    the browser workspace, default nanobot tools removed, worker-role Memory,
    BrowserSessionState bound to it, pinned domain, session tools, worker
    hook, memory hook, iteration cap) so the ONLY difference to the
    orchestrator topology is the missing strategic layer.
    """
    from nanobot import Nanobot
    from runagent_superbrowser._capture import run_and_capture
    from superbrowser_bridge.memory import Memory, set_orchestrator_memory
    from superbrowser_bridge.orchestrator_tools.constants import BROWSER_WORKSPACE
    from superbrowser_bridge.session_tools import BrowserSessionState, register_session_tools
    from superbrowser_bridge.usage import UsageHook, pop, snapshot, track_task
    from superbrowser_bridge.worker_hook import BrowserWorkerHook
    from superbrowser_bridge.workspaces import provision
    from urllib.parse import urlparse

    provision()
    task = spec["task"]
    short = uuid.uuid4().hex[:8]
    task_id, session_key = short, f"worker:{short}"
    bot = Nanobot.from_config(workspace=BROWSER_WORKSPACE)
    # Same list as the orchestrator and the delegated worker, so all three
    # topologies face an identical toolset (this list was shorter than
    # delegation.py's, which would have been a confound in the E8 comparison).
    for name in _NON_BROWSER_TOOLS:
        try:
            bot._loop.tools.unregister(name)
        except Exception:
            pass
    env_iter = os.environ.get("SUPERBROWSER_WORKER_MAX_ITER")
    cfg_iter = getattr(getattr(bot, "_loop", None), "max_iterations", None)
    max_iterations = int(env_iter) if env_iter else int(cfg_iter or 100)

    memory = Memory(task_id, session_key=session_key, role="worker")
    set_orchestrator_memory(memory)  # usage task-id fallback; no orchestrator exists
    memory.set_goal(task["instruction"][:200])
    memory.begin_subgoal(f"flat: {task['instruction'][:80]}", message_floor=0)
    state = BrowserSessionState(memory=memory)
    state.set_task_context(task_instruction=task["instruction"], target_url=task.get("start_url") or "",
                           is_research=False)
    host = urlparse(task.get("start_url") or "").netloc.lower()
    if host:
        state.pinned_domain = host[4:] if host.startswith("www.") else host
    register_session_tools(bot, state)
    worker_hook = BrowserWorkerHook(state, max_iterations=max_iterations)
    memory_hook = memory.attach(bot)
    bot._loop.max_iterations = max_iterations

    out: dict[str, Any] = {"orch_task_id": task_id, "stop_reason": "ok", "error": None, "final_answer": "",
                           "raw_content": "", "role_task_ids": [task_id]}
    start = time.time()
    result_messages: list[Any] = []
    try:
        with track_task(task_id):
            # run_and_capture returns text; we also need the raw messages for the
            # transcript dump, so capture them via the hook context.
            from nanobot.agent.hook import AgentHook

            class _Tap(AgentHook):
                async def after_run(self, context):  # type: ignore[override]
                    result_messages.extend(list(getattr(context, "messages", []) or []))

            text, raw = await run_and_capture(bot, framed_task, session_key,
                                              hooks=[memory_hook, worker_hook, UsageHook("worker"), _Tap()],
                                              timeout=timeout)
        out["final_answer"], out["raw_content"] = text, raw
    except asyncio.TimeoutError:
        out["stop_reason"] = "timeout"
    except Exception as exc:  # noqa: BLE001
        out["stop_reason"], out["error"] = "error", f"{type(exc).__name__}: {exc}"
    out["duration_s"] = round(time.time() - start, 2)
    # transcript dump in the same shape as the delegation tap
    cap_dir = os.environ.get("SUPERBROWSER_EVAL_CAPTURE_DIR")
    if cap_dir:
        try:
            Path(cap_dir).mkdir(parents=True, exist_ok=True)
            payload = {
                "task_id": task_id, "instructions": task["instruction"], "url": task.get("start_url"),
                "content": out["final_answer"], "messages": result_messages,
                "tool_schemas": bot._loop.tools.get_definitions(),
                "meta": {"topology": "flat", "step_count": len(getattr(state, "step_history", []) or []),
                         "vision_calls": getattr(state, "vision_calls", None),
                         "text_calls": getattr(state, "text_calls", None),
                         "sessions_opened": getattr(state, "sessions_opened", None),
                         "regression_count": getattr(state, "regression_count", None),
                         "current_url": getattr(state, "current_url", None),
                         "network_blocked": getattr(state, "network_blocked", None)},
            }
            (Path(cap_dir) / f"{task_id}.json").write_text(json.dumps(payload, default=str, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(f"[flat transcript dump failed: {exc}]", file=sys.stderr)
    try:
        memory.write_task_summary(success=(out["stop_reason"] == "ok"))
    except Exception:
        pass
    usage = snapshot(task_id)
    pop(task_id)
    out["usage"] = usage.to_dict() if usage is not None else None
    return out


# -------------------------------------------------------------------- harvest
def harvest_memory_dirs(run_dir: Path, role_task_ids: list[str]) -> list[str]:
    """Copy every /tmp/superbrowser/<id>/ the run produced (orchestrator +
    each worker seen in workers/) into run_dir/ledgers/<id>/."""
    return _audit.harvest_memory_dirs(run_dir, role_task_ids, memory_base=MEMORY_BASE)


def index_screenshots(run_dir: Path) -> int:
    """Count screenshots; write a fallback index only if the bridge did not."""
    return _audit.index_screenshots(run_dir)


# ----------------------------------------------------------------------- main
def frame_task(task: dict[str, Any]) -> str:
    """The text handed to the agent: verbatim instruction + start URL."""
    url = task.get("start_url")
    if url and url not in task["instruction"]:
        return f"{task['instruction']}\n\nStart URL: {url}"
    return task["instruction"]


# The delegated worker already has these removed (delegation.py), but the
# ORCHESTRATOR keeps nanobot's full default toolset. Observed live: an
# orchestrator gave up on the browser and made 33 `exec` calls, shell-scraping a
# site with curl/grep/sed instead of browsing it. That run measures nothing about
# a browser agent -- and every arm would reach for the shell differently, so the
# comparison is confounded -- besides handing an LLM arbitrary shell on the host.
# Eval-only: production keeps whatever tools the operator configured.
_NON_BROWSER_TOOLS = (
    "exec", "run_cli_app", "write_stdin", "list_exec_sessions",
    "spawn", "long_task", "cron", "message",
    "read_file", "write_file", "edit_file", "apply_patch",
    "list_dir", "glob", "grep", "find_files",
    "web_search", "web_fetch",
)


def strip_non_browser_tools(bot: Any) -> list[str]:
    """Remove shell/filesystem/search tools from an eval orchestrator."""
    removed = []
    tools = getattr(getattr(bot, "_loop", None), "tools", None)
    if tools is None:
        return removed
    keep = os.environ.get("SUPERBROWSER_EVAL_KEEP_HOST_TOOLS", "") not in ("", "0", "false")
    if keep:
        return removed
    for name in _NON_BROWSER_TOOLS:
        try:
            tools.unregister(name)
            removed.append(name)
        except Exception:
            pass
    if removed:
        print(f"[run_one] removed non-browser tools from the orchestrator: {', '.join(removed)}")
    return removed


def _server_base() -> str:
    return os.environ.get("SUPERBROWSER_URL") or "http://localhost:3100"


def _list_sessions() -> set[str]:
    try:
        import httpx

        tok = os.environ.get("TOKEN") or os.environ.get("SUPERBROWSER_TOKEN")
        h = {"Authorization": f"Bearer {tok}"} if tok else {}
        r = httpx.get(f"{_server_base()}/sessions", headers=h, timeout=10.0)
        return set((r.json() or {}).get("sessions") or [])
    except Exception:
        return set()


def close_new_sessions(before: set[str]) -> int:
    """Close browser sessions this run opened, and only those.

    Nothing in the bridge closes a session unless the model chooses to call
    browser_close, and the server only reaps at 30 min idle / 2 h lifetime while
    refusing new sessions past MAX_SESSIONS (20). A sweep of short runs therefore
    walks into 429s that look like task failures. Diffing the session list around
    the run closes ours without touching a session the operator already had open.
    """
    if os.environ.get("SUPERBROWSER_EVAL_CLOSE_SESSIONS", "1") == "0":
        return 0
    leaked = _list_sessions() - before
    if not leaked:
        return 0
    import httpx

    tok = os.environ.get("TOKEN") or os.environ.get("SUPERBROWSER_TOKEN")
    h = {"Authorization": f"Bearer {tok}"} if tok else {}
    n = 0
    for sid in leaked:
        try:
            httpx.delete(f"{_server_base()}/session/{sid}", headers=h, timeout=20.0)
            n += 1
        except Exception:
            pass
    print(f"[run_one] closed {n} browser session(s) left open by this run")
    return n


async def _main(spec: dict[str, Any]) -> int:
    run_dir = Path(spec["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "workers").mkdir(exist_ok=True)
    (run_dir / "screenshots").mkdir(exist_ok=True)

    # sanity: the runner must have pointed the bridge at this run's dirs
    expect = {"SUPERBROWSER_EVAL_CAPTURE_DIR": str(run_dir / "workers"),
              "SUPERBROWSER_SCREENSHOT_DIR": str(run_dir / "screenshots")}
    for k, v in expect.items():
        os.environ.setdefault(k, v)

    from nanobot.config.loader import set_config_path

    cfg_path, effective = prepare_nanobot_config(
        run_dir, overrides=spec.get("nanobot_overrides") or {}, model=spec.get("model"))
    set_config_path(cfg_path)

    framed = frame_task(spec["task"])
    timeout = spec.get("internal_timeout_s")
    topology = (spec.get("topology") or os.environ.get("SUPERBROWSER_TOPOLOGY") or "orchestrator").lower()
    started = time.time()
    sessions_before = _list_sessions()
    try:
        if topology == "flat":
            out = await _run_flat(spec, framed, timeout)
        else:
            out = await _run_orchestrator(spec, framed, timeout)
    finally:
        try:
            close_new_sessions(sessions_before)
        except Exception:
            pass
        try:
            os.unlink(cfg_path)
        except OSError:
            pass

    (run_dir / "result.txt").write_text(out.get("final_answer") or "", encoding="utf-8")
    if out.get("usage"):
        (run_dir / "usage.json").write_text(json.dumps(out["usage"], indent=2, default=str))
    ids = harvest_memory_dirs(run_dir, out.get("role_task_ids") or [])
    n_shots = index_screenshots(run_dir)

    from eval.core.protocol import describe_environment

    meta = {
        "run_id": spec["run_id"], "experiment": spec["experiment"], "arm": spec["arm"]["name"],
        "task_id": spec["task"]["task_id"], "seed": spec["seed"], "topology": topology,
        "orch_task_id": out.get("orch_task_id"), "role_task_ids": ids,
        "stop_reason": out.get("stop_reason"), "error": out.get("error"),
        "started_at": started, "ended_at": time.time(), "duration_s": out.get("duration_s"),
        "final_answer": out.get("final_answer") or "", "raw_content": out.get("raw_content") or "",
        "framed_task": framed, "arm_env": spec["arm"].get("env", {}),
        "effective_defaults": effective, "n_screenshots": n_shots,
        "environment": describe_environment(),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str, ensure_ascii=False))
    print(f"[run_one] {spec['run_id']} stop={out.get('stop_reason')} dur={out.get('duration_s')}s "
          f"workers={len(ids)} shots={n_shots}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Execute one evaluation run (called by eval.core.runner)")
    ap.add_argument("--spec", required=True, help="path to the run's spec.json")
    args = ap.parse_args(argv)
    spec = json.loads(Path(args.spec).read_text())
    return asyncio.run(_main(spec))


if __name__ == "__main__":
    sys.exit(main())
