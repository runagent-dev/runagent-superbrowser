"""Eval-only switches: distractor hook (E3), perception-reuse and click-ladder
umbrellas (E5/E7), and their inertness by default."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

_NANOBOT_ROOT = Path(__file__).resolve().parents[2]
if str(_NANOBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_NANOBOT_ROOT))

from superbrowser_bridge.memory import Memory  # noqa: E402
from superbrowser_bridge.memory.eval_instrumentation import (_token_len, eval_worker_hooks,  # noqa: E402
                                                             generate_distractor_block)
from superbrowser_bridge.memory.hook import _ELEMENT_LIST_RE, _FAILURE_RE, _STATE_BLOCK_RE  # noqa: E402
from superbrowser_bridge.session_tools import ablations  # noqa: E402

KEYS = ("SUPERBROWSER_EVAL_DISTRACTOR_TOKENS", "ABLATE_VISION_REUSE", "ABLATE_CLICK_LADDER", "VISION_ASYNC_PREFETCH",
        "VISION_CACHE_TTL_SEC", "VISION_MAX_AGE_TURNS", "FRESH_VISION_SECONDS", "CLICK_LADDER_AUTO")


class _Env:
    def __init__(self, **env):
        self.env = env

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in KEYS}
        for k in KEYS:
            os.environ.pop(k, None)
        os.environ.update(self.env)

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_distractor_block_is_deterministic_sized_and_marker_free():
    a = generate_distractor_block("task:0:3:call_1", 1500)
    b = generate_distractor_block("task:0:3:call_1", 1500)
    c = generate_distractor_block("task:0:4:call_1", 1500)
    assert a == b and a != c
    assert 1500 <= _token_len(a) <= 1500 * 1.15
    assert generate_distractor_block("x", 0) == ""
    for rx in (_FAILURE_RE, _STATE_BLOCK_RE, _ELEMENT_LIST_RE, re.compile(r"\[V\d+\]"), re.compile(r"DEAD_ENDS_HERE|GUIDANCE|index=")):
        assert not rx.search(a)
    assert a.startswith("\n\n[PAGE_CONTEXT_SNAPSHOT")


def test_eval_worker_hooks_empty_by_default_and_appends_distractors_when_set():
    tid = f"dx-{uuid.uuid4().hex[:8]}"
    try:
        memory = Memory(tid, session_key="worker:dx", role="worker")
        with _Env():
            assert eval_worker_hooks(memory) == []
        with _Env(SUPERBROWSER_EVAL_DISTRACTOR_TOKENS="300"):
            hooks = eval_worker_hooks(memory)
        assert len(hooks) == 1
        hook = hooks[0]
        messages = [
            {"role": "system", "content": "s"}, {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "browser_click_at", "arguments": "{}"}},
                                                                {"id": "c2", "function": {"name": "browser_screenshot", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "clicked"},
            {"role": "tool", "tool_call_id": "c2", "content": [{"type": "text", "text": "[VISION ...]"}]},
        ]
        ctx = SimpleNamespace(iteration=4, messages=messages,
                              tool_calls=[SimpleNamespace(id="c1"), SimpleNamespace(id="c2")])
        asyncio.run(hook.after_iteration(ctx))
        assert messages[3]["content"].startswith("clicked\n\n[PAGE_CONTEXT_SNAPSHOT")
        assert messages[4]["content"][-1]["text"].startswith("\n\n[PAGE_CONTEXT_SNAPSHOT")
        assert messages[2]["content"] == ""  # assistant untouched
        # deterministic across a replay of the same iteration
        again = [{"role": "tool", "tool_call_id": "c1", "content": "clicked"}]
        ctx2 = SimpleNamespace(iteration=4, messages=messages[:3] + again, tool_calls=[SimpleNamespace(id="c1")])
        asyncio.run(hook.after_iteration(ctx2))
        assert again[0]["content"] == messages[3]["content"]
        events = (Path("/tmp/superbrowser") / tid / "memory" / "events.jsonl").read_text()
        assert '"distractor_appended"' in events
    finally:
        shutil.rmtree(Path("/tmp/superbrowser") / tid, ignore_errors=True)


def test_ablation_resolvers_default_and_umbrella():
    with _Env():
        assert ablations.async_prefetch_enabled() is True
        assert ablations.vision_cache_ttl_s() == 60.0
        assert ablations.vision_max_age_turns() == 1
        assert ablations.fresh_vision_seconds() == 10.0
        assert ablations.click_ladder_auto_enabled() is True
    with _Env(VISION_ASYNC_PREFETCH="0", VISION_CACHE_TTL_SEC="5", VISION_MAX_AGE_TURNS="3", FRESH_VISION_SECONDS="2.5",
              CLICK_LADDER_AUTO="0"):
        assert ablations.async_prefetch_enabled() is False
        assert ablations.vision_cache_ttl_s() == 5.0 and ablations.vision_max_age_turns() == 3
        assert ablations.fresh_vision_seconds() == 2.5 and ablations.click_ladder_auto_enabled() is False
    with _Env(ABLATE_VISION_REUSE="1", ABLATE_CLICK_LADDER="1", VISION_CACHE_TTL_SEC="99", VISION_MAX_AGE_TURNS="9"):
        assert ablations.async_prefetch_enabled() is False
        assert ablations.vision_cache_ttl_s() == 0.0 and ablations.vision_max_age_turns() == 0
        assert ablations.fresh_vision_seconds() == 0.0 and ablations.click_ladder_auto_enabled() is False
        from vision_agent.cache import VisionCache

        assert VisionCache.from_env()._ttl == 0.0
