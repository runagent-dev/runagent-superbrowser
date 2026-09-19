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
# Warm window: the Puppeteer session is expected to still be alive, so the
# next worker can attach to the live page and carry on mid-flow.
RESUMPTION_TTL_SEC = 300
# Cold window: the session is gone (the worker closed it, or it expired),
# but knowing WHERE the last worker got to is still worth far more than
# starting from a blank page. A worker that spent 47 iterations reaching a
# filtered result page should not have to rediscover that URL. Beyond this
# the page state is too likely to have moved on to be a useful hint.
RESUMPTION_COLD_TTL_SEC = 1800


def save_resumption_artifact(
    state: "BrowserSessionState",
    domain: str,
    help_reason: str = "",
    help_failed_tactics: str = "",
    progress_note: str = "",
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
            # What the previous worker actually established. Without it the
            # successor repeats the reasoning as well as the navigation.
            "progress_note": (progress_note or "")[:1200],
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
    """Read and validate a resumption artifact for ``domain``.

    Returns the payload with a ``warm`` flag, or None when there is
    nothing usable:

      warm=True   the referenced Puppeteer session answered a liveness
                  probe, so the successor can attach to the live page.
      warm=False  the session is gone or the warm window has passed, but
                  the artifact is still inside the cold window. The URL,
                  the best checkpoint and the list of failed tactics
                  remain useful: the successor navigates back and
                  continues instead of rediscovering all of it.

    Previously any dead session threw the whole artifact away, which is
    what made a worker that ran out of iterations hand its successor a
    blank page.
    """
    if not os.path.exists(RESUMPTION_PATH):
        return None
    try:
        with open(RESUMPTION_PATH) as f:
            payload = json.load(f)
    except (ValueError, OSError):
        return None

    age = time.time() - float(payload.get("written_at", 0) or 0)
    if age > RESUMPTION_COLD_TTL_SEC:
        try:
            os.remove(RESUMPTION_PATH)
        except OSError:
            pass
        return None
    if payload.get("domain") != domain:
        return None
    if not payload.get("current_url"):
        return None            # nothing to navigate back to

    sid = payload.get("session_id")
    warm = False
    if sid and age <= RESUMPTION_TTL_SEC:
        # Cheap liveness probe — hit whichever backend owns this session.
        try:
            r = await _request_with_backoff(
                "GET",
                f"{SUPERBROWSER_URL}/session/{sid}/state",
                params={"vision": "false"},
                timeout=5.0,
            )
            warm = r.status_code == 200
        except Exception:
            warm = False

    payload["warm"] = warm
    payload["age_s"] = int(age)
    return payload


def clear_resumption_artifact() -> None:
    """Remove the resumption artifact (call when a new session successfully supersedes it)."""
    if os.path.exists(RESUMPTION_PATH):
        try:
            os.remove(RESUMPTION_PATH)
        except OSError:
            pass
