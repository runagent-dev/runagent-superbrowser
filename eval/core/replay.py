"""Adapt legacy §7.4 model-split runs into the new RunRecord shape (offline).

The model-split harness (``eval/experiments/modelsplit``) left runs under
``eval/runs/<model-label>/<task>/seed<k>/`` with the OLD ``meta.json`` schema
and no ``spec.json``. This module COPIES each such run into
``eval/runs/<experiment>/<label>/<task>/seed<k>/`` and writes a ``spec.json`` +
a normalised ``meta.json`` into the copy so the current ``harvest``/metrics/
analysis stack can ingest it. The original run directories are never touched
(the legacy analyzer in ``experiments/modelsplit`` keeps reading them).

It is the P6 dress rehearsal: it exercises harvest + every process metric +
the analyzers on REAL recorded transcripts/ledgers without a browser or an LLM
(the recorded judge verdict is reused as a stubbed ``webjudge``).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from eval.core.harvest import build_record
from eval.core.records import RunRecord, append_result, read_results, results_path
from eval.core.tasks import load_benchmark

_CUSTOM_PATH = Path(__file__).parents[1] / "benchmarks" / "custom_dev.jsonl"
_CUSTOM = {t.task_id: t for t in load_benchmark("custom_dev", annotate=False)} if _CUSTOM_PATH.exists() else {}


def is_legacy_run(d: Path) -> bool:
    """A §7.4 model-split run dir: old meta.json carrying a 'label', no spec.json."""
    if not (d / "workers").exists() or not (d / "meta.json").exists() or (d / "spec.json").exists():
        return False
    try:
        return "label" in json.loads((d / "meta.json").read_text())
    except Exception:
        return False


def copy_run(src: Path, runs_root: Path, experiment: str) -> Path:
    """Copy a legacy run into the experiment tree (idempotent: re-copied fresh)."""
    meta = json.loads((src / "meta.json").read_text())
    dest = Path(runs_root) / experiment / str(meta.get("label", "modelsplit")) / str(meta.get("task_id")) / f"seed{int(meta.get('seed') or 0)}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    (dest / "source_run_dir").write_text(str(src.resolve()) + "\n")
    return dest


def adapt_run(d: Path, *, experiment: str) -> Path:
    """Write spec.json + normalised meta.json INTO A COPY produced by copy_run."""
    src_meta = d / "meta.orig.json" if (d / "meta.orig.json").exists() else d / "meta.json"
    meta = json.loads(src_meta.read_text())
    task_id = meta.get("task_id")
    label = meta.get("label", "modelsplit")
    seed = int(meta.get("seed") or 0)
    ct = _CUSTOM.get(task_id)
    spec = {
        "run_id": f"{experiment}:{label}:{task_id}:s{seed}", "experiment": experiment, "seed": seed,
        "arm": {"name": label, "env": {"model": (meta.get("model") or {}).get("model")}, "side": "python",
                "family": "modelsplit", "description": f"legacy §7.4 run ({label})"},
        "task": {"task_id": task_id, "benchmark": "custom_dev", "level": "custom",
                 "website": meta.get("url"), "start_url": meta.get("url"),
                 "instruction": meta.get("instruction") or (ct.instruction if ct else ""),
                 "reference": ct.reference if ct else None, "critical_state": [], "checks": []},
        "benchmark": "custom_dev", "run_dir": str(d), "model": (meta.get("model") or {}).get("model"),
        "topology": "orchestrator", "protocol": {"hash": "legacy", "note": "adapted from a §7.4 model-split run"},
        "nanobot_overrides": {}, "internal_timeout_s": None, "_legacy": True,
    }
    (d / "spec.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False))
    if not (d / "meta.orig.json").exists():
        shutil.copy2(d / "meta.json", d / "meta.orig.json")
    norm = {
        "run_id": spec["run_id"], "experiment": experiment, "arm": label, "task_id": task_id, "seed": seed,
        "topology": "orchestrator", "orch_task_id": meta.get("orch_task_id"),
        "role_task_ids": [meta.get("orch_task_id")] + list(meta.get("worker_ids") or []),
        "stop_reason": meta.get("stop_reason"), "error": meta.get("error"),
        "started_at": (meta.get("timestamp") or 0) - (meta.get("duration_sec") or 0), "ended_at": meta.get("timestamp"),
        "duration_s": meta.get("duration_sec"), "final_answer": meta.get("final_answer") or "",
        "raw_content": meta.get("final_answer") or "", "framed_task": meta.get("instruction") or "",
        "arm_env": {}, "effective_defaults": {"model": (meta.get("model") or {}).get("model"),
                                              "provider": (meta.get("model") or {}).get("provider")},
        "n_screenshots": 0, "environment": {"git_sha": meta.get("harness_git_sha"), "vision_model": meta.get("vision_model")},
        "_legacy_judge": meta.get("judge"),
    }
    (d / "meta.json").write_text(json.dumps(norm, indent=2, ensure_ascii=False))
    j = meta.get("judge") or {}
    if j.get("success") is not None:
        (d / "judges").mkdir(exist_ok=True)
        (d / "judges" / "webjudge.json").write_text(json.dumps(
            {"judge": "webjudge", "success": bool(j["success"]), "rationale": "replayed from legacy §7.4 judge",
             "model": j.get("judge_model"), "details": {"replayed": True}, "usage": {}}, indent=2))
    return d


def replay_experiment(runs_root: Path, *, source_labels: list[str] | None = None,
                      experiment: str = "modelsplit_replay") -> int:
    """Copy every legacy run under runs_root into ``<runs_root>/<experiment>/``,
    adapt the copies, and (re)build their records + results.jsonl. Originals
    are read-only inputs; re-running rebuilds the copies from scratch."""
    n = 0
    results = results_path(runs_root, experiment)
    if results.exists():
        results.unlink()
    legacy = []
    for meta_path in sorted(Path(runs_root).glob("*/*/seed*/meta.json")):
        d = meta_path.parent
        if d.parents[1].name == experiment or not is_legacy_run(d):
            continue
        if source_labels and json.loads(meta_path.read_text()).get("label") not in source_labels:
            continue
        legacy.append(d)
    for src in legacy:
        dest = copy_run(src, Path(runs_root), experiment)
        adapt_run(dest, experiment=experiment)
        rec = build_record(dest)
        rec.write(dest)
        append_result(results, rec)
        n += 1
    (Path(runs_root) / experiment).mkdir(parents=True, exist_ok=True)
    return n


def load_replayed(runs_root: Path, experiment: str = "modelsplit_replay") -> list[RunRecord]:
    return read_results(results_path(runs_root, experiment))
