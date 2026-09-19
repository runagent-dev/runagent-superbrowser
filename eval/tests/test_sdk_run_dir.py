"""An SDK audit-trail run directory must be a first-class run for the harness:
records build from it, the analyzers find it, and a benchmark instruction typed
verbatim resolves to the frozen task."""
import json
import sys
from pathlib import Path

from eval._bootstrap import NANOBOT_TREE
from eval.core.harvest import build_record
from eval.core.record import record_experiment
from eval.core.records import iter_run_dirs, read_results, results_path
from eval.core.tasks import find_by_instruction
from eval.tests.helpers import PNG_1x1, transcript

if str(NANOBOT_TREE) not in sys.path:
    sys.path.insert(0, str(NANOBOT_TREE))


def _sdk_run(tmp_path):
    from superbrowser_bridge.audit import AuditRecorder

    base = tmp_path / "superbrowser"
    rec = AuditRecorder(tmp_path / "runs", experiment="sdk", memory_base=base)
    run = rec.begin(task={"instruction": "Find the 5-day price chart for Bitcoin.", "start_url": "https://www.google.com/finance/"},
                    model="test/model", timeout=600)
    orch, wid = "orch-aaaa1111", "bbbb2222"
    for tid, role in ((orch, "orchestrator"), (wid, "worker")):
        mem = base / tid / "memory"
        mem.mkdir(parents=True)
        events = [{"ts": 1, "type": "memory_attach", "role": role}]
        for i, t in enumerate((40000, 42000, 45000)):
            events.append({"ts": 2 + i, "type": "memory_after_iter", "iter": i, "role": role, "tokens_in": t, "tokens_out": 100,
                           "cache_read": 0, "cache_creation": 0, "messages": 3, "tool_calls": 1})
            if role == "worker":
                events.append({"ts": 2 + i, "type": "iteration", "iter": i, "tokens_in": t, "tokens_out": 100, "messages": 3, "tool_calls": 1})
        (mem / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        (mem / "ledger.json").write_text("{}")
    tr = transcript(task_id=wid, instruction="Find the 5-day price chart for Bitcoin.", url="https://www.google.com/finance/",
                    turns=[("browser_open", {"url": "https://www.google.com/finance/"}, "session=s1"),
                           ("browser_click_at", {"vision_index": 2}, "ok")], final="here is the chart")
    (run.run_dir / "workers" / f"{wid}.json").write_text(json.dumps(tr))
    (run.run_dir / "screenshots" / "001-open.png").write_bytes(PNG_1x1)
    run.write_preliminary_meta(orch, "framed")
    run.finish(final_answer="here is the chart", raw_content="here is the chart", stop_reason="ok", error=None,
               usage={"by_role": {"worker": {"input_tokens": 127000, "output_tokens": 300, "calls": 3}}, "input_tokens": 127000,
                      "output_tokens": 300, "total_tokens": 127300}, orch_task_id=orch, framed_task="framed",
               effective_defaults={"model": "test/model", "provider": "openrouter"}, screenshot_src=None)
    return run


def test_sdk_run_dir_builds_a_record_and_is_found_by_the_harness(tmp_path):
    run = _sdk_run(tmp_path)
    rec = build_record(run.run_dir)
    assert rec.ids["experiment"] == "sdk" and rec.ids["arm"] == "ledger" and rec.ids["benchmark"] == "custom"
    assert rec.protocol["model"] == "test/model" and rec.protocol["version"] == "sdk"
    assert rec.counts["worker_iterations"] == 3 and rec.counts["tool_calls_attempted"] == 2
    assert rec.tokens["prompt_tokens_per_iter_peak"] == 45000
    assert rec.outcome["success"] is None, "unjudged until eval.core.judge runs"
    assert list(iter_run_dirs(tmp_path / "runs", "sdk")) == []
    done = record_experiment(tmp_path / "runs", "sdk")
    assert done == [run.run_dir] and list(iter_run_dirs(tmp_path / "runs", "sdk")) == [run.run_dir]
    rows = read_results(results_path(tmp_path / "runs", "sdk"))
    assert rows[0].run_id == run.run_id


def test_find_by_instruction_resolves_a_verbatim_benchmark_task():
    t = find_by_instruction("What is the second stop among the best stops along the road trip from Yellowstone National Park to Las Vegas?",
                            "https://wanderlog.com/")
    assert t is not None and t.task_id == "662ae0f2d3ac851dbcdd245f908277e3" and t.benchmark == "online_mind2web"
    assert find_by_instruction("  what IS the second stop among the best stops along the road trip from yellowstone national park to las vegas? ") is not None
    assert find_by_instruction("What is the second stop", "https://wanderlog.com/") is None
    assert find_by_instruction("What is the second stop among the best stops along the road trip from Yellowstone National Park to Las Vegas?",
                               "https://example.com/") is None, "a different site is not the benchmark task"
