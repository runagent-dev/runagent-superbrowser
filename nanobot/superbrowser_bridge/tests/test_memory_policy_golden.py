"""Characterisation ("golden") test for the memory hook's default behaviour.

The memory-policy experiment (eval E2) adds alternative retention policies
behind ``SUPERBROWSER_MEMORY_POLICY``. The production path — the six-phase
eviction loop + Ledger injection — must stay BYTE-IDENTICAL when that env is
unset. This test replays a deterministic synthetic worker transcript through
``MemoryHook.before_iteration`` iteration by iteration (mirroring the runner:
the hook sees the live list, then the next turn is appended) and compares the
resulting message lists with a recorded expectation.

Recorded on the pre-refactor hook (commit before the policy plug-in landed).
Regenerate ONLY when a behaviour change is intended:

    RECORD_GOLDEN=1 PYTHONPATH=nanobot venv/bin/python -m pytest \
        nanobot/superbrowser_bridge/tests/test_memory_policy_golden.py -q

Three recorded variants: env unset, ABLATE_MEMORY_EVICTION=1,
ABLATE_STRUCTURED_LEDGER=1.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

import pytest

_NANOBOT_ROOT = Path(__file__).resolve().parents[2]
if str(_NANOBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_NANOBOT_ROOT))

from nanobot.agent.hook import AgentHookContext  # noqa: E402

from superbrowser_bridge.memory import Memory  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "memory_policy"
RECORD = os.environ.get("RECORD_GOLDEN") == "1"
PNG_1x1_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


# ----------------------------------------------------------------- fixture
def _state_block(url: str, title: str, step: int) -> str:
    return (f"[SESSION_STATE session_id=session-1 url={url} title={title} step={step}]\n"
            f"Elements: 12 interactive\n\nPage body text for step {step}.")


def _elements_block(n: int) -> str:
    rows = "\n".join(f"[{i}] <button> Option {i}" for i in range(n))
    return f"[ELEMENTS {n} shown of {n}]\n{rows}"


def build_raw_transcript(n_turns: int = 24) -> list[list[dict]]:
    """Deterministic worker transcript as a list of turns; turn 0 is
    [system, user]; later turns are [assistant(+tool_calls), tool...]."""
    turns: list[list[dict]] = [[
        {"role": "system", "content": "You are the browser worker. SOUL text."},
        {"role": "user", "content": "Go to https://shop.example.com/ and find a red 2019 Corolla under $20,000."},
    ]]
    urls = ["https://shop.example.com/", "https://shop.example.com/search?q=corolla",
            "https://shop.example.com/search?q=corolla&color=red", "https://shop.example.com/listing/42"]
    for i in range(1, n_turns + 1):
        cid = f"call_{i}"
        url = urls[min(3, i // 6)]
        title = f"Page {i // 6}"
        kind = i % 6
        if kind == 1:
            tc = {"id": cid, "type": "function", "function": {"name": "browser_screenshot", "arguments": "{}"}}
            content = [
                {"type": "text", "text": f"[VISION intent=observe page_type=search_results cached=false model=vision dur=900ms]\n"
                                         f"[V1] Search box [V2] Red filter [V3] Year ▼\n" + _state_block(url, title, i)},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_1x1_B64}"}},
            ]
            turn = [{"role": "assistant", "content": "", "tool_calls": [tc],
                     "thinking_blocks": [{"type": "thinking", "thinking": f"think {i}"}], "reasoning_content": f"reason {i}"},
                    {"role": "tool", "tool_call_id": cid, "name": "browser_screenshot", "content": content}]
        elif kind == 2:
            tc = {"id": cid, "type": "function", "function": {"name": "browser_list_elements", "arguments": "{}"}}
            turn = [{"role": "assistant", "content": "", "tool_calls": [tc]},
                    {"role": "tool", "tool_call_id": cid, "name": "browser_list_elements",
                     "content": _elements_block(8) + "\n" + _state_block(url, title, i)}]
        elif kind == 3:
            tc = {"id": cid, "type": "function", "function": {"name": "browser_click_at",
                                                              "arguments": json.dumps({"vision_index": 2, "expected_label": "Red"})}}
            result = ("[click_at_failed:blocker_active layer=L0_banner] A blocker layer is on top of content.\n"
                      if i % 12 == 3 else f"Clicked V2 snapped→(120,340) button.filter-red\n") + _state_block(url, title, i)
            turn = [{"role": "assistant", "content": "", "tool_calls": [tc],
                     "thinking_blocks": [{"type": "thinking", "thinking": f"think {i}"}]},
                    {"role": "tool", "tool_call_id": cid, "name": "browser_click_at", "content": result}]
        elif kind == 4:
            # multi-tool turn (two calls, two results) — pairing must survive every pass
            tc1 = {"id": cid + "a", "type": "function", "function": {"name": "browser_type_at",
                                                                     "arguments": json.dumps({"vision_index": 1, "text": "corolla"})}}
            tc2 = {"id": cid + "b", "type": "function", "function": {"name": "browser_keys", "arguments": json.dumps({"keys": "Enter"})}}
            turn = [{"role": "assistant", "content": "", "tool_calls": [tc1, tc2]},
                    {"role": "tool", "tool_call_id": cid + "a", "name": "browser_type_at", "content": "typed corolla"},
                    {"role": "tool", "tool_call_id": cid + "b", "name": "browser_keys",
                     "content": ("HTTP 403 Forbidden while loading results\n" if i == 10 else "pressed Enter\n") + _state_block(url, title, i)}]
        elif kind == 5:
            # image-bearing result WITHOUT a state block (e.g. an image-region
            # crop) so the screenshot back-patch pass has something to evict
            # that the state-block pass does not stringify first
            tc = {"id": cid, "type": "function", "function": {"name": "browser_get_markdown", "arguments": "{}"}}
            turn = [{"role": "assistant", "content": f"Reading results at step {i}.", "tool_calls": [tc]},
                    {"role": "tool", "tool_call_id": cid, "name": "browser_get_markdown",
                     "content": [{"type": "text", "text": f"# Results\n2019 Corolla red $18,500 — listing 42 (step {i})"},
                                 {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_1x1_B64}"}}]}]
        else:
            tc = {"id": cid, "type": "function", "function": {"name": "browser_scroll", "arguments": json.dumps({"direction": "down"})}}
            turn = [{"role": "assistant", "content": "", "tool_calls": [tc]},
                    {"role": "tool", "tool_call_id": cid, "name": "browser_scroll",
                     "content": ("[VERIFY_MISS kind=dom_mutated reason=no_change]\n" if i == 18 else "scrolled 600px\n") + _state_block(url, title, i)}]
        if i == 13:
            turn.append({"role": "user", "content": "[GUIDANCE] mid-run operator note: stay on the same site."})
        turns.append(turn)
    return turns


_TS_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}\b")


def _normalise(snapshots: list) -> str:
    text = json.dumps(snapshots, sort_keys=True, ensure_ascii=False, indent=1)
    return _TS_RE.sub("HH:MM:SS", text)


async def _replay(turns: list[list[dict]], *, env: dict[str, str]) -> list[list[dict]]:
    old_env = {k: os.environ.get(k) for k in ("SUPERBROWSER_MEMORY_POLICY", "ABLATE_MEMORY_EVICTION",
                                               "ABLATE_STRUCTURED_LEDGER", "SUPERBROWSER_EVAL_CONTEXT_DUMP",
                                               "SUPERBROWSER_MEMORY_RECENT_K", "SUPERBROWSER_MEMORY_BUDGET_TOKENS",
                                               "SUPERBROWSER_MEMORY_KEEP_SCREENSHOTS")}
    for k in old_env:
        os.environ.pop(k, None)
    os.environ.update(env)
    task_id = f"golden-{uuid.uuid4().hex[:8]}"
    try:
        memory = Memory(task_id, session_key="worker:golden", role="worker")
        memory.set_goal("Find a red 2019 Corolla under $20,000 on the shop")  # no URL: avoids site-model ingest
        memory.begin_subgoal("delegated: golden", message_floor=0)
        hook = memory.attach(None)
        live: list[dict] = copy.deepcopy(turns[0])
        snapshots: list[list[dict]] = []
        for i, turn in enumerate(turns[1:]):
            ctx = AgentHookContext(iteration=i, messages=live, session_key="worker:golden")
            await hook.before_iteration(ctx)
            assert ctx.messages is live
            snapshots.append(copy.deepcopy(live))
            live.extend(copy.deepcopy(turn))
            await hook.after_iteration(ctx)
        return snapshots
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(Path("/tmp/superbrowser") / task_id, ignore_errors=True)


VARIANTS = {
    "ledger_default": {},
    "ablate_eviction": {"ABLATE_MEMORY_EVICTION": "1"},
    "ablate_ledger": {"ABLATE_STRUCTURED_LEDGER": "1"},
}


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_default_memory_hook_is_byte_identical_to_golden(name):
    turns = build_raw_transcript()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    raw_path = FIXTURES / "raw_transcript.json"
    if RECORD or not raw_path.exists():
        raw_path.write_text(json.dumps(turns, indent=1, ensure_ascii=False))
    assert json.loads(raw_path.read_text()) == turns, "fixture builder changed — regenerate goldens deliberately"
    got = _normalise(asyncio.run(_replay(turns, env=VARIANTS[name])))
    expected_path = FIXTURES / f"{name}.expected.json"
    if RECORD or not expected_path.exists():
        expected_path.write_text(got)
        pytest.skip(f"recorded golden {expected_path.name}")
    expected = expected_path.read_text()
    assert got == expected, f"memory hook output diverged from golden {expected_path.name}"


def test_golden_fixture_exercises_every_pass():
    """The recorded default run must show all six passes doing work,
    otherwise the golden would not protect them."""
    text = (FIXTURES / "ledger_default.expected.json").read_text()
    assert "[screenshot from prior turn evicted]" in text
    assert "[collapsed earlier failure:" in text
    assert "[earlier element list collapsed" in text
    assert "[state from prior turn evicted]" in text
    assert '"[archived]"' in text
    assert "[Agent Ledger v1]" in text
    assert '"thinking_blocks": []' in text
