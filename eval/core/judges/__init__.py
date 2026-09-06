"""Success evaluators.

* ``webjudge``      — Python port of Online-Mind2Web's WebJudge (3-step,
  screenshot-based). PRIMARY automatic evaluator; prompts are verbatim.
* ``answer_judge``  — text-only final-answer judge (the legacy oracle),
  SECONDARY; cheap, no screenshots.
* ``deterministic`` — per-task URL / answer regex checks from
  ``eval/benchmarks/checks.json``; treated as ground truth where defined.

``judge_run`` runs any subset on a finished run directory and writes
``judges/<name>.json``; ``primary_success`` combines them per the protocol
(deterministic if defined, else WebJudge, else answer judge).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Iterable

from eval.core.tasks import Task

from .base import Verdict, load_transcripts, read_final_answer

JUDGE_NAMES = ("deterministic", "webjudge", "answer_judge")


def _write(run_dir: Path, name: str, verdict: Verdict) -> None:
    d = Path(run_dir) / "judges"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(verdict.to_dict(), indent=2, ensure_ascii=False, default=str))


def read_verdicts(run_dir: Path) -> dict[str, Verdict]:
    out: dict[str, Verdict] = {}
    d = Path(run_dir) / "judges"
    if d.exists():
        for p in d.glob("*.json"):
            try:
                out[p.stem] = Verdict.from_dict(json.loads(p.read_text()))
            except Exception:
                continue
    return out


async def judge_run_async(run_dir: Path, task: Task, *, which: Iterable[str] = JUDGE_NAMES,
                          client: Any = None, force: bool = False) -> dict[str, Verdict]:
    """Run the requested judges on ``run_dir`` (skipping ones already stored
    unless ``force``). ``client`` injects an OpenAI-compatible client (tests)."""
    from . import answer_judge, deterministic, webjudge

    run_dir = Path(run_dir)
    have = {} if force else read_verdicts(run_dir)
    final_answer = read_final_answer(run_dir)
    transcripts = load_transcripts(run_dir)
    out: dict[str, Verdict] = dict(have)
    for name in which:
        if name in have and have[name].success is not None:
            continue
        if name == "deterministic":
            v = deterministic.judge(task, run_dir, final_answer=final_answer, transcripts=transcripts)
        elif name == "webjudge":
            v = await webjudge.judge(task, run_dir, transcripts=transcripts, client=client)
        elif name == "answer_judge":
            v = await answer_judge.judge(task, final_answer, client=client)
        else:
            raise KeyError(f"unknown judge {name!r}")
        _write(run_dir, name, v)
        out[name] = v
    return out


def judge_run(run_dir: Path, task: Task, **kw: Any) -> dict[str, Verdict]:
    return asyncio.run(judge_run_async(run_dir, task, **kw))


def primary_success(verdicts: dict[str, Verdict]) -> tuple[bool | None, str]:
    """Protocol rule: deterministic (if it produced a decision) > webjudge > answer_judge."""
    for name in ("deterministic", "webjudge", "answer_judge"):
        v = verdicts.get(name)
        if v is not None and v.success is not None:
            return bool(v.success), name
    return None, "none"
