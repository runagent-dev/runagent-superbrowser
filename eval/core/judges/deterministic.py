"""Deterministic success checks (ground truth where definable).

``eval/benchmarks/checks.json`` maps task_id -> {"checks": [...]} with check
objects of the form::

    {"type": "final_url_regex",  "pattern": "...", "flags": "i"}
    {"type": "answer_regex",     "pattern": "...", "flags": "i"}
    {"type": "answer_contains_all", "values": ["a", "b"]}
    {"type": "visited_url_regex", "pattern": "..."}   # any step url matches

All checks of a task must pass (conjunction). A task with no checks yields
``success=None`` so the LLM judges decide. These are conservative: they can
prove a filter/URL state was reached, not that a free-form answer is right.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .base import Verdict, final_url


def _flags(spec: dict[str, Any]) -> int:
    f = 0
    for ch in str(spec.get("flags", "")):
        if ch == "i":
            f |= re.IGNORECASE
        elif ch == "s":
            f |= re.DOTALL
        elif ch == "m":
            f |= re.MULTILINE
    return f


def _visited_urls(run_dir: Path) -> list[str]:
    urls: list[str] = []
    for p in sorted(Path(run_dir).glob("ledgers/*/steps.jsonl")):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    u = json.loads(line).get("url")
                    if u:
                        urls.append(str(u))
        except Exception:
            continue
    return urls


def evaluate_checks(checks: list[dict[str, Any]], *, final_answer: str, last_url: str | None,
                    visited: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for c in checks:
        kind = c.get("type")
        ok = False
        if kind == "final_url_regex":
            ok = bool(last_url) and re.search(c["pattern"], last_url or "", _flags(c)) is not None
        elif kind == "answer_regex":
            ok = re.search(c["pattern"], final_answer or "", _flags(c)) is not None
        elif kind == "answer_contains_all":
            low = (final_answer or "").lower()
            ok = all(str(v).lower() in low for v in c.get("values", []))
        elif kind == "visited_url_regex":
            rx = re.compile(c["pattern"], _flags(c))
            ok = any(rx.search(u) for u in visited)
        else:
            results.append({"check": c, "ok": False, "error": f"unknown check type {kind!r}"})
            continue
        results.append({"check": c, "ok": bool(ok)})
    return results


def judge(task: Any, run_dir: Path, *, final_answer: str, transcripts: list[dict[str, Any]]) -> Verdict:
    checks = list(getattr(task, "checks", None) or [])
    if not checks:
        return Verdict("deterministic", None, "no deterministic checks defined for this task")
    last_url = final_url(transcripts)
    visited = _visited_urls(run_dir)
    results = evaluate_checks(checks, final_answer=final_answer, last_url=last_url, visited=visited)
    ok = all(r["ok"] for r in results)
    failed = [r for r in results if not r["ok"]]
    return Verdict("deterministic", ok,
                   "all checks passed" if ok else f"{len(failed)}/{len(results)} checks failed",
                   details={"results": results, "final_url": last_url, "n_visited": len(visited)})
