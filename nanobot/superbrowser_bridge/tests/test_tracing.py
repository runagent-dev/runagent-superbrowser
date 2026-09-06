"""Research trace helpers: inert by default, JSONL next to the ledger when enabled."""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

_NANOBOT_ROOT = Path(__file__).resolve().parents[2]
if str(_NANOBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_NANOBOT_ROOT))

from superbrowser_bridge.memory import Memory  # noqa: E402
from superbrowser_bridge.session_tools import tracing  # noqa: E402
from superbrowser_bridge.session_tools.tools._click_core import trace_click_outcome  # noqa: E402


def _state(task_id: str):
    memory = Memory(task_id, session_key="worker:trace", role="worker")
    return SimpleNamespace(memory=memory, session_id="session-1", current_url="https://x.com/a",
                           step_counter=3, action_count=5, vision_calls=2)


def _with_env(**env):
    class _Ctx:
        def __enter__(self):
            self.old = {k: os.environ.get(k) for k in env}
            os.environ.update(env)

        def __exit__(self, *a):
            for k, v in self.old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return _Ctx()


def test_vision_and_click_traces_are_inert_by_default():
    tid = f"trace-{uuid.uuid4().hex[:8]}"
    try:
        st = _state(tid)
        for k in ("SUPERBROWSER_TRACE_VISION", "SUPERBROWSER_TRACE_CLICKS"):
            os.environ.pop(k, None)
        tracing.trace_vision(st, path="sync", url="u", dom_hash="d", dom_text_hash="t", resp=None)
        tracing.trace_click(st, tool="browser_click_at")
        trace_click_outcome(st, tool="browser_click_at", target="V1", data={"snap": {}}, verify_note="")
        mem_dir = Path("/tmp/superbrowser") / tid / "memory"
        assert not (mem_dir / "vision_calls.jsonl").exists()
        assert not (mem_dir / "clicks.jsonl").exists()
    finally:
        shutil.rmtree(Path("/tmp/superbrowser") / tid, ignore_errors=True)


def test_vision_trace_records_fingerprint_and_cache_flag():
    tid = f"trace-{uuid.uuid4().hex[:8]}"
    try:
        st = _state(tid)
        resp = SimpleNamespace(cached=True, model="vision-x", duration_ms=12, tokens_used=345,
                               bboxes=[1, 2, 3], page_type="search_results", screenshot_freshness="fresh")
        with _with_env(SUPERBROWSER_TRACE_VISION="1"):
            tracing.trace_vision(st, path="prefetch", url="https://x.com/b", dom_hash="abc", dom_text_hash="def",
                                 resp=resp, intent="observe")
            tracing.trace_vision(st, path="sync", url="https://x.com/b", dom_hash="abc", dom_text_hash="",
                                 resp=None)
        rows = [json.loads(l) for l in (Path("/tmp/superbrowser") / tid / "memory" / "vision_calls.jsonl").read_text().splitlines()]
        assert rows[0]["cached"] is True and rows[0]["n_bboxes"] == 3 and rows[0]["path"] == "prefetch"
        assert rows[0]["dom_hash"] == "abc" and rows[0]["dom_text_hash"] == "def"
        assert rows[1]["ok"] is False and rows[1]["dom_text_hash"] is None
    finally:
        shutil.rmtree(Path("/tmp/superbrowser") / tid, ignore_errors=True)


def test_click_outcome_trace_extracts_strategy_and_snap_fields():
    tid = f"trace-{uuid.uuid4().hex[:8]}"
    try:
        st = _state(tid)
        data = {"success": True, "snap": {"snapped": True, "method": "grid_scan", "label_score": 1.0,
                                          "chevron_score": 3, "candidates": 4, "target": "button[Year]"},
                "effect": {"url_changed": False, "mutation_delta": 7}, "tried": ["cdp"]}
        with _with_env(SUPERBROWSER_TRACE_CLICKS="1"):
            trace_click_outcome(st, tool="browser_click_at", target="V3", data=data,
                                verify_note="\n[click_escalated strategy=keyboard] landed", vision_index=3, label="Year")
            trace_click_outcome(st, tool="browser_click", target="[12]", data={"snap": {"snapped": False, "method": "fallback"}},
                                verify_note="\n[click_silent reason=no_change]")
        rows = [json.loads(l) for l in (Path("/tmp/superbrowser") / tid / "memory" / "clicks.jsonl").read_text().splitlines()]
        assert rows[0]["strategy"] == "keyboard" and rows[0]["escalated"] is True and rows[0]["silent"] is False
        assert rows[0]["method"] == "grid_scan" and rows[0]["chevron_score"] == 3 and rows[0]["mutation_delta"] == 7
        assert rows[0]["tried"] == ["cdp"] and rows[0]["vision_index"] == 3 and rows[0]["label"] == "Year"
        assert rows[1]["strategy"] == "primary" and rows[1]["silent"] is True and rows[1]["method"] == "fallback"
    finally:
        shutil.rmtree(Path("/tmp/superbrowser") / tid, ignore_errors=True)
