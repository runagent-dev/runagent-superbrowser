import json
import time

from eval.core.harvest import build_record
from eval.core.metrics import compute_all
from eval.core.metrics.cost import cost_of_record
from eval.core.metrics.csd import candidate_values, normalise, observation_events
from eval.core.metrics.drr import drr_from_steps, normalise_target
from eval.core.metrics.grounding import from_clicks
from eval.core.metrics.rpr import rpr_from_calls
from eval.tests.helpers import make_run_dir


def test_rpr_counts_redundant_and_churn():
    calls = [
        {"ok": True, "url": "https://www.x.com/a/", "dom_hash": "h1", "dom_text_hash": "t1", "cached": False, "path": "sync"},
        {"ok": True, "url": "https://x.com/a", "dom_hash": "h1", "dom_text_hash": "t1", "cached": True, "path": "prefetch"},
        {"ok": True, "url": "https://x.com/a", "dom_hash": "h1", "dom_text_hash": "t1", "cached": False, "path": "sync"},
        {"ok": True, "url": "https://x.com/b", "dom_hash": "h2", "dom_text_hash": "t2", "cached": False, "path": "prefetch"},
        {"ok": False, "url": "https://x.com/b", "dom_hash": "h2", "dom_text_hash": "t2"},
    ]
    r = rpr_from_calls(calls)
    assert r["vision_calls"] == 4 and r["redundant"] == 2 and r["redundant_cached"] == 1 and r["redundant_uncached"] == 1
    assert abs(r["rpr"] - 0.5) < 1e-9 and abs(r["rpr_uncached"] - 0.25) < 1e-9
    assert abs(r["page_churn"] - 1 / 3) < 1e-9
    assert r["by_path"] == {"prefetch": 2, "sync": 2}


def test_drr_revisits_and_repeats():
    steps = [
        {"tool": "browser_open", "args": "https://s.com/", "result": "ok", "url": "https://s.com/", "success": True},
        {"tool": "browser_click_at", "args": 'V3|"Apply filter"', "result": "[click_at_failed:blocker_active]", "url": "https://s.com/list", "success": False},
        {"tool": "browser_screenshot", "args": "", "result": "url=...", "url": "https://s.com/list", "success": True},
        {"tool": "browser_click_at", "args": 'V7|"Apply filter"', "result": "[no_effect:browser_click_at]", "url": "https://www.s.com/list/", "success": False},
        {"tool": "browser_click_at", "args": 'V7|"Apply filter"', "result": "ok", "url": "https://s.com/list", "success": True},
        {"tool": "browser_type_at", "args": '{"vision_index": 2, "text": "corolla"}', "result": "typed", "url": "https://s.com/list", "success": True},
    ]
    r = drr_from_steps(steps)
    assert r["dead_end_signatures"] == 1          # same label on the same page = one dead end
    assert r["revisits"] == 2 and r["drr"] == 2.0
    assert r["repeated_actions"] == 1              # steps 3 and 4 identical
    assert normalise_target("browser_click_at", 'V3|"Apply filter"') == "apply filter"
    assert normalise_target("browser_type_at", '{"vision_index": 2, "text": "corolla"}') == "corolla"


def test_csd_helpers_and_observation_events():
    assert normalise("$18,500 Red Corolla") == "18500 red corolla"
    vals = candidate_values("Listing 42 at https://s.com/l/42 — $18,500, posted Sep 3, 2026, VIN 1HGCM82633A004352")
    assert "https://s.com/l/42" in vals and "$18,500" in vals and "1HGCM82633A004352" in vals
    steps = [
        {"result": "session=1", "args": "https://s.com/", "timestamp": 1},
        {"result": "Results: 2019 Corolla $18,500 listing https://s.com/l/42", "args": "", "timestamp": 2},
        {"result": "opened", "args": '{"url": "https://s.com/l/42"}', "timestamp": 3},
    ]
    ev = observation_events(steps, [], "Best deal: $18,500 at https://s.com/l/42", "Find a red Corolla")
    kinds = {(e["item"], e["reuse"]) for e in ev}
    assert ("https://s.com/l/42", "action") in kinds and ("18500", "final_answer") in kinds


def test_compute_all_on_synthetic_run(tmp_path):
    now = time.time()
    live = [
        {"iter": 0, "ts": now - 95, "policy": "fifo", "messages": [{"role": "system", "text": "S"}, {"role": "user", "text": "Find a red Toyota Corolla from 2018 to 2023."}]},
        {"iter": 1, "ts": now - 85, "policy": "fifo", "messages": [{"role": "system", "text": "S"}, {"role": "user", "text": "Find a red Toyota Corolla from 2018 to 2023."},
                                                                  {"role": "tool", "text": "Results: 2019 Corolla $18,500 listing https://s.com/l/42"}]},
        {"iter": 2, "ts": now - 60, "policy": "fifo", "messages": [{"role": "system", "text": "S"}, {"role": "user", "text": "Find a red Toyota Corolla from 2018 to 2023."},
                                                                  {"role": "tool", "text": "[archived]"}]},
    ]
    turns = [
        ("browser_open", {"url": "https://s.com/"}, "session=1"),
        ("browser_get_markdown", {}, "Results: 2019 Corolla $18,500 listing https://s.com/l/42"),
        ("browser_navigate", {"url": "https://s.com/l/42"}, "opened listing 42"),
        ("browser_click_at", {"vision_index": 3, "target_label": "Contact"}, "[click_escalated strategy=js] ok"),
    ]
    clicks = [{"tool": "browser_click_at", "strategy": "js", "escalated": True, "silent": False, "ok": True, "method": "grid_scan"}]
    vision = [{"ok": True, "url": "https://s.com/", "dom_hash": "a", "dom_text_hash": "x", "cached": False},
              {"ok": True, "url": "https://s.com/", "dom_hash": "a", "dom_text_hash": "x", "cached": False}]
    run_dir = make_run_dir(tmp_path, arm="fifo", turns=turns, final="Best: 2019 Corolla $18,500 https://s.com/l/42",
                           judges={"webjudge": True}, live_context=live, clicks=clicks, vision_calls=vision,
                           tokens_in=[40000, 42000, 39000, 41000])
    # steps timestamps: make_run_dir uses now-90+i; the navigate (step 2) happens at now-88 -> context row iter1 (now-85)? no:
    # rows before the step: iter0 (now-95). The listing url is NOT in iter0 -> lost; final answer checks last row -> lost.
    rec = build_record(run_dir)
    rec.protocol["model"] = "openai/gpt-4o"
    rec.protocol["environment"] = {"vision_model": "google/gemini-3-flash-preview"}
    rec.cost = cost_of_record(rec)
    assert rec.cost["priced"] and rec.cost["usd"] > 0 and rec.cost["usd_cached"] <= rec.cost["usd"]
    m = compute_all(run_dir, rec)
    assert m["rpr"]["rpr"] == 0.5 and m["rpr"]["source"] == "vision_calls.jsonl"
    assert m["grounding"]["source"] == "clicks.jsonl" and m["grounding"]["recovery_success"] == 1.0
    assert m["grounding"]["first_path_success"] == 0.0
    assert m["efficiency"]["tool_calls"] == 4 and m["efficiency"]["worker_iterations"] == 4
    csd = m["csd"]
    assert csd["has_context_dump"] and csd["task_given_n"] == 3 and csd["csd_task_given"] == 1.0
    assert csd["observed_events"] >= 1 and csd["csd_observed"] is not None
    assert m["drr"]["drr"] == 0.0 and m["drr"]["steps"] == 4
    assert rec.metrics["csd"] is csd
