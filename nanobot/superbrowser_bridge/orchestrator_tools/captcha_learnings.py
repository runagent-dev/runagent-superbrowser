"""Per-domain captcha learning: parse step_history → write JSON stats.

`_update_captcha_learnings` is called once per delegation finalize from
`delegation.py`. `_domain_needs_human_handoff` is read at delegation
start to decide whether to auto-enable the human handoff path.
"""

from __future__ import annotations

import json as _json
import os
from typing import Any

from superbrowser_bridge.routing import (
    _captcha_learnings_path,
    learning_reads_enabled,
)


def _update_captcha_learnings(domain: str, steps: list[dict]) -> dict | None:
    """Parse captcha solve results from step_history and update per-domain JSON.

    Looks for browser_solve_captcha steps whose result payload contains a
    structured JSON block (we emit one from BrowserSolveCaptchaTool). Each
    solve contributes:
      - method/subMethod that succeeded, plus vendor + duration
      - success_rate over the last 10 attempts
      - median_solve_ms of successful attempts

    Stale entries (>30 days or ≥5 consecutive failures) are pruned when the
    file is rewritten. The schema is intentionally small so future tasks
    can read it quickly.
    """
    from datetime import datetime, timezone
    import statistics

    # Extract solve attempts from steps.
    new_attempts: list[dict] = []
    for step in steps or []:
        if step.get("tool") != "browser_solve_captcha":
            continue
        result = step.get("result") or ""
        # The tool returns "<summary>\n\nResult JSON:\n{...}" — pull the JSON.
        brace = result.find("{")
        if brace < 0:
            continue
        try:
            parsed = _json.loads(result[brace:])
        except (_json.JSONDecodeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        parsed["observed_at"] = datetime.now(timezone.utc).isoformat()
        parsed["step_url"] = step.get("url") or ""
        new_attempts.append(parsed)

    if not new_attempts:
        return None

    path = _captcha_learnings_path(domain)
    existing: dict = {"attempts": [], "updated_at": None}
    if learning_reads_enabled() and os.path.exists(path):
        try:
            with open(path) as f:
                existing = _json.load(f)
        except (ValueError, OSError):
            existing = {"attempts": [], "updated_at": None}

    # Prune stale attempts (>30 days old).
    cutoff = (datetime.now(timezone.utc).timestamp() - 30 * 86400)
    def _is_fresh(a: dict) -> bool:
        ts = a.get("observed_at")
        if not ts:
            return False
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() >= cutoff
        except ValueError:
            return False
    kept = [a for a in existing.get("attempts", []) if _is_fresh(a)]
    kept.extend(new_attempts)
    # Cap at last 50 attempts to keep the file bounded.
    kept = kept[-50:]

    # Consecutive-failure decay: if last 5 attempts all failed, mark domain
    # as cold so future workers know not to trust the cached "winning method".
    last_five = kept[-5:]
    cold = len(last_five) == 5 and all(not a.get("solved") for a in last_five)

    # Compute per-method stats.
    per_method: dict[str, dict] = {}
    for a in kept:
        method = a.get("method") or "unknown"
        bucket = per_method.setdefault(
            method,
            {"attempts": 0, "solved": 0, "durations": [], "steps": []},
        )
        bucket["attempts"] += 1
        if a.get("solved"):
            bucket["solved"] += 1
            if a.get("durationMs"):
                bucket["durations"].append(int(a["durationMs"]))
            # Iterative loop emits a "steps" field. Track for budget
            # tuning: if p95 steps creeps up on a domain, the site has
            # likely hardened its challenge cadence and we should widen
            # the screenshot budget or shortcut to human handoff earlier.
            if isinstance(a.get("steps"), int) and a["steps"] > 0:
                bucket["steps"].append(int(a["steps"]))

    # Pick the winning method = highest success rate, tiebreak by speed.
    best_method = None
    best_rate = -1.0
    best_duration = float("inf")
    for method, bucket in per_method.items():
        rate = bucket["solved"] / bucket["attempts"] if bucket["attempts"] else 0.0
        median = statistics.median(bucket["durations"]) if bucket["durations"] else float("inf")
        if rate > best_rate or (rate == best_rate and median < best_duration):
            best_method = method
            best_rate = rate
            best_duration = median

    last10 = kept[-10:]
    last10_success = sum(1 for a in last10 if a.get("solved"))
    # Human-handoff flag: if any captcha ever succeeded via the
    # human_handoff strategy on this domain, record it so future tasks can
    # auto-enable the handoff path. Existing value is sticky once true
    # (cheap, and a false negative would silently re-break the flow).
    any_human_success = any(
        a.get("solved") and (a.get("method") or "").startswith("human_handoff")
        for a in kept
    )
    needs_human = bool(existing.get("needs_human_handoff")) or any_human_success

    summary = {
        "domain": domain,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "winning_method": best_method,
        "winning_success_rate": round(best_rate, 3) if best_rate >= 0 else None,
        "winning_median_ms": None if best_duration == float("inf") else int(best_duration),
        "success_rate_last_10": round(last10_success / len(last10), 3) if last10 else None,
        "cold": cold,
        "needs_human_handoff": needs_human,
        "per_method": {
            m: {
                "attempts": b["attempts"],
                "solved": b["solved"],
                "success_rate": round(b["solved"] / b["attempts"], 3) if b["attempts"] else 0.0,
                "median_ms": int(statistics.median(b["durations"])) if b["durations"] else None,
                "steps_p50": int(statistics.median(b["steps"])) if b["steps"] else None,
                "steps_p95": (
                    int(statistics.quantiles(b["steps"], n=20)[18])
                    if len(b["steps"]) >= 2 else (
                        b["steps"][0] if b["steps"] else None
                    )
                ),
            }
            for m, b in per_method.items()
        },
        "attempts": kept,
    }

    try:
        with open(path, "w") as f:
            _json.dump(summary, f, indent=2, default=str)
    except OSError:
        return None
    return summary


def _domain_needs_human_handoff(domain: str) -> bool:
    """Return True if a prior task on this domain succeeded via human handoff.

    Cheap read on the captcha-learnings JSON. Missing file / malformed
    JSON / missing field all return False so this is safe to call on
    first-touch domains.
    """
    if not domain:
        return False
    if not learning_reads_enabled():
        return False
    try:
        path = _captcha_learnings_path(domain)
    except Exception:
        return False
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            data = _json.load(f)
    except (ValueError, OSError):
        return False
    return bool(data.get("needs_human_handoff"))
