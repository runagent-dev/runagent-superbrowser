"""The frozen evaluation protocol.

Everything a reader needs to reproduce a run — and everything that must be
identical across the arms of a paired comparison — lives here, as data, and
is hashed into every RunRecord. Change a field → the protocol hash changes →
runs are no longer comparable, on purpose.

Two kinds of settings:

* **Budgets / policy** (``max_iterations``, ``wall_clock_s``, captcha policy,
  ``context_window_tokens`` / ``max_tokens`` pins). The nanobot host trims
  history itself when ``context_window_tokens - max_tokens - 1024 > 0`` and the
  prompt exceeds that budget; the memory-policy experiment needs the "full
  history" arm to be a true no-eviction upper bound, so the protocol pins both
  numbers and records them.
* **Confound pins** (``env_pins``): env vars the runner forces for EVERY run so
  one run cannot seed the next (cross-task site models, cookie/identity jars,
  learning reads) and so the instrumentation the analyzers need is on.

The brain model is deliberately NOT frozen here: it is chosen at launch
(``--model``) and recorded per run, because the model is one of the
robustness axes (E10).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any

PROTOCOL_VERSION = "iclr2027-v1"


def _default_env_pins() -> dict[str, str]:
    return {
        # --- confound control: no state may leak between runs/arms -----------
        "SUPERBROWSER_CROSS_TASK_MEMORY": "0",   # no site-model ingest/merge
        "LEARNING_READS_ENABLED": "0",           # per-domain routing history reads off
        "SUPERBROWSER_COOKIE_JAR": "0",          # bot-protection cookie persistence off
        "SUPERBROWSER_IDENTITY_JAR": "0",        # login-cookie persistence off
        "SUPERBROWSER_EVAL_SCHEMA_REMINDER": "", # legacy rescue prompt never on by accident
        # --- instrumentation the analyzers rely on ---------------------------
        "SUPERBROWSER_TRACE_VISION": "1",        # memory/vision_calls.jsonl
        "SUPERBROWSER_TRACE_CLICKS": "1",        # memory/clicks.jsonl
        "SUPERBROWSER_TRACE_SCREENSHOTS": "1",   # per-run ordered screenshots (WebJudge)
        "SUPERBROWSER_EVAL_CONTEXT_DUMP": "1",   # memory/live_context.jsonl.gz (CSD)
    }


@dataclass(frozen=True)
class Protocol:
    version: str = PROTOCOL_VERSION
    benchmark: str = "online_mind2web_hard"
    # per-run budgets
    max_iterations: int = 50           # worker step cap (SUPERBROWSER_WORKER_MAX_ITER)
    wall_clock_s: int = 1800           # subprocess budget per run; timeout => failure
    # host-model context pins (see module docstring)
    context_window_tokens: int = 200_000
    max_tokens: int = 16_384
    temperature: float = 1.0
    # policies
    captcha_policy: str = "solver_then_human_handoff; handoff counts as failure"
    start_url_policy: str = "worker starts at the benchmark start_url; direct navigation allowed within the pinned domain"
    impossible_task_rule: str = "excluded only if the same deterministic marker (site_unavailable|geo_blocked|captcha_unsolved) fires in EVERY paired arm; both all-task and excluded-set results are reported"
    # evaluator ids (recorded; the judge model is pinned by env at judge time)
    primary_evaluator: str = "webjudge"          # Online-Mind2Web 3-step screenshot judge
    secondary_evaluator: str = "answer_judge"    # text-only final-answer judge
    env_pins: dict[str, str] = field(default_factory=_default_env_pins)

    # ------------------------------------------------------------------ utils
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def env(self) -> dict[str, str]:
        """Env the runner injects into every run (pins + budgets)."""
        out = dict(self.env_pins)
        out["SUPERBROWSER_WORKER_MAX_ITER"] = str(self.max_iterations)
        out["SUPERBROWSER_WORKER_MAX_ITER_RESEARCH"] = str(self.max_iterations)
        return out

    def nanobot_config_overrides(self) -> dict[str, Any]:
        """Fields patched into the per-run nanobot config (agents.defaults)."""
        return {
            "contextWindowTokens": self.context_window_tokens,
            "maxTokens": self.max_tokens,
            "temperature": self.temperature,
            "maxToolIterations": self.max_iterations,
        }

    def host_snip_active(self) -> bool:
        """True if nanobot's own history snip can fire under these pins."""
        return (self.context_window_tokens - self.max_tokens - 1024) > 0


DEFAULT_PROTOCOL = Protocol()


def protocol_from_args(**overrides: Any) -> Protocol:
    """Build a Protocol with CLI overrides (None values are ignored)."""
    clean = {k: v for k, v in overrides.items() if v is not None}
    return Protocol(**clean) if clean else DEFAULT_PROTOCOL


def describe_environment() -> dict[str, Any]:
    """Provenance snapshot recorded once per run (no secrets)."""
    import platform
    import subprocess

    from eval._bootstrap import REPO_ROOT, read_active_model

    def sh(cmd: list[str]) -> str:
        try:
            return subprocess.check_output(cmd, cwd=str(REPO_ROOT), text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            return "unknown"

    info: dict[str, Any] = {
        "git_sha": sh(["git", "rev-parse", "--short", "HEAD"]),
        "git_branch": sh(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "git_dirty": bool(sh(["git", "status", "--porcelain"])),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "vision_model": os.environ.get("VISION_MODEL", ""),
        "vision_provider": os.environ.get("VISION_PROVIDER", ""),
        "headless_mode": os.environ.get("SUPERBROWSER_HEADLESS_MODE", os.environ.get("HEADLESS", "")),
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
