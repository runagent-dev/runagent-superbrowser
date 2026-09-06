"""Eval-only instrumentation for the memory hook (all gated by env, default off).

``ContextDumper`` — when ``SUPERBROWSER_EVAL_CONTEXT_DUMP=1`` — records, per
LLM call, the live message list *as the memory policy left it* (text only;
images become "[image]") into ``<memory_dir>/live_context.jsonl.gz`` and emits
a ``context_size`` event with the estimated prompt tokens before and after
the hook ran. This is the raw material for Critical State Durability (was the
exact value still in the context when it was needed?) and for exact
per-iteration context-size curves.

Estimates use nanobot's own tokenizer chain (``estimate_prompt_tokens_chain``,
tiktoken cl100k fallback) so they are comparable with the runner's numbers;
images are not counted by tiktoken, so the image-block count is recorded too.

``DistractorHook`` (memory-pressure ladder, eval E3) lives here as well and is
added in P3 together with the policies.
"""
from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path
from typing import Any

_TRUE = ("1", "true", "yes", "on")


def context_dump_enabled() -> bool:
    return os.environ.get("SUPERBROWSER_EVAL_CONTEXT_DUMP", "").lower() in _TRUE


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") in ("image", "image_url"):
                    parts.append("[image]")
                else:
                    parts.append(str(b.get("text") or b.get("content") or ""))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return "" if content is None else str(content)


def _count_images(messages: list[dict[str, Any]]) -> int:
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            n += sum(1 for b in c if isinstance(b, dict) and b.get("type") in ("image", "image_url"))
    return n


def compact_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Text-only view of the live context, one entry per message."""
    out: list[dict[str, Any]] = []
    for m in messages:
        entry: dict[str, Any] = {"role": m.get("role"), "text": _text_of(m.get("content"))}
        if m.get("tool_calls"):
            entry["tool_calls"] = [
                ((tc.get("function") or {}).get("name") or tc.get("name") or "tool")
                for tc in m.get("tool_calls") or [] if isinstance(tc, dict)
            ]
        if m.get("tool_call_id"):
            entry["tool_call_id"] = m.get("tool_call_id")
        if m.get("name"):
            entry["name"] = m.get("name")
        if m.get("_archived"):
            entry["archived"] = True
        if m.get("_summary_slot"):
            entry["summary_slot"] = True
        out.append(entry)
    return out


class ContextDumper:
    """Per-iteration live-context recorder. Construct once per hook."""

    def __init__(self, memory_dir: Path, *, role: str, bot_getter) -> None:
        self.path = Path(memory_dir) / "live_context.jsonl.gz"
        self.role = role
        self._bot_getter = bot_getter

    # ---- token estimate via nanobot's chain (never raises) ----------------
    def estimate(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        est_tokens: int | None = None
        est_messages_only: int | None = None
        try:
            from nanobot.utils.helpers import estimate_prompt_tokens_chain

            bot = self._bot_getter()
            loop = getattr(bot, "_loop", None)
            provider = getattr(loop, "provider", None)
            model = getattr(loop, "model", None)
            tools = None
            try:
                tools = loop.tools.get_definitions() if loop is not None else None
            except Exception:
                tools = None
            est_tokens, _ = estimate_prompt_tokens_chain(provider, model, messages, tools)
            est_messages_only, _ = estimate_prompt_tokens_chain(provider, model, messages, None)
        except Exception:
            try:
                from nanobot.utils.helpers import estimate_message_tokens

                est_messages_only = sum(estimate_message_tokens(m) for m in messages)
                est_tokens = est_messages_only
            except Exception:
                pass
        return {"est_tokens": est_tokens, "est_tokens_messages_only": est_messages_only,
                "image_blocks": _count_images(messages), "n_messages": len(messages)}

    def dump(self, *, iteration: int, policy: str, messages: list[dict[str, Any]],
             before: dict[str, Any] | None, after: dict[str, Any]) -> None:
        row = {
            "iter": iteration, "ts": time.time(), "role": self.role, "policy": policy,
            "n_messages": len(messages), "est_tokens": after.get("est_tokens"),
            "est_tokens_before": (before or {}).get("est_tokens"),
            "image_blocks": after.get("image_blocks"),
            "messages": compact_messages(messages),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(self.path, "at", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass


def read_context_dump(path: Path) -> list[dict[str, Any]]:
    """Reader used by the analyzers (and tests)."""
    rows: list[dict[str, Any]] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except (OSError, EOFError):
        pass
    return rows
