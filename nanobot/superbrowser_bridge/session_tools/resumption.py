"""Resumption-handoff helpers.

When a worker exits (stuck, captcha-blocked, or after browser_request_help),
we save enough tactical state that the NEXT worker can resume on the same
live Puppeteer session with knowledge of what already failed — instead of
spawning a fresh session from the home page.

File: /tmp/superbrowser/resumption.json
Expiry: 5 minutes (RESUMPTION_TTL_SEC). Past that, the Puppeteer session
has likely been GC'd server-side so liveness is doubtful regardless.

`save_resumption_artifact`, `load_resumption_artifact`, and
`clear_resumption_artifact` are imported by `orchestrator_tools` — keep
the names reachable from the package `__init__`.
"""

from __future__ import annotations

import json
import os
import time

from .http_client import SUPERBROWSER_URL, _request_with_backoff
from .telemetry import _extract_recent_failures


RESUMPTION_PATH = "/tmp/superbrowser/resumption.json"
RESUMPTION_TTL_SEC = 300


def save_resumption_artifact(
    state: "BrowserSessionState",
    domain: str,
    help_reason: str = "",
    help_failed_tactics: str = "",
) -> bool:
    """Write a resumption hint so the next delegation can pick up where we left off.

    Returns True if the artifact was written. Never raises.
    """
    try:
        if not state.session_id or not state.current_url:
            return False
        payload = {
            "session_id": state.session_id,
            "current_url": state.current_url,
            "best_checkpoint_url": state.best_checkpoint_url,
            "domain": domain,
            "task_id": state.task_id,
            "recent_failures": _extract_recent_failures(state.step_history),
            "help_reason": help_reason or "",
            "help_failed_tactics": help_failed_tactics or "",
            "written_at": time.time(),
        }
        os.makedirs(os.path.dirname(RESUMPTION_PATH), exist_ok=True)
        with open(RESUMPTION_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"  [resumption artifact saved: session={state.session_id} url={state.current_url}]")
        return True
    except OSError as exc:
        print(f"  [resumption save failed: {exc}]")
        return False


async def load_resumption_artifact(domain: str) -> dict | None:
    """Read and validate a resumption artifact for the given domain.

    Returns None if the artifact is missing, expired, from a different
    domain, or the referenced Puppeteer session is no longer alive
    on the TS server.
    """
    if not os.path.exists(RESUMPTION_PATH):
        return None
    try:
        with open(RESUMPTION_PATH) as f:
            payload = json.load(f)
    except (ValueError, OSError):
        return None

    age = time.time() - float(payload.get("written_at", 0) or 0)
    if age > RESUMPTION_TTL_SEC:
        try:
            os.remove(RESUMPTION_PATH)
        except OSError:
            pass
        return None
    if payload.get("domain") != domain:
        return None

    sid = payload.get("session_id")
    if not sid:
        return None

    # Cheap liveness probe — hit whichever backend owns this session.
    try:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{sid}/state",
            params={"vision": "false"},
            timeout=5.0,
        )
        if r.status_code != 200:
            try:
                os.remove(RESUMPTION_PATH)
            except OSError:
                pass
            return None
    except Exception:
        return None

    return payload


def clear_resumption_artifact() -> None:
    """Remove the resumption artifact (call when a new session successfully supersedes it)."""
    if os.path.exists(RESUMPTION_PATH):
        try:
            os.remove(RESUMPTION_PATH)
        except OSError:
            pass
