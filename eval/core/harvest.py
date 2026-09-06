"""Turn a finished run directory into a RunRecord.

Reads only files produced by ``run_one.py`` and the bridge instrumentation
(``meta.json``, ``usage.json``, ``workers/*.json``, ``ledgers/<id>/*``,
``screenshots/index.jsonl``, ``judges/*.json``) and never the network, so a
record can be rebuilt at any time (``python -m eval.core.harvest <run_dir>``).
Process metrics (CSD/DRR/RPR/...) are added by the analyzers in
``eval/core/metrics``; this module fills ids, protocol, timing, outcome,
counts and tokens.
"""
from __future__ import annotations

import gzip
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

from eval.core.judges import primary_success, read_verdicts
from eval.core.judges.answer_judge import looks_like_api_error
from eval.core.records import RunRecord

# Tags emitted by the bridge into tool results (see docs: perception/action map).
TAGS = {
    "click_escalated": "[click_escalated",
    "click_silent": "[click_silent",
    "no_effect": "[no_effect:",
    "dead_click_blocked": "[dead_click_blocked]",
    "same_element_blocked": "[same_element_blocked",
    "click_loop_detected": "[click_loop_detected]",
    "stale_index": "[stale_index]",
    "click_at_failed": "[click_at_failed:",
    "label_mismatch": "label_mismatch=True",
    "element_mismatch": "element_mismatch",
    "vision_lag": "[vision_lag]",
    "verify_miss": "[VERIFY_MISS",
    "captcha_detected": "CAPTCHA DETECTED",
    "captcha_unsolved": "captcha_unsolved",
    "network_blocked": "NETWORK_BLOCKED",
    "cf_interstitial": "CF_INTERSTITIAL",
    "human_handoff_timeout": "human_handoff_timeout",
    "domain_pinned": "DOMAIN_PINNED",
    "dead_ends_here": "[DEAD_ENDS_HERE",
}
EVICTION_EVENTS = ("screenshot_evicted", "failures_collapsed", "element_list_collapsed",
                   "state_block_collapsed", "thinking_blocks_stripped", "messages_gutted",
                   "subgoal_compacted", "messages_archived", "summary_refreshed")
_GEO_RE = re.compile(r"not available in your (country|region)|unavailable in your region|geo.?block|"
                     r"access denied.*(country|region)|451\b", re.I)
_SITE_DOWN_RE = re.compile(r"\b50[234]\b|ERR_NAME_NOT_RESOLVED|ERR_CONNECTION|net::ERR_|\bDNS\b|"
                           r"site can.t be reached|took too long to respond", re.I)


# ----------------------------------------------------------------- readers
def _json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as f:  # type: ignore[arg-type]
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        return out
    return out


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join((b.get("text") or "") if isinstance(b, dict) else str(b) for b in content)
    return "" if content is None else str(content)


def iter_tool_results(transcripts: list[dict[str, Any]]) -> Iterator[tuple[str, str]]:
    """(tool_name, result_text) for every tool message, across workers."""
    for t in transcripts:
        msgs = t.get("messages") or []
        name_by_id: dict[str, str] = {}
        for m in msgs:
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if tc.get("id"):
                    name_by_id[tc["id"]] = fn.get("name") or tc.get("name") or "tool"
        for m in msgs:
            if m.get("role") == "tool":
                yield name_by_id.get(m.get("tool_call_id", ""), m.get("name") or "tool"), _text_of(m.get("content"))


def count_tags(transcripts: list[dict[str, Any]]) -> dict[str, int]:
    c: Counter[str] = Counter()
    for _name, text in iter_tool_results(transcripts):
        for key, needle in TAGS.items():
            if needle in text:
                c[key] += text.count(needle) if key in ("click_escalated", "click_silent", "no_effect") else 1
    return dict(c)


def attempted_tool_calls(transcripts: list[dict[str, Any]]) -> Counter[str]:
    c: Counter[str] = Counter()
    for t in transcripts:
        for m in t.get("messages") or []:
            if m.get("role") == "assistant":
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    c[fn.get("name") or tc.get("name") or "tool"] += 1
    return c


# ------------------------------------------------------------ classification
def classify_failure(*, success: bool | None, stop_reason: str | None, final_answer: str,
                     tags: dict[str, int], error: str | None) -> str | None:
    if success:
        return None
    text = (final_answer or "") + " " + (error or "")
    if looks_like_api_error(final_answer) or (error and "quota" in error.lower()):
        return "api_error"
    if stop_reason == "timeout":
        return "timeout"
    if tags.get("captcha_unsolved") or tags.get("human_handoff_timeout"):
        return "captcha_unsolved"
    if _GEO_RE.search(text):
        return "geo_blocked"
    if tags.get("network_blocked") or tags.get("cf_interstitial") or "bot" in text.lower()[:400]:
        return "bot_block"
    if _SITE_DOWN_RE.search(text):
        return "site_unavailable"
    loops = tags.get("dead_click_blocked", 0) + tags.get("same_element_blocked", 0) + tags.get("click_loop_detected", 0)
    if loops >= 3:
        return "loop"
    if tags.get("label_mismatch") or tags.get("element_mismatch") or tags.get("click_at_failed", 0) >= 3:
        return "grounding"
    if stop_reason == "ok" and success is False:
        return "premature_done"
    return "other" if success is False else None


def exclusion_label(failure_reason: str | None) -> str | None:
    return failure_reason if failure_reason in ("site_unavailable", "geo_blocked", "captcha_unsolved") else None


# ---------------------------------------------------------------------- build
def build_record(run_dir: Path) -> RunRecord:
    run_dir = Path(run_dir)
    spec = _json(run_dir / "spec.json", {}) or {}
    meta = _json(run_dir / "meta.json", {}) or {}
    usage = _json(run_dir / "usage.json", None)
    transcripts = [t for t in (_json(p, None) for p in sorted((run_dir / "workers").glob("*.json"))) if t]
    task = spec.get("task", {})
    arm = spec.get("arm", {})

    # ---- ledgers: events / steps per role id
    events_by_id: dict[str, list[dict[str, Any]]] = {}
    steps_by_id: dict[str, list[dict[str, Any]]] = {}
    vision_calls: list[dict[str, Any]] = []
    clicks: list[dict[str, Any]] = []
    for d in sorted((run_dir / "ledgers").glob("*")):
        if not d.is_dir():
            continue
        events_by_id[d.name] = _jsonl(d / "events.jsonl")
        steps_by_id[d.name] = _jsonl(d / "steps.jsonl")
        vision_calls += _jsonl(d / "vision_calls.jsonl")
        clicks += _jsonl(d / "clicks.jsonl")
    all_events = [e for evs in events_by_id.values() for e in evs]
    iter_events = [e for e in all_events if e.get("type") == "iteration"]
    orch_iters = [e for e in all_events if e.get("type") == "memory_after_iter" and e.get("role") == "orchestrator"]
    worker_after = [e for e in all_events if e.get("type") == "memory_after_iter" and e.get("role") == "worker"]
    tokens_in = [int(e.get("tokens_in") or 0) for e in (iter_events or worker_after)]
    tokens_in = [t for t in tokens_in if t > 0]
    ctx_after = [e for e in all_events if e.get("type") == "context_size"]

    steps = [s for ss in steps_by_id.values() for s in ss]
    tool_by_name = Counter(s.get("tool") or "tool" for s in steps)
    tags = count_tags(transcripts)
    attempted = attempted_tool_calls(transcripts)
    evictions = Counter(e.get("type") for e in all_events if e.get("type") in EVICTION_EVENTS)

    # ---- outcome
    verdicts = read_verdicts(run_dir)
    success, decided_by = primary_success(verdicts)
    final_answer = meta.get("final_answer") or ""
    if success is None and looks_like_api_error(final_answer):
        success, decided_by = False, "api_error"
    stop_reason = meta.get("stop_reason")
    failure_reason = classify_failure(success=success, stop_reason=stop_reason, final_answer=final_answer,
                                      tags=tags, error=meta.get("error"))

    # ---- tokens
    by_role = (usage or {}).get("by_role", {}) if usage else {}
    role_calls = {r: int(v.get("calls", 0) or 0) for r, v in by_role.items()}
    vision_meta = sum(int((t.get("meta") or {}).get("vision_calls") or 0) for t in transcripts)

    rec = RunRecord(
        ids={
            "run_id": spec.get("run_id") or meta.get("run_id"),
            "experiment": spec.get("experiment") or meta.get("experiment"),
            "arm": arm.get("name") or meta.get("arm"),
            "arm_family": arm.get("family"),
            "benchmark": task.get("benchmark") or spec.get("benchmark"),
            "task_id": task.get("task_id") or meta.get("task_id"),
            "level": task.get("level"),
            "website": task.get("website") or task.get("start_url"),
            "seed": spec.get("seed", meta.get("seed")),
            "run_dir": str(run_dir),
            "orch_task_id": meta.get("orch_task_id"),
            "role_task_ids": meta.get("role_task_ids", []),
        },
        protocol={
            **(spec.get("protocol") or {}),
            "arm_env": arm.get("env", {}),
            "topology": meta.get("topology") or spec.get("topology"),
            "model": (meta.get("effective_defaults") or {}).get("model") or spec.get("model"),
            "provider": (meta.get("effective_defaults") or {}).get("provider"),
            "effective_defaults": meta.get("effective_defaults"),
            "environment": meta.get("environment"),
        },
        timing={
            "started_at": meta.get("started_at"), "ended_at": meta.get("ended_at"),
            "wall_s": meta.get("duration_s"),
        },
        outcome={
            "success": success, "decided_by": decided_by,
            "success_webjudge": (verdicts.get("webjudge").success if verdicts.get("webjudge") else None),
            "success_answer_judge": (verdicts.get("answer_judge").success if verdicts.get("answer_judge") else None),
            "success_deterministic": (verdicts.get("deterministic").success if verdicts.get("deterministic") else None),
            "judge_models": {k: v.model for k, v in verdicts.items()},
            "stop_reason": stop_reason, "error": meta.get("error"),
            "failure_reason": failure_reason, "exclusion_label": exclusion_label(failure_reason),
            "api_error": looks_like_api_error(final_answer),
            "final_answer_chars": len(final_answer),
        },
        counts={
            "worker_iterations": len(iter_events) if iter_events else len(worker_after),
            "orchestrator_iterations": len(orch_iters),
            "llm_calls_by_role": role_calls,
            "tool_calls_executed": len(steps),
            "tool_calls_attempted": int(sum(attempted.values())),
            "tool_calls_by_name": dict(tool_by_name),
            "tool_calls_attempted_by_name": dict(attempted),
            "vision_calls": len(vision_calls) if vision_calls else vision_meta,
            "vision_calls_source": "vision_calls.jsonl" if vision_calls else "worker_meta",
            "vision_cache_hits": sum(1 for v in vision_calls if v.get("cached")),
            "screenshots": meta.get("n_screenshots", 0),
            "clicks_logged": len(clicks),
            "compressor_calls": role_calls.get("compressor", 0),
            "n_workers": len(transcripts),
            "tags": tags,
            "evictions": dict(evictions),
        },
        tokens={
            "by_role": by_role,
            "input_tokens": (usage or {}).get("input_tokens"),
            "output_tokens": (usage or {}).get("output_tokens"),
            "total_tokens": (usage or {}).get("total_tokens"),
            "cache_read_tokens": (usage or {}).get("cache_read_tokens"),
            "vision_tokens": (usage or {}).get("vision_tokens"),
            "prompt_tokens_per_iter_mean": (statistics.fmean(tokens_in) if tokens_in else None),
            "prompt_tokens_per_iter_peak": (max(tokens_in) if tokens_in else None),
            "prompt_tokens_per_iter_n": len(tokens_in),
            "context_est_after_mean": (statistics.fmean([e["est_after"] for e in ctx_after if e.get("est_after")])
                                       if any(e.get("est_after") for e in ctx_after) else None),
            "context_est_after_peak": (max(e["est_after"] for e in ctx_after if e.get("est_after"))
                                       if any(e.get("est_after") for e in ctx_after) else None),
            "judge_usage": {k: v.usage for k, v in verdicts.items() if v.usage},
        },
        artifacts={
            "workers": [p.name for p in sorted((run_dir / "workers").glob("*.json"))],
            "ledgers": sorted(events_by_id),
            "has_context_dump": any((run_dir / "ledgers" / i / "live_context.jsonl.gz").exists() for i in events_by_id),
            "has_vision_trace": bool(vision_calls),
            "has_click_trace": bool(clicks),
            "judges": sorted(verdicts),
        },
    )
    try:  # cost is optional until pricing exists (P4)
        from eval.core.metrics.cost import cost_of_record

        rec.cost = cost_of_record(rec)
    except Exception:
        rec.cost = {}
    return rec


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Rebuild run_record.json for run directories")
    ap.add_argument("run_dirs", nargs="+")
    args = ap.parse_args(argv)
    for d in args.run_dirs:
        rec = build_record(Path(d))
        rec.write(Path(d))
        print(f"{rec.run_id}: success={rec.success} stop={rec.outcome.get('stop_reason')} "
              f"iters={rec.counts.get('worker_iterations')} tools={rec.counts.get('tool_calls_executed')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
