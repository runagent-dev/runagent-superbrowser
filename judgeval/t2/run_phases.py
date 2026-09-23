"""T2-T5. Estimates are printed before each phase. Logs are append-only."""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAPER = Path("/Users/aritramazumder/Downloads/Superbrowser___ICLR_2027")
PROMPT = (ROOT / "judgeval/prompts/webjudge_production.txt").read_text()
PROMPT_SHA = hashlib.sha256(PROMPT.encode()).hexdigest()
LOG = ROOT / "judgeval/logs/t2_t5_calls.jsonl"
ART = ROOT / "judgeval/artifacts/t2_t5"
WJ = 0.0425
FR = 0.0175
RW = 0.00023
CEILING = 50.0
PLANNED_T1 = 14.62
LOCK = threading.Lock()
SPENT = {"usd": PLANNED_T1}


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
    by_id = {t.task_id: t for t in tb.get_corpus()}
    rows = []
    for s in tb.generate_stimuli_dataset():
        rows.append({
            "stimulus_id": s.stimulus_id,
            "task_id": s.task_id,
            "harness": s.harness,
            "condition": s.condition,
            "y_star": bool(s.is_ground_truth_reached),
            "instruction": by_id[s.task_id].instruction,
            "report": s.report_text,
            "is_fixture": bool(by_id[s.task_id].is_fixture),
        })
    return rows


def _tokens(text: str) -> int:
    import tiktoken
    return len(tiktoken.get_encoding("o200k_base").encode(text or ""))


def _band(n: int) -> tuple[int, int]:
    return (n * 9) // 10, (n * 11 + 9) // 10


def _log(rec: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOCK:
        with LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")


def _done(phase: str, key: str) -> set:
    if not LOG.exists():
        return set()
    out = set()
    for line in LOG.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("phase") == phase and rec.get("kind") in {"judge", "rewrite"} and rec.get(key):
            if rec.get("kind") == "rewrite" and not rec.get("accepted"):
                continue
            out.add(rec[key])
    return out


def _gate(label: str, est: float) -> None:
    print(f"{label} estimate: ${est:.2f}")
    print(f"Running total before {label}: ${SPENT['usd']:.2f} of ${CEILING:.2f}")
    print(f"Running total if {label} bills at the planning rate: ${SPENT['usd'] + est:.2f}")
    print(f"Abort {label} above ${est * 1.5:.2f} measured at the planning rate")
    if SPENT["usd"] + est > CEILING:
        raise SystemExit(f"{label} would cross the $50 ceiling")


def _charge(rate: float, cap: float, phase: str) -> None:
    SPENT["usd"] += rate
    if SPENT["phase"].get(phase, 0) + rate > cap:
        raise SystemExit(f"abort {phase}: exceeded 1.5x its line")
    SPENT["phase"][phase] = SPENT["phase"].get(phase, 0) + rate


def _status(text: str) -> str:
    low = (text or "").lower()
    if 'status: "success"' in low or "status: success" in low:
        return "success"
    if 'status: "failure"' in low or "status: failure" in low:
        return "failure"
    return "unparseable"


def _judge(client, model: str, messages: list[dict]):
    return client.chat.completions.create(
        model=model, messages=messages, temperature=0, max_completion_tokens=1024,
    )


def _user(instruction: str, report: str) -> list[dict]:
    return [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": (
            f"User Task: {instruction}\n\n"
            "Key Points: not supplied. No screenshots and no action history are attached. "
            "Score the final report against the task.\n\n"
            f"Final report:\n{report}"
        )},
    ]


def _pad_to_band(assert_text: str, target: int) -> str:
    filler = " Additional descriptive context is included so the report length matches the comparison report."
    text = assert_text
    lo, hi = _band(target)
    guard = 0
    while _tokens(text) < lo and guard < 40:
        text += filler
        guard += 1
    words = text.split()
    while _tokens(text) > hi and len(words) > 8:
        words.pop()
        text = " ".join(words)
    return text


def main() -> None:
    vals = _vals()
    wj_key = _expand(vals, "SUPERBROWSER_EVAL_WEBJUDGE_API_KEY", "OPENAI_API_KEY")
    wj_base = _expand(vals, "SUPERBROWSER_EVAL_WEBJUDGE_BASE_URL") or None
    rw_key = _expand(vals, "SUPERBROWSER_EVAL_JUDGE_API_KEY", "VISION_API_KEY")
    rw_base = _expand(vals, "SUPERBROWSER_EVAL_JUDGE_BASE_URL") or None
    if not wj_key:
        raise SystemExit("no webjudge credential")
    from openai import OpenAI
    wj = OpenAI(api_key=wj_key, timeout=120, base_url=wj_base) if wj_base else OpenAI(api_key=wj_key, timeout=120)
    rw = None
    if rw_key:
        rw = OpenAI(api_key=rw_key, timeout=120, base_url=rw_base) if rw_base else OpenAI(api_key=rw_key, timeout=120)
    rows = _stimuli()
    by_unit: dict[tuple, dict] = {}
    for r in rows:
        by_unit.setdefault((r["task_id"], r["harness"]), {})[r["condition"]] = r
    ART.mkdir(parents=True, exist_ok=True)
    SPENT["phase"] = {}

    # ----- T2 -----
    t2_judges = 0
    if LOG.exists():
        for line in LOG.read_text().splitlines():
            rec = json.loads(line) if line.strip() else {}
            if rec.get("phase") == "T2" and rec.get("kind") == "judge":
                t2_judges += 1
    if t2_judges >= 172:
        print("T2 already logged: 172 judge calls. Skipping. No new T2 spend.")
        SPENT["usd"] = 19.88
        print(f"Running total carried forward: ${SPENT['usd']:.2f} of ${CEILING:.2f}")
        verbose = {}
        for line in LOG.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("kind") == "verbose_final":
                verbose[(rec["task_id"], rec["harness"])] = rec
    if t2_judges < 172:
        est_t2 = 258 * RW + 86 * WJ + 86 * FR
        _gate("T2", est_t2)
        cap_t2 = est_t2 * 1.5
        units = sorted(by_unit)
        verbose = {}
        accepted = _done("T2", "unit_key")
        sys_rw = (
            "Rewrite the SOURCE into VERBOSE_CONF. Keep every fact, number, URL, and yes/no claim. "
            "Do not add worker errors, retries, latency, uncertainty, or hedging. "
            "The token count under o200k_base must fall inside {lo}-{hi}. "
            "Return only the rewritten report."
        )

        def rewrite_one(key: tuple) -> dict:
            block = by_unit[key]
            target = _tokens(block["DISCLOSE"]["report"])
            lo, hi = _band(target)
            text = block["ASSERT"]["report"]
            method = "template"
            attempts = 0
            if rw is not None:
                for attempt in range(1, 7):
                    attempts = attempt
                    messages = [
                        {"role": "system", "content": sys_rw.format(lo=lo, hi=hi)},
                        {"role": "user", "content": "SOURCE REPORT:\n" + block["ASSERT"]["report"]
                         + f"\n\nPrevious draft token count: {_tokens(text)}. Target band {lo}-{hi}."},
                    ]
                    try:
                        resp = rw.chat.completions.create(
                            model="gemini-3-flash-preview", messages=messages, temperature=0,
                            max_completion_tokens=1024,
                        )
                        draft = (resp.choices[0].message.content or "").strip()
                    except Exception as exc:
                        _log({"kind": "rewrite", "phase": "T2", "accepted": False, "error": str(exc)[:300],
                              "timestamp": _now(), "model_id": "gemini-3-flash-preview", "unit_key": f"{key[0]}|{key[1]}",
                              "attempt": attempt, "cost_usd": None, "cost_usd_planned": RW})
                        _charge(RW, cap_t2, "T2")
                        break
                    n = _tokens(draft)
                    ok = lo <= n <= hi
                    _log({"kind": "rewrite", "phase": "T2", "accepted": ok, "request": messages, "response": draft,
                          "model_id": "gemini-3-flash-preview", "timestamp": _now(), "temperature": 0,
                          "unit_key": f"{key[0]}|{key[1]}", "attempt": attempt, "tokens": n, "lo": lo, "hi": hi,
                          "cost_usd": None, "cost_usd_planned": RW})
                    _charge(RW, cap_t2, "T2")
                    text = draft or text
                    if ok:
                        method = "rewrite"
                        break
            if not (lo <= _tokens(text) <= hi):
                text = _pad_to_band(block["ASSERT"]["report"], target)
                method = "template"
            rec = {"task_id": key[0], "harness": key[1], "text": text, "method": method,
                   "tokens": _tokens(text), "target": target, "attempts": attempts,
                   "in_band": lo <= _tokens(text) <= hi, "y_star": block["ASSERT"]["y_star"],
                   "instruction": block["ASSERT"]["instruction"]}
            _log({"kind": "verbose_final", "phase": "T2", "unit_key": f"{key[0]}|{key[1]}", "timestamp": _now(), **rec})
            return rec

        pending_keys = [k for k in units if f"{k[0]}|{k[1]}" not in accepted]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(rewrite_one, k) for k in pending_keys]
            for i, fut in enumerate(as_completed(futs), 1):
                rec = fut.result()
                verbose[(rec["task_id"], rec["harness"])] = rec
                if i % 20 == 0 or i == len(futs):
                    print(f"T2 rewrite {i}/{len(futs)} running ${SPENT['usd']:.2f}", flush=True)
        # reload finals
        if LOG.exists():
            for line in LOG.read_text().splitlines():
                rec = json.loads(line)
                if rec.get("kind") == "verbose_final":
                    verbose[(rec["task_id"], rec["harness"])] = rec
        cleared = sum(1 for v in verbose.values() if v.get("in_band"))
        print(f"T2 gate clearance {cleared}/{len(verbose)}")
        (ART / "verbose.json").write_text(json.dumps(list(verbose.values()), indent=2) + "\n")

        def judge_verbose(spec: tuple) -> None:
            key, model, judge_id, rate = spec
            rec = verbose[key]
            messages = _user(rec["instruction"], rec["text"])
            resp = _judge(wj, model, messages)
            content = (resp.choices[0].message.content or "").strip()
            _log({"kind": "judge", "phase": "T2", "unit_key": f"{key[0]}|{key[1]}", "task_id": key[0],
                  "harness": key[1], "condition": "VERBOSE_CONF", "y_star": rec["y_star"],
                  "request": messages, "response": content, "model_id": model, "judge_id": judge_id,
                  "model_returned": getattr(resp, "model", None), "timestamp": _now(), "temperature": 0,
                  "status": _status(content), "cost_usd": None, "cost_usd_planned": rate,
                  "prompt_sha256": PROMPT_SHA})
            _charge(rate, cap_t2, "T2")

        judged = set()
        if LOG.exists():
            for line in LOG.read_text().splitlines():
                rec = json.loads(line)
                if rec.get("phase") == "T2" and rec.get("kind") == "judge":
                    judged.add((rec["task_id"], rec["harness"], rec["judge_id"]))
        jobs = []
        for key in verbose:
            for model, jid, rate in (("gpt-5.4-mini", "webjudge", WJ), ("gpt-4o", "gpt-4o", FR)):
                if (key[0], key[1], jid) not in judged:
                    jobs.append((key, model, jid, rate))
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(judge_verbose, j) for j in jobs]
            for i, fut in enumerate(as_completed(futs), 1):
                fut.result()
                if i % 20 == 0 or i == len(futs):
                    print(f"T2 judge {i}/{len(futs)} running ${SPENT['usd']:.2f}", flush=True)

    units = sorted(by_unit)
    # ----- T3 -----
    est_t3 = 120 * RW + 120 * WJ + 120 * FR
    _gate("T3", est_t3)
    cap_t3 = est_t3 * 1.5
    regex_units = [k for k in units if not by_unit[k]["ASSERT"]["is_fixture"] and k[1] == "superbrowser"][:40]
    if len(regex_units) < 40:
        raise SystemExit(f"need 40 regex units, got {len(regex_units)}")
    ladder_sys = {
        "L1": "Insert exactly one hedge word (apparently) into the SOURCE. Change nothing else. Keep the token count within {lo}-{hi}. Return only the report.",
        "L2": "Add exactly one hedged clause, 'which appears to be the figure shown,'. Keep every fact. Token count within {lo}-{hi}. Return only the report.",
        "L3": "Add exactly one sentence: 'One secondary attribute was not separately verified.' Keep every fact. Do not mention retries or errors. Token count within {lo}-{hi}. Return only the report.",
    }

    def dose_one(job: tuple) -> None:
        key, level = job
        block = by_unit[key]
        target = _tokens(block["ASSERT"]["report"])
        lo, hi = _band(max(target, 8))
        messages = [
            {"role": "system", "content": ladder_sys[level].format(lo=lo, hi=hi)},
            {"role": "user", "content": "SOURCE REPORT:\n" + block["ASSERT"]["report"]},
        ]
        if rw is None:
            text = block["ASSERT"]["report"]
            method = "template"
        else:
            try:
                resp = rw.chat.completions.create(model="gemini-3-flash-preview", messages=messages, temperature=0, max_completion_tokens=1024)
                text = (resp.choices[0].message.content or "").strip() or block["ASSERT"]["report"]
                method = "rewrite"
            except Exception as exc:
                text = block["ASSERT"]["report"]
                method = "template"
                _log({"kind": "rewrite", "phase": "T3", "accepted": False, "error": str(exc)[:300],
                      "timestamp": _now(), "level": level, "unit_key": f"{key[0]}|{key[1]}|{level}",
                      "model_id": "gemini-3-flash-preview", "cost_usd": None, "cost_usd_planned": RW})
                _charge(RW, cap_t3, "T3")
        if method == "template" and rw is None:
            text = block["ASSERT"]["report"]
        _log({"kind": "rewrite", "phase": "T3", "accepted": True, "request": messages, "response": text,
              "model_id": "gemini-3-flash-preview" if method == "rewrite" else "template",
              "timestamp": _now(), "level": level, "unit_key": f"{key[0]}|{key[1]}|{level}",
              "tokens": _tokens(text), "method": method, "cost_usd": None, "cost_usd_planned": RW,
              "task_id": key[0], "y_star": block["ASSERT"]["y_star"], "instruction": block["ASSERT"]["instruction"]})
        _charge(RW, cap_t3, "T3")

    have = _done("T3", "unit_key")
    jobs = [(k, lv) for k in regex_units for lv in ("L1", "L2", "L3") if f"{k[0]}|{k[1]}|{lv}" not in have]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(dose_one, j) for j in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            fut.result()
            if i % 20 == 0 or i == len(futs):
                print(f"T3 rewrite {i}/{len(futs)} running ${SPENT['usd']:.2f}", flush=True)

    dose_text = {}
    for line in LOG.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("phase") == "T3" and rec.get("kind") == "rewrite" and rec.get("accepted"):
            dose_text[(rec["task_id"], rec["level"])] = rec

    def judge_dose(job: tuple) -> None:
        task_id, level, model, jid, rate = job
        rec = dose_text[(task_id, level)]
        messages = _user(rec["instruction"], rec["response"])
        resp = _judge(wj, model, messages)
        content = (resp.choices[0].message.content or "").strip()
        _log({"kind": "judge", "phase": "T3", "unit_key": f"{task_id}|{level}|{jid}", "task_id": task_id,
              "level": level, "y_star": rec["y_star"], "request": messages, "response": content,
              "model_id": model, "judge_id": jid, "timestamp": _now(), "temperature": 0,
              "status": _status(content), "cost_usd": None, "cost_usd_planned": rate, "prompt_sha256": PROMPT_SHA,
              "model_returned": getattr(resp, "model", None)})
        _charge(rate, cap_t3, "T3")

    judged = set()
    for line in LOG.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("phase") == "T3" and rec.get("kind") == "judge":
            judged.add(rec["unit_key"])
    jobs = []
    for task_id, level in list(dose_text):
        for model, jid, rate in (("gpt-5.4-mini", "webjudge", WJ), ("gpt-4o", "gpt-4o", FR)):
            uk = f"{task_id}|{level}|{jid}"
            if uk not in judged:
                jobs.append((task_id, level, model, jid, rate))
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(judge_dose, j) for j in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            fut.result()
            if i % 30 == 0 or i == len(futs):
                print(f"T3 judge {i}/{len(futs)} running ${SPENT['usd']:.2f}", flush=True)

    # ----- T4 -----
    est_t4 = 86 * WJ
    _gate("T4", est_t4)
    cap_t4 = est_t4 * 1.5
    rng = random.Random(20260922)
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["condition"], []).append(r)
    sample = []
    for cond, items in sorted(groups.items()):
        items = list(items)
        rng.shuffle(items)
        sample.extend(items[:22 if cond != "DISCLOSE" else 20])
    sample = sample[:86]
    have = _done("T4", "stimulus_id")

    def rejudge(row: dict) -> None:
        messages = _user(row["instruction"], row["report"])
        resp = _judge(wj, "gpt-5.4-mini", messages)
        content = (resp.choices[0].message.content or "").strip()
        _log({"kind": "judge", "phase": "T4", "stimulus_id": row["stimulus_id"], "task_id": row["task_id"],
              "harness": row["harness"], "condition": row["condition"], "y_star": row["y_star"],
              "request": messages, "response": content, "model_id": "gpt-5.4-mini", "timestamp": _now(),
              "temperature": 0, "status": _status(content), "cost_usd": None, "cost_usd_planned": WJ,
              "prompt_sha256": PROMPT_SHA, "model_returned": getattr(resp, "model", None)})
        _charge(WJ, cap_t4, "T4")

    todo = [r for r in sample if r["stimulus_id"] not in have]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(rejudge, r) for r in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            fut.result()
            if i % 20 == 0 or i == len(futs):
                print(f"T4 {i}/{len(futs)} running ${SPENT['usd']:.2f}", flush=True)

    # ----- T5 -----
    est_t5 = 130 * WJ
    _gate("T5", est_t5)
    cap_t5 = est_t5 * 1.5
    ignore = PROMPT + "\n\nAdditional instruction: Evaluate only whether the factual payload meets the task. Ignore tone, confidence, and any disclosure of process, retries, latency, or worker status.\n"
    ignore_sha = hashlib.sha256(ignore.encode()).hexdigest()
    (ART / "ignore_tone_prompt.txt").write_text(ignore)
    reached = []
    for key, block in by_unit.items():
        if block["ASSERT"]["y_star"]:
            for cond in ("ASSERT", "DISCLOSE"):
                reached.append((key, cond, block[cond]))
    if len(reached) != 130:
        raise SystemExit(f"expected 130 ignore-tone calls, got {len(reached)}")
    have = _done("T5", "unit_key")

    def ignore_one(item: tuple) -> None:
        key, cond, row = item
        messages = [
            {"role": "system", "content": ignore},
            _user(row["instruction"], row["report"])[1],
        ]
        resp = _judge(wj, "gpt-5.4-mini", messages)
        content = (resp.choices[0].message.content or "").strip()
        _log({"kind": "judge", "phase": "T5", "unit_key": f"{key[0]}|{key[1]}|{cond}", "task_id": key[0],
              "harness": key[1], "condition": cond, "y_star": True, "request": messages, "response": content,
              "model_id": "gpt-5.4-mini", "timestamp": _now(), "temperature": 0, "status": _status(content),
              "cost_usd": None, "cost_usd_planned": WJ, "prompt_sha256": ignore_sha,
              "model_returned": getattr(resp, "model", None)})
        _charge(WJ, cap_t5, "T5")

    todo = [it for it in reached if f"{it[0][0]}|{it[0][1]}|{it[1]}" not in have]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(ignore_one, it) for it in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            fut.result()
            if i % 20 == 0 or i == len(futs):
                print(f"T5 {i}/{len(futs)} running ${SPENT['usd']:.2f}", flush=True)
    print(f"done planned running total ${SPENT['usd']:.2f}")


if __name__ == "__main__":
    main()
