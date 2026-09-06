import json

from eval.core.harvest import build_record, classify_failure, count_tags
from eval.core.records import (RunRecord, append_result, iter_run_dirs, read_results, rebuild_results,
                               results_path)
from eval.tests.helpers import make_run_dir


def test_record_roundtrip_and_results_dedup(tmp_path):
    rec = RunRecord(ids={"run_id": "x:a:t:s0", "experiment": "x"}, protocol={"hash": "h"})
    rec.write(tmp_path)
    assert RunRecord.read(tmp_path).run_id == "x:a:t:s0"
    rp = tmp_path / "results.jsonl"
    append_result(rp, rec)
    rec.outcome["success"] = True
    append_result(rp, rec)
    rows = read_results(rp)
    assert len(rows) == 1 and rows[0].success is True


def test_harvest_builds_consistent_record(tmp_path):
    run_dir = make_run_dir(tmp_path, judges={"webjudge": True, "answer_judge": False})
    rec = build_record(run_dir)
    assert rec.ids["run_id"] == "e_test:ledger:t1:s0" and rec.ids["arm"] == "ledger"
    assert rec.outcome["success"] is True and rec.outcome["decided_by"] == "webjudge"
    assert rec.outcome["success_answer_judge"] is False
    assert rec.outcome["failure_reason"] is None
    assert rec.counts["worker_iterations"] == 5 and rec.counts["orchestrator_iterations"] == 2
    assert rec.counts["tool_calls_executed"] == 5 and rec.counts["tool_calls_attempted"] == 5
    assert rec.counts["tool_calls_by_name"]["browser_click_at"] == 2
    assert rec.counts["tags"]["click_escalated"] == 1
    assert rec.counts["vision_calls"] == 3 and rec.counts["vision_calls_source"] == "worker_meta"
    assert rec.counts["evictions"]["messages_gutted"] == 1
    assert rec.tokens["prompt_tokens_per_iter_peak"] == 45000
    assert rec.tokens["prompt_tokens_per_iter_mean"] == 42200
    assert rec.tokens["by_role"]["worker"]["calls"] == 5
    assert rec.protocol["model"] == "test/model"
    rec.write(run_dir)
    out = rebuild_results(tmp_path, "e_test")
    assert len(read_results(out)) == 1
    assert list(iter_run_dirs(tmp_path, "e_test")) == [run_dir]


def test_harvest_prefers_deterministic_then_webjudge(tmp_path):
    run_dir = make_run_dir(tmp_path, task_id="t2", judges={"deterministic": False, "webjudge": True})
    rec = build_record(run_dir)
    assert rec.outcome["success"] is False and rec.outcome["decided_by"] == "deterministic"
    assert rec.outcome["failure_reason"] == "premature_done"


def test_failure_classification_rules():
    assert classify_failure(success=True, stop_reason="ok", final_answer="x", tags={}, error=None) is None
    assert classify_failure(success=False, stop_reason="timeout", final_answer="", tags={}, error=None) == "timeout"
    assert classify_failure(success=None, stop_reason="ok", final_answer="Error: insufficient_quota", tags={}, error=None) == "api_error"
    assert classify_failure(success=False, stop_reason="ok", final_answer="x", tags={"captcha_unsolved": 1}, error=None) == "captcha_unsolved"
    assert classify_failure(success=False, stop_reason="ok", final_answer="x", tags={"dead_click_blocked": 2, "same_element_blocked": 1}, error=None) == "loop"
    assert classify_failure(success=False, stop_reason="ok", final_answer="This site is not available in your region", tags={}, error=None) == "geo_blocked"
    assert classify_failure(success=False, stop_reason="ok", final_answer="net::ERR_CONNECTION_RESET", tags={}, error=None) == "site_unavailable"
    assert classify_failure(success=False, stop_reason="ok", final_answer="x", tags={"label_mismatch": 1}, error=None) == "grounding"


def test_count_tags_counts_escalations_per_occurrence():
    tr = [{"messages": [
        {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "browser_click_at", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "ok [click_escalated strategy=js] [no_effect:browser_click_at]"},
        {"role": "tool", "tool_call_id": "2", "content": "[dead_click_blocked] [dead_click_blocked]"},
    ]}]
    tags = count_tags(tr)
    assert tags["click_escalated"] == 1 and tags["no_effect"] == 1 and tags["dead_click_blocked"] == 1
