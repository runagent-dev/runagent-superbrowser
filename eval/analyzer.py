"""Offline analyzer: worker transcripts -> per-call classes -> per-model metrics.

Reads eval/runs/<label>/<task>/seed<k>/{meta.json, workers/*.json}, classifies
every Worker tool attempt into one of nine mutually-exclusive outcome classes
(using BOTH the raw tool_call name/args checked against the worker's captured
live registry AND the result-string tags the system already emits), reconstructs
vision-epoch boundaries to score [Vn] index discipline, and writes:
    eval/results/per_call.csv   (one row per tool attempt)
    eval/results/per_model.csv  (one row per model/run-label)

Outcome classes (exhaustive, mutually exclusive):
  well_formed_declarative   browser_click_at (vision-grounded click), valid
  well_formed_procedural    browser_click / browser_click_selector, valid
  well_formed_other         any other valid registered tool call
  bad_tool_name             unregistered / hallucinated tool
  bad_arg_name              synonym/unknown arg, missing required, or malformed args
  prose_instead_of_call     terminal narration of an action instead of calling it
  stale_or_oob_vision_index vision-grounded call w/ bad/stale [Vn]
  dead_click_violation      click that hit a dead-click / no-effect guard
  no_effect_retry           non-click no-effect, or repeating an identical failing call

Metrics (schema_fidelity etc.) are defined in the README/plan; see _run_metrics.

Usage:  python -m eval.analyzer [--runs eval/runs] [--out eval/results]
"""
from __future__ import annotations

from . import _bootstrap  # noqa: F401
from . import models as model_registry
from ._bootstrap import REPO_ROOT

import argparse
import csv
import json
import re
import statistics
from collections import Counter
from pathlib import Path

# --- tool name groupings -----------------------------------------------------
DECLARATIVE_CLICK = {"browser_click_at"}
PROCEDURAL_CLICK = {"browser_click", "browser_click_selector"}
CLICK_VERBS = DECLARATIVE_CLICK | PROCEDURAL_CLICK
AUTO_INJECTED = {"session_id"}  # infra param; not a reasoning-model fidelity signal

# --- result-string tags the system already emits ----------------------------
_DEAD_TAGS = ("[same_element_blocked", "[dead_click_blocked]", "[click_loop_detected]")
_NOEFFECT = "[no_effect:"
_INDEXFAIL = "click_at_failed"  # [click_at_failed:bad_vision_index]/:no_vision/:no_vision_index

_VINDEX_RE = re.compile(r"\[V(\d+)\]")
_ACTION_INTENT_RE = re.compile(
    r"browser_[a-z_]+\s*\(|\b(i'?ll|i will|let me|i'm going to|next i)\b.{0,40}"
    r"\b(click|type|select|navigate|search|scroll|open)\b",
    re.I,
)

BUCKETS = [
    "well_formed_declarative",
    "well_formed_procedural",
    "well_formed_other",
    "bad_tool_name",
    "bad_arg_name",
    "prose_instead_of_call",
    "stale_or_oob_vision_index",
    "dead_click_violation",
    "no_effect_retry",
]
_SCHEMA_FAIL = {"bad_tool_name", "bad_arg_name", "prose_instead_of_call"}


# --- transcript parsing ------------------------------------------------------
def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            (b.get("text") or b.get("content") or "") if isinstance(b, dict) else str(b)
            for b in content
        )
    return "" if content is None else str(content)


def _registry_from_schemas(tool_schemas) -> dict:
    reg = {}
    for s in tool_schemas or []:
        if not isinstance(s, dict):
            continue
        fn = s.get("function") if isinstance(s.get("function"), dict) else s
        name = fn.get("name")
        params = fn.get("parameters")
        if isinstance(name, str):
            reg[name] = params if isinstance(params, dict) else {}
    return reg


def _parse_args(raw):
    """Return (args_dict_or_None, parsed_ok)."""
    if isinstance(raw, dict):
        return raw, True
    if raw is None or raw == "":
        return {}, True
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
            return (obj, True) if isinstance(obj, dict) else (None, False)
        except Exception:
            return None, False
    return None, False


def _validate_args(name, args, registry):
    """Return (schema_ok, subtag). Drift = unknown/synonym arg, missing required
    (excluding session_id), malformed args, or a clear scalar type error."""
    if name not in registry:
        return False, "bad_tool_name"
    if args is None:
        return False, "malformed_json"
    schema = registry.get(name) or {}
    props = schema.get("properties") or {}
    required = [r for r in (schema.get("required") or []) if r not in AUTO_INJECTED]
    unknown = [k for k in args.keys() if k not in props]
    missing = [r for r in required if r not in args]
    if unknown:
        return False, "unknown_arg:" + ",".join(map(str, unknown[:3]))
    if missing:
        return False, "missing_required:" + ",".join(missing[:3])
    for k, v in args.items():
        ps = props.get(k) or {}
        t = ps.get("type")
        if isinstance(t, list):
            t = next((x for x in t if x != "null"), None)
        if t == "integer":
            ok = (isinstance(v, int) and not isinstance(v, bool)) or (
                isinstance(v, str) and v.lstrip("-").isdigit()
            )
            if v is not None and not ok:
                return False, f"bad_type:{k}"
    return True, "ok"


def _epoch_from_text(text):
    idxs = [int(m) for m in _VINDEX_RE.findall(text or "")]
    if not idxs:
        return None
    return {"count": max(idxs), "n_distinct": len(set(idxs))}


def _classify_call(name, args, schema_ok, subtag, res, epoch, prev_sig):
    if not schema_ok:
        return ("bad_tool_name" if subtag == "bad_tool_name" else "bad_arg_name")
    args = args or {}
    vi = args.get("vision_index")
    is_vg = "vision_index" in args
    is_click = name in CLICK_VERBS
    if is_vg:
        oob = isinstance(vi, int) and epoch is not None and vi > epoch["count"]
        if (_INDEXFAIL in res) or oob:
            return "stale_or_oob_vision_index"
    if is_click and (any(t in res for t in _DEAD_TAGS) or "[no_effect:browser_click" in res):
        return "dead_click_violation"
    if (_NOEFFECT in res) and not is_click:
        return "no_effect_retry"
    cur_sig = (name, json.dumps(args, sort_keys=True, default=str), res[:40])
    if prev_sig == cur_sig and (
        "error" in res.lower() or _NOEFFECT in res or _INDEXFAIL in res
    ):
        return "no_effect_retry"
    if name in DECLARATIVE_CLICK:
        return "well_formed_declarative"
    if name in PROCEDURAL_CLICK:
        return "well_formed_procedural"
    return "well_formed_other"


def classify_worker(transcript) -> list[dict]:
    messages = transcript.get("messages") or []
    registry = _registry_from_schemas(transcript.get("tool_schemas"))
    results = {
        m.get("tool_call_id"): _text_of(m.get("content"))
        for m in messages
        if m.get("role") == "tool"
    }
    last_assistant = max(
        (i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1
    )
    rows: list[dict] = []
    epoch = None
    prev_sig = None
    for i, m in enumerate(messages):
        if m.get("role") != "assistant":
            ep = _epoch_from_text(_text_of(m.get("content")))
            if ep:
                epoch = ep
            continue
        tcs = m.get("tool_calls") or []
        if not tcs:
            txt = _text_of(m.get("content"))
            if i == last_assistant and txt and _ACTION_INTENT_RE.search(txt):
                rows.append(_mkrow(i, 0, "(none)", "prose_instead_of_call", False, "prose", "", epoch, {}))
            continue
        for ci, tc in enumerate(tcs):
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name") or "(unnamed)"
            args, parsed = _parse_args(fn.get("arguments"))
            schema_ok, subtag = _validate_args(name, args if parsed else None, registry)
            res = results.get(tc.get("id"), "")
            cls = _classify_call(name, args if parsed else None, schema_ok, subtag, res, epoch, prev_sig)
            rows.append(_mkrow(i, ci, name, cls, schema_ok, subtag, res, epoch, args if parsed else {}))
            prev_sig = (name, json.dumps(args, sort_keys=True, default=str) if parsed else "?", res[:40])
    return rows


def _mkrow(msg_i, call_i, name, cls, schema_ok, subtag, res, epoch, args):
    args = args or {}
    tags = []
    for t in (*_DEAD_TAGS, _NOEFFECT, _INDEXFAIL):
        if t in res:
            tags.append(t.strip("[]"))
    return {
        "msg_index": msg_i,
        "call_index": call_i,
        "tool_name": name,
        "outcome_class": cls,
        "schema_ok": schema_ok,
        "drift": subtag if not schema_ok else "",
        "is_click": name in CLICK_VERBS,
        "is_vision_grounded": bool(schema_ok and "vision_index" in args),
        "is_declarative_click": bool(schema_ok and name in DECLARATIVE_CLICK),
        "is_procedural_click": bool(schema_ok and name in PROCEDURAL_CLICK),
        "vision_index": args.get("vision_index"),
        "epoch_bbox_count": epoch["count"] if epoch else None,
        "result_tags": "|".join(tags),
    }


# --- aggregation -------------------------------------------------------------
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    xs = [x for x in xs if x is not None]
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def _run_metrics(meta, workers):
    rows: list[dict] = []
    vision_calls = 0
    for w in workers:
        rows += classify_worker(w)
        vc = (w.get("meta") or {}).get("vision_calls")
        if isinstance(vc, int):
            vision_calls += vc
    counts = Counter(r["outcome_class"] for r in rows)
    total = sum(counts.values())
    schema_fail = sum(counts[b] for b in _SCHEMA_FAIL)
    vg = sum(1 for r in rows if r["is_vision_grounded"])
    clicks = sum(1 for r in rows if r["is_click"])
    wf_proc = counts["well_formed_procedural"]
    wf_decl = counts["well_formed_declarative"]
    j = (meta.get("judge") or {}).get("success")
    success = j if j is not None else (1.0 if meta.get("heuristic_success") else 0.0)
    m = {
        "schema_fidelity": (1 - schema_fail / total) if total else None,
        "index_discipline": (1 - counts["stale_or_oob_vision_index"] / vg) if vg else None,
        "procedural_share": (wf_proc / (wf_proc + wf_decl)) if (wf_proc + wf_decl) else None,
        "dead_click_violation_rate": (counts["dead_click_violation"] / clicks) if clicks else None,
        "vision_calls": vision_calls,
        "task_success": float(success),
        "total_attempts": total,
        "counts": counts,
    }
    return m, rows


def _scaffold_hash(workers) -> str:
    for w in workers:
        names = sorted(_registry_from_schemas(w.get("tool_schemas")).keys())
        if names:
            return str(hash(tuple(names)) & 0xFFFFFFFF)
    return "none"


def load_runs(runs_dir, task=None):
    out = []
    for meta_path in sorted(Path(runs_dir).glob("*/*/seed*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        if task and task != "all" and meta.get("task_id") != task:
            continue
        workers = []
        for wf in sorted((meta_path.parent / "workers").glob("*.json")):
            try:
                workers.append(json.loads(wf.read_text()))
            except Exception:
                pass
        out.append((meta, meta_path.parent, workers))
    return out


def _aggregate_row(label, metas, ms) -> dict:
    """Aggregate one group of runs (same label, or same label+task) into a
    per-model CSV row: registry metadata + metric mean/std + outcome-class
    counts. Shared by per_model.csv and per_model_task.csv so the two stay
    definitionally identical."""
    model_id = (metas[0].get("model") or {}).get("model")
    provider = (metas[0].get("model") or {}).get("provider")
    spec = model_registry.resolve(model_id or "")
    is_rescue = label.endswith("_rescue") or any(mt.get("schema_reminder") for mt in metas)
    totals = Counter()
    for mm in ms:
        totals.update(mm["counts"])
    row = {
        "model_label": label,
        "model_id": model_id,
        "provider": provider,
        "lab": spec.lab if spec else "?",
        "is_rescue": is_rescue,
        "short_name": spec.short_name if spec else (model_id or label),
        "bench_composite": spec.bench_composite if spec else "",
        "bench_confirmed": spec.confirmed if spec else False,
        "sources": spec.sources if spec else "",
        "n_runs": len(ms),
        "n_workers": sum(mt.get("n_worker_transcripts", 0) for mt in metas),
        "task_success_mean": _mean([mm["task_success"] for mm in ms]),
        "task_success_std": _std([mm["task_success"] for mm in ms]),
        "schema_fidelity_mean": _mean([mm["schema_fidelity"] for mm in ms]),
        "schema_fidelity_std": _std([mm["schema_fidelity"] for mm in ms]),
        "index_discipline_mean": _mean([mm["index_discipline"] for mm in ms]),
        "index_discipline_std": _std([mm["index_discipline"] for mm in ms]),
        "procedural_share_mean": _mean([mm["procedural_share"] for mm in ms]),
        "procedural_share_std": _std([mm["procedural_share"] for mm in ms]),
        "vision_calls_per_task_mean": _mean([mm["vision_calls"] for mm in ms]),
        "vision_calls_per_task_std": _std([mm["vision_calls"] for mm in ms]),
        "dead_click_violation_rate_mean": _mean([mm["dead_click_violation_rate"] for mm in ms]),
        "dead_click_violation_rate_std": _std([mm["dead_click_violation_rate"] for mm in ms]),
        "total_tool_attempts": sum(mm["total_attempts"] for mm in ms),
    }
    for b in BUCKETS:
        row[f"cnt_{b}"] = totals.get(b, 0)
    return row


def analyze(runs_dir: Path, out_dir: Path, task=None):
    runs = load_runs(runs_dir, task=task)
    if not runs:
        scope = f" for task={task!r}" if task and task != "all" else ""
        print(f"[error] no runs found under {runs_dir}{scope}. Run `python -m eval.run_eval` first.")
        return
    out_dir.mkdir(parents=True, exist_ok=True)

    per_call_rows: list[dict] = []
    by_label: dict[str, list[dict]] = {}
    by_label_task: dict[tuple, list[dict]] = {}
    scaffold_hashes: dict[str, set] = {}

    for meta, seed_dir, workers in runs:
        label = meta.get("label", "unknown")
        m, rows = _run_metrics(meta, workers)
        for r in rows:
            per_call_rows.append({
                "model_label": label,
                "model_id": (meta.get("model") or {}).get("model"),
                "task_id": meta.get("task_id"),
                "seed": meta.get("seed"),
                **r,
            })
        by_label.setdefault(label, []).append((meta, m))
        by_label_task.setdefault((label, meta.get("task_id") or "unknown"), []).append((meta, m))
        scaffold_hashes.setdefault(_rescue_base(label), set()).add(_scaffold_hash(workers))

    # scaffolding-consistency guard (rescue variants share base, allowed to differ)
    multi = {k: v for k, v in scaffold_hashes.items() if len(v) > 1}
    if multi:
        print(f"[warn] scaffolding hash varies within {list(multi)} — tool sets not identical?")

    # per_call.csv
    pc_path = out_dir / "per_call.csv"
    with pc_path.open("w", newline="") as f:
        cols = ["model_label", "model_id", "task_id", "seed", "msg_index", "call_index",
                "tool_name", "outcome_class", "schema_ok", "drift", "is_click",
                "is_vision_grounded", "is_declarative_click", "is_procedural_click",
                "vision_index", "epoch_bbox_count", "result_tags"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in per_call_rows:
            w.writerow({k: r.get(k) for k in cols})

    # per_model.csv
    pm_path = out_dir / "per_model.csv"
    pm_cols = ["model_label", "model_id", "provider", "lab", "is_rescue", "short_name",
               "bench_composite", "bench_confirmed", "sources", "n_runs", "n_workers",
               "task_success_mean", "task_success_std", "schema_fidelity_mean",
               "schema_fidelity_std", "index_discipline_mean", "index_discipline_std",
               "procedural_share_mean", "procedural_share_std", "vision_calls_per_task_mean",
               "vision_calls_per_task_std", "dead_click_violation_rate_mean",
               "dead_click_violation_rate_std", "total_tool_attempts"]
    pm_cols += [f"cnt_{b}" for b in BUCKETS]
    with pm_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pm_cols)
        w.writeheader()
        for label, items in sorted(by_label.items()):
            metas = [it[0] for it in items]
            ms = [it[1] for it in items]
            w.writerow(_aggregate_row(label, metas, ms))

    # per_model_task.csv — same row shape, one row per (label, task) so figures
    # can break the model split down task-by-task (heatmap per-task panels).
    pmt_path = out_dir / "per_model_task.csv"
    pmt_cols = ["model_label", "task_id"] + [c for c in pm_cols if c != "model_label"]
    with pmt_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pmt_cols)
        w.writeheader()
        for (label, task_id), items in sorted(by_label_task.items()):
            metas = [it[0] for it in items]
            ms = [it[1] for it in items]
            row = _aggregate_row(label, metas, ms)
            row["task_id"] = task_id
            w.writerow(row)

    print(f"Wrote {pc_path} ({len(per_call_rows)} tool attempts)")
    print(f"Wrote {pm_path} ({len(by_label)} model labels)")
    print(f"Wrote {pmt_path} ({len(by_label_task)} model×task cells)")
    # quick console summary
    for label, items in sorted(by_label.items()):
        ms = [it[1] for it in items]
        sf = _mean([mm["schema_fidelity"] for mm in ms])
        su = _mean([mm["task_success"] for mm in ms])
        print(f"  {label:28s} success={_fmt(su)} schema_fid={_fmt(sf)} runs={len(ms)}")


def _rescue_base(label: str) -> str:
    return label[:-7] if label.endswith("_rescue") else label


def _fmt(x):
    return f"{x:.2f}" if isinstance(x, (int, float)) else "n/a"


def main():
    p = argparse.ArgumentParser(description="Classify worker transcripts -> metrics")
    p.add_argument("--runs", default=str(REPO_ROOT / "eval" / "runs"))
    p.add_argument("--out", default=str(REPO_ROOT / "eval" / "results"))
    p.add_argument("--task", default=None,
                   help="restrict to one task id (e.g. petfinder_rabbits) for a fair "
                        "per-task model comparison; default = pool all tasks")
    args = p.parse_args()
    analyze(Path(args.runs), Path(args.out), task=args.task)


if __name__ == "__main__":
    main()
