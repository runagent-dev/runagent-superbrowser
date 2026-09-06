"""Critical State Durability (CSD).

For a task-critical item introduced at step *i* and needed again at step *j*,
CSD is 1 when the exact (normalised) string is still present in the live
context the acting agent saw at *j*, else 0; aggregated over items, then
tasks. Two item families are scored separately because they behave very
differently:

* **task-given** items (hand-reviewed ``critical_state.json``): the values in
  the instruction. Every policy protects the initial task message, so this
  family is a FLOOR check — it catches a policy that mangles the prompt but
  is not expected to separate the arms. Reuse point = the last iteration.
* **observation-derived** items: values that first appear in a *tool result*
  at step *i* (URLs, prices, ids, dates, capitalised names) and are later
  REUSED in an action argument or in the final answer at step *j > i*. This is
  the browser analogue of LRE's "access token issued at login, needed at the
  call": what the agent had to carry across the eviction horizon.

Presence is checked in the live-context dump (``live_context.jsonl.gz``; the
row whose timestamp precedes the reuse step, or the last row for the final
answer). Matching is case-insensitive, whitespace-normalised, and tolerant of
thousands separators / currency symbols for numeric values.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from eval.core.loaders import load_context_dump, load_steps, load_transcripts
from eval.core.records import RunRecord

_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
_MONEY_RE = re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d+)?")
_NUM_RE = re.compile(r"(?<![\d,.])\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|(?<![\d,.])\d{3,}(?:\.\d+)?\b")
_ID_RE = re.compile(r"\b[A-Z0-9]{2,}[-_][A-Z0-9-]{3,}\b|\b[a-f0-9]{8,}\b|"
                    r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{8,}\b")
_DATE_RE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?\b|"
                      r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b", re.I)
_NAME_RE = re.compile(r"\b[A-Z][a-zA-Z0-9&'\-]+(?:\s+[A-Z][a-zA-Z0-9&'\-]+){1,3}\b")
_SKIP = {"session state", "session_state", "cached vision", "vision", "elements", "dead ends"}


def normalise(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[$€£]", "", t)
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)   # thousands separators
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join((b.get("text") or "") if isinstance(b, dict) else str(b) for b in content)
    return "" if content is None else str(content)


def candidate_values(text: str) -> set[str]:
    vals: set[str] = set()
    for rx in (_URL_RE, _MONEY_RE, _DATE_RE, _ID_RE, _NUM_RE):
        for m in rx.finditer(text):
            v = m.group(0).strip().rstrip(".,;:")
            if len(v) >= 3:
                vals.add(v)
    for m in _NAME_RE.finditer(text):
        v = m.group(0).strip()
        if v.lower() not in _SKIP and len(v) <= 60:
            vals.add(v)
    return vals


def _iteration_rows(dump: list[dict[str, Any]]) -> list[tuple[float, str]]:
    rows = []
    for r in dump:
        text = "\n".join(_text_of(m.get("text")) for m in r.get("messages") or [])
        rows.append((float(r.get("ts") or 0), normalise(text)))
    return rows


def _context_before(rows: list[tuple[float, str]], ts: float | None) -> str | None:
    if not rows:
        return None
    if ts is None:
        return rows[-1][1]
    chosen = None
    for t, text in rows:
        if t <= ts:
            chosen = text
        else:
            break
    return chosen if chosen is not None else rows[0][1]


def score_items(items: list[str], context: str | None) -> list[dict[str, Any]]:
    out = []
    for it in items:
        n = normalise(it)
        present = (n in context) if (context is not None and n) else None
        out.append({"item": it, "present": present})
    return out


def observation_events(steps: list[dict[str, Any]], transcripts: list[dict[str, Any]], final_answer: str,
                       instruction: str, *, max_events: int = 60) -> list[dict[str, Any]]:
    """(item, introduced_step, reuse_step|None(final)) for values first seen in a
    tool result and reused later in an action argument or the final answer."""
    task_norm = normalise(instruction)
    first_seen: dict[str, int] = {}
    events: list[dict[str, Any]] = []
    # values introduced by results
    for i, s in enumerate(steps):
        for v in candidate_values(str(s.get("result") or "")):
            key = normalise(v)
            if key and key not in first_seen and key not in task_norm:
                first_seen[key] = i
    # reuse in a later action argument
    for j, s in enumerate(steps):
        args_norm = normalise(str(s.get("args") or ""))
        if not args_norm:
            continue
        for key, i in first_seen.items():
            if j > i and key in args_norm and len(key) >= 4:
                events.append({"item": key, "introduced_step": i, "reuse_step": j, "reuse": "action"})
    # reuse in the final answer
    fa = normalise(final_answer or "")
    for key, i in first_seen.items():
        if key in fa and len(key) >= 4 and i < len(steps) - 1:
            events.append({"item": key, "introduced_step": i, "reuse_step": None, "reuse": "final_answer"})
    # dedupe (item, reuse_step) and cap
    seen = set()
    uniq = []
    for e in events:
        k = (e["item"], e["reuse_step"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq[:max_events]


def compute(run_dir: Path, rec: RunRecord) -> dict[str, Any]:
    spec = {}
    try:
        spec = json.loads((Path(run_dir) / "spec.json").read_text())
    except Exception:
        pass
    task = spec.get("task", {})
    items = list(task.get("critical_state") or [])
    dump = load_context_dump(run_dir)
    rows = _iteration_rows(dump)
    steps = load_steps(run_dir)
    final_answer = ""
    try:
        final_answer = (Path(run_dir) / "result.txt").read_text(encoding="utf-8")
    except Exception:
        pass
    out: dict[str, Any] = {"has_context_dump": bool(rows), "n_iterations_dumped": len(rows)}
    # task-given (floor check at the terminal iteration)
    terminal = _context_before(rows, None)
    tg = score_items(items, terminal)
    scored = [x for x in tg if x["present"] is not None]
    out["task_given_n"] = len(items)
    out["task_given_scored"] = len(scored)
    out["csd_task_given"] = (sum(1 for x in scored if x["present"]) / len(scored)) if scored else None
    out["task_given_lost"] = [x["item"] for x in scored if not x["present"]]
    # observation-derived
    events = observation_events(steps, load_transcripts(run_dir), final_answer, task.get("instruction", ""))
    ev_scored = []
    for e in events:
        ts = steps[e["reuse_step"]].get("timestamp") if e["reuse_step"] is not None else None
        ctx = _context_before(rows, ts)
        present = (e["item"] in ctx) if ctx is not None else None
        ev_scored.append({**e, "present": present})
    valid = [e for e in ev_scored if e["present"] is not None]
    out["observed_events"] = len(events)
    out["observed_scored"] = len(valid)
    out["csd_observed"] = (sum(1 for e in valid if e["present"]) / len(valid)) if valid else None
    out["observed_lost"] = [{"item": e["item"], "introduced_step": e["introduced_step"], "reuse_step": e["reuse_step"]}
                            for e in valid if not e["present"]][:20]
    return out
