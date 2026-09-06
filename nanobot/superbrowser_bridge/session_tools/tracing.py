"""Opt-in per-run trace records for the research harness.

Zero-cost unless enabled: every helper first checks its env flag and returns.
Records are appended as JSONL next to the task's other ledger files
(``/tmp/superbrowser/<task_id>/memory/``) so the eval harness harvests them
together with ``events.jsonl`` / ``steps.jsonl``:

* ``SUPERBROWSER_TRACE_VISION=1``  -> ``vision_calls.jsonl`` — one record per
  vision pass (sync screenshot, background prefetch, legacy image path,
  verify-fact), with the page fingerprint the pass was keyed on and whether
  the cache served it. This is what Redundant Perception Rate is computed
  from; ``state.vision_calls`` alone cannot tell a cache hit from a miss.
* ``SUPERBROWSER_TRACE_CLICKS=1``  -> ``clicks.jsonl`` — one record per click
  attempt with the snapper's decision (``method``, label/chevron scores when
  the server reports them), the escalation strategy that landed, and the
  effect envelope. This is what first-path / recovery success are computed
  from without parsing prose.

Writes are best-effort and never raise into the agent loop.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_TRUE = ("1", "true", "yes", "on")


def vision_trace_enabled() -> bool:
    return os.environ.get("SUPERBROWSER_TRACE_VISION", "").lower() in _TRUE


def click_trace_enabled() -> bool:
    return os.environ.get("SUPERBROWSER_TRACE_CLICKS", "").lower() in _TRUE


def _memory_dir(state: Any) -> Path | None:
    try:
        return Path(state.memory.events.path).parent
    except Exception:
        return None


def _append(state: Any, filename: str, record: dict[str, Any]) -> None:
    d = _memory_dir(state)
    if d is None:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        with (d / filename).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def trace_vision(state: Any, *, path: str, url: str | None, dom_hash: str | None,
                 dom_text_hash: str | None, resp: Any, intent: str | None = None) -> None:
    """Record one vision pass. ``resp`` is the VisionResponse (or None on failure)."""
    if not vision_trace_enabled():
        return
    rec: dict[str, Any] = {
        "ts": time.time(),
        "session_id": getattr(state, "session_id", None),
        "path": path,                       # sync | prefetch | legacy_image | verify
        "url": url or getattr(state, "current_url", None),
        "dom_hash": dom_hash or None,
        "dom_text_hash": dom_text_hash or None,
        "intent": intent,
        "step": getattr(state, "step_counter", None),
        "action_count": getattr(state, "action_count", None),
        "vision_calls_so_far": getattr(state, "vision_calls", None),
    }
    if resp is None:
        rec["ok"] = False
    else:
        rec.update({
            "ok": True,
            "cached": bool(getattr(resp, "cached", False)),
            "model": getattr(resp, "model", None),
            "duration_ms": getattr(resp, "duration_ms", None),
            "tokens_used": getattr(resp, "tokens_used", None),
            "n_bboxes": len(getattr(resp, "bboxes", []) or []),
            "page_type": getattr(resp, "page_type", None),
            "freshness": getattr(resp, "screenshot_freshness", None),
        })
    _append(state, "vision_calls.jsonl", rec)


def trace_click(state: Any, **fields: Any) -> None:
    """Record one click attempt. Callers pass whatever they know; keys used by
    the analyzers: tool, target, vision_index, label, snapped, method,
    label_score, chevron_score, candidates, label_mismatch, strategy,
    escalated, silent, ok, error, url_changed, mutation_delta, tags."""
    if not click_trace_enabled():
        return
    rec: dict[str, Any] = {
        "ts": time.time(),
        "session_id": getattr(state, "session_id", None),
        "url": getattr(state, "current_url", None),
        "step": getattr(state, "step_counter", None),
        "action_count": getattr(state, "action_count", None),
    }
    rec.update(fields)
    _append(state, "clicks.jsonl", rec)
