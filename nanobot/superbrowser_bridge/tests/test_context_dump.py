"""SUPERBROWSER_EVAL_CONTEXT_DUMP=1 records the post-policy live context."""
from __future__ import annotations

import asyncio
import copy
import os
import shutil
import sys
import uuid
from pathlib import Path

_NANOBOT_ROOT = Path(__file__).resolve().parents[2]
if str(_NANOBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_NANOBOT_ROOT))

from nanobot.agent.hook import AgentHookContext  # noqa: E402

from superbrowser_bridge.memory import Memory  # noqa: E402
from superbrowser_bridge.memory.eval_instrumentation import read_context_dump  # noqa: E402
from superbrowser_bridge.tests.test_memory_policy_golden import build_raw_transcript  # noqa: E402


def _run(env_on: bool):
    task_id = f"ctxdump-{uuid.uuid4().hex[:8]}"
    old = os.environ.get("SUPERBROWSER_EVAL_CONTEXT_DUMP")
    if env_on:
        os.environ["SUPERBROWSER_EVAL_CONTEXT_DUMP"] = "1"
    else:
        os.environ.pop("SUPERBROWSER_EVAL_CONTEXT_DUMP", None)
    try:
        memory = Memory(task_id, session_key="worker:ctx", role="worker")
        memory.set_goal("goal without url")
        hook = memory.attach(None)
        turns = build_raw_transcript(8)
        live = copy.deepcopy(turns[0])

        async def go():
            for i, turn in enumerate(turns[1:]):
                ctx = AgentHookContext(iteration=i, messages=live, session_key="worker:ctx")
                await hook.before_iteration(ctx)
                live.extend(copy.deepcopy(turn))
                await hook.after_iteration(ctx)

        asyncio.run(go())
        mem_dir = Path("/tmp/superbrowser") / task_id / "memory"
        rows = read_context_dump(mem_dir / "live_context.jsonl.gz")
        events = [l for l in (mem_dir / "events.jsonl").read_text().splitlines() if '"context_size"' in l]
        return rows, events, mem_dir
    finally:
        if old is None:
            os.environ.pop("SUPERBROWSER_EVAL_CONTEXT_DUMP", None)
        else:
            os.environ["SUPERBROWSER_EVAL_CONTEXT_DUMP"] = old
        shutil.rmtree(Path("/tmp/superbrowser") / task_id, ignore_errors=True)


def test_dump_off_by_default_writes_nothing():
    rows, events, mem_dir = _run(env_on=False)
    assert rows == [] and events == []


def test_dump_on_records_every_iteration_text_only():
    rows, events, _ = _run(env_on=True)
    assert len(rows) == 8 and len(events) == 8
    assert [r["iter"] for r in rows] == list(range(8))
    last = rows[-1]
    assert last["policy"] == "ledger"
    assert last["n_messages"] == len(last["messages"])
    assert last["messages"][0]["role"] == "system" and "[Agent Ledger v1]" in last["messages"][0]["text"]
    assert any(m["text"] == "[image]" or "[image]" in m["text"] for m in last["messages"])
    assert not any("base64" in m["text"] for m in last["messages"])
    assert last["est_tokens"] is None or last["est_tokens"] > 0
    assert any(m.get("tool_calls") for m in last["messages"])
