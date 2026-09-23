"""T1: 344 production-judge calls. 86 trajectories x 4 conditions.

Budget line: $14.62 at $0.0425 per call. Abort if measured dollars exceed 1.5x.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAPER = Path("/Users/aritramazumder/Downloads/Superbrowser___ICLR_2027")
PROMPT = ROOT / "judgeval/prompts/webjudge_production.txt"
LOG = ROOT / "judgeval/logs/t1_calls.jsonl"
OUT = ROOT / "judgeval/artifacts/t1/verdicts.json"
EXPECTED_PREFIX = "c3093dba"
RATE = 0.0425
N_EXPECTED = 344
CAP = RATE * N_EXPECTED * 1.5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _vals() -> dict:
    from dotenv import dotenv_values
    return {k: v for k, v in dotenv_values(ROOT / ".env").items() if v}


def _expand(vals: dict, *names: str) -> str:
    for name in names:
        raw = (os.environ.get(name) or vals.get(name) or "").strip()
        if raw.startswith("${") and raw.endswith("}"):
            inner = raw[2:-1]
            raw = (os.environ.get(inner) or vals.get(inner) or "").strip()
        if raw and not raw.startswith("${"):
            return raw
    return ""


def _stimuli() -> list[dict]:
    sys.modules.setdefault("scipy", types.ModuleType("scipy"))
    sys.modules.setdefault("scipy.stats", types.ModuleType("scipy.stats"))
    sys.path.insert(0, str(PAPER / "diagnostic"))
    import task_b_experiment as tb
    by_id = {t.task_id: t.instruction for t in tb.get_corpus()}
    rows = []
    for s in tb.generate_stimuli_dataset():
        rows.append({
            "stimulus_id": s.stimulus_id,
            "task_id": s.task_id,
            "harness": s.harness,
            "condition": s.condition,
            "y_star": bool(s.is_ground_truth_reached),
            "instruction": by_id[s.task_id],
            "report": s.report_text,
        })
    if len(rows) != N_EXPECTED:
        raise SystemExit(f"expected {N_EXPECTED} stimuli, got {len(rows)}")
    return rows


def _messages(system: str, row: dict) -> list[dict]:
    user = (
        f"User Task: {row['instruction']}\n\n"
        "Key Points: not supplied. No screenshots and no action history are attached. "
        "Score the final report against the task.\n\n"
        f"Final report:\n{row['report']}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _status(text: str) -> str:
    low = (text or "").lower()
    if 'status: "success"' in low or "status: success" in low:
        return "success"
    if 'status: "failure"' in low or "status: failure" in low:
        return "failure"
    return "unparseable"


def main() -> None:
    vals = _vals()
    system = PROMPT.read_text()
    digest = hashlib.sha256(system.encode()).hexdigest()
    if not digest.startswith(EXPECTED_PREFIX):
        raise SystemExit(f"prompt hash {digest} does not start with {EXPECTED_PREFIX}")
    key = _expand(vals, "SUPERBROWSER_EVAL_WEBJUDGE_API_KEY", "OPENAI_API_KEY")
    base = _expand(vals, "SUPERBROWSER_EVAL_WEBJUDGE_BASE_URL") or None
    if not key:
        raise SystemExit("no webjudge credential")
    rows = _stimuli()
    done_ids = set()
    if LOG.exists():
        for line in LOG.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("kind") == "judge" and rec.get("stimulus_id"):
                done_ids.add(rec["stimulus_id"])
    pending = [r for r in rows if r["stimulus_id"] not in done_ids]
    est = RATE * len(pending)
    spent_est = RATE * len(done_ids)
    print(f"T1 estimate for remaining calls: ${est:.2f} ({len(pending)} x ${RATE})")
    print(f"T1 running total before this batch: ${spent_est:.2f} of $50.00 ceiling")
    print(f"T1 line after this batch if every call is billed at the planning rate: ${spent_est + est:.2f}")
    print(f"Abort line: ${CAP:.2f}")
    if spent_est + est > CAP + 1e-9:
        raise SystemExit("remaining batch would exceed 1.5x the T1 line")
    from openai import OpenAI
    client = OpenAI(api_key=key, timeout=120, base_url=base) if base else OpenAI(api_key=key, timeout=120)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    measured = 0.0

    def one(row: dict) -> dict:
        messages = _messages(system, row)
        resp = client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=messages,
            temperature=0,
            max_completion_tokens=1024,
        )
        content = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        usage_d = None
        if usage is not None:
            usage_d = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        rec = {
            "kind": "judge",
            "phase": "T1",
            "stimulus_id": row["stimulus_id"],
            "task_id": row["task_id"],
            "harness": row["harness"],
            "condition": row["condition"],
            "y_star": row["y_star"],
            "request": messages,
            "response": content,
            "model_id": "gpt-5.4-mini",
            "model_returned": getattr(resp, "model", None),
            "timestamp": _now(),
            "temperature": 0,
            "prompt_sha256": digest,
            "usage": usage_d,
            "cost_usd": None,
            "cost_usd_planned": RATE,
            "deviation": ["NO_IMAGES", "NO_ACTION_HISTORY", "NO_KEY_POINTS"],
            "status": _status(content),
        }
        with lock:
            with LOG.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        return rec

    results = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(one, row): row for row in pending}
        n = 0
        for fut in as_completed(futs):
            n += 1
            rec = fut.result()
            results.append(rec)
            measured += RATE
            if n % 20 == 0 or n == len(pending):
                print(f"T1 {n}/{len(pending)} planned_running ${spent_est + measured:.2f}", flush=True)
            if spent_est + measured > CAP:
                raise SystemExit("abort: T1 exceeded 1.5x its line")
    # reload full log for the summary
    verdicts = []
    for line in LOG.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("kind") == "judge" and rec.get("phase") == "T1":
            verdicts.append({k: rec[k] for k in (
                "stimulus_id", "task_id", "harness", "condition", "y_star", "status", "model_id")})
    OUT.write_text(json.dumps(verdicts, indent=2) + "\n")
    print(f"wrote {OUT} n={len(verdicts)}")


if __name__ == "__main__":
    main()
