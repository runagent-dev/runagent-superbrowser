"""Process-level metrics computed from run directories (see PROTOCOL.md).

* ``cost``        — USD from usage by role and the dated price table
* ``rpr``         — Redundant Perception Rate from vision_calls.jsonl
* ``drr``         — Dead-End Revisit Rate + repeats from steps/events
* ``csd``         — Critical State Durability from the live-context dump
* ``grounding``   — first-path / recovery / grounding-error from clicks + tags
* ``efficiency``  — iterations, calls, tokens, wall clock (from the record)

``compute_all(run_dir, record)`` fills ``record.metrics`` in place and is what
the analyzers call; each module also exposes its own function for tests.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from eval.core.records import RunRecord


def compute_all(run_dir: Path, rec: RunRecord) -> dict[str, Any]:
    from . import csd, drr, efficiency, grounding, rpr

    out: dict[str, Any] = {}
    for name, fn in (("efficiency", efficiency.compute), ("rpr", rpr.compute), ("drr", drr.compute),
                     ("grounding", grounding.compute), ("csd", csd.compute)):
        try:
            out[name] = fn(Path(run_dir), rec)
        except Exception as exc:  # noqa: BLE001 - one metric must not sink the others
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    rec.metrics.update(out)
    return out
