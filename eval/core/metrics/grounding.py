"""Grounding / execution metrics.

* first-path execution success — click attempts that landed without any
  escalation, silent-failure or no-effect marker
* recovery success — escalations that landed / (escalations + silent)
* label-mismatch rate, snapper method distribution (from clicks.jsonl)
* grounding error — the run failed and its trajectory carries the
  label/element-mismatch signature (record.outcome.failure_reason == grounding)

Source: ``clicks.jsonl`` (SUPERBROWSER_TRACE_CLICKS); fallback: the tag
counts the harvest extracted from transcripts.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from eval.core.loaders import load_clicks
from eval.core.records import RunRecord


def from_clicks(clicks: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(clicks)
    escalated = sum(1 for c in clicks if c.get("escalated"))
    silent = sum(1 for c in clicks if c.get("silent"))
    first = sum(1 for c in clicks if not c.get("escalated") and not c.get("silent") and c.get("ok", True))
    mismatch = sum(1 for c in clicks if c.get("label_mismatch"))
    methods = Counter(str(c.get("method") or "unknown") for c in clicks)
    strategies = Counter(str(c.get("strategy") or "primary") for c in clicks)
    return {"source": "clicks.jsonl", "clicks": n,
            "first_path_success": (first / n) if n else None,
            "escalated": escalated, "silent": silent,
            "recovery_success": (escalated / (escalated + silent)) if (escalated + silent) else None,
            "label_mismatch_rate": (mismatch / n) if n else None,
            "methods": dict(methods), "strategies": dict(strategies),
            "snapped_rate": (sum(1 for c in clicks if c.get("snapped")) / n) if n else None}


def from_tags(rec: RunRecord) -> dict[str, Any]:
    tags = (rec.counts or {}).get("tags") or {}
    by_name = (rec.counts or {}).get("tool_calls_by_name") or {}
    n = sum(v for k, v in by_name.items() if k in ("browser_click", "browser_click_at", "browser_click_selector"))
    escalated = int(tags.get("click_escalated") or 0)
    silent = int(tags.get("click_silent") or 0)
    no_effect = int(tags.get("no_effect") or 0)
    first = max(0, n - escalated - silent - no_effect)
    return {"source": "tags", "clicks": n, "first_path_success": (first / n) if n else None,
            "escalated": escalated, "silent": silent,
            "recovery_success": (escalated / (escalated + silent)) if (escalated + silent) else None,
            "label_mismatch_rate": (int(tags.get("label_mismatch") or 0) / n) if n else None,
            "methods": {}, "strategies": {}, "snapped_rate": None}


def compute(run_dir: Path, rec: RunRecord) -> dict[str, Any]:
    clicks = load_clicks(run_dir)
    out = from_clicks(clicks) if clicks else from_tags(rec)
    out["grounding_error"] = (rec.outcome or {}).get("failure_reason") == "grounding"
    out["click_at_failed"] = int(((rec.counts or {}).get("tags") or {}).get("click_at_failed") or 0)
    return out
