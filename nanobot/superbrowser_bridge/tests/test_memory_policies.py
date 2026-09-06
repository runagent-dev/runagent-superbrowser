"""Matched memory policies (SUPERBROWSER_MEMORY_POLICY) — structural invariants,
budget behaviour, compressor accounting, dead-end and cross-task gates."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

_NANOBOT_ROOT = Path(__file__).resolve().parents[2]
if str(_NANOBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_NANOBOT_ROOT))

from nanobot.agent.hook import AgentHookContext  # noqa: E402

from superbrowser_bridge.memory import Memory  # noqa: E402
from superbrowser_bridge.memory.policy import (MemoryPolicyConfig, archive_range, cap_ledger_render,  # noqa: E402
                                               turn_starts, window_start)
from superbrowser_bridge.tests.test_memory_policy_golden import build_raw_transcript  # noqa: E402
from superbrowser_bridge.usage import pop, snapshot, track_task  # noqa: E402

ENV_KEYS = ("SUPERBROWSER_MEMORY_POLICY", "SUPERBROWSER_MEMORY_RECENT_K", "SUPERBROWSER_MEMORY_BUDGET_TOKENS",
            "SUPERBROWSER_MEMORY_KEEP_SCREENSHOTS", "ABLATE_DEAD_END_MEMORY", "SUPERBROWSER_CROSS_TASK_MEMORY",
            "ABLATE_MEMORY_EVICTION", "ABLATE_STRUCTURED_LEDGER", "SUPERBROWSER_EVAL_CONTEXT_DUMP")


class _Env:
    def __init__(self, **env):
        self.env = env

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(self.env)

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeProvider:
    def __init__(self, replies=None, fail=False):
        self.replies = list(replies or [])
        self.fail = fail
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("provider down")
        text = self.replies.pop(0) if self.replies else "### TASK PROGRESS\nsummary v%d" % len(self.calls)
        return SimpleNamespace(content=text, usage={"input_tokens": 1000, "output_tokens": 50}, finish_reason="stop")


def _fake_bot(provider):
    loop = SimpleNamespace(provider=provider, model="test/model", tools=SimpleNamespace(get_definitions=lambda: []))
    return SimpleNamespace(_loop=loop)


def _replay(env: dict, *, n_turns: int = 20, bot=None, task_uid: str | None = None, goal="find red corolla"):
    """Run the hook over the golden transcript; return (snapshots, memory, task_id)."""
    task_id = task_uid or f"pol-{uuid.uuid4().hex[:8]}"
    with _Env(**env):
        memory = Memory(task_id, session_key="worker:pol", role="worker")
        memory.set_goal(goal)
        memory.begin_subgoal("delegated: pol", message_floor=0)
        hook = memory.attach(bot)
        turns = build_raw_transcript(n_turns)
        live = copy.deepcopy(turns[0])
        snaps = []

        async def go():
            for i, turn in enumerate(turns[1:]):
                ctx = AgentHookContext(iteration=i, messages=live, session_key="worker:pol")
                await hook.before_iteration(ctx)
                snaps.append(copy.deepcopy(live))
                live.extend(copy.deepcopy(turn))
                await hook.after_iteration(ctx)
            ctx = AgentHookContext(iteration=n_turns, messages=live, session_key="worker:pol")
            await hook.before_iteration(ctx)
            snaps.append(copy.deepcopy(live))

        asyncio.run(go())
    return snaps, memory, task_id, turns


def _cleanup(task_id):
    shutil.rmtree(Path("/tmp/superbrowser") / task_id, ignore_errors=True)


def _assert_structure(before: list[dict], after: list[dict]):
    assert len(before) == len(after)
    for b, a in zip(before, after):
        assert b.get("role") == a.get("role")
        assert b.get("tool_calls") == a.get("tool_calls")
        assert b.get("tool_call_id") == a.get("tool_call_id")
    assert after[0]["role"] == "system" and after[1]["role"] == "user"


def _events(task_id):
    p = Path("/tmp/superbrowser") / task_id / "memory" / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# ------------------------------------------------------------------ helpers
def test_turn_helpers():
    msgs = [{"role": "system"}, {"role": "user"}]
    for i in range(7):
        msgs.append({"role": "assistant", "tool_calls": [{"id": f"c{i}"}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "r"})
        if i == 2:
            msgs.append({"role": "tool", "tool_call_id": f"c{i}b", "content": "r2"})
    starts = turn_starts(msgs)
    assert len(starts) == 7 and all(msgs[i]["role"] == "assistant" for i in starts)
    assert window_start(msgs, 5) == starts[-5]
    assert window_start(msgs, 7) is None and window_start(msgs, 10) is None
    n = archive_range(msgs, 2, starts[-5])
    assert n == 2 * 2  # turns 0 and 1 (assistant + tool each); turn 2's extra result is inside the window
    assert msgs[3]["content"] == "[archived]" and msgs[3]["tool_call_id"] == "c0"
    assert archive_range(msgs, 2, starts[-5]) == 0  # idempotent


def test_config_from_env_and_defaults():
    with _Env():
        cfg = MemoryPolicyConfig.from_env()
        assert cfg.is_default and cfg.name == "ledger" and cfg.recent_k == 5 and cfg.keep_screenshots == 2
    with _Env(SUPERBROWSER_MEMORY_POLICY="fifo", SUPERBROWSER_MEMORY_RECENT_K="3",
              SUPERBROWSER_MEMORY_BUDGET_TOKENS="1024", ABLATE_DEAD_END_MEMORY="1", SUPERBROWSER_CROSS_TASK_MEMORY="0"):
        cfg = MemoryPolicyConfig.from_env()
        assert (cfg.name, cfg.recent_k, cfg.budget_tokens, cfg.dead_ends, cfg.cross_task) == ("fifo", 3, 1024, False, False)
        assert not cfg.inject_dead_ends and not cfg.inject_ledger
    with _Env(SUPERBROWSER_MEMORY_POLICY="nope"):
        with pytest.raises(ValueError):
            MemoryPolicyConfig.from_env()
    with _Env(SUPERBROWSER_MEMORY_KEEP_SCREENSHOTS="all"):
        assert MemoryPolicyConfig.from_env().keep_screenshots >= 10**5


# ----------------------------------------------------------------- policies
def test_fifo_keeps_last_k_turns_and_structure():
    snaps, memory, tid, turns = _replay({"SUPERBROWSER_MEMORY_POLICY": "fifo", "SUPERBROWSER_MEMORY_RECENT_K": "4"})
    try:
        raw = copy.deepcopy(turns[0])
        for t in turns[1:]:
            raw.extend(copy.deepcopy(t))
        final = snaps[-1]
        _assert_structure(raw, final)
        assert isinstance(final[0]["content"], str) and "[Agent Ledger" not in final[0]["content"]
        starts = turn_starts(final)
        ws = starts[-4]
        # everything before the window is archived, the window is verbatim
        for i in range(2, ws):
            m = final[i]
            assert m.get("_archived"), i
            if m["role"] == "tool":
                assert m["content"] == "[archived]"
        for i in range(ws, len(final)):
            assert not final[i].get("_archived")
            assert final[i]["content"] == raw[i]["content"]
        assert memory.dead_ends_for_url("https://shop.example.com/") == []
        ev = _events(tid)
        assert any(e["type"] == "messages_archived" for e in ev)
        assert not any(e["type"] in ("messages_gutted", "ledger_injected", "failures_collapsed") for e in ev)
        assert [e for e in ev if e["type"] == "memory_attach"][0]["policy"] == "fifo"
    finally:
        _cleanup(tid)


def test_full_history_never_mutates_messages():
    snaps, memory, tid, turns = _replay({"SUPERBROWSER_MEMORY_POLICY": "full"})
    try:
        raw = copy.deepcopy(turns[0])
        for t in turns[1:]:
            raw.extend(copy.deepcopy(t))
        assert snaps[-1] == raw
        assert not any(e["type"] in ("messages_archived", "messages_gutted", "ledger_injected", "screenshot_evicted")
                       for e in _events(tid))
    finally:
        _cleanup(tid)


def test_ledger_noevict_injects_ledger_records_dead_ends_once():
    snaps, memory, tid, turns = _replay({"SUPERBROWSER_MEMORY_POLICY": "ledger_noevict"})
    try:
        raw = copy.deepcopy(turns[0])
        for t in turns[1:]:
            raw.extend(copy.deepcopy(t))
        final = snaps[-1]
        assert isinstance(final[0]["content"], list) and "[Agent Ledger v1]" in final[0]["content"][1]["text"]
        assert final[1:] == raw[1:]  # nothing but the system slot changed
        n_fail_msgs = sum(1 for t in turns[1:] for m in t if m.get("role") == "tool"
                          and any(k in json.dumps(m.get("content")) for k in ("click_at_failed", "HTTP 403", "VERIFY_MISS")))
        assert n_fail_msgs >= 3
        # one dead-end per failing tool message (deduplicated across iterations)
        assert len(memory.ledger.dead_ends) == n_fail_msgs - 1 or len(memory.ledger.dead_ends) == n_fail_msgs
        assert any(e["type"] == "failures_scanned" for e in _events(tid))
    finally:
        _cleanup(tid)


def test_summary_policy_compresses_with_host_model_and_books_compressor_usage():
    provider = FakeProvider(["### TASK PROGRESS\nfirst", "### TASK PROGRESS\nsecond with $18,500"])
    tid = f"pol-{uuid.uuid4().hex[:8]}"
    try:
        with track_task(tid):
            snaps, memory, tid, turns = _replay({"SUPERBROWSER_MEMORY_POLICY": "summary", "SUPERBROWSER_MEMORY_RECENT_K": "3",
                                                 "SUPERBROWSER_MEMORY_BUDGET_TOKENS": "200"},
                                                bot=_fake_bot(provider), task_uid=tid)
            usage = snapshot(tid)
        assert provider.calls, "compressor never called"
        first = provider.calls[0]
        assert first["messages"][0]["role"] == "system" and "TASK PROGRESS" in first["messages"][0]["content"]
        assert "PREVIOUS SUMMARY:\n(none)" in first["messages"][1]["content"]
        assert first["model"] == "test/model" and first["tools"] is None
        if len(provider.calls) > 1:
            assert "PREVIOUS SUMMARY:\n### TASK PROGRESS\nfirst" in provider.calls[1]["messages"][1]["content"]
        final = snaps[-1]
        slots = [m for m in final if m.get("_summary_slot")]
        assert len(slots) == 1
        slot = slots[0]
        assert slot["role"] == "tool" and slot["tool_call_id"] == "call_1"
        assert slot["content"].startswith("[HISTORY SUMMARY")
        raw = copy.deepcopy(turns[0])
        for t in turns[1:]:
            raw.extend(copy.deepcopy(t))
        _assert_structure(raw, final)
        assert usage is not None and usage.by_role["compressor"].calls == len(provider.calls)
        assert usage.by_role["compressor"].input_tokens == 1000 * len(provider.calls)
        ev = _events(tid)
        assert sum(1 for e in ev if e["type"] == "compressor_call" and e["ok"]) == len(provider.calls)
        assert any(e["type"] == "summary_refreshed" for e in ev)
    finally:
        pop(tid)
        _cleanup(tid)


def test_summary_policy_falls_back_extractively_after_two_failures():
    provider = FakeProvider(fail=True)
    snaps, memory, tid, turns = _replay({"SUPERBROWSER_MEMORY_POLICY": "summary", "SUPERBROWSER_MEMORY_RECENT_K": "3",
                                         "SUPERBROWSER_MEMORY_BUDGET_TOKENS": "100"}, bot=_fake_bot(provider))
    try:
        ev = _events(tid)
        assert any(e["type"] == "compressor_fallback" for e in ev)
        assert any(m.get("_summary_slot") for m in snaps[-1])
    finally:
        _cleanup(tid)


def test_ledger_budget_cap_only_when_env_set():
    tid = f"pol-{uuid.uuid4().hex[:8]}"
    try:
        with _Env():
            memory = Memory(tid, session_key="worker:pol", role="worker")
            memory.set_goal("g")
            for i in range(40):
                memory.remember(f"fact_{i}", "value " * 30 + str(i))
            for i in range(20):
                memory.mark_dead_end(f"dead end number {i} with a long description " * 3, url="https://x.com/a")
            full = memory.render_for_llm()
        capped, caps = cap_ledger_render(memory, full, 300)
        assert caps is not None and len(capped) < len(full)
        from superbrowser_bridge.memory.policy import estimate_tokens
        assert estimate_tokens(capped) <= 300 + 20
        same, none = cap_ledger_render(memory, full, 10**6)
        assert none is None and same == full
    finally:
        _cleanup(tid)


def test_dead_end_ablation_and_cross_task_gate():
    tid = f"pol-{uuid.uuid4().hex[:8]}"
    try:
        with _Env(ABLATE_DEAD_END_MEMORY="1", SUPERBROWSER_CROSS_TASK_MEMORY="0"):
            memory = Memory(tid, session_key="worker:pol", role="worker")
            memory.set_goal("Go to https://petfinder.com/ and find rabbits")  # would ingest a site model
            memory.mark_dead_end("clicked the wrong thing", url="https://petfinder.com/")
            assert memory.ledger.dead_ends == []
            assert memory.dead_ends_for_url("https://petfinder.com/") == []
            assert "DEAD_ENDS" not in memory.render_for_llm()
            assert not any(e["type"] == "site_model_ingested" for e in _events(tid))
            memory.write_task_summary(success=True)
            assert not any(e["type"] == "task_summary_written" for e in _events(tid))
    finally:
        _cleanup(tid)
