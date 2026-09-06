"""RunRecord — the one machine-readable row per run.

A RunRecord is written as ``run_record.json`` inside the run directory and
appended as one line to the experiment's ``results.jsonl``. Everything the
paper reports is recomputed from these rows (E0 audits them; E1–E12
aggregate them), never from a spreadsheet.

The record is a plain dict with a fixed top-level shape so it stays
readable without this module::

    schema_version, ids, protocol, timing, outcome, counts, tokens, cost,
    metrics, artifacts

Field meanings are documented in ``eval/PROTOCOL.md``. ``metrics`` is filled
by the analyzers (P4) and may be empty right after a run.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

STOP_REASONS = ("ok", "timeout", "error", "max_iterations", "handoff", "cancelled")
FAILURE_REASONS = ("bot_block", "captcha_unsolved", "site_unavailable", "geo_blocked",
                   "grounding", "loop", "premature_done", "api_error", "timeout", "other")


@dataclass
class RunRecord:
    ids: dict[str, Any]
    protocol: dict[str, Any]
    timing: dict[str, Any] = field(default_factory=dict)
    outcome: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, Any] = field(default_factory=dict)
    tokens: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    # ---------------------------------------------------------------- helpers
    @property
    def run_id(self) -> str:
        return str(self.ids.get("run_id", ""))

    @property
    def success(self) -> bool | None:
        return self.outcome.get("success")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: d[k] for k in ("schema_version", "ids", "protocol", "timing", "outcome",
                                  "counts", "tokens", "cost", "metrics", "artifacts")}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunRecord":
        return cls(
            ids=dict(d.get("ids", {})),
            protocol=dict(d.get("protocol", {})),
            timing=dict(d.get("timing", {})),
            outcome=dict(d.get("outcome", {})),
            counts=dict(d.get("counts", {})),
            tokens=dict(d.get("tokens", {})),
            cost=dict(d.get("cost", {})),
            metrics=dict(d.get("metrics", {})),
            artifacts=dict(d.get("artifacts", {})),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )

    # -------------------------------------------------------------------- io
    def write(self, run_dir: Path) -> Path:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "run_record.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, default=str, ensure_ascii=False))
        tmp.replace(path)
        return path

    @classmethod
    def read(cls, run_dir: Path) -> "RunRecord":
        return cls.from_dict(json.loads((Path(run_dir) / "run_record.json").read_text()))


def make_run_id(experiment: str, arm: str, task_id: str, seed: int) -> str:
    return f"{experiment}:{arm}:{task_id}:s{seed}"


def run_dir_for(runs_root: Path, experiment: str, arm: str, task_id: str, seed: int) -> Path:
    return Path(runs_root) / experiment / arm / task_id / f"seed{seed}"


# ---------------------------------------------------------------- results.jsonl
def results_path(runs_root: Path, experiment: str) -> Path:
    return Path(runs_root) / experiment / "results.jsonl"


def append_result(path: Path, record: RunRecord) -> None:
    """Append one row; an existing row with the same run_id is superseded
    (readers keep the LAST occurrence) so re-runs never leave duplicates in
    the aggregate."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = record.to_dict()
    row["_written_at"] = time.time()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")


def read_results(path: Path) -> list[RunRecord]:
    """All rows, de-duplicated by run_id (last write wins), stable order."""
    path = Path(path)
    if not path.exists():
        return []
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rid = str(row.get("ids", {}).get("run_id"))
            if rid not in by_id:
                order.append(rid)
            by_id[rid] = row
    return [RunRecord.from_dict(by_id[r]) for r in order]


def iter_run_dirs(runs_root: Path, experiment: str) -> Iterator[Path]:
    """Every run directory of an experiment that holds a run_record.json."""
    base = Path(runs_root) / experiment
    if not base.exists():
        return
    for p in sorted(base.glob("*/*/seed*/run_record.json")):
        yield p.parent


def rebuild_results(runs_root: Path, experiment: str) -> Path:
    """Regenerate results.jsonl from the per-run records (source of truth)."""
    out = results_path(runs_root, experiment)
    tmp = out.with_suffix(".jsonl.tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as f:
        for d in iter_run_dirs(runs_root, experiment):
            rec = RunRecord.read(d)
            row = rec.to_dict()
            row["_written_at"] = time.time()
            f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
    tmp.replace(out)
    return out
