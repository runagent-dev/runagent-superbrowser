"""Run directories -> tables for the analyzers (pandas where convenient)."""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Iterable

from eval._bootstrap import REPO_ROOT
from eval.core.records import RunRecord, iter_run_dirs, read_results, results_path

DEFAULT_RUNS_ROOT = REPO_ROOT / "eval" / "runs"


def jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as f:  # type: ignore[arg-type]
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except (OSError, EOFError):
        pass
    return out


def load_records(experiment: str, *, runs_root: Path = DEFAULT_RUNS_ROOT, prefer_dirs: bool = True) -> list[RunRecord]:
    """Records of an experiment. ``prefer_dirs`` rebuilds from run_record.json
    files (source of truth); otherwise reads results.jsonl."""
    if prefer_dirs:
        recs = [RunRecord.read(d) for d in iter_run_dirs(runs_root, experiment)]
        if recs:
            return recs
    return read_results(results_path(runs_root, experiment))


def load_many(experiments: Iterable[str], *, runs_root: Path = DEFAULT_RUNS_ROOT) -> list[RunRecord]:
    out: list[RunRecord] = []
    for e in experiments:
        out.extend(load_records(e, runs_root=runs_root))
    return out


def run_dir_of(rec: RunRecord) -> Path:
    return Path(rec.ids["run_dir"])


def worker_dirs(run_dir: Path) -> list[Path]:
    """Ledger dirs of the worker(s) (everything under ledgers/ that is not the orchestrator)."""
    dirs = [d for d in sorted((Path(run_dir) / "ledgers").glob("*")) if d.is_dir() and not d.name.startswith("orch-")]
    return dirs


def load_steps(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in worker_dirs(run_dir):
        for r in jsonl(d / "steps.jsonl"):
            r["_worker"] = d.name
            rows.append(r)
    rows.sort(key=lambda r: r.get("timestamp") or 0)
    return rows


def load_events(run_dir: Path, *, include_orchestrator: bool = True) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in sorted((Path(run_dir) / "ledgers").glob("*")):
        if not d.is_dir():
            continue
        if not include_orchestrator and d.name.startswith("orch-"):
            continue
        for r in jsonl(d / "events.jsonl"):
            r["_worker"] = d.name
            rows.append(r)
    rows.sort(key=lambda r: r.get("ts") or 0)
    return rows


def load_vision_calls(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in worker_dirs(run_dir):
        rows.extend(jsonl(d / "vision_calls.jsonl"))
    rows.sort(key=lambda r: r.get("ts") or 0)
    return rows


def load_clicks(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in worker_dirs(run_dir):
        rows.extend(jsonl(d / "clicks.jsonl"))
    rows.sort(key=lambda r: r.get("ts") or 0)
    return rows


def load_context_dump(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in worker_dirs(run_dir):
        rows.extend(jsonl(d / "live_context.jsonl.gz"))
    rows.sort(key=lambda r: (r.get("ts") or 0, r.get("iter") or 0))
    return rows


def load_transcripts(run_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted((Path(run_dir) / "workers").glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def load_ledger(run_dir: Path) -> dict[str, Any]:
    for d in worker_dirs(run_dir):
        p = d / "ledger.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                continue
    return {}


def records_frame(records: list[RunRecord]):
    """Flat pandas DataFrame with the columns the analyzers group by."""
    import pandas as pd

    rows = []
    for r in records:
        rows.append({
            "run_id": r.run_id, "experiment": r.ids.get("experiment"), "arm": r.ids.get("arm"),
            "task_id": r.ids.get("task_id"), "seed": r.ids.get("seed"), "level": r.ids.get("level"),
            "website": r.ids.get("website"), "model": r.protocol.get("model"),
            "success": r.outcome.get("success"), "decided_by": r.outcome.get("decided_by"),
            "stop_reason": r.outcome.get("stop_reason"), "failure_reason": r.outcome.get("failure_reason"),
            "exclusion_label": r.outcome.get("exclusion_label"),
            "worker_iterations": r.counts.get("worker_iterations"),
            "orchestrator_iterations": r.counts.get("orchestrator_iterations"),
            "tool_calls": r.counts.get("tool_calls_executed"), "vision_calls": r.counts.get("vision_calls"),
            "vision_cache_hits": r.counts.get("vision_cache_hits"), "compressor_calls": r.counts.get("compressor_calls"),
            "input_tokens": r.tokens.get("input_tokens"), "output_tokens": r.tokens.get("output_tokens"),
            "cache_read_tokens": r.tokens.get("cache_read_tokens"),
            "prompt_mean": r.tokens.get("prompt_tokens_per_iter_mean"), "prompt_peak": r.tokens.get("prompt_tokens_per_iter_peak"),
            "ctx_mean": r.tokens.get("context_est_after_mean"), "ctx_peak": r.tokens.get("context_est_after_peak"),
            "wall_s": r.timing.get("wall_s"), "usd": (r.cost or {}).get("usd"), "usd_cached": (r.cost or {}).get("usd_cached"),
            "started_at": r.timing.get("started_at"),
            **{f"m_{k}_{kk}": vv for k, v in (r.metrics or {}).items() if isinstance(v, dict)
               for kk, vv in v.items() if isinstance(vv, (int, float, bool)) or vv is None},
        })
    return pd.DataFrame(rows)
