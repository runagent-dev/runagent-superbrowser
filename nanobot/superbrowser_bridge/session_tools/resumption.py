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
# How many workers may inherit one lineage of progress before the artifact
# is dropped. Each hop drops the live session and keeps only the URL and
# the accumulated dead ends, so it cannot re-seed a stuck session; the cap
# exists because a task that has defeated this many workers is not going
# to be solved by a fourth reading the same notes.
RESUMPTION_MAX_HOPS = 3


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


def _merge_failures(old: list, new: list, cap: int = 8) -> list:
    """Union of two failed-tactic lists, newest last, de-duplicated.

    The point of carrying these across a handoff is that each worker adds
    what IT discovered does not work. Replacing rather than merging would
    make every successor re-learn its predecessor's dead ends.
    """
    seen: set = set()
    out: list = []
    for item in list(old or []) + list(new or []):
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("tool", "")),
            str(item.get("args", ""))[:80],
            str(item.get("result_excerpt", ""))[:80],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[-cap:]


def demote_resumption_artifact(
    state: "BrowserSessionState",
    domain: str,
    progress_note: str = "",
) -> bool:
    """Carry progress forward after a worker resumed and still failed.

    This replaces an outright `clear_resumption_artifact()`. Clearing was
    aimed at a real failure — re-seeding the next worker with a LIVE
    session that walks its LLM straight back into the same loop — but it
    also discarded the furthest URL reached and every dead end both
    workers had paid for. With a 50-step worker cap, a task needing three
    workers got knowledge transfer on the first handoff and nothing
    afterwards.

    Demoting keeps what is safe and drops what poisons:

      - the live `session_id` is dropped, so the successor opens its own
        page and cannot be walked back into the stuck one;
      - the furthest URL survives, preferring whichever worker got deeper
        (a successor that regressed does not overwrite a better URL);
      - failed tactics from both workers are merged, so each successor
        starts knowing strictly more than its predecessor did;
      - `hops` increments, and past RESUMPTION_MAX_HOPS the artifact is
        dropped entirely.

    Returns True when an artifact survives for the next worker.
    """
    try:
        if not os.path.exists(RESUMPTION_PATH):
            return False
        with open(RESUMPTION_PATH) as f:
            prev = json.load(f)
    except (ValueError, OSError):
        return False

    if prev.get("domain") != domain:
        # A different site's artifact is not ours to demote or destroy.
        return False

    hops = int(prev.get("hops", 0) or 0) + 1
    if hops > RESUMPTION_MAX_HOPS:
        clear_resumption_artifact()
        print(f"  [resumption artifact dropped after {hops - 1} hops]")
        return False

    new_url = getattr(state, "current_url", "") or ""
    new_cp = getattr(state, "best_checkpoint_url", "") or ""
    payload = dict(prev)
    payload.pop("session_id", None)       # never hand on a live session
    payload["hops"] = hops
    # Keep the deeper of the two URLs. A worker that bounced back to the
    # home page must not erase the filtered result page its predecessor
    # spent its whole budget reaching.
    if new_url and len(new_url) >= len(str(prev.get("current_url") or "")):
        payload["current_url"] = new_url
    if new_cp:
        payload["best_checkpoint_url"] = new_cp
    payload["recent_failures"] = _merge_failures(
        prev.get("recent_failures"),
        _extract_recent_failures(getattr(state, "step_history", []) or []),
    )
    if progress_note:
        earlier = str(prev.get("progress_note") or "")
        payload["progress_note"] = (
            (earlier + "\n---\n" + progress_note)[-1200:] if earlier
            else progress_note[:1200]
        )
    payload["written_at"] = time.time()

    try:
        os.makedirs(os.path.dirname(RESUMPTION_PATH), exist_ok=True)
        with open(RESUMPTION_PATH, "w") as f:
            json.dump(payload, f, indent=2)
    except OSError as exc:
        print(f"  [resumption demote failed: {exc}]")
        return False
    print(
        f"  [resumption artifact demoted to hop {hops}/{RESUMPTION_MAX_HOPS}: "
        f"session dropped, url={payload.get('current_url')} "
        f"failures={len(payload.get('recent_failures') or [])}]"
    )
    return True


def clear_resumption_artifact() -> None:
    """Remove the resumption artifact (call when a new session successfully supersedes it)."""
    if os.path.exists(RESUMPTION_PATH):
        try:
            os.remove(RESUMPTION_PATH)
        except OSError:
            pass
