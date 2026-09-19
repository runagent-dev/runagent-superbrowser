"""End-to-end offline check of the analysis layer on synthetic run directories."""
import json

from eval.core import analysis, report
from eval.core.harvest import build_record
from eval.experiments.e0_headline_audit.analyze import audit
from eval.experiments.e2_memory_policy import SPEC as E2
from eval.core.experiment import standard_analyze
from eval.tests.helpers import make_run_dir


def _build_e2(tmp_path):
    outcomes = {  # task -> (ledger, fifo)
        "t1": (True, True), "t2": (True, False), "t3": (True, False), "t4": (False, False),
        "t5": (True, True), "t6": (False, True), "t7": (True, False), "t8": (True, True),
    }
    recs = []
    for task, (la, lb) in outcomes.items():
        for arm, ok in (("ledger", la), ("fifo", lb)):
            final = "Found the listing at $18,500" if ok else "Browser worker failed: captcha_unsolved" if task == "t4" else "no result"
            d = make_run_dir(tmp_path, experiment="e2_memory_policy", arm=arm, task_id=task,
                             judges={"webjudge": ok}, final=final,
                             tokens_in=[42000, 44000, 46000] if arm == "ledger" else [42000, 60000, 80000])
            rec = build_record(d)
            rec.protocol["model"] = "openai/gpt-4o"
            rec.write(d)
            recs.append(rec)
    return recs


def test_standard_analyze_writes_paired_artifacts(tmp_path):
    report.set_artifacts_root(tmp_path / "artifacts")
    _build_e2(tmp_path)
    res = standard_analyze(E2, runs_root=tmp_path)
    assert res["n_records"] == 16
    arms = res["arms"]
    assert arms["ledger"]["k"] == 6 and arms["fifo"]["k"] == 4
    pb = {(p["arm_a"], p["arm_b"]): p for p in res["paired_binary"]}
    c1 = pb[("ledger", "fifo")]
    # t4 is impossible in BOTH arms (captcha) -> excluded from the pair
    assert c1["n"] == 7 and c1["n_excluded"] == 1 and c1["confirmatory"] is True
    assert c1["a_only"] == 3 and c1["b_only"] == 1
    assert abs(c1["diff"] - 2 / 7) < 1e-9
    out = tmp_path / "artifacts" / "e2_memory_policy"
    for name in ("per_run.csv", "arm_summary.csv", "arm_summary.tex", "paired_binary.json", "paired_binary.tex", "paired_metrics.csv", "summary.json"):
        assert (out / name).exists(), name
    tex = (out / "paired_binary.tex").read_text()
    assert "ledger vs fifo" in tex and "\\dagger" in tex
    pm = [r for r in res["paired_metrics"] if r["metric"] == "prompt_peak" and r["arm_b"] == "fifo"]
    assert pm and pm[0]["mean_diff"] < 0  # ledger's peak prompt is lower


def test_headline_audit_from_records(tmp_path):
    report.set_artifacts_root(tmp_path / "artifacts")
    recs = []
    from eval.core.tasks import load_benchmark
    hard = load_benchmark("online_mind2web_hard")[:10]
    for i, t in enumerate(hard):
        d = make_run_dir(tmp_path, experiment="e1_main", arm="ledger", task_id=t.task_id,
                         url=t.start_url, instruction=t.instruction, judges={"webjudge": i % 3 != 0},
                         final="ok" if i % 3 != 0 else "no")
        rec = build_record(d)
        rec.ids["website"] = t.start_url
        rec.ids["level"] = "hard"
        recs.append(rec)
    a = audit(recs)
    assert a["evaluated_n"] == 10 and a["all_tasks"]["k"] == 6 and a["all_tasks"]["exact_fraction"] == "6/10"
    assert len(a["missing_tasks"]) == 64
    assert a["paper_draft_claim"]["consistent_fraction"] is False
    assert sum(v["n"] for v in a["by_site_family"].values()) == 10


def test_analyzer_can_read_a_combined_sweep(tmp_path, capsys):
    """All ablations can be swept as ONE experiment so they share a baseline
    arm; each experiment's analyzer must then pick out only its own arms."""
    import importlib

    from eval.core.harvest import build_record
    from eval.core.records import append_result, results_path
    from eval.tests.helpers import make_run_dir

    runs = tmp_path / "runs"
    combined = "combined10"
    # one pool holding every ablation arm, 3 tasks each
    all_arms = ["ledger", "fifo", "summary", "full_history", "no_deadend", "fresh_vision", "flat"]
    for arm in all_arms:
        for t in range(3):
            d = make_run_dir(runs, experiment=combined, arm=arm, task_id=f"t{t}",
                             judges={"webjudge": arm == "ledger"})
            rec = build_record(d)
            rec.write(d)
            append_result(results_path(runs, combined), rec)

    m = importlib.import_module("eval.experiments.e4_deadend.analyze")
    assert m.main(["--runs", str(runs), "--experiment", combined]) == 0
    out = capsys.readouterr().out
    assert "reading 'combined10'" in out
    # E4 compares ledger vs no_deadend only: 2 arms x 3 tasks out of the 21-record pool
    assert "6/21 records" in out
    assert "MISSING" not in out


def test_provider_errors_are_never_scored_as_task_failures(tmp_path):
    """An api_error on one side of a pair drops the pair (there is no task
    outcome to compare), and the run leaves its arm's denominator."""
    from eval.core.harvest import build_record

    recs = []
    for task, (ok_a, ok_b) in {"t1": (True, False), "t2": (True, True), "t3": (False, True)}.items():
        for arm, ok in (("ledger", ok_a), ("fifo", ok_b)):
            final = "Found it" if ok else "no result"
            if task == "t3" and arm == "ledger":
                final = "The AI provider rejected the request because the API key is out of quota"
            d = make_run_dir(tmp_path, experiment="x", arm=arm, task_id=task, judges={"webjudge": ok}, final=final)
            recs.append(build_record(d))
    arms = analysis.by_arm(recs)
    pb = analysis.paired_binary(arms, "ledger", "fifo")
    assert pb["n"] == 2 and pb["n_excluded"] == 1 and "api_error" in pb["excluded"]["t3:s0"]
    summ = analysis.arm_summary(recs)
    assert summ["ledger"]["n"] == 2 and summ["ledger"]["n_provider_errors"] == 1
    assert summ["fifo"]["n"] == 3 and summ["fifo"]["n_provider_errors"] == 0
