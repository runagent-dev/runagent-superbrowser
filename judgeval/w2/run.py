"""Execute the tagged W2 plan. Missing credentials and missing prompts are recorded, not imputed."""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path

from eval.core.judges.webjudge import STEP1_KEY_POINTS_SYSTEM, STEP3_OUTCOME_SYSTEM, parse_status
from eval.core.stats import mcnemar_exact

from .claims import claim_diff
from .decide import holm, in_band, length_band, narrative_read, token_count
from .planlock import (
    instructions,
    plan_format_ids,
    plan_stimulus_sha,
    plan_task_ids,
    verify_plan_frozen,
)
from .sample import stratified_quarter
from .stimuli import parse_stimuli, stimulus_sha256

SEED = 20260922
REWRITER_MODEL = "gemini-3-flash-preview"
PRIMARY_CONDITIONS = ("ASSERT", "DISCLOSE", "VERBOSE_CONF")
LOG_REL = Path("judgeval/logs/w2_calls.jsonl")
ART = Path("judgeval/artifacts/w2")
_KEY_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{10,}|AQ\.[A-Za-z0-9_\-]{10,}|Bearer\s+\S+)")
_STATUS_WORD = re.compile(r"Status:\s*\"?(success|failure)\"?", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scrub(obj):
    if isinstance(obj, str):
        return _KEY_RE.sub("[redacted]", obj)
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    return obj


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(_scrub(record), ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def _expand(vals: dict, *names: str) -> str:
    for name in names:
        raw = (os.environ.get(name) or vals.get(name) or "").strip()
        if raw.startswith("${") and raw.endswith("}"):
            inner = raw[2:-1]
            raw = (os.environ.get(inner) or vals.get(inner) or "").strip()
        if raw:
            return raw
    return ""


def _dotenv(root: Path) -> dict:
    try:
        from dotenv import dotenv_values
        return {k: v for k, v in dotenv_values(root / ".env").items() if v}
    except Exception:
        return {}


def write_prompts(root: Path) -> dict[str, str]:
    dest = {
        "judgeval/prompts/webjudge_production.txt": STEP3_OUTCOME_SYSTEM,
        "judgeval/prompts/webjudge_step1.txt": STEP1_KEY_POINTS_SYSTEM,
    }
    hashes = {}
    for rel, text in dest.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text() != text:
            raise SystemExit(f"{rel} is not byte-identical to the production prompt")
        if not path.exists():
            path.write_text(text)
        hashes[rel] = hashlib.sha256(text.encode()).hexdigest()
    return hashes


def fill_instruction(template: str, target: int | None) -> str:
    if "{target}" not in template:
        return template
    if target is None:
        raise ValueError("length target required")
    lo, hi = length_band(target)
    return (template.replace("{target}", str(target))
            .replace("{lo}", str(lo))
            .replace("{hi}", str(hi)))


def rewriter_messages(instruction: str, source: str, failure: dict | None = None) -> list[dict]:
    system = instruction
    if failure:
        lines = ["", "The previous rewrite failed the gate.", "Missing claims:"]
        lines.extend(f"- {c}" for c in (failure.get("missing") or ["(none)"]))
        lines.append("Added claims:")
        lines.extend(f"- {c}" for c in (failure.get("added") or ["(none)"]))
        if failure.get("token_count") is not None:
            lines.append(
                f"Previous token count: {failure['token_count']}. "
                f"Required band: {failure['lo']}-{failure['hi']}."
            )
        system = instruction + "\n" + "\n".join(lines)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "SOURCE REPORT:\n" + source},
    ]


def judge_messages(system: str, instruction: str, key_points: str, condition_text: str) -> list[dict]:
    user = (
        f"User Task: {instruction}\n\n"
        f"Key Points: {key_points}\n\n"
        f"Action History:\n"
        f"1. {condition_text}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def status_word(text: str) -> str | None:
    m = _STATUS_WORD.search(text or "")
    if not m:
        return None
    word = m.group(1).lower()
    if bool(parse_status(text)) != (word == "success"):
        return None
    return word


def shuffled_triples(triples: list[tuple], seed: int = SEED) -> list[tuple]:
    rows = list(triples)
    random.Random(seed).shuffle(rows)
    return rows


def _chat(client, model: str, messages: list[dict], *, allow_temperature_retry: bool):
    """One completion. Seed is dropped on rejection. Temperature is dropped only for judges."""
    kwargs: dict = {"model": model, "messages": messages, "temperature": 0, "seed": SEED}
    seed_sent = True
    retry = False
    try:
        return client.chat.completions.create(**kwargs), seed_sent, retry, kwargs
    except Exception as exc:
        msg = str(exc).lower()
        if "seed" in msg:
            seed_sent = False
            kwargs.pop("seed", None)
            try:
                return client.chat.completions.create(**kwargs), seed_sent, retry, kwargs
            except Exception as exc2:
                exc = exc2
                msg = str(exc).lower()
        if allow_temperature_retry and any(k in msg for k in ("temperature", "unsupported", "not supported")):
            retry = True
            kwargs.pop("temperature", None)
            kwargs.pop("seed", None)
            seed_sent = False
            return client.chat.completions.create(**kwargs), seed_sent, retry, kwargs
        raise


def _snapshot(resp) -> str:
    return "not_returned"


def _log_call(log_path: Path, *, kind: str, messages, content: str, model: str, resp,
              seed_sent: bool, retry: bool, extra: dict) -> None:
    append_jsonl(log_path, {
        "kind": kind,
        "request": messages,
        "response": content,
        "model_id": model,
        "model_returned": getattr(resp, "model", None) or "not_returned",
        "snapshot_date": _snapshot(resp),
        "system_fingerprint": getattr(resp, "system_fingerprint", None) or "not_returned",
        "temperature": 0,
        "seed": SEED,
        "seed_sent": seed_sent,
        "timestamp": _now(),
        "retry": retry,
        **extra,
    })


def _client(key: str, base: str | None):
    from openai import OpenAI
    kwargs = {"api_key": key}
    if base:
        kwargs["base_url"] = base
    return OpenAI(**kwargs)


def _auth_failure(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in ("api key", "unauthorized", "401", "403", "permission", "invalid_api_key"))


def run_rewrites(root: Path, units: list[dict], templates: dict[str, str], log_path: Path,
                 key: str, base: str) -> list[dict]:
    client = _client(key, base)
    format_ids = set(plan_format_ids(verify_plan_frozen(root)))
    rows: list[dict] = []

    def one(unit: dict, condition: str, template: str, target: int | None, failure: dict | None, attempt: int):
        instruction = fill_instruction(template, target)
        messages = rewriter_messages(instruction, unit["source_report"], failure)
        try:
            resp, seed_sent, retry, _ = _chat(client, REWRITER_MODEL, messages, allow_temperature_retry=False)
        except Exception as exc:
            append_jsonl(log_path, {
                "kind": "rewrite", "request": messages, "response": f"error: {exc}",
                "model_id": REWRITER_MODEL, "model_returned": "not_returned",
                "snapshot_date": "not_returned", "temperature": 0, "seed": SEED,
                "seed_sent": False, "timestamp": _now(), "retry": False,
                "unit_id": unit["unit_id"], "condition": condition, "attempt": attempt,
                "deviation": [], "error": True,
            })
            if _auth_failure(exc):
                raise SystemExit(f"rewriter credential rejected: {type(exc).__name__}")
            return None
        content = (resp.choices[0].message.content or "").strip()
        _log_call(log_path, kind="rewrite", messages=messages, content=content, model=REWRITER_MODEL,
                  resp=resp, seed_sent=seed_sent, retry=retry,
                  extra={"unit_id": unit["unit_id"], "condition": condition, "attempt": attempt,
                         "deviation": [], "source_arm": unit["source_arm"], "task_id": unit["task_id"]})
        return content

    for unit in units:
        produced: dict[str, str] = {}
        for condition in ("ASSERT", "DISCLOSE"):
            text = one(unit, condition, templates[condition], None, None, 0)
            if text is None:
                rows.append({**_public_unit(unit), "condition": condition, "text": None, "error": True})
                continue
            diff = claim_diff(unit["source_report"], text)
            if not diff["equal"]:
                repaired = one(unit, condition, templates[condition], None, diff, 1)
                if repaired is not None:
                    text = repaired
            produced[condition] = text
            rows.append({**_public_unit(unit), "condition": condition, "text": text, "error": False})
        disclose = produced.get("DISCLOSE")
        if disclose is None:
            rows.append({**_public_unit(unit), "condition": "VERBOSE_CONF", "text": None,
                         "error": True, "reason": "no_disclose_target"})
        else:
            disclose_diff = claim_diff(unit["source_report"], disclose)
            if not disclose_diff["equal"]:
                rows.append({**_public_unit(unit), "condition": "VERBOSE_CONF", "text": None,
                             "error": True, "reason": "no_disclose_target"})
            else:
                target = token_count(disclose)
                text = one(unit, "VERBOSE_CONF", templates["VERBOSE_CONF"], target, None, 0)
                if text is not None:
                    diff = claim_diff(unit["source_report"], text)
                    n = token_count(text)
                    lo, hi = length_band(target)
                    if not diff["equal"] or not (lo <= n <= hi):
                        failure = dict(diff)
                        failure.update({"token_count": n, "lo": lo, "hi": hi})
                        repaired = one(unit, "VERBOSE_CONF", templates["VERBOSE_CONF"], target, failure, 1)
                        if repaired is not None:
                            text = repaired
                rows.append({**_public_unit(unit), "condition": "VERBOSE_CONF", "text": text,
                             "error": text is None, "length_target": target})
        if unit["task_id"] not in format_ids:
            continue
        prose_target = token_count(unit["source_report"])
        prose = one(unit, "FORMAT_PROSE", templates["FORMAT_PROSE"], prose_target, None, 0)
        if prose is not None:
            diff = claim_diff(unit["source_report"], prose)
            n = token_count(prose)
            lo, hi = length_band(prose_target)
            if not diff["equal"] or not (lo <= n <= hi):
                failure = dict(diff)
                failure.update({"token_count": n, "lo": lo, "hi": hi})
                repaired = one(unit, "FORMAT_PROSE", templates["FORMAT_PROSE"], prose_target, failure, 1)
                if repaired is not None:
                    prose = repaired
        rows.append({**_public_unit(unit), "condition": "FORMAT_PROSE", "text": prose, "error": prose is None})
        prose_ok = False
        if prose is not None:
            prose_diff = claim_diff(unit["source_report"], prose)
            prose_ok = prose_diff["equal"] and in_band(token_count(prose), prose_target)
        if not prose_ok:
            rows.append({**_public_unit(unit), "condition": "FORMAT_BULLETS", "text": None,
                         "error": True, "reason": "no_prose_target"})
            continue
        bullet_target = token_count(prose)
        bullets = one(unit, "FORMAT_BULLETS", templates["FORMAT_BULLETS"], bullet_target, None, 0)
        if bullets is not None:
            diff = claim_diff(unit["source_report"], bullets)
            n = token_count(bullets)
            lo, hi = length_band(bullet_target)
            if not diff["equal"] or not (lo <= n <= hi):
                failure = dict(diff)
                failure.update({"token_count": n, "lo": lo, "hi": hi})
                repaired = one(unit, "FORMAT_BULLETS", templates["FORMAT_BULLETS"], bullet_target, failure, 1)
                if repaired is not None:
                    bullets = repaired
        rows.append({**_public_unit(unit), "condition": "FORMAT_BULLETS", "text": bullets, "error": bullets is None})
    return rows


def _public_unit(unit: dict) -> dict:
    return {"unit_id": unit["unit_id"], "task_id": unit["task_id"],
            "source_arm": unit["source_arm"], "instruction": unit["instruction"],
            "source_report": unit["source_report"]}


def gate_rows(rows: list[dict]) -> tuple[list[dict], dict]:
    """Apply the claim gate and the length gate. Returns per-row gate records and exclusion counts."""
    by_unit: dict[str, dict[str, dict]] = {}
    for row in rows:
        by_unit.setdefault(row["unit_id"], {})[row["condition"]] = row
    gated = []
    counts = {"claim_gate_fail": 0, "length_gate_fail": 0, "rewriter_error": 0, "no_target": 0}
    for unit_id, conds in by_unit.items():
        source = next(iter(conds.values()))["source_report"]
        disclose = conds.get("DISCLOSE") or {}
        disclose_tokens = token_count(disclose["text"]) if disclose.get("text") else None
        prose = conds.get("FORMAT_PROSE") or {}
        prose_tokens = token_count(prose["text"]) if prose.get("text") else None
        for condition, row in conds.items():
            record = {
                "unit_id": unit_id, "condition": condition, "task_id": row["task_id"],
                "source_arm": row["source_arm"], "passed_claims": False, "passed_length": True,
                "exclude": True, "reason": None, "tokens": None, "characters": None,
            }
            text = row.get("text")
            if row.get("reason") in ("no_disclose_target", "no_prose_target") or text is None:
                record["reason"] = row.get("reason") or "rewriter_error"
                counts["no_target" if record["reason"] != "rewriter_error" else "rewriter_error"] += 1
                gated.append(record)
                continue
            diff = claim_diff(source, text)
            n = token_count(text)
            record["tokens"] = n
            record["characters"] = len(text)
            record["missing"] = diff["missing"]
            record["added"] = diff["added"]
            record["passed_claims"] = diff["equal"]
            if condition == "VERBOSE_CONF":
                record["passed_length"] = bool(disclose_tokens is not None and in_band(n, disclose_tokens))
                record["length_target"] = disclose_tokens
            elif condition == "FORMAT_PROSE":
                record["passed_length"] = in_band(n, token_count(source))
                record["length_target"] = token_count(source)
            elif condition == "FORMAT_BULLETS":
                record["passed_length"] = bool(prose_tokens is not None and in_band(n, prose_tokens))
                record["length_target"] = prose_tokens
                if prose_tokens is not None and record["passed_length"]:
                    longer = max(n, prose_tokens)
                    record["passed_length"] = abs(n - prose_tokens) <= 0.10 * longer
            if not record["passed_claims"]:
                record["reason"] = "claim_gate_fail"
                counts["claim_gate_fail"] += 1
            elif not record["passed_length"]:
                record["reason"] = "length_gate_fail"
                counts["length_gate_fail"] += 1
            else:
                record["exclude"] = False
                record["reason"] = None
            gated.append(record)
    return gated, counts


def human_sheet(rows: list[dict], gated: list[dict]) -> tuple[list[dict], list[dict]]:
    emitted = []
    gate_by = {(g["unit_id"], g["condition"]): g for g in gated}
    for row in rows:
        if not row.get("text"):
            continue
        g = gate_by[(row["unit_id"], row["condition"])]
        emitted.append({
            "unit_id": row["unit_id"],
            "source_arm": row["source_arm"],
            "condition": row["condition"],
            "source_report": row["source_report"],
            "rewrite": row["text"],
            "missing": g.get("missing") or [],
            "added": g.get("added") or [],
        })
    sample = stratified_quarter(emitted)
    sheet, mapping = [], []
    for i, item in enumerate(sample, start=1):
        sid = f"S-{i:04d}"
        mapping.append({"sample_id": sid, "unit_id": item["unit_id"], "condition": item["condition"],
                        "source_arm": item["source_arm"]})
        sheet.append({
            "sample_id": sid,
            "source_report": item["source_report"],
            "rewrite": item["rewrite"],
            "automatic_missing": item["missing"],
            "automatic_added": item["added"],
            "claims_identical": "",
            "entity_agreement": "",
        })
    return sheet, mapping


def contrasts(verdicts: list[dict]) -> dict:
    """Paired contrasts and the narrative read. verdicts hold parseable success bools only."""
    by_judge: dict[str, dict[tuple, dict[str, bool]]] = {}
    for v in verdicts:
        if v.get("status") not in ("success", "failure"):
            continue
        slot = by_judge.setdefault(v["judge_id"], {}).setdefault((v["task_id"], v["source_arm"]), {})
        slot[v["condition"]] = v["status"] == "success"
    out = {}
    for judge_id, units in by_judge.items():
        judge_out = {"contrasts": {}, "read": "indeterminate", "n_common": 0}
        pvalues = {}
        for left, right in (("VERBOSE_CONF", "ASSERT"), ("VERBOSE_CONF", "DISCLOSE"), ("DISCLOSE", "ASSERT")):
            paired = [(c[left], c[right]) for c in units.values() if left in c and right in c]
            if not paired:
                judge_out["contrasts"][f"{left}_minus_{right}"] = {"n": 0}
                continue
            a = [x for x, _ in paired]
            b = [y for _, y in paired]
            result = mcnemar_exact(a, b, seed=SEED, n_boot=10000)
            name = f"{left}_minus_{right}"
            judge_out["contrasts"][name] = result.to_dict()
            pvalues[name] = result.p_value
        if pvalues:
            adjusted = holm(pvalues)
            for name, p in adjusted.items():
                judge_out["contrasts"][name]["p_holm"] = p
        common = [c for c in units.values() if all(k in c for k in PRIMARY_CONDITIONS)]
        judge_out["n_common"] = len(common)
        if common:
            def rate(cond: str) -> float:
                return 100.0 * sum(1 for c in common if c[cond]) / len(common)
            judge_out["pass_pct"] = {c: rate(c) for c in PRIMARY_CONDITIONS}
            judge_out["read"] = narrative_read(
                judge_out["pass_pct"]["ASSERT"],
                judge_out["pass_pct"]["DISCLOSE"],
                judge_out["pass_pct"]["VERBOSE_CONF"],
            )
        prose_pairs = [(c["FORMAT_PROSE"], c["FORMAT_BULLETS"])
                       for c in units.values() if "FORMAT_PROSE" in c and "FORMAT_BULLETS" in c]
        if prose_pairs:
            result = mcnemar_exact([a for a, _ in prose_pairs], [b for _, b in prose_pairs],
                                   seed=SEED, n_boot=10000)
            judge_out["format"] = result.to_dict()
            judge_out["format"]["interval_covers_zero"] = result.diff_ci[0] <= 0 <= result.diff_ci[1]
            judge_out["format"]["conclusion"] = (
                "inconclusive" if judge_out["format"]["interval_covers_zero"] else "reported"
            )
        out[judge_id] = judge_out
    return out


def find_arm_dirs(root: Path, arm: str) -> list[str]:
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__"}
    hits = []
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        if Path(dirpath).name == arm:
            hits.append(str(Path(dirpath).relative_to(root)))
    return hits


def bubench_prompt_present(root: Path) -> bool:
    external = Path("/root/agentic-browser/harness-bench")
    if external.exists():
        return True
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".venv", "venv"}]
        for name in filenames:
            if "bubench" in name.lower() and name.endswith((".txt", ".md", ".py")):
                return True
    return False


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    plan = verify_plan_frozen(root)
    expected_sha = plan_stimulus_sha(plan)
    actual_sha = stimulus_sha256(root)
    if actual_sha != expected_sha:
        raise SystemExit("stimulus file hash differs from the frozen plan")
    prompt_hashes = write_prompts(root)
    units = parse_stimuli(root)
    if [u["task_id"] for u in units if u["source_arm"] == "ledger"] != plan_task_ids(plan):
        raise SystemExit("parsed task ids differ from the frozen plan")
    if len(units) != 24:
        raise SystemExit(f"expected 24 source reports, parsed {len(units)}")

    art = root / ART
    art.mkdir(parents=True, exist_ok=True)
    log_path = root / LOG_REL
    (art / "prompt_hashes.json").write_text(json.dumps(prompt_hashes, indent=2) + "\n")

    adaptive = find_arm_dirs(root, "adaptive87")
    summary = find_arm_dirs(root, "summary")
    (art / "w23_status.json").write_text(json.dumps({
        "label": "PROSPECTIVE",
        "h2_label": "RETROSPECTIVE",
        "adaptive87_run_dirs": adaptive,
        "summary_run_dirs": summary,
        "n_runs": 0 if not adaptive else None,
        "kappa_adaptive87": None,
        "kappa_summary": None,
        "reason": None if adaptive else "run directories absent; trajectories were not synthesized",
        "annotator_columns_filled": False,
    }, indent=2) + "\n")

    try:
        token_count("gate")
    except Exception as exc:
        raise SystemExit(f"o200k_base tokenizer failed to load: {exc}") from exc

    vals = _dotenv(root)
    rewriter_key = _expand(vals, "SUPERBROWSER_EVAL_JUDGE_API_KEY", "VISION_API_KEY")
    rewriter_base = _expand(vals, "SUPERBROWSER_EVAL_JUDGE_BASE_URL")
    templates = instructions(plan)

    if not rewriter_key:
        rows = []
        gated, counts = [], {"claim_gate_fail": 0, "length_gate_fail": 0,
                              "rewriter_error": 0, "no_target": 0, "rewriter": "not_run_no_credential"}
    else:
        rows = run_rewrites(root, units, templates, log_path, rewriter_key, rewriter_base)
        (art / "rewrites.json").write_text(json.dumps(rows, indent=2) + "\n")
        gated, counts = gate_rows(rows)
        sheet, mapping = human_sheet(rows, gated)
        (art / "content_sample_sheet.json").write_text(json.dumps(sheet, indent=2) + "\n")
        (art / "content_sample_mapping.json").write_text(json.dumps(mapping, indent=2) + "\n")
        counts["human_sample_n"] = len(sheet)
        counts["human_sample_labeled"] = 0
        counts["human_agreement"] = None

    exclusion = {"counts": counts, "rows": gated}
    excl_path = art / "exclusions.json"
    excl_path.write_text(json.dumps(exclusion, indent=2) + "\n")
    excl_hash = hashlib.sha256(excl_path.read_bytes()).hexdigest()
    append_jsonl(log_path, {"kind": "exclusion_freeze", "sha256": excl_hash, "timestamp": _now(),
                            "counts": counts})
    if hashlib.sha256(excl_path.read_bytes()).hexdigest() != excl_hash:
        raise SystemExit("exclusion file changed after it was hashed")

    judge_specs = [
        {"judge_id": "webjudge_gpt-5.4-mini", "model": "gpt-5.4-mini", "role": "primary",
         "key": _expand(vals, "SUPERBROWSER_EVAL_WEBJUDGE_API_KEY", "OPENAI_API_KEY"),
         "base": _expand(vals, "SUPERBROWSER_EVAL_WEBJUDGE_BASE_URL") or None},
        {"judge_id": "gpt-4o", "model": "gpt-4o", "role": "generalization",
         "key": _expand(vals, "OPENAI_API_KEY", "SUPERBROWSER_EVAL_WEBJUDGE_API_KEY"),
         "base": _expand(vals, "SUPERBROWSER_EVAL_WEBJUDGE_BASE_URL") or None},
        {"judge_id": "claude-3-5-sonnet", "model": "claude-3-5-sonnet", "role": "generalization",
         "key": _expand(vals, "ANTHROPIC_API_KEY"),
         "base": _expand(vals, "ANTHROPIC_BASE_URL") or None},
        {"judge_id": "gemini-1.5-pro", "model": "gemini-1.5-pro", "role": "generalization",
         "key": _expand(vals, "SUPERBROWSER_EVAL_JUDGE_API_KEY", "VISION_API_KEY"),
         "base": _expand(vals, "SUPERBROWSER_EVAL_JUDGE_BASE_URL") or None},
    ]
    arm_status = {
        "bubench_deepseek-v4.1-flash": "not_run_prompt_absent" if not bubench_prompt_present(root)
        else "not_run_prompt_unverified",
    }
    # An external tree existing is not the production prompt. Do not call deepseek on it
    # unless a prompt file inside this repository was found. The external path check above
    # returns True for the directory; require an in-repo file.
    if not any("bubench" in p.name.lower() for p in (root / "judgeval").rglob("*") if p.is_file()):
        arm_status["bubench_deepseek-v4.1-flash"] = "not_run_prompt_absent"

    passed = {(g["unit_id"], g["condition"]) for g in gated if not g["exclude"]}
    text_by = {(r["unit_id"], r["condition"]): r for r in rows}
    runnable = [s for s in judge_specs if s["key"]]
    for spec in judge_specs:
        if not spec["key"]:
            arm_status[spec["judge_id"]] = "not_run_no_credential"

    verdicts: list[dict] = []
    if runnable and passed:
        triples = []
        for spec in runnable:
            for row in rows:
                if (row["unit_id"], row["condition"]) in passed and row.get("text"):
                    triples.append((row["unit_id"], row["condition"], spec["judge_id"]))
        order = shuffled_triples(triples)
        append_jsonl(log_path, {"kind": "call_order", "seed": SEED,
                                "order": [list(t) for t in order], "timestamp": _now()})
        clients = {s["judge_id"]: _client(s["key"], s["base"]) for s in runnable}
        models = {s["judge_id"]: s["model"] for s in runnable}
        key_points: dict[tuple, str | None] = {}
        tasks = {}
        for row in rows:
            tasks[row["task_id"]] = row["instruction"]
        for spec in runnable:
            for task_id, instruction in sorted(tasks.items()):
                messages = [
                    {"role": "system", "content": STEP1_KEY_POINTS_SYSTEM},
                    {"role": "user", "content": f"Task: {instruction}"},
                ]
                try:
                    resp, seed_sent, retry, _ = _chat(clients[spec["judge_id"]], spec["model"], messages,
                                                       allow_temperature_retry=True)
                    content = (resp.choices[0].message.content or "").strip()
                    _log_call(log_path, kind="key_points", messages=messages, content=content,
                              model=spec["model"], resp=resp, seed_sent=seed_sent, retry=retry,
                              extra={"task_id": task_id, "judge_id": spec["judge_id"], "deviation": ["NO_IMAGES"]})
                    key_points[(spec["judge_id"], task_id)] = content
                except Exception as exc:
                    append_jsonl(log_path, {"kind": "key_points", "response": f"error: {exc}",
                                            "model_id": spec["model"], "timestamp": _now(), "retry": False,
                                            "task_id": task_id, "judge_id": spec["judge_id"], "error": True,
                                            "snapshot_date": "not_returned", "deviation": ["NO_IMAGES"]})
                    if _auth_failure(exc):
                        arm_status[spec["judge_id"]] = "not_run_no_credential"
                        key_points[(spec["judge_id"], task_id)] = None
                        break
                    key_points[(spec["judge_id"], task_id)] = None
        for unit_id, condition, judge_id in order:
            if arm_status.get(judge_id) == "not_run_no_credential":
                continue
            row = text_by[(unit_id, condition)]
            points = key_points.get((judge_id, row["task_id"]))
            if not points:
                verdicts.append({"judge_id": judge_id, "unit_id": unit_id, "condition": condition,
                                 "task_id": row["task_id"], "source_arm": row["source_arm"],
                                 "status": "unparseable", "reason": "no_key_points"})
                continue
            messages = judge_messages(STEP3_OUTCOME_SYSTEM, row["instruction"], points, row["text"])
            try:
                resp, seed_sent, retry, _ = _chat(clients[judge_id], models[judge_id], messages,
                                                   allow_temperature_retry=True)
                content = (resp.choices[0].message.content or "").strip()
                _log_call(log_path, kind="judge", messages=messages, content=content, model=models[judge_id],
                          resp=resp, seed_sent=seed_sent, retry=retry,
                          extra={"unit_id": unit_id, "condition": condition, "judge_id": judge_id,
                                 "task_id": row["task_id"], "deviation": ["NO_IMAGES"]})
                word = status_word(content)
                verdicts.append({"judge_id": judge_id, "unit_id": unit_id, "condition": condition,
                                 "task_id": row["task_id"], "source_arm": row["source_arm"],
                                 "status": word or "unparseable"})
            except Exception as exc:
                append_jsonl(log_path, {"kind": "judge", "response": f"error: {exc}", "model_id": models[judge_id],
                                        "timestamp": _now(), "retry": False, "unit_id": unit_id,
                                        "condition": condition, "judge_id": judge_id, "error": True,
                                        "snapshot_date": "not_returned", "deviation": ["NO_IMAGES"]})
                if _auth_failure(exc):
                    arm_status[judge_id] = "not_run_no_credential"
                    continue
                verdicts.append({"judge_id": judge_id, "unit_id": unit_id, "condition": condition,
                                 "task_id": row["task_id"], "source_arm": row["source_arm"],
                                 "status": "unparseable"})
        for spec in runnable:
            arm_status.setdefault(spec["judge_id"], "ran")

    unparseable = sum(1 for v in verdicts if v["status"] == "unparseable")
    counts["unparseable_judge_outputs"] = unparseable
    analysis = contrasts([v for v in verdicts if v["status"] in ("success", "failure")])
    headline = "not_run"
    primary = analysis.get("webjudge_gpt-5.4-mini")
    if primary:
        headline = primary["read"]
    results = {
        "label": "PROSPECTIVE",
        "h1_h2": "RETROSPECTIVE",
        "plan_tag": "w2-analysis-plan-2026-09-22",
        "rewriter_model": REWRITER_MODEL,
        "arm_status": arm_status,
        "exclusion_counts": counts,
        "exclusion_sha256": excl_hash,
        "headline_read": headline,
        "judges": analysis,
        "n_parseable_verdicts": sum(1 for v in verdicts if v["status"] in ("success", "failure")),
        "deviation": ["NO_IMAGES"],
        "two_unnamed_h1_conditions": "not_run_artifact_absent",
    }
    (art / "verdicts.json").write_text(json.dumps(verdicts, indent=2) + "\n")
    (art / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    # Refresh the frozen exclusion counts with the judge unparseable tally in a sibling
    # file. The hashed exclusion file is not rewritten.
    (art / "exclusion_counts_final.json").write_text(json.dumps({
        "exclusion_sha256": excl_hash, "counts": counts, "unparseable_judge_outputs": unparseable,
    }, indent=2) + "\n")
    print(json.dumps({"headline_read": headline, "arm_status": arm_status, "counts": counts}, indent=2))


if __name__ == "__main__":
    main()
