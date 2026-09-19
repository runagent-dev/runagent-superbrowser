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


# =========================================================================
# Memory-pressure ladder (eval E3): deterministic distractor observations
# =========================================================================
_DISTRACTOR_TOKENS_ENV = "SUPERBROWSER_EVAL_DISTRACTOR_TOKENS"

# Vocabulary deliberately avoids every marker the memory passes / worker hook
# key on ([SESSION_STATE, [ELEMENTS, DEAD_ENDS_HERE, [GUIDANCE], failure
# tokens, [Vn], index=) so a distractor block is inert for the ledger arm's
# targeted collapses and for the analyzers' tag counters.
_DX_TAGS = ("div", "span", "section", "article", "nav", "aside", "footer", "header", "li", "p", "figure")
_DX_CLASSES = ("layout-grid", "promo-strip", "media-card", "sidebar-module", "legal-notice", "tracking-pixel",
               "cookie-preferences", "newsletter-teaser", "breadcrumb-shell", "hero-banner", "sponsor-slot",
               "recommendation-rail", "ad-slot", "flex-wrap", "sticky-footer", "region-picker")
_DX_WORDS = ("seasonal", "featured", "trending", "sponsored", "related", "recently", "viewed", "editorial",
             "gallery", "carousel", "notice", "update", "preferences", "membership", "rewards", "delivery",
             "returns", "support", "careers", "investors", "accessibility", "sitemap", "regional", "offers")


def distractor_tokens_per_step() -> int:
    raw = os.environ.get(_DISTRACTOR_TOKENS_ENV, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _token_len(text: str) -> int:
    try:
        from nanobot.utils.helpers import estimate_message_tokens

        return int(estimate_message_tokens({"role": "tool", "content": text}))
    except Exception:
        return max(1, len(text) // 4)


def generate_distractor_block(seed: str, n_tokens: int) -> str:
    """Deterministic pseudo-DOM page-context block of ~n_tokens tokens."""
    import hashlib
    import random

    if n_tokens <= 0:
        return ""
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest(), 16))
    lines = [f"\n\n[PAGE_CONTEXT_SNAPSHOT id={hashlib.sha1(seed.encode()).hexdigest()[:8]} nodes={rng.randint(40, 400)}]"]
    text = "\n".join(lines)
    guard = 0
    while _token_len(text) < n_tokens and guard < 4000:
        guard += 1
        tag = rng.choice(_DX_TAGS)
        cls = rng.choice(_DX_CLASSES)
        words = " ".join(rng.choice(_DX_WORDS) for _ in range(rng.randint(2, 6)))
        x, y, w, h = rng.randint(0, 1280), rng.randint(0, 4000), rng.randint(20, 900), rng.randint(10, 300)
        lines.append(f'<{tag} class="{cls} {rng.choice(_DX_CLASSES)}" data-qa="{cls}-{rng.randint(1, 999)}"'
                     f' role=region aria-label="{words}" x={x},y={y} w={w},h={h}>')
        text = "\n".join(lines)
    return text


class DistractorHook:
    """AgentHook that appends a distractor block to THIS iteration's tool
    results (after they were produced, before the memory policy sees them
    next iteration). Inert unless SUPERBROWSER_EVAL_DISTRACTOR_TOKENS > 0."""

    def __init__(self, memory: Any, tokens_per_step: int) -> None:
        self.memory = memory
        self.tokens_per_step = tokens_per_step
        self.task_id = getattr(memory, "task_id", "task")
        self.seed_salt = os.environ.get("SUPERBROWSER_EVAL_SEED", "0")

    # nanobot AgentHook surface (only after_iteration does anything)
    async def before_run(self, context: Any) -> None:  # pragma: no cover - interface
        return None

    async def after_run(self, context: Any) -> None:  # pragma: no cover - interface
        return None

    async def on_error(self, context: Any) -> None:  # pragma: no cover - interface
        return None

    async def on_finally(self, context: Any) -> None:  # pragma: no cover - interface
        return None

    async def before_iteration(self, context: Any) -> None:
        return None

    async def before_execute_tools(self, context: Any) -> None:  # pragma: no cover - interface
        return None

    async def after_iteration(self, context: Any) -> None:
        if self.tokens_per_step <= 0:
            return
        try:
            ids = {tc.id if hasattr(tc, "id") else (tc.get("id") if isinstance(tc, dict) else None)
                   for tc in (context.tool_calls or [])}
        except Exception:
            ids = set()
        ids.discard(None)
        messages = context.messages or []
        touched = 0
        tokens = 0
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if m.get("role") != "tool":
                break
            if ids and m.get("tool_call_id") not in ids:
                continue
            seed = f"{self.task_id}:{self.seed_salt}:{context.iteration}:{m.get('tool_call_id') or i}"
            block = generate_distractor_block(seed, self.tokens_per_step)
            content = m.get("content")
            if isinstance(content, str):
                m["content"] = content + block
            elif isinstance(content, list):
                content.append({"type": "text", "text": block})
            else:
                m["content"] = block
            touched += 1
            tokens += _token_len(block)
        if touched:
            try:
                self.memory.events.log("distractor_appended", {"iter": context.iteration, "count": touched,
                                                               "tokens": tokens, "per_step": self.tokens_per_step})
            except Exception:
                pass


def eval_worker_hooks(memory: Any) -> list[Any]:
    """Hooks the delegation adds right after the memory hook — empty unless
    an eval-only env is set, so production hook lists are unchanged."""
    hooks: list[Any] = []
    n = distractor_tokens_per_step()
    if n > 0:
        try:
            from nanobot.agent.hook import AgentHook

            class _DistractorAgentHook(DistractorHook, AgentHook):  # type: ignore[misc]
                pass

            hooks.append(_DistractorAgentHook(memory, n))
        except Exception:
            hooks.append(DistractorHook(memory, n))
    # The audit trail (SuperBrowser(audit_dir=...) / eval harness) banks one row
    # per worker iteration; gated so production hook lists stay unchanged.
    if os.environ.get("SUPERBROWSER_AUDIT_ITERATIONS", "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from superbrowser_bridge.audit import ROSTER_NAME, AuditHook

            cap = os.environ.get("SUPERBROWSER_EVAL_CAPTURE_DIR")
            roster = os.path.join(cap, ROSTER_NAME) if cap else None
            hooks.append(AuditHook("worker", memory=memory, roster_path=roster))
        except Exception:
            pass
    return hooks
