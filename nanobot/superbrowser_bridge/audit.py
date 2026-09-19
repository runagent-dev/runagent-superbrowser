"""Per-run audit trail: the evaluation harness's run directory, from any entry point.

The research harness (``eval/core/run_one.py``) leaves one self-contained
directory per run — ``spec.json``, ``meta.json``, ``result.txt``, ``usage.json``,
``config.redacted.json``, the worker transcripts under ``workers/``, every memory
directory the run produced under ``ledgers/<role_id>/`` (events, steps, ledger,
vision and click traces, the live-context dump), the ordered screenshots the
judge scores, and a ``manifest.json`` of provenance. This module is that writer,
lifted out of the harness so the SDK (``SuperBrowser(audit_dir=...)``) and the
harness produce byte-compatible layouts::

    <runs_root>/<experiment>/<arm>/<task_id>/seed<N>/

which is what ``eval.core.records.iter_run_dirs``, the judges
(``python -m eval.core.judge --experiment <name>``), ``python -m eval.core.record``
and every analyzer walk.

Design rules:

* Nothing here changes what the agent does. The recorder only turns on the
  bridge's own gated writers (trace / capture / context-dump env vars) and
  copies files afterwards. The confound pins of the frozen protocol are NOT
  applied — a production run stays a production run; the manifest records
  whatever env was in effect.
* ``SUPERBROWSER_SCREENSHOT_DIR`` is frozen when ``session_tools`` is imported,
  so the SDK points it at a per-process *inbox* before that import and the
  recorder moves the files into the run directory at ``finish``.
* Every write is best-effort. A recorder bug must never fail a run; a run that
  dies is still recoverable: ``meta.json`` is written at ``begin`` with
  ``stop_reason: running`` (see ``eval.core.record --rescue``).
* Hooks bank per iteration (``iterations.jsonl``): nanobot skips ``after_run``
  on cancellation and timeout, and ``on_finally`` always runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

try:  # nanobot is a dependency of the bridge; fall back to a bare class for tooling
    from nanobot.agent.hook import AgentHook
except Exception:  # pragma: no cover - import-time robustness only
    class AgentHook:  # type: ignore[no-redef]
        def __init__(self, reraise: bool = False) -> None:
            self._reraise = reraise

MEMORY_BASE = Path("/tmp/superbrowser")
LEDGER_FILES = ("events.jsonl", "steps.jsonl", "facts.jsonl", "episodic.jsonl", "ledger.json",
                "vision_calls.jsonl", "clicks.jsonl", "live_context.jsonl.gz", "screenshots.jsonl",
                "iterations.jsonl")
TASK_LEVEL_FILES = ("step_history.json", "step_history.md", "task_summary.json", "usage.json", "checkpoint.json")
SECRET_KEYS = ("apikey", "api_key", "token", "secret", "password")
ROSTER_NAME = "_roster.jsonl"          # under workers/: every role that ran, even if it never dumped a transcript
TRUE_VALUES = ("1", "true", "yes", "on")
SCHEMA_VERSION = 1


# ------------------------------------------------------------------ helpers
def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUE_VALUES


def redact(obj: Any) -> Any:
    """Blank string values whose key looks like a credential (recursive)."""
    if isinstance(obj, dict):
        return {k: ("***" if any(s in k.lower() for s in SECRET_KEYS) and isinstance(v, str) and v else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def default_config_path() -> Path:
    return Path(os.environ.get("NANOBOT_CONFIG", str(Path.home() / ".nanobot" / "config.json")))


def read_active_model(config_path: Path | None = None) -> dict[str, Any]:
    """``agents.defaults.{model,provider,temperature}`` of the active nanobot config."""
    path = Path(config_path) if config_path else default_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        defaults = data.get("agents", {}).get("defaults", {})
        return {"model": defaults.get("model", "unknown"), "provider": defaults.get("provider", "unknown"),
                "temperature": defaults.get("temperature")}
    except Exception:
        return {"model": "unknown", "provider": "unknown", "temperature": None}


def redact_config(run_dir: Path, *, overrides: dict[str, Any] | None = None, model: str | None = None,
                  config_path: Path | None = None) -> tuple[Path | None, dict[str, Any]]:
    """Write ``config.redacted.json`` into ``run_dir``; return
    ``(temp_config_path | None, redacted_effective_defaults)``.

    With ``overrides`` and/or ``model`` (the harness) the patched config is also
    written to a private 0600 temp file for ``nanobot.config.loader.set_config_path``.
    Without either (the SDK) only the redacted copy is written and the temp path
    is ``None`` — the run uses whatever config nanobot already resolved.
    """
    src = Path(config_path) if config_path else default_config_path()
    data: dict[str, Any] = json.loads(src.read_text(encoding="utf-8")) if src.exists() else {}
    defaults = data.setdefault("agents", {}).setdefault("defaults", {})
    tmp_path: Path | None = None
    if overrides is not None or model:
        defaults.update(overrides or {})
        if model:
            defaults["model"] = model
        fd, tmp = tempfile.mkstemp(prefix="sb-eval-config-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        tmp_path = Path(tmp)
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / "config.redacted.json").write_text(json.dumps(redact(data), indent=2), encoding="utf-8")
    return tmp_path, redact(defaults)


def roster_ids(run_dir: Path) -> list[str]:
    ids: list[str] = []
    p = Path(run_dir) / "workers" / ROSTER_NAME
    if not p.exists():
        return ids
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            tid = json.loads(line).get("task_id")
        except Exception:
            continue
        if tid and tid not in ids:
            ids.append(str(tid))
    return ids


def harvest_memory_dirs(run_dir: Path, role_task_ids: list[str], *, memory_base: Path | None = None) -> list[str]:
    """Copy every ``<memory_base>/<id>/`` the run produced (orchestrator + each
    worker seen in ``workers/`` or the roster) into ``run_dir/ledgers/<id>/``."""
    run_dir = Path(run_dir)
    base = Path(memory_base) if memory_base is not None else MEMORY_BASE
    ids: list[str] = list(role_task_ids)
    for tf in sorted((run_dir / "workers").glob("*.json")):
        if tf.stem not in ids:
            ids.append(tf.stem)
    for tid in roster_ids(run_dir):
        if tid not in ids:
            ids.append(tid)
    dest_root = run_dir / "ledgers"
    for tid in ids:
        src = base / tid
        if not src.exists():
            continue
        dest = dest_root / tid
        dest.mkdir(parents=True, exist_ok=True)
        for name in LEDGER_FILES:
            p = src / "memory" / name
            if p.exists():
                shutil.copy2(p, dest / name)
        for name in TASK_LEVEL_FILES:
            p = src / name
            if p.exists():
                shutil.copy2(p, dest / name)
    return ids


def index_screenshots(run_dir: Path) -> int:
    """Count screenshots; write a fallback ``index.jsonl`` only if the bridge did
    not (it writes one itself when ``SUPERBROWSER_TRACE_SCREENSHOTS=1``)."""
    d = Path(run_dir) / "screenshots"
    if not d.exists():
        return 0
    files = sorted(p for p in d.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if not (d / "index.jsonl").exists():
        with (d / "index.jsonl").open("w", encoding="utf-8") as f:
            for i, p in enumerate(files):
                f.write(json.dumps({"idx": i, "file": p.name, "bytes": p.stat().st_size,
                                    "mtime": p.stat().st_mtime, "source": "unknown"}) + "\n")
    return len(files)


def move_screenshots(src: Path | None, run_dir: Path) -> int:
    """Move the files of the per-process screenshot inbox into ``run_dir/screenshots``
    (rename, never copy, so the next run cannot inherit this run's frames)."""
    if src is None:
        return 0
    src, dest = Path(src), Path(run_dir) / "screenshots"
    if not src.exists() or src.resolve() == dest.resolve():
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in sorted(src.iterdir()):
        if not p.is_file():
            continue
        target = dest / p.name
        if p.name == "index.jsonl" and target.exists():
            with target.open("a", encoding="utf-8") as out, p.open(encoding="utf-8") as inp:
                out.write(inp.read())
            p.unlink()
            continue
        shutil.move(str(p), str(target))
        n += 1
    return n


# ------------------------------------------------------------ provenance
def repo_root() -> Path | None:
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _sh(cmd: list[str], cwd: Path | None) -> str:
    try:
        return subprocess.check_output(cmd, cwd=str(cwd) if cwd else None, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def git_provenance(root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    if root is None:
        return {"sha": "unknown", "short_sha": "unknown", "branch": "unknown", "dirty": None, "diff_sha256": None,
                "diff_bytes": 0, "root": None}
    status = _sh(["git", "status", "--porcelain"], root)
    try:
        diff = subprocess.check_output(["git", "diff", "HEAD"], cwd=str(root), stderr=subprocess.DEVNULL)
    except Exception:
        diff = b""
    return {"sha": _sh(["git", "rev-parse", "HEAD"], root), "short_sha": _sh(["git", "rev-parse", "--short", "HEAD"], root),
            "branch": _sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], root),
            "dirty": bool(status) if status != "unknown" else None,
            "diff_sha256": hashlib.sha256(diff).hexdigest() if diff else None, "diff_bytes": len(diff), "root": str(root)}


def describe_environment(root: Path | None = None) -> dict[str, Any]:
    """Provenance snapshot recorded once per run in ``meta.json`` (no secrets).
    Keys are the ones the research records have always carried."""
    root = root or repo_root()
    info: dict[str, Any] = {
        "git_sha": _sh(["git", "rev-parse", "--short", "HEAD"], root),
        "git_branch": _sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], root),
        "git_dirty": bool(_sh(["git", "status", "--porcelain"], root)),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "vision_model": os.environ.get("VISION_MODEL", ""),
        "vision_provider": os.environ.get("VISION_PROVIDER", ""),
        "headless_mode": os.environ.get("SUPERBROWSER_HEADLESS_MODE", os.environ.get("HEADLESS", "")),
        # Which topology the orchestrator ran with: "browser" pins the engine
        # (protocol default for the ablation sweeps), "auto" leaves the search
        # worker available. Recorded so a run's artifacts say which it was.
        "eval_mode": os.environ.get("SUPERBROWSER_EVAL_MODE", "browser"),
        "active_model": read_active_model(),
    }
    try:
        import nanobot  # noqa: WPS433

        info["nanobot_version"] = getattr(nanobot, "__version__", "unknown")
        info["nanobot_path"] = os.path.dirname(nanobot.__file__)
        runner = os.path.join(os.path.dirname(nanobot.__file__), "agent", "runner.py")
        with open(runner, "rb") as f:
            info["nanobot_runner_sha256"] = hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        info["nanobot_version"] = "unknown"
    return info


ENV_PREFIXES = ("ABLATE_", "SUPERBROWSER_", "VISION_", "LLM_", "CLICK_LADDER", "MOTOR_", "LEARNING_READS",
                "HEADLESS", "NANOBOT_", "T3_", "PROXY")


def env_snapshot(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Every configuration-shaped env key in effect, credentials blanked."""
    env = environ if environ is not None else os.environ
    out: dict[str, str] = {}
    for k in sorted(env):
        if not k.startswith(ENV_PREFIXES):
            continue
        v = env[k]
        if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "B64")) and v:
            v = "***"
        out[k] = v
    return out


def soul_hashes() -> dict[str, dict[str, Any]]:
    """sha256 of each role's SOUL.md — the prompt provenance the records lacked."""
    out: dict[str, dict[str, Any]] = {}
    try:
        from superbrowser_bridge.workspaces import ROLES, workspace_for
    except Exception:
        return out
    for role in ROLES:
        try:
            p = workspace_for(role) / "SOUL.md"
            data = p.read_bytes() if p.exists() else b""
            out[role] = {"path": str(p), "sha256": hashlib.sha256(data).hexdigest() if data else None, "bytes": len(data)}
        except Exception as exc:  # noqa: BLE001
            out[role] = {"path": None, "sha256": None, "bytes": 0, "error": str(exc)}
    return out


def package_versions() -> dict[str, str]:
    from importlib import metadata

    out: dict[str, str] = {"python": sys.version.split()[0]}
    for name in ("nanobot-ai", "runagent-superbrowser", "httpx", "patchright", "playwright", "curl_cffi", "openai", "pydantic"):
        try:
            out[name] = metadata.version(name)
        except Exception:
            continue
    return out


def effective_screenshot_dir() -> Path | None:
    """The directory the bridge is actually writing screenshots to: the frozen
    module constant when ``session_tools`` is imported, else the env value."""
    for mod in ("superbrowser_bridge.session_tools.state", "superbrowser_bridge.session_tools.http_client"):
        m = sys.modules.get(mod)
        if m is not None and getattr(m, "SCREENSHOT_DIR", None):
            return Path(getattr(m, "SCREENSHOT_DIR"))
    v = os.environ.get("SUPERBROWSER_SCREENSHOT_DIR")
    return Path(v) if v else None


def repoint_screenshot_dir(path: Path) -> list[str]:
    """Re-point the frozen ``SCREENSHOT_DIR`` constant on bridge modules that are
    ALREADY imported (a second SuperBrowser in one process). Returns the modules
    touched; nothing happens for modules not yet imported (the env suffices)."""
    touched = []
    for mod in ("superbrowser_bridge.session_tools.http_client", "superbrowser_bridge.session_tools.state",
                "superbrowser_bridge.session_tools"):
        m = sys.modules.get(mod)
        if m is not None and hasattr(m, "SCREENSHOT_DIR"):
            setattr(m, "SCREENSHOT_DIR", str(path))
            touched.append(mod)
    return touched


# ------------------------------------------------------------------- hook
class AuditHook(AgentHook):
    """Bank one row per iteration (and a terminal row) for a role.

    ``before_run`` also appends the role to ``workers/_roster.jsonl`` so a worker
    that is killed before it dumps its transcript is still harvested.
    """

    def __init__(self, role: str, *, memory: Any = None, memory_dir: Path | None = None,
                 task_id: str | None = None, roster_path: Path | None = None) -> None:
        super().__init__()
        self.role = role
        self.task_id = task_id or getattr(memory, "task_id", None) or "unknown"
        if memory_dir is None and memory is not None:
            try:
                memory_dir = Path(memory.events.path).parent
            except Exception:
                memory_dir = None
        self.memory_dir = Path(memory_dir) if memory_dir else None
        self.roster_path = Path(roster_path) if roster_path else None
        self._t0 = time.time()
        self._rows = 0

    def _append(self, row: dict[str, Any]) -> None:
        if self.memory_dir is None:
            return
        try:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            with (self.memory_dir / "iterations.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
            self._rows += 1
        except Exception:  # noqa: BLE001 - never break a run
            pass

    async def before_run(self, context: Any) -> None:
        if self.roster_path is None:
            return
        try:
            self.roster_path.parent.mkdir(parents=True, exist_ok=True)
            with self.roster_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "role": self.role, "task_id": self.task_id,
                                    "memory_dir": str(self.memory_dir) if self.memory_dir else None}) + "\n")
        except Exception:  # noqa: BLE001
            pass

    async def after_iteration(self, context: Any) -> None:
        try:
            usage = dict(getattr(context, "usage", None) or {})
            calls = getattr(context, "tool_calls", None) or []
            names = []
            for tc in calls:
                fn = getattr(tc, "name", None) or (tc.get("function", {}).get("name") if isinstance(tc, dict) else None)
                names.append(str(fn or "tool"))
            self._append({"ts": time.time(), "elapsed_s": round(time.time() - self._t0, 3), "role": self.role,
                          "task_id": self.task_id, "iter": getattr(context, "iteration", None),
                          "tokens_in": usage.get("input_tokens") or usage.get("prompt_tokens"),
                          "tokens_out": usage.get("output_tokens") or usage.get("completion_tokens"),
                          "n_messages": len(getattr(context, "messages", None) or []),
                          "n_tool_calls": len(calls), "tools": names,
                          "stop_reason": getattr(context, "stop_reason", None), "error": getattr(context, "error", None)})
        except Exception:  # noqa: BLE001
            pass

    async def on_finally(self, context: Any) -> None:
        try:
            final = getattr(context, "final_content", None)
            self._append({"ts": time.time(), "elapsed_s": round(time.time() - self._t0, 3), "role": self.role,
                          "task_id": self.task_id, "final": True,
                          "stop_reason": getattr(context, "stop_reason", None), "error": getattr(context, "error", None),
                          "exception": (type(getattr(context, "exception", None)).__name__
                                        if getattr(context, "exception", None) is not None else None),
                          "final_content_chars": len(final) if isinstance(final, str) else None,
                          "tools_used": list(getattr(context, "tools_used", None) or []), "rows": self._rows})
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------- recorder
def derived_task_id(instruction: str, url: str | None) -> str:
    return hashlib.sha256(f"{instruction.strip()}\n{(url or '').strip()}".encode("utf-8")).hexdigest()[:32]


def run_dir_for(runs_root: Path, experiment: str, arm: str, task_id: str, seed: int) -> Path:
    return Path(runs_root) / experiment / arm / task_id / f"seed{seed}"


class AuditRun:
    """One recorded run. Created by :meth:`AuditRecorder.begin`."""

    def __init__(self, recorder: "AuditRecorder", *, run_dir: Path, run_id: str, task: dict[str, Any], seed: int,
                 spec: dict[str, Any]) -> None:
        self.recorder = recorder
        self.run_dir = run_dir
        self.run_id = run_id
        self.task = task
        self.seed = seed
        self.spec = spec
        self.started_at = time.time()
        self.orch_task_id: str | None = None
        self._saved_env: dict[str, str | None] = {}
        self.finished = False
        self._ids: list[str] = []

    # ----- env
    @property
    def roster_path(self) -> Path:
        return self.run_dir / "workers" / ROSTER_NAME

    def env(self) -> dict[str, str]:
        return {
            "SUPERBROWSER_TRACE_VISION": "1",
            "SUPERBROWSER_TRACE_CLICKS": "1",
            "SUPERBROWSER_TRACE_SCREENSHOTS": "1",
            "SUPERBROWSER_EVAL_CONTEXT_DUMP": "1",
            "SUPERBROWSER_EVAL_CAPTURE_DIR": str(self.run_dir / "workers"),
            "SUPERBROWSER_EVAL_RUN_ID": self.run_id,
            "SUPERBROWSER_EVAL_SEED": str(self.seed),
            "SUPERBROWSER_EVAL_TASK_ID": str(self.task.get("task_id")),
            "SUPERBROWSER_AUDIT_ITERATIONS": "1",
            "SUPERBROWSER_AUDIT_RUN_DIR": str(self.run_dir),
        }

    def apply_env(self) -> None:
        for k, v in self.env().items():
            self._saved_env[k] = os.environ.get(k)
            os.environ[k] = v

    def restore_env(self) -> None:
        for k, old in self._saved_env.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
        self._saved_env = {}

    # ----- files
    def _write(self, name: str, obj: Any) -> None:
        try:
            (self.run_dir / name).write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def write_preliminary_meta(self, orch_task_id: str, framed_task: str | None = None) -> None:
        self.orch_task_id = orch_task_id
        self._write("meta.json", {
            "run_id": self.run_id, "experiment": self.spec["experiment"], "arm": self.spec["arm"]["name"],
            "task_id": self.task.get("task_id"), "seed": self.seed, "topology": self.spec.get("topology"),
            "orch_task_id": orch_task_id, "role_task_ids": [orch_task_id], "stop_reason": "running", "error": None,
            "started_at": self.started_at, "ended_at": None, "duration_s": None, "final_answer": "", "raw_content": "",
            "framed_task": framed_task or "", "arm_env": {}, "effective_defaults": None, "n_screenshots": 0,
            "environment": None,
        })

    def write_manifest(self, *, server_url: str | None = None, health: Any = None,
                       effective_defaults: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> None:
        manifest = {
            "schema_version": SCHEMA_VERSION, "written_at": time.time(), "run_id": self.run_id,
            "run_dir": str(self.run_dir), "experiment": self.spec["experiment"], "arm": self.spec["arm"]["name"],
            "task": self.task, "git": git_provenance(), "soul": soul_hashes(), "env": env_snapshot(),
            "effective_nanobot_defaults": effective_defaults, "server": {"url": server_url, "health": health},
            "python": package_versions(), "environment": describe_environment(),
            "recorder": {"experiment": self.recorder.experiment, "arm": self.recorder.arm,
                         "screenshot_inbox": str(self.recorder._inbox) if self.recorder._inbox else None},
            **(extra or {}),
        }
        self._write("manifest.json", manifest)

    def finish(self, *, final_answer: str = "", raw_content: str = "", stop_reason: str = "ok", error: str | None = None,
               usage: dict[str, Any] | None = None, orch_task_id: str | None = None, framed_task: str | None = None,
               effective_defaults: dict[str, Any] | None = None, screenshot_src: Path | None = None) -> list[str]:
        """Write the terminal files and harvest. Idempotent; never raises."""
        if self.finished:
            return list(self._ids)
        self.finished = True
        ids: list[str] = []
        orch = orch_task_id or self.orch_task_id
        try:
            (self.run_dir / "result.txt").write_text(final_answer or "", encoding="utf-8")
            if usage:
                self._write("usage.json", usage)
            move_screenshots(screenshot_src if screenshot_src is not None else effective_screenshot_dir(), self.run_dir)
            ids = harvest_memory_dirs(self.run_dir, [orch] if orch else [], memory_base=self.recorder.memory_base)
            n_shots = index_screenshots(self.run_dir)
            self._write("meta.json", {
                "run_id": self.run_id, "experiment": self.spec["experiment"], "arm": self.spec["arm"]["name"],
                "task_id": self.task.get("task_id"), "seed": self.seed, "topology": self.spec.get("topology"),
                "orch_task_id": orch, "role_task_ids": ids, "stop_reason": stop_reason, "error": error,
                "started_at": self.started_at, "ended_at": time.time(),
                "duration_s": round(time.time() - self.started_at, 2),
                "final_answer": final_answer or "", "raw_content": raw_content or "",
                "framed_task": framed_task or "", "arm_env": {}, "effective_defaults": effective_defaults,
                "n_screenshots": n_shots, "environment": describe_environment(),
            })
        except Exception:  # noqa: BLE001 - the run's outcome must not depend on the recorder
            pass
        self._ids = list(ids)
        return ids


class AuditRecorder:
    """Factory for :class:`AuditRun` directories under ``<runs_root>/<experiment>/<arm>/``."""

    def __init__(self, runs_root: str | Path, *, experiment: str = "sdk", arm: str = "ledger",
                 memory_base: Path | None = None) -> None:
        self.runs_root = Path(runs_root).expanduser().resolve()
        self.experiment = experiment
        self.arm = arm
        self.memory_base = Path(memory_base) if memory_base is not None else None
        self._inbox: Path | None = None

    def inbox_screenshot_dir(self) -> Path:
        """A per-process staging dir for screenshots (created lazily)."""
        if self._inbox is None:
            self._inbox = self.runs_root / self.experiment / "_inbox" / f"{os.getpid()}-{uuid.uuid4().hex[:8]}" / "screenshots"
        self._inbox.mkdir(parents=True, exist_ok=True)
        return self._inbox

    def _archive_stale(self, d: Path, arm: str, task_id: str, seed: int) -> Path | None:
        """A previous unfinished attempt (no run_record.json) is moved aside, never
        deleted, exactly like ``eval.core.runner.archive_failed_attempt``."""
        if not d.exists():
            return None
        if not any(c.name != "spec.json" for c in d.iterdir()):
            return None
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(d.stat().st_mtime))
        dest = self.runs_root / self.experiment / "_failed_attempts" / f"{arm}__{task_id}__seed{seed}__{stamp}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(d), str(dest))
        return dest

    def begin(self, *, task: dict[str, Any], seed: int = 0, model: str | None = None, timeout: float | None = None,
              server_url: str | None = None, mode: str = "browser", topology: str = "orchestrator") -> AuditRun:
        instruction = str(task.get("instruction") or "")
        url = task.get("start_url") or task.get("url")
        task_id = str(task.get("task_id") or derived_task_id(instruction, url))
        row = {
            "task_id": task_id, "benchmark": task.get("benchmark") or "custom", "level": task.get("level"),
            "website": task.get("website") or url, "start_url": url, "instruction": instruction,
            "reference": task.get("reference"), "reference_length": task.get("reference_length"),
            "critical_state": list(task.get("critical_state") or []), "checks": list(task.get("checks") or []),
        }
        # never clobber a judged run: bump the seed; move a stale unjudged attempt aside
        while (run_dir_for(self.runs_root, self.experiment, self.arm, task_id, seed) / "run_record.json").exists():
            seed += 1
        run_dir = run_dir_for(self.runs_root, self.experiment, self.arm, task_id, seed)
        self._archive_stale(run_dir, self.arm, task_id, seed)
        (run_dir / "workers").mkdir(parents=True, exist_ok=True)
        (run_dir / "screenshots").mkdir(exist_ok=True)
        run_id = f"{self.experiment}:{self.arm}:{task_id}:s{seed}"
        env_pins = {k: v for k, v in AuditRun(self, run_dir=run_dir, run_id=run_id, task=row, seed=seed, spec={}).env().items()
                    if not k.startswith("SUPERBROWSER_EVAL_") and k != "SUPERBROWSER_AUDIT_RUN_DIR"}
        max_iter = os.environ.get("SUPERBROWSER_WORKER_MAX_ITER")
        spec = {
            "run_id": run_id, "experiment": self.experiment, "seed": seed,
            "arm": {"name": self.arm, "env": {}, "side": "python", "family": "system",
                    "description": "SDK run (production configuration; no arm env applied)"},
            "task": row, "benchmark": row["benchmark"], "run_dir": str(run_dir), "model": model,
            "topology": topology, "server_url": server_url, "mode": mode,
            "protocol": {"version": "sdk", "hash": hashlib.sha256(json.dumps(env_pins, sort_keys=True).encode()).hexdigest()[:16],
                         "benchmark": row["benchmark"], "max_iterations": int(max_iter) if max_iter else None,
                         "wall_clock_s": timeout, "env_pins": env_pins,
                         "note": "SDK audit trail: production behaviour with the bridge's trace/capture writers on; "
                                 "the frozen research protocol's confound pins are NOT applied"},
            "nanobot_overrides": {}, "internal_timeout_s": timeout,
        }
        run = AuditRun(self, run_dir=run_dir, run_id=run_id, task=row, seed=seed, spec=spec)
        run._write("spec.json", spec)
        return run
