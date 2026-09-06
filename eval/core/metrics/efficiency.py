"""Efficiency metrics (packed from the record; one place for the definitions)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from eval.core.records import RunRecord


def compute(run_dir: Path, rec: RunRecord) -> dict[str, Any]:
    c, t, cost = rec.counts or {}, rec.tokens or {}, rec.cost or {}
    calls = c.get("llm_calls_by_role") or {}
    return {
        "worker_iterations": c.get("worker_iterations"),
        "orchestrator_iterations": c.get("orchestrator_iterations"),
        "llm_calls": sum(int(v) for v in calls.values()) if calls else None,
        "tool_calls": c.get("tool_calls_executed"),
        "tool_calls_attempted": c.get("tool_calls_attempted"),
        "vision_calls": c.get("vision_calls"),
        "vision_cache_hits": c.get("vision_cache_hits"),
        "compressor_calls": c.get("compressor_calls"),
        "screenshots": c.get("screenshots"),
        "input_tokens": t.get("input_tokens"), "output_tokens": t.get("output_tokens"),
        "cache_read_tokens": t.get("cache_read_tokens"), "vision_tokens": t.get("vision_tokens"),
        "prompt_tokens_per_iter_mean": t.get("prompt_tokens_per_iter_mean"),
        "prompt_tokens_per_iter_peak": t.get("prompt_tokens_per_iter_peak"),
        "context_est_after_mean": t.get("context_est_after_mean"),
        "context_est_after_peak": t.get("context_est_after_peak"),
        "wall_s": (rec.timing or {}).get("wall_s"),
        "usd": cost.get("usd"), "usd_cached": cost.get("usd_cached"), "usd_judge": cost.get("usd_judge"),
    }
