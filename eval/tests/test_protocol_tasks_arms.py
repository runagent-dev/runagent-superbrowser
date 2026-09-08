import json

import pytest

from eval.core import arms
from eval.core.protocol import DEFAULT_PROTOCOL, Protocol, protocol_from_args
from eval.core.tasks import (BENCH_DIR, excluded_task_ids, load_benchmark, load_subsets, resolve_tasks,
                             site_type, stratified_subset)


def test_protocol_hash_is_stable_and_sensitive():
    assert DEFAULT_PROTOCOL.hash() == Protocol().hash()
    assert Protocol(max_iterations=51).hash() != DEFAULT_PROTOCOL.hash()
    assert protocol_from_args(max_iterations=None, wall_clock_s=None) is DEFAULT_PROTOCOL


def test_protocol_env_pins_confounds_and_instrumentation():
    env = DEFAULT_PROTOCOL.env()
    assert env["SUPERBROWSER_CROSS_TASK_MEMORY"] == "0"
    assert env["LEARNING_READS_ENABLED"] == "0"
    assert env["SUPERBROWSER_COOKIE_JAR"] == "0" and env["SUPERBROWSER_IDENTITY_JAR"] == "0"
    assert env["SUPERBROWSER_TRACE_VISION"] == "1" and env["SUPERBROWSER_EVAL_CONTEXT_DUMP"] == "1"
    assert env["SUPERBROWSER_WORKER_MAX_ITER"] == str(DEFAULT_PROTOCOL.max_iterations)
    assert DEFAULT_PROTOCOL.nanobot_config_overrides()["maxToolIterations"] == DEFAULT_PROTOCOL.max_iterations


def test_host_snip_flag():
    assert Protocol(context_window_tokens=65536, max_tokens=100000).host_snip_active() is False
    assert Protocol(context_window_tokens=200000, max_tokens=16384).host_snip_active() is True


def test_frozen_hard_split_matches_manifest():
    manifest = json.loads((BENCH_DIR / "manifest.json").read_text())
    tasks = load_benchmark("online_mind2web_hard")
    assert len(tasks) == manifest["files"]["online_mind2web_hard.jsonl"]["n"] == 74
    assert all(t.level == "hard" for t in tasks)
    assert len({t.task_id for t in tasks}) == len(tasks)
    assert all(t.start_url and t.instruction for t in tasks)
    # every hard task has hand-reviewed critical-state items
    crit = json.loads((BENCH_DIR / "critical_state.json").read_text())
    assert all(crit[t.task_id]["reviewed"] for t in tasks)
    assert all(t.critical_state for t in tasks)


def test_subsets_resolve_and_are_pre_registered():
    subsets = load_subsets()
    assert "ablation24" in subsets and subsets["ablation24"]["n"] == 24
    sub = resolve_tasks("ablation24", benchmark="online_mind2web_hard")
    assert [t.task_id for t in sub] == [t for t in [x.task_id for x in load_benchmark("online_mind2web_hard")]
                                        if t in set(subsets["ablation24"]["task_ids"])]
    ids = ",".join(t.task_id for t in sub[:2])
    assert [t.task_id for t in resolve_tasks(ids, benchmark="online_mind2web_hard")] == [t.task_id for t in sub[:2]]
    with pytest.raises(KeyError):
        resolve_tasks("no-such-task-id", benchmark="online_mind2web_hard")


def test_stratified_subset_is_deterministic_and_covers_strata():
    tasks = load_benchmark("online_mind2web_hard")
    a = stratified_subset(tasks, 24)
    b = stratified_subset(tasks, 24)
    assert [t.task_id for t in a] == [t.task_id for t in b]
    assert len(a) == 24
    assert len({site_type(t) for t in a}) == len({site_type(t) for t in tasks})


def test_exclusions_hook_is_empty_until_user_fills_it():
    assert excluded_task_ids() == {}


def test_arm_registry_integrity():
    assert arms.get("ledger").env == {}
    names = list(arms.ARMS)
    assert len(names) == len(set(names))
    for a in arms.ARMS.values():
        if a.side == "python":
            assert not a.ts_env, a.name
        if a.side == "ts":
            assert a.env == a.ts_env or a.env == {}, a.name
    assert arms.get("no_ladder").side == "both" and arms.get("no_ladder").ts_env == {"SUPERBROWSER_CLICK_TIERS": "tier1"}
    assert arms.resolve("ledger,fifo")[1].env == {"SUPERBROWSER_MEMORY_POLICY": "fifo"}
    with pytest.raises(KeyError):
        arms.get("nope")


def test_derived_arms_keep_provenance():
    p = arms.pressure_arm("fifo", 1500)
    assert p.name == "fifo__p1500" and p.env["SUPERBROWSER_MEMORY_POLICY"] == "fifo"
    assert p.env["SUPERBROWSER_EVAL_DISTRACTOR_TOKENS"] == "1500"
    b = arms.budget_arm("ledger", budget_tokens=1024, recent_k=3)
    assert b.name == "ledger__B1024_K3" and b.env["SUPERBROWSER_MEMORY_BUDGET_TOKENS"] == "1024"
    assert arms.ts_signature(arms.get("snap_center")) == (("SUPERBROWSER_SNAP_STRATEGY", "center"),)


def test_annotation_filter_selects_and_stays_deterministic():
    """--tasks "level=hard,n=10" must give the same 10 tasks every time: a task
    set has to be fixed before any arm runs, not re-drawn per launch."""
    from collections import Counter

    from eval.core.tasks import resolve_tasks

    a = [t.task_id for t in resolve_tasks("level=hard,n=10", benchmark="online_mind2web_all")]
    b = [t.task_id for t in resolve_tasks("level=hard,n=10", benchmark="online_mind2web_all")]
    assert a == b and len(a) == 10
    assert a != [t.task_id for t in resolve_tasks("level=hard,n=10,seed=99", benchmark="online_mind2web_all")]

    multi = resolve_tasks("level=easy|medium,antibot=low,n=8", benchmark="online_mind2web_all")
    assert len(multi) == 8
    assert {t.level for t in multi} <= {"easy", "medium"}
    assert {str(t.extra.get("antibot_risk")) for t in multi} == {"low"}

    strat = resolve_tasks("n=12,stratify=level", benchmark="online_mind2web_all")
    assert dict(Counter(t.level for t in strat)) == {"easy": 4, "medium": 4, "hard": 4}

    # a filter is never mistaken for an id list or a subset name
    assert [t.task_id for t in resolve_tasks("level=hard", benchmark="online_mind2web_all")] == \
           sorted(t.task_id for t in resolve_tasks("all", benchmark="online_mind2web_hard"))


def test_annotation_filter_errors_are_actionable():
    import pytest

    from eval.core.tasks import resolve_tasks

    with pytest.raises(KeyError, match="online_mind2web_all"):
        resolve_tasks("level=easy,n=5", benchmark="online_mind2web_hard")
    with pytest.raises(KeyError, match="unknown filter field"):
        resolve_tasks("difficulty=hard,n=5", benchmark="online_mind2web_all")
    with pytest.raises(ValueError, match="expected key=value"):
        resolve_tasks("level=hard,10", benchmark="online_mind2web_all")


def test_catalog_annotations_reach_the_tasks():
    """category / antibot_risk / attention_level must survive into Task.extra,
    and the external third-party columns must NOT be in the benchmark rows."""
    from eval.core.tasks import load_benchmark

    tasks = load_benchmark("online_mind2web_all", annotate=False)
    assert len(tasks) == 300
    assert all(t.extra.get("category") for t in tasks)
    assert {str(t.extra.get("antibot_risk")) for t in tasks} <= {"low", "medium", "high", "unknown"}
    assert not any("external" in t.extra or "tinyfish" in str(t.extra).lower() for t in tasks)


def test_subset_resolves_against_the_benchmark_it_was_frozen_from():
    """A pilot drawn from the 300-task split must still resolve when the
    experiment's default benchmark is the hard-only file."""
    from eval.core.tasks import load_subsets, resolve_tasks

    subsets = load_subsets()
    pilot = next((k for k, v in subsets.items() if v.get("benchmark") == "online_mind2web_all"), None)
    if pilot is None:
        import pytest
        pytest.skip("no cross-benchmark subset registered")
    tasks = resolve_tasks(pilot, benchmark="online_mind2web_hard")
    assert len(tasks) == subsets[pilot]["n"]
    assert {t.level for t in tasks} - {"hard"}, "the pilot should span more than the hard split"
