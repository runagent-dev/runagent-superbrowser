"""Sweep-level audit: everything a reader needs to trust (or discount) a sweep.

    python -m eval.core.audit_sweep --experiment ablate10
    python -m eval.core.audit_sweep --experiment ablate10 --results eval/artifacts/ablate10_paper/results.jsonl

Reads the per-run records (and the run directories when present) and writes
``eval/artifacts/<experiment>_audit/audit.{json,md}``. Nothing here is a
result; it is the provenance the results tables rest on:

* shape        — records / tasks / arms, one record per (task, arm, seed)
* protocol     — one protocol hash, the pinned budgets, the judge models
* host model   — model per run; tasks whose arms disagree on the host model
                 (must appear in ``exclusions.json`` as ``host_model_mismatch``)
* code drift   — git SHA histogram per arm, dirty count, and whether the
                 histogram is the same in every arm (interleaving balances it)
* toolset      — non-browser tools EXECUTED (must be 0) and merely attempted
* instructions — as-run instruction vs the frozen benchmark wording per task;
                 every mismatch must be declared in ``instruction_deviations``
* traces       — vision/click trace presence, metric sources (tags vs traces)
* context      — max peak prompt per arm and runs above the host snip threshold
* judge input  — screenshots handed to WebJudge per arm (evidence asymmetry)
* infra errors — runs whose tool results carry proxy / dead-session markers,
                 counted with ONE fixed regex so the number is reproducible
* archives     — the ``_discarded_*`` / ``_orphaned_*`` / ``_failed_attempts``
                 directories under the experiment, with the reasons the subset
                 file records for them
* cost / time  — summed USD (agent, judge) and summed run time

The paper's prose numbers that describe the sweep (not its results) are
generated from ``audit.json`` by ``eval.experiments.paper_tables``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from eval._bootstrap import REPO_ROOT
from eval.core import report
from eval.core.loaders import DEFAULT_RUNS_ROOT, load_records
from eval.core.records import RunRecord, read_results
from eval.core.tasks import load_benchmark, load_exclusions, load_subsets

# Same list as eval/core/run_one._NON_BROWSER_TOOLS (kept literal here so the
# audit does not import the bridge).
NON_BROWSER_TOOLS = frozenset({
    "exec", "run_cli_app", "write_stdin", "list_exec_sessions",
    "spawn", "long_task", "cron", "message",
    "read_file", "write_file", "edit_file", "apply_patch",
    "list_dir", "glob", "grep", "find_files",
    "web_search", "web_fetch",
})

# One fixed marker set for "the browser session / proxy was gone". Counted
# over the worker transcripts' tool results and run.log; a run is flagged when
# ANY marker appears, and "dominant" when it appears at least three times.
# ("session closed" is the worker's normal close message and "exec session not
# found" is a rejected hallucinated tool call; neither is an infrastructure
# failure, so neither is in the set.)
INFRA_MARKERS = re.compile(
    r"ERR_NO_SUPPORTED_PROXIES|ERR_TUNNEL_CONNECTION_FAILED|ERR_PROXY_CONNECTION_FAILED|"
    r"proxy error|session backend lost|session (?:is )?(?:dead|expired)", re.I)
INFRA_DOMINANT_MIN = 3
# The commit that unregistered shell/filesystem tools from the eval orchestrator
# (eval/core/run_one.strip_non_browser_tools). Runs before it had those tools
# available; the audit counts them and checks none was executed.
HOST_TOOL_STRIP_SHA = "536706c"


def _jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except Exception:
        pass
    return out


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join((b.get("text") or "") if isinstance(b, dict) else str(b) for b in content)
    return "" if content is None else str(content)


def infra_marker_count(run_dir: Path) -> int:
    """Occurrences of INFRA_MARKERS in the run's tool results and run.log."""
    n = 0
    for p in sorted((run_dir / "workers").glob("*.json")):
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for m in t.get("messages") or []:
            if m.get("role") == "tool":
                n += len(INFRA_MARKERS.findall(_text_of(m.get("content"))))
    log = run_dir / "run.log"
    if log.exists():
        try:
            n += len(INFRA_MARKERS.findall(log.read_text(encoding="utf-8", errors="replace")))
        except Exception:
            pass
    return n


def _snip_threshold(r: RunRecord) -> int | None:
    proto = r.protocol or {}
    try:
        return int(proto["context_window_tokens"]) - int(proto["max_tokens"]) - 1024
    except (KeyError, TypeError, ValueError):
        return None


def audit(records: list[RunRecord], *, experiment: str, runs_root: Path) -> dict[str, Any]:
    recs = list(records)
    ex = load_exclusions()
    excluded = {e["task_id"]: e for e in ex.get("task_exclusions", []) if isinstance(e, dict)}
    deviations = {d["task_id"]: d for d in ex.get("instruction_deviations", []) if isinstance(d, dict)}
    arms = sorted({str(r.ids.get("arm")) for r in recs})
    tasks = sorted({str(r.ids.get("task_id")) for r in recs})
    by_task: dict[str, list[RunRecord]] = defaultdict(list)
    for r in recs:
        by_task[str(r.ids.get("task_id"))].append(r)

    # ---- shape
    per_task_counts = Counter(len(v) for v in by_task.values())
    run_ids = Counter(r.run_id for r in recs)
    shape = {"records": len(recs), "tasks": len(tasks), "arms": arms, "records_per_task": dict(per_task_counts),
             "duplicate_run_ids": [k for k, v in run_ids.items() if v > 1],
             "seeds": sorted({int(r.ids.get("seed") or 0) for r in recs})}

    # ---- protocol / judges
    proto = {"hashes": dict(Counter(str((r.protocol or {}).get("hash")) for r in recs)),
             "version": sorted({str((r.protocol or {}).get("version")) for r in recs}),
             "max_iterations": sorted({(r.protocol or {}).get("max_iterations") for r in recs}, key=str),
             "wall_clock_s": sorted({(r.protocol or {}).get("wall_clock_s") for r in recs}, key=str),
             "context_window_tokens": sorted({(r.protocol or {}).get("context_window_tokens") for r in recs}, key=str),
             "benchmark": sorted({str((r.protocol or {}).get("benchmark")) for r in recs}),
             "judge_models": {k: dict(Counter(str((r.outcome.get("judge_models") or {}).get(k)) for r in recs))
                              for k in ("webjudge", "answer_judge", "deterministic")},
             "decided_by": dict(Counter(str(r.outcome.get("decided_by")) for r in recs)),
             "vision_model": dict(Counter(str(((r.protocol or {}).get("environment") or {}).get("vision_model")) for r in recs))}

    # ---- host model
    model_hist = Counter(str((r.protocol or {}).get("model")) for r in recs)
    mixed = {}
    for t, rs in by_task.items():
        ms = {str((r.protocol or {}).get("model")) for r in rs}
        if len(ms) > 1:
            mixed[t] = {str(r.ids.get("arm")): str((r.protocol or {}).get("model")) for r in rs}
    host = {"models": dict(model_hist), "mixed_model_tasks": mixed,
            "mixed_model_tasks_excluded": {t: (t in excluded and excluded[t].get("rule") == "host_model_mismatch") for t in mixed},
            "excluded_tasks": {t: e.get("rule") for t, e in excluded.items()}}

    # ---- code drift
    sha_by_arm: dict[str, Counter] = defaultdict(Counter)
    dirty = 0
    for r in recs:
        env = (r.protocol or {}).get("environment") or {}
        sha_by_arm[str(r.ids.get("arm"))][str(env.get("git_sha"))] += 1
        dirty += 1 if env.get("git_dirty") else 0
    hists = {a: dict(sorted(c.items())) for a, c in sha_by_arm.items()}
    balanced = len({json.dumps(h, sort_keys=True) for h in hists.values()}) == 1
    shas = sorted({s for h in hists.values() for s in h})
    per_task_shas = {t: sorted({str(((r.protocol or {}).get("environment") or {}).get("git_sha")) for r in rs}) for t, rs in by_task.items()}
    multi = {t: s for t, s in per_task_shas.items() if len(s) > 1}
    # interleaving (seed -> task -> arm) makes every arm see the same SHA
    # sequence, EXCEPT where a commit landed between two arms of one task;
    # the histogram over single-SHA tasks is the balance that matters
    single_hist: dict[str, Counter] = defaultdict(Counter)
    for r in recs:
        if str(r.ids.get("task_id")) in multi:
            continue
        single_hist[str(r.ids.get("arm"))][str(((r.protocol or {}).get("environment") or {}).get("git_sha"))] += 1
    balanced_single = len({json.dumps(dict(sorted(c.items())), sort_keys=True) for c in single_hist.values()}) == 1
    first_seen: dict[str, float] = {}
    for r in recs:
        sha = str(((r.protocol or {}).get("environment") or {}).get("git_sha"))
        t = float(r.timing.get("started_at") or 0)
        first_seen[sha] = min(first_seen.get(sha, t), t)
    drift = {"shas": shas, "shas_chronological": sorted(shas, key=lambda x: first_seen.get(x, 0)),
             "n_shas": len(shas), "dirty_records": dirty, "sha_histogram_by_arm": hists,
             "balanced_across_arms": balanced,
             "balanced_across_arms_excluding_multi_sha_tasks": balanced_single,
             "tasks_spanning_several_shas": multi,
             "records_on_multi_sha_tasks": sum(len(by_task[t]) for t in multi)}

    # ---- toolset
    executed = Counter()
    attempted = Counter()
    runs_attempting = 0
    for r in recs:
        by = (r.counts or {}).get("tool_calls_by_name") or {}
        at = (r.counts or {}).get("tool_calls_attempted_by_name") or {}
        hit = False
        for n, v in by.items():
            if n in NON_BROWSER_TOOLS and v:
                executed[n] += int(v)
        for n, v in at.items():
            if n in NON_BROWSER_TOOLS and v:
                attempted[n] += int(v)
                hit = True
        runs_attempting += 1 if hit else 0
    before_strip = None
    try:  # runs whose commit predates the host-tool strip (eval-only tool removal, 536706c)
        import subprocess

        shas_seen = {str(((r.protocol or {}).get("environment") or {}).get("git_sha")) for r in recs}
        older = set()
        for sha in shas_seen:
            rc = subprocess.run(["git", "merge-base", "--is-ancestor", sha, HOST_TOOL_STRIP_SHA], cwd=str(REPO_ROOT),
                                capture_output=True).returncode
            if rc == 0 and sha != HOST_TOOL_STRIP_SHA[:len(sha)]:
                older.add(sha)
        before_strip = sum(1 for r in recs if str(((r.protocol or {}).get("environment") or {}).get("git_sha")) in older)
    except Exception:
        before_strip = None
    toolset = {"non_browser_executed": dict(executed), "non_browser_executed_total": sum(executed.values()),
               "non_browser_attempted": dict(attempted), "runs_attempting_non_browser": runs_attempting,
               "host_tool_strip_sha": HOST_TOOL_STRIP_SHA, "records_before_host_tool_strip": before_strip}

    # ---- instructions vs frozen benchmark
    bench_cache: dict[str, dict[str, str]] = {}
    instr: dict[str, Any] = {"checked": 0, "mismatches": {}, "undeclared_mismatches": [], "declared_without_mismatch": [],
                             "benchmark_missing": []}
    for t, rs in by_task.items():
        d = Path(rs[0].ids.get("run_dir", ""))
        spec = {}
        try:
            spec = json.loads((d / "spec.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        as_run = str((spec.get("task") or {}).get("instruction") or "")
        bench = str((spec.get("protocol") or {}).get("benchmark") or "")
        if bench not in bench_cache:
            try:
                bench_cache[bench] = {x.task_id: x.instruction for x in load_benchmark(bench, annotate=False)}
            except Exception:
                bench_cache[bench] = {}
        frozen = bench_cache[bench].get(t)
        if frozen is None:
            instr["benchmark_missing"].append(t)
            continue
        instr["checked"] += 1
        if as_run.strip() != frozen.strip():
            instr["mismatches"][t] = {"as_run": as_run, "frozen": frozen, "declared": t in deviations}
            if t not in deviations:
                instr["undeclared_mismatches"].append(t)
    for t in deviations:
        if t in by_task and t not in instr["mismatches"]:
            instr["declared_without_mismatch"].append(t)
    instr["declared_deviations"] = {t: {"reason": d.get("reason"), "handling": d.get("handling")} for t, d in deviations.items()}

    # ---- traces / metric sources
    traces = {"has_vision_trace": sum(1 for r in recs if (r.artifacts or {}).get("has_vision_trace")),
              "has_click_trace": sum(1 for r in recs if (r.artifacts or {}).get("has_click_trace")),
              "has_context_dump": sum(1 for r in recs if (r.artifacts or {}).get("has_context_dump")),
              "grounding_source": dict(Counter(str((r.metrics.get("grounding") or {}).get("source")) for r in recs)),
              "rpr_source": dict(Counter(str((r.metrics.get("rpr") or {}).get("source")) for r in recs)),
              "rpr_available": sum(1 for r in recs if isinstance((r.metrics.get("rpr") or {}).get("rpr"), (int, float))),
              "recovery_available": sum(1 for r in recs if isinstance((r.metrics.get("grounding") or {}).get("recovery_success"), (int, float))),
              "first_path_available": sum(1 for r in recs if isinstance((r.metrics.get("grounding") or {}).get("first_path_success"), (int, float))),
              "csd_observed_available": sum(1 for r in recs if isinstance((r.metrics.get("csd") or {}).get("csd_observed"), (int, float)))}

    # ---- context
    ctx: dict[str, Any] = {"by_arm": {}, "snip_threshold": sorted({_snip_threshold(r) for r in recs}, key=str)}
    for a in arms:
        rs = [r for r in recs if str(r.ids.get("arm")) == a]
        peaks = [r.tokens.get("prompt_tokens_per_iter_peak") for r in rs if isinstance(r.tokens.get("prompt_tokens_per_iter_peak"), (int, float))]
        ests = [r.tokens.get("context_est_after_peak") for r in rs if isinstance(r.tokens.get("context_est_after_peak"), (int, float))]
        over = sum(1 for r in rs if isinstance(r.tokens.get("prompt_tokens_per_iter_peak"), (int, float)) and _snip_threshold(r)
                   and r.tokens["prompt_tokens_per_iter_peak"] > _snip_threshold(r))
        ctx["by_arm"][a] = {"prompt_peak_mean": (sum(peaks) / len(peaks)) if peaks else None,
                            "prompt_peak_max": max(peaks) if peaks else None,
                            "ctx_est_peak_max": max(ests) if ests else None,
                            "runs_over_snip_threshold": over}
    ctx["prompt_peak_max_overall"] = max((v["prompt_peak_max"] or 0) for v in ctx["by_arm"].values()) if arms else None
    ctx["runs_over_snip_threshold"] = sum(v["runs_over_snip_threshold"] for v in ctx["by_arm"].values())

    # ---- judge input + infra markers + screenshots (need run dirs)
    judge_shots: dict[str, list[int]] = defaultdict(list)
    infra: dict[str, Counter] = defaultdict(Counter)
    infra_runs: dict[str, dict[str, int]] = defaultdict(dict)
    for r in recs:
        d = Path(r.ids.get("run_dir", ""))
        a = str(r.ids.get("arm"))
        wj = d / "judges" / "webjudge.json"
        if wj.exists():
            try:
                det = (json.loads(wj.read_text(encoding="utf-8")).get("details") or {})
                if isinstance(det.get("screenshots_evaluated"), int):
                    judge_shots[a].append(int(det["screenshots_evaluated"]))
            except Exception:
                pass
        if d.exists():
            n = infra_marker_count(d)
            if n:
                infra[a]["any"] += 1
                infra_runs[a][str(r.ids.get("task_id"))] = n
            if n >= INFRA_DOMINANT_MIN:
                infra[a]["dominant"] += 1
    judge_input = {a: {"mean_screenshots_evaluated": (sum(v) / len(v)) if v else None, "n": len(v)} for a, v in judge_shots.items()}
    infra_out = {"markers": INFRA_MARKERS.pattern, "dominant_min": INFRA_DOMINANT_MIN,
                 "count_histogram": dict(sorted(Counter(n for d in infra_runs.values() for n in d.values()).items())),
                 "by_arm": {a: {"runs_with_any_marker": infra[a]["any"], "runs_dominant": infra[a]["dominant"]} for a in arms},
                 "runs": {a: infra_runs[a] for a in arms if infra_runs[a]}}

    # ---- archives
    base = Path(runs_root) / experiment
    archives = {}
    if base.exists():
        for d in sorted(base.glob("_*")):
            if d.is_dir():
                archives[d.name] = {"entries": len([p for p in d.iterdir()]),
                                    "size_mb": round(sum(p.stat().st_size for p in d.rglob("*") if p.is_file()) / 1048576, 1)}
    subset_note = None
    for name, sub in load_subsets().items():
        if set(sub.get("task_ids", [])) == set(tasks) or (set(tasks) - set(excluded)) <= set(sub.get("task_ids", [])) and len(sub.get("task_ids", [])) == len(set(tasks)):
            subset_note = {"name": name, "method": sub.get("method"), "note": sub.get("note"),
                           "level_counts": sub.get("level_counts"), "screened": sub.get("screened")}
            break

    # ---- cost / time / outcome
    usd = sum(float((r.cost or {}).get("usd") or 0) for r in recs)
    usd_judge = sum(float((r.cost or {}).get("usd_judge") or 0) for r in recs)
    wall = sum(float(r.timing.get("wall_s") or 0) for r in recs)
    started = [float(r.timing.get("started_at") or 0) for r in recs if r.timing.get("started_at")]
    ended = [float(r.timing.get("ended_at") or 0) for r in recs if r.timing.get("ended_at")]
    cost = {"usd_agent": round(usd, 2), "usd_judge": round(usd_judge, 2), "usd_total": round(usd + usd_judge, 2),
            "unpriced_records": sum(1 for r in recs if not (r.cost or {}).get("priced")),
            "summed_run_time_h": round(wall / 3600, 2),
            "elapsed_h_first_start_to_last_end": round((max(ended) - min(started)) / 3600, 1) if started and ended else None,
            "price_table_as_of": sorted({str((r.cost or {}).get("as_of")) for r in recs})}
    levels = Counter(str(r.ids.get("level")) for r in recs if str(r.ids.get("arm")) == arms[0]) if arms else Counter()
    outcome = {"failure_reasons": dict(Counter(str(r.outcome.get("failure_reason")) for r in recs if not r.outcome.get("success"))),
               "exclusion_labels": dict(Counter(str(r.outcome.get("exclusion_label")) for r in recs if r.outcome.get("exclusion_label"))),
               "level_counts_per_arm": dict(levels),
               "primary_task_count": len([t for t in tasks if t not in excluded])}

    checks = {
        "one_record_per_task_arm": per_task_counts == Counter({len(arms): len(tasks)}) and not shape["duplicate_run_ids"],
        "single_protocol_hash": len(proto["hashes"]) == 1,
        "single_webjudge_model": len(proto["judge_models"]["webjudge"]) == 1,
        "mixed_model_tasks_all_excluded": all(host["mixed_model_tasks_excluded"].values()),
        "no_non_browser_tool_executed": toolset["non_browser_executed_total"] == 0,
        "all_instruction_mismatches_declared": not instr["undeclared_mismatches"],
        "code_drift_balanced_across_arms_excluding_multi_sha_tasks": balanced_single,
        "no_run_over_snip_threshold": ctx["runs_over_snip_threshold"] == 0,
        "all_records_priced": cost["unpriced_records"] == 0,
    }
    return {"experiment": experiment, "shape": shape, "protocol": proto, "host_model": host, "code_drift": drift,
            "toolset": toolset, "instructions": instr, "traces": traces, "context": ctx, "judge_input": judge_input,
            "infra_errors": infra_out, "archives": archives, "subset": subset_note, "cost": cost, "outcome": outcome,
            "checks": checks}


def render_markdown(a: dict[str, Any]) -> str:
    L = [f"# Sweep audit — `{a['experiment']}`", ""]
    L.append("| Check | Result |\n|---|---|")
    for k, v in a["checks"].items():
        L.append(f"| {k} | {'PASS' if v else 'FAIL'} |")
    s = a["shape"]
    L += ["", f"**Shape.** {s['records']} records, {s['tasks']} tasks, arms {', '.join(s['arms'])}; records per task {s['records_per_task']}.",
          f"**Protocol.** hash {a['protocol']['hashes']}, judge {a['protocol']['judge_models']['webjudge']}, decided_by {a['protocol']['decided_by']}.",
          f"**Host model.** {a['host_model']['models']}; mixed-model tasks: {a['host_model']['mixed_model_tasks'] or 'none'}; excluded: {a['host_model']['excluded_tasks'] or 'none'}.",
          f"**Code drift.** {a['code_drift']['n_shas']} SHAs {a['code_drift']['shas']}, dirty records {a['code_drift']['dirty_records']}, SHA histogram identical across arms: {a['code_drift']['balanced_across_arms']} (excluding tasks whose arms span several SHAs: {a['code_drift']['balanced_across_arms_excluding_multi_sha_tasks']}); tasks spanning several SHAs: {a['code_drift']['tasks_spanning_several_shas'] or 'none'}.",
          f"**Toolset.** non-browser tools executed: {a['toolset']['non_browser_executed_total']}; runs attempting one: {a['toolset']['runs_attempting_non_browser']} ({a['toolset']['non_browser_attempted']}).",
          f"**Instructions.** {a['instructions']['checked']} checked; mismatches {list(a['instructions']['mismatches'])}; undeclared {a['instructions']['undeclared_mismatches']}.",
          f"**Traces.** vision {a['traces']['has_vision_trace']}, click {a['traces']['has_click_trace']}, context dump {a['traces']['has_context_dump']}; grounding source {a['traces']['grounding_source']}; RPR available {a['traces']['rpr_available']}, recovery {a['traces']['recovery_available']}, first-path {a['traces']['first_path_available']}, CSD(obs) {a['traces']['csd_observed_available']}.",
          f"**Context.** max peak prompt overall {a['context']['prompt_peak_max_overall']}; snip threshold {a['context']['snip_threshold']}; runs over it {a['context']['runs_over_snip_threshold']}.",
          "", "| Arm | mean peak | max peak | max ctx est | judge screenshots | infra any | infra dominant |", "|---|---|---|---|---|---|---|"]
    for arm, c in a["context"]["by_arm"].items():
        j = a["judge_input"].get(arm, {})
        i = a["infra_errors"]["by_arm"].get(arm, {})
        L.append(f"| {arm} | {c['prompt_peak_mean']:.0f} | {c['prompt_peak_max']} | {c['ctx_est_peak_max']} | "
                 f"{(j.get('mean_screenshots_evaluated') or 0):.1f} | {i.get('runs_with_any_marker', 0)} | {i.get('runs_dominant', 0)} |")
    L += ["", f"**Infra markers.** `{a['infra_errors']['markers']}` (dominant = ≥{a['infra_errors']['dominant_min']} hits).",
          f"**Archives under the experiment.** {a['archives'] or 'none'}.",
          f"**Cost / time.** agent ${a['cost']['usd_agent']}, judge ${a['cost']['usd_judge']}, total ${a['cost']['usd_total']}; summed run time {a['cost']['summed_run_time_h']} h; elapsed {a['cost']['elapsed_h_first_start_to_last_end']} h; unpriced {a['cost']['unpriced_records']}.",
          f"**Outcome.** failure reasons {a['outcome']['failure_reasons']}; exclusion labels {a['outcome']['exclusion_labels']}; levels per arm {a['outcome']['level_counts_per_arm']}; primary tasks {a['outcome']['primary_task_count']}."]
    if a.get("subset"):
        L += ["", f"**Subset.** `{a['subset']['name']}` — {a['subset']['method']}", "", a["subset"]["note"] or ""]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sweep-level provenance audit")
    ap.add_argument("--experiment", required=True)
    ap.add_argument("--runs", default=str(DEFAULT_RUNS_ROOT))
    ap.add_argument("--results", default=None, help="read this results.jsonl instead of the run directories")
    ap.add_argument("--out", default=None, help="artifact dir (default eval/artifacts/<experiment>_audit)")
    args = ap.parse_args(argv)
    recs = read_results(Path(args.results)) if args.results else load_records(args.experiment, runs_root=Path(args.runs))
    if not recs:
        print("no records")
        return 1
    a = audit(recs, experiment=args.experiment, runs_root=Path(args.runs))
    out = Path(args.out) if args.out else report.out_dir(f"{args.experiment}_audit")
    out.mkdir(parents=True, exist_ok=True)
    report.write_json(out / "audit.json", a)
    (out / "audit.md").write_text(render_markdown(a))
    print(render_markdown(a))
    print(f"-> {out}")
    return 0 if all(a["checks"].values()) else 2


if __name__ == "__main__":
    sys.exit(main())
