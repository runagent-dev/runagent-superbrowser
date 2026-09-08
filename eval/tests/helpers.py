"""Builders for synthetic run directories used across the harness tests.

The shapes mirror what ``run_one.py`` + the bridge write for a real run:
``spec.json``, ``meta.json``, ``usage.json``, ``workers/<wid>.json``
(delegation-tap transcript), ``ledgers/<wid>/{events,steps}.jsonl`` and
optional trace files. Numbers are small but internally consistent so the
harvest/metric assertions can be exact.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def tool_call(cid: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def transcript(*, task_id: str, instruction: str, url: str, turns: list[tuple[str, dict[str, Any], str]],
               final: str, vision_calls: int = 3) -> dict[str, Any]:
    """turns = [(tool_name, args, result_text), ...] -> one assistant+tool pair each."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "SOUL"},
        {"role": "user", "content": instruction},
    ]
    for i, (name, args, result) in enumerate(turns):
        cid = f"call_{i}"
        messages.append({"role": "assistant", "content": "", "tool_calls": [tool_call(cid, name, args)]})
        messages.append({"role": "tool", "tool_call_id": cid, "name": name, "content": result})
    messages.append({"role": "assistant", "content": final})
    return {
        "task_id": task_id, "instructions": instruction, "url": url, "content": final,
        "messages": messages,
        "tool_schemas": [{"type": "function", "function": {"name": n, "parameters": {"type": "object", "properties": {}}}}
                         for n in sorted({t[0] for t in turns} | {"browser_open", "browser_click_at"})],
        "meta": {"schema_reminder": False, "step_count": len(turns), "vision_calls": vision_calls,
                 "text_calls": 0, "sessions_opened": 1, "regression_count": 0,
                 "current_url": url + "search?x=1", "network_blocked": False},
    }


def make_run_dir(root: Path, *, experiment: str = "e_test", arm: str = "ledger", task_id: str = "t1",
                 seed: int = 0, instruction: str = "Find a red Toyota Corolla from 2018 to 2023.",
                 url: str = "https://www.example.com/", turns: list[tuple[str, dict[str, Any], str]] | None = None,
                 final: str = "Found 3 listings: 2019 Corolla $18,000 ...", stop_reason: str = "ok",
                 tokens_in: list[int] | None = None, judges: dict[str, bool | None] | None = None,
                 screenshots: int = 2, critical_state: list[str] | None = None,
                 arm_env: dict[str, str] | None = None, extra_events: list[dict[str, Any]] | None = None,
                 vision_calls: list[dict[str, Any]] | None = None, clicks: list[dict[str, Any]] | None = None,
                 live_context: list[dict[str, Any]] | None = None) -> Path:
    run_dir = root / experiment / arm / task_id / f"seed{seed}"
    (run_dir / "workers").mkdir(parents=True, exist_ok=True)
    (run_dir / "screenshots").mkdir(exist_ok=True)
    turns = turns if turns is not None else [
        ("browser_open", {"url": url}, "session=session-1"),
        ("browser_click_at", {"vision_index": 3, "target_label": "Toyota"}, "url=... snapped→(10,10) a.brand"),
        ("browser_type_at", {"vision_index": 5, "text": "Corolla"}, "typed"),
        ("browser_click_at", {"vision_index": 7, "target_label": "2018"}, "[click_escalated strategy=js] url=..."),
        ("browser_get_markdown", {}, "# Results\n2019 Corolla $18,000"),
    ]
    wid = "w" + task_id[:6]
    tr = transcript(task_id=wid, instruction=instruction, url=url, turns=turns, final=final)
    (run_dir / "workers" / f"{wid}.json").write_text(json.dumps(tr))
    now = time.time()
    spec = {
        "run_id": f"{experiment}:{arm}:{task_id}:s{seed}", "experiment": experiment, "seed": seed,
        "arm": {"name": arm, "env": arm_env or {}, "side": "python", "family": "memory", "description": ""},
        "task": {"task_id": task_id, "benchmark": "synthetic", "level": "hard", "website": url,
                 "start_url": url, "instruction": instruction, "reference": None,
                 "critical_state": critical_state or ["Toyota Corolla", "2018", "2023"], "checks": []},
        "benchmark": "synthetic", "run_dir": str(run_dir), "model": "test/model", "topology": "orchestrator",
        "protocol": {"hash": "deadbeef", "max_iterations": 50, "wall_clock_s": 1800},
        "nanobot_overrides": {}, "internal_timeout_s": 1710,
    }
    (run_dir / "spec.json").write_text(json.dumps(spec))
    meta = {
        "run_id": spec["run_id"], "experiment": experiment, "arm": arm, "task_id": task_id, "seed": seed,
        "topology": "orchestrator", "orch_task_id": "orch-abc", "role_task_ids": ["orch-abc", wid],
        "stop_reason": stop_reason, "error": None, "started_at": now - 100, "ended_at": now,
        "duration_s": 100.0, "final_answer": final, "raw_content": final, "framed_task": instruction,
        "arm_env": arm_env or {}, "effective_defaults": {"model": "test/model", "provider": "openrouter"},
        "n_screenshots": screenshots, "environment": {"git_sha": "abc123"},
    }
    (run_dir / "meta.json").write_text(json.dumps(meta))
    (run_dir / "result.txt").write_text(final)
    tokens_in = tokens_in or [40000, 42000, 45000, 43000, 41000]
    usage = {
        "task_id": "orch-abc", "input_tokens": sum(tokens_in) + 5000, "output_tokens": 1200,
        "total_tokens": sum(tokens_in) + 6200 + 900, "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "vision_tokens": 900, "vision_calls": 3, "image_blocks": 0, "estimated_embedded_image_tokens": 0,
        "by_role": {
            "orchestrator": {"input_tokens": 5000, "output_tokens": 200, "total_tokens": 5200,
                             "cache_read_tokens": 0, "cache_creation_tokens": 0, "calls": 2},
            "worker": {"input_tokens": sum(tokens_in), "output_tokens": 1000, "total_tokens": sum(tokens_in) + 1000,
                       "cache_read_tokens": 0, "cache_creation_tokens": 0, "calls": len(tokens_in)},
            "vision": {"input_tokens": 600, "output_tokens": 300, "total_tokens": 900,
                       "cache_read_tokens": 0, "cache_creation_tokens": 0, "calls": 3},
        },
    }
    (run_dir / "usage.json").write_text(json.dumps(usage))
    # ledgers
    led = run_dir / "ledgers" / wid
    led.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = [{"ts": now - 99, "type": "memory_attach", "role": "worker", "session_key": f"worker:{wid}", "resumed": False}]
    for i, t in enumerate(tokens_in):
        events.append({"ts": now - 90 + i, "type": "memory_after_iter", "iter": i, "role": "worker", "tokens_in": t,
                       "tokens_out": 200, "cache_read": 0, "cache_creation": 0, "messages": 4 + 2 * i, "tool_calls": 1})
        events.append({"ts": now - 90 + i, "type": "iteration", "iter": i, "tokens_in": t, "tokens_out": 200,
                       "cache_read": 0, "cache_creation": 0, "images": 0, "messages": 4 + 2 * i, "tool_calls": 1,
                       "url": url, "step": i + 1})
        events.append({"ts": now - 90 + i, "type": "ledger_injected", "iter": i, "role": "worker", "chars": 1500})
    events.append({"ts": now - 50, "type": "messages_gutted", "iter": 3, "role": "worker", "count": 4, "messages_in_flight": 30})
    events += extra_events or []
    (led / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    steps = []
    for i, (name, args, result) in enumerate(turns):
        steps.append({"tool": name, "args": json.dumps(args) if args else "", "result": result[:200],
                      "url": url if i == 0 else url + "search?x=1", "success": "[click_at_failed" not in result and "[no_effect" not in result,
                      "subgoal": "delegated", "iteration": -1, "timestamp": now - 90 + i, "caption": "", "time": "00:00:00"})
    (led / "steps.jsonl").write_text("".join(json.dumps(s) + "\n" for s in steps))
    (led / "ledger.json").write_text(json.dumps({"goal": instruction, "dead_ends": [], "facts": {}, "checkpoints": [],
                                                 "recent": [], "current_url": url}))
    if vision_calls:
        (led / "vision_calls.jsonl").write_text("".join(json.dumps(v) + "\n" for v in vision_calls))
    if clicks:
        (led / "clicks.jsonl").write_text("".join(json.dumps(c) + "\n" for c in clicks))
    if live_context is not None:
        import gzip
        with gzip.open(led / "live_context.jsonl.gz", "wt", encoding="utf-8") as f:
            for row in live_context:
                f.write(json.dumps(row) + "\n")
    # orchestrator ledger
    oled = run_dir / "ledgers" / "orch-abc"
    oled.mkdir(parents=True, exist_ok=True)
    (oled / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
        {"ts": now - 99, "type": "memory_after_iter", "iter": 0, "role": "orchestrator", "tokens_in": 2500,
         "tokens_out": 100, "cache_read": 0, "cache_creation": 0, "messages": 3, "tool_calls": 1},
        {"ts": now - 1, "type": "memory_after_iter", "iter": 1, "role": "orchestrator", "tokens_in": 2500,
         "tokens_out": 100, "cache_read": 0, "cache_creation": 0, "messages": 5, "tool_calls": 0},
    ]))
    for i in range(screenshots):
        (run_dir / "screenshots" / f"{i + 1:03d}-shot.png").write_bytes(PNG_1x1)
    if judges:
        jd = run_dir / "judges"
        jd.mkdir(exist_ok=True)
        for name, ok in judges.items():
            jd.joinpath(f"{name}.json").write_text(json.dumps({"judge": name, "success": ok, "rationale": "synthetic",
                                                                "model": "judge/test", "details": {}, "usage": {}}))
    return run_dir


class FakeChatClient:
    """Minimal OpenAI-compatible async client: returns canned replies in order
    (or by a callable) and records every call."""

    def __init__(self, replies: list[str] | None = None, *, responder=None):
        self.replies = list(replies or [])
        self.responder = responder
        self.calls: list[dict[str, Any]] = []
        self.chat = self
        self.completions = self

    @staticmethod
    def response(text: str, *, finish_reason: str | None = "stop", prompt_tokens: int = 100,
                 completion_tokens: int = 20):
        """An OpenAI-shaped chat completion, reusable by tests that need their
        own client (e.g. to count parameter shapes or simulate a token cap)."""

        class _Msg:
            def __init__(self, c: str) -> None:
                self.content = c

        class _Choice:
            def __init__(self, c: str) -> None:
                self.message = _Msg(c)
                self.finish_reason = finish_reason

        class _Usage:
            pass

        u = _Usage()
        u.prompt_tokens, u.completion_tokens = prompt_tokens, completion_tokens

        class _Resp:
            def __init__(self, c: str) -> None:
                self.choices = [_Choice(c)]
                self.usage = u

        return _Resp(text)

    async def create(self, **kwargs: Any):
        self.calls.append(kwargs)
        if self.responder is not None:
            text = self.responder(kwargs)
        elif self.replies:
            text = self.replies.pop(0)
        else:
            text = ""
        return self.response(text)
