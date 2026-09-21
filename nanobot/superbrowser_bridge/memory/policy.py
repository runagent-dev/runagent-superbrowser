"""Matched memory-retention policies for the memory experiment (eval E2/E3/E10).

The production system keeps the live context bounded with the six-phase
eviction loop plus the injected structured Ledger (``MemoryHook`` in
``hook.py``). To measure what that policy buys, the harness needs *matched*
alternatives that differ ONLY in how older history is retained (the design
follows "Learning What Not to Forget": no-compression / recency / LLM
compression / the system's own policy, same budget, same recent window):

======================  ==============================================================
``ledger`` (default)    today's loop, byte-identical (this module is not on that path)
``full``                keep everything verbatim; no passes, no Ledger, no injections
``fifo``                last K assistant-anchored turns verbatim, older turns archived
                        to ``[archived]`` (count + tool pairing preserved), no Ledger
``summary``             fifo + ONE regenerated structured summary of the archived turns
                        (same host model; counted as role ``compressor``)
``ledger_noevict``      Ledger injected, nothing evicted (separates Ledger from eviction)
======================  ==============================================================

Selection: ``SUPERBROWSER_MEMORY_POLICY``; window ``SUPERBROWSER_MEMORY_RECENT_K``
(default 5 = Phase 6's keep_last_turns); screenshots kept
``SUPERBROWSER_MEMORY_KEEP_SCREENSHOTS`` (default 2); budget for the older
history ``SUPERBROWSER_MEMORY_BUDGET_TOKENS`` (unset = today's defaults; when
set it caps the summary and the rendered Ledger).

All policies obey the two structural constraints the runner imposes on the
live list (see ``_gut_old_message_content`` in hook.py): never change the
message COUNT (``_save_turn`` uses a fixed skip boundary) and never touch an
assistant's ``tool_calls`` / a tool message's ``tool_call_id`` (Anthropic
requires the pairing). Archiving therefore blanks content in place.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover
    from .hook import MemoryHook
    from .memory import Memory

PolicyName = Literal["ledger", "full", "fifo", "summary", "ledger_noevict"]
POLICY_NAMES: tuple[str, ...] = (
    "ledger", "full", "fifo", "summary", "ledger_noevict", "adaptive",
)

# Budget-adaptive hybrid: keep the raw window uncompacted while there is
# context headroom, and only pay for the six-phase pass once headroom runs
# out (or a subgoal closes, which is the natural seam to compact across).
#
# The sweep measured eviction as an always-on policy against never-on
# baselines, and the always-on arm lost. Neither arm asks the question this
# one does: whether compaction is worth its cost *when the window is not
# under pressure*. `full_history` never compacts and never bounds; `ledger`
# always compacts whether or not it needs to. The threshold below is the
# fraction of the window that must remain FREE for compaction to be
# deferred.
# Calibrated against the observed distribution, not chosen a priori. The
# first sweep of this arm ran at 0.30 and compacted on 0 of 795 turns: a
# 200K window and a 0.30 threshold only trigger above 140K tokens, and
# these tasks peak between 43K and 91K, so headroom never fell below
# 0.744. The arm silently degenerated into "Ledger, never evict".
#
# Measured over those 795 turns, the fraction that WOULD compact is:
#     0.30 -> 0.0%   0.80 ->  9.2%   0.85 -> 28.3%
#     0.87 -> 45.3%  0.90 -> 73.2%   0.92 -> 87.5%
# 0.87 splits the turns near evenly, which is where the arm carries the
# most information. Expect the realised rate to come in lower: compaction
# frees context, which raises headroom, which suppresses the next trigger.
_DEFAULT_ADAPTIVE_HEADROOM = 0.87


def adaptive_headroom_threshold() -> float:
    """Headroom below which the six-phase pass runs. Read at call time so
    an arm can set it per run and have the value recorded in the run's env."""
    raw = os.environ.get("SUPERBROWSER_ADAPTIVE_HEADROOM", "").strip()
    if raw:
        try:
            v = float(raw)
            if 0.0 < v < 1.0:
                return v
            logger.warning("SUPERBROWSER_ADAPTIVE_HEADROOM={!r} out of (0,1); using default", raw)
        except ValueError:
            logger.warning("SUPERBROWSER_ADAPTIVE_HEADROOM={!r} is not a float; using default", raw)
    return _DEFAULT_ADAPTIVE_HEADROOM


# Back-compat alias for anything importing the constant directly.
ADAPTIVE_HEADROOM_THRESHOLD = _DEFAULT_ADAPTIVE_HEADROOM

_DEFAULT_RECENT_K = 5          # == hook._DEFAULT_KEEP_RECENT_TURNS
_DEFAULT_KEEP_SCREENSHOTS = 2  # == hook._DEFAULT_KEEP_LAST_SCREENSHOTS
_DEFAULT_BUDGET_TOKENS = 2048  # used by `summary` when the budget env is unset


def _int_env(name: str, default: int | None) -> int | None:
    """Integer env knob; a malformed value falls back to the default with a
    warning (a typo in .env must never take the production agent down)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("{}={!r} is not an integer; using default {}", name, raw, default)
        return default


_TRUE = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class MemoryPolicyConfig:
    name: str = "ledger"
    recent_k: int = _DEFAULT_RECENT_K
    keep_screenshots: int = _DEFAULT_KEEP_SCREENSHOTS
    budget_tokens: int | None = None       # None => default path untouched
    dead_ends: bool = True                 # ABLATE_DEAD_END_MEMORY=1 -> False (eval E4)
    cross_task: bool = True                # SUPERBROWSER_CROSS_TASK_MEMORY=0 -> False (confound control)

    @classmethod
    def from_env(cls) -> "MemoryPolicyConfig":
        name = (os.environ.get("SUPERBROWSER_MEMORY_POLICY") or "ledger").strip().lower()
        if name not in POLICY_NAMES:
            # Fail OPEN: production must keep running on a typo. The eval harness
            # validates arm names against its registry before a run starts, so an
            # experiment can never silently land here.
            logger.warning("SUPERBROWSER_MEMORY_POLICY={!r} is not one of {}; using 'ledger'", name, POLICY_NAMES)
            name = "ledger"
        keep = os.environ.get("SUPERBROWSER_MEMORY_KEEP_SCREENSHOTS", "").strip().lower()
        keep_n = 10**6 if keep == "all" else (_int_env("SUPERBROWSER_MEMORY_KEEP_SCREENSHOTS", _DEFAULT_KEEP_SCREENSHOTS) or 0)
        return cls(
            name=name,
            recent_k=_int_env("SUPERBROWSER_MEMORY_RECENT_K", _DEFAULT_RECENT_K) or _DEFAULT_RECENT_K,
            keep_screenshots=keep_n,
            budget_tokens=_int_env("SUPERBROWSER_MEMORY_BUDGET_TOKENS", None),
            dead_ends=os.environ.get("ABLATE_DEAD_END_MEMORY", "").lower() not in _TRUE,
            cross_task=os.environ.get("SUPERBROWSER_CROSS_TASK_MEMORY", "1").lower() in _TRUE,
        )

    # what each policy does (data, so the analyzers can read it back)
    @property
    def is_default(self) -> bool:
        """True when nothing deviates from production (golden path)."""
        return (self.name == "ledger" and self.budget_tokens is None and self.dead_ends
                and self.cross_task and self.recent_k == _DEFAULT_RECENT_K
                and self.keep_screenshots == _DEFAULT_KEEP_SCREENSHOTS)

    @property
    def inject_ledger(self) -> bool:
        return self.name in ("ledger", "ledger_noevict")

    @property
    def inject_dead_ends(self) -> bool:
        return self.dead_ends and self.name in ("ledger", "ledger_noevict")

    @property
    def record_dead_ends(self) -> bool:
        return self.dead_ends and self.name in ("ledger", "ledger_noevict")

    @property
    def summary_budget(self) -> int:
        return self.budget_tokens or _DEFAULT_BUDGET_TOKENS

    def to_dict(self) -> dict[str, Any]:
        return {"policy": self.name, "recent_k": self.recent_k, "keep_screenshots": self.keep_screenshots,
                "budget_tokens": self.budget_tokens, "dead_ends": self.dead_ends, "cross_task": self.cross_task}


# --------------------------------------------------------------- helpers
def turn_starts(messages: list[dict[str, Any]]) -> list[int]:
    """Indices of assistant messages after the protected prefix (0=system,
    1=initial task). A turn = an assistant message + everything up to the
    next assistant message, so multi-tool-call turns are never split."""
    return [i for i in range(2, len(messages)) if messages[i].get("role") == "assistant"]


def window_start(messages: list[dict[str, Any]], k: int) -> int | None:
    """Index where the last-k-turns verbatim window begins, or None when
    fewer than k+1 turns exist (nothing to archive yet)."""
    starts = turn_starts(messages)
    if len(starts) <= k:
        return None
    return starts[-k] if k > 0 else len(messages)


def archive_range(messages: list[dict[str, Any]], lo: int, hi: int) -> int:
    """Blank message content in [lo, hi) exactly like Phase 6 does:
    assistant -> content "", thinking cleared; tool/user -> "[archived]";
    tool_calls / tool_call_id untouched; ``_archived`` flag makes it
    idempotent; ``_summary_slot`` messages are left alone."""
    n = 0
    lo = max(lo, 2)
    for i in range(lo, min(hi, len(messages))):
        msg = messages[i]
        if msg.get("_archived") or msg.get("_summary_slot"):
            continue
        role = msg.get("role")
        if role == "assistant":
            if msg.get("content"):
                msg["content"] = ""
            if msg.get("thinking_blocks"):
                msg["thinking_blocks"] = []
            if msg.get("reasoning_content"):
                msg["reasoning_content"] = ""
            msg["_archived"] = True
            n += 1
        elif role in ("tool", "user"):
            content = msg.get("content")
            if isinstance(content, (str, list)) and content:
                msg["content"] = "[archived]"
                msg["_archived"] = True
                n += 1
    return n


def _message_text(msg: dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text") or "") if isinstance(b, dict) else str(b) for b in content
        )
    return "" if content is None else str(content)


def scan_failures(messages: list[dict[str, Any]], *, keep_last_n: int, seen: set[str]) -> list[dict[str, str]]:
    """NON-mutating twin of Phase 2's selection: the failure-bearing tool
    messages older than the newest ``keep_last_n``, each reduced to
    ``{"reason", "url", "cause"}`` exactly as the collapse pass would, but
    without rewriting content. ``seen`` (tool_call_id) makes it record each
    failure once across iterations."""
    from .hook import (_FAILURE_RE, _STATE_URL_RE, _classify_failure, _extract_failure_snippet)

    failure_indices: list[int] = []
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "tool":
            continue
        if _FAILURE_RE.search(_message_text(msg)):
            failure_indices.append(i)
    if len(failure_indices) <= keep_last_n:
        return []
    out: list[dict[str, str]] = []
    for i in failure_indices[keep_last_n:]:
        msg = messages[i]
        key = str(msg.get("tool_call_id") or id(msg))
        if key in seen:
            continue
        seen.add(key)
        msg_text = _message_text(msg)
        reason = _extract_failure_snippet(msg_text)
        url = ""
        url_m = _STATE_URL_RE.search(msg_text)
        if url_m:
            url = url_m.group(1)
        else:
            for j in range(i - 1, -1, -1):
                prev_m = _STATE_URL_RE.search(_message_text(messages[j]))
                if prev_m:
                    url = prev_m.group(1)
                    break
        cause_m = _FAILURE_RE.search(msg_text)
        cause = _classify_failure(cause_m) if cause_m else "unknown"
        out.append({"reason": reason, "url": url, "cause": cause})
    return out


def estimate_tokens(text: str) -> int:
    try:
        from nanobot.utils.helpers import estimate_message_tokens

        return int(estimate_message_tokens({"role": "system", "content": text}))
    except Exception:
        return max(1, len(text) // 4)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    try:
        from nanobot.utils.helpers import truncate_text_to_tokens

        return truncate_text_to_tokens(text, max_tokens)
    except Exception:
        return text[: max_tokens * 4]


def cap_ledger_render(memory: "Memory", text: str, budget_tokens: int) -> tuple[str, dict[str, int] | None]:
    """Shrink the rendered Ledger until it fits ``budget_tokens``: halve the
    section caps (facts -> dead-ends -> checkpoints -> episodic) and
    re-render, then token-truncate as a last resort. Returns (text, caps
    used | None when the original fit)."""
    if estimate_tokens(text) <= budget_tokens:
        return text, None
    caps = {"max_facts": 30, "max_dead_ends": 12, "max_checkpoints": 10, "max_episodic": 8}
    floors = {"max_facts": 4, "max_dead_ends": 2, "max_checkpoints": 2, "max_episodic": 1}
    order = ["max_facts", "max_dead_ends", "max_checkpoints", "max_episodic"]
    for _round in range(12):
        for key in order:
            if caps[key] > floors[key]:
                caps[key] = max(floors[key], caps[key] // 2)
                text = memory.render_for_llm(**caps)
                if estimate_tokens(text) <= budget_tokens:
                    return text, dict(caps)
    return truncate_to_tokens(text, budget_tokens) + "\n[ledger truncated to budget]", dict(caps)


def render_message_for_summary(msg: dict[str, Any], *, max_chars: int = 4000) -> str:
    role = msg.get("role")
    if role == "assistant":
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or ""
            if not isinstance(args, str):
                args = str(args)
            calls.append(f"{fn.get('name') or tc.get('name') or 'tool'}({args[:300]})")
        text = _message_text(msg)[:max_chars]
        return f"ASSISTANT: {text}" + (f" | CALLS: {'; '.join(calls)}" if calls else "")
    if role == "tool":
        content = msg.get("content")
        if isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") in ("image", "image_url"):
                    parts.append("[image]")
                elif isinstance(b, dict):
                    parts.append(str(b.get("text") or ""))
            text = "\n".join(parts)
        else:
            text = _message_text(msg)
        return f"TOOL({msg.get('name') or 'tool'}): {text[:max_chars]}"
    return f"{str(role or 'user').upper()}: {_message_text(msg)[:max_chars]}"


# ---------------------------------------------------------------- policies
class _Policy:
    name = "base"

    def __init__(self, cfg: MemoryPolicyConfig) -> None:
        self.cfg = cfg

    async def apply(self, hook: "MemoryHook", context: Any) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # shared bits -----------------------------------------------------------
    def _evict_screenshots(self, hook: "MemoryHook", context: Any) -> None:
        from .hook import _back_patch_screenshots

        evicted = _back_patch_screenshots(context.messages, keep_last_n=self.cfg.keep_screenshots)
        if evicted:
            hook.memory.events.log("screenshot_evicted", {"iter": context.iteration, "role": hook.memory.role,
                                                          "count": evicted})

    def _archive_older(self, hook: "MemoryHook", context: Any) -> int | None:
        ws = window_start(context.messages, self.cfg.recent_k)
        if ws is None:
            return None
        n = archive_range(context.messages, 2, ws)
        if n:
            hook.memory.events.log("messages_archived", {"iter": context.iteration, "role": hook.memory.role,
                                                         "count": n, "window_start": ws,
                                                         "messages_in_flight": len(context.messages)})
        return ws


class FullPolicy(_Policy):
    """No eviction, no Ledger: the unbounded upper bound."""
    name = "full"

    async def apply(self, hook: "MemoryHook", context: Any) -> None:
        return None


class LedgerNoEvictPolicy(_Policy):
    """Ledger injected every turn, history never mutated; dead-ends recorded
    by a non-mutating scan (deduplicated per tool_call_id)."""
    name = "ledger_noevict"

    async def apply(self, hook: "MemoryHook", context: Any) -> None:
        from .hook import _refresh_ledger_in_system_message

        try:
            hook._ingest_autocompact_summary()
        except Exception:
            pass
        found = scan_failures(context.messages, keep_last_n=hook.keep_last_failures, seen=hook._dead_end_seen)
        if found:
            hook.memory.events.log("failures_scanned", {"iter": context.iteration, "role": hook.memory.role,
                                                        "count": len(found), "reasons": [f["reason"] for f in found],
                                                        "causes": [f["cause"] for f in found]})
            for f in found:
                try:
                    hook.memory.mark_dead_end(f["reason"], url=f.get("url", ""), cause=f.get("cause", "unknown"))
                except Exception as exc:
                    logger.debug("mark_dead_end failed: {}", exc)
        ledger_text = hook._render_ledger_text()
        if _refresh_ledger_in_system_message(context.messages, ledger_text):
            hook.memory.events.log("ledger_injected", {"iter": context.iteration, "role": hook.memory.role,
                                                       "chars": len(ledger_text)})


class FifoPolicy(_Policy):
    """Recency only."""
    name = "fifo"

    async def apply(self, hook: "MemoryHook", context: Any) -> None:
        self._evict_screenshots(hook, context)
        self._archive_older(hook, context)


class SummaryPolicy(FifoPolicy):
    """Recency + one regenerated structured summary of everything archived."""
    name = "summary"

    def __init__(self, cfg: MemoryPolicyConfig) -> None:
        super().__init__(cfg)
        self.summary_text: str = ""
        self.covered_upto: int = 2
        self.pending_chunks: list[str] = []
        self.pending_tokens: int = 0
        self.consecutive_failures: int = 0
        self.slot_idx: int | None = None

    async def apply(self, hook: "MemoryHook", context: Any) -> None:
        from .compressor import compress_history

        self._evict_screenshots(hook, context)
        msgs = context.messages
        ws = window_start(msgs, self.cfg.recent_k)
        if ws is None:
            return
        # capture BEFORE archiving; skip what the summary already covers
        for i in range(max(self.covered_upto, 2), ws):
            m = msgs[i]
            if m.get("_archived") or m.get("_summary_slot"):
                continue
            chunk = render_message_for_summary(m)
            self.pending_chunks.append(chunk)
            self.pending_tokens += estimate_tokens(chunk)
        self._archive_older(hook, context)
        budget = self.cfg.summary_budget
        if self.pending_chunks and self.pending_tokens >= budget:
            new = await compress_history(hook._bot, hook.memory, self.summary_text, self.pending_chunks,
                                         budget=budget, iteration=context.iteration)
            if new:
                self.summary_text = new
                self.consecutive_failures = 0
                hook.memory.events.log("summary_refreshed", {"iter": context.iteration, "role": hook.memory.role,
                                                             "covers_upto": ws, "summary_tokens": estimate_tokens(new),
                                                             "pending_tokens": self.pending_tokens,
                                                             "chunks": len(self.pending_chunks)})
                self.covered_upto = ws
                self.pending_chunks, self.pending_tokens = [], 0
            else:
                self.consecutive_failures += 1
                if self.consecutive_failures >= 2:
                    # extractive stub so the arm never silently degrades to fifo
                    stub = "\n".join(c[:200] for c in self.pending_chunks)
                    self.summary_text = truncate_to_tokens((self.summary_text + "\n" + stub).strip(), budget)
                    hook.memory.events.log("compressor_fallback", {"iter": context.iteration, "role": hook.memory.role,
                                                                   "chunks": len(self.pending_chunks)})
                    self.covered_upto = ws
                    self.pending_chunks, self.pending_tokens = [], 0
                    self.consecutive_failures = 0
        if self.summary_text:
            self._write_slot(msgs)

    def _write_slot(self, msgs: list[dict[str, Any]]) -> None:
        """Put the summary into an existing message slot: the first tool
        result of turn 2 (keeps count + tool pairing), else append a section
        to the initial task message."""
        text = ("[HISTORY SUMMARY — earlier turns compressed; the raw turns were archived]\n"
                + self.summary_text)
        if self.slot_idx is None:
            first_assistant = next((i for i in range(2, len(msgs)) if msgs[i].get("role") == "assistant"), None)
            if first_assistant is not None:
                ids = {tc.get("id") for tc in msgs[first_assistant].get("tool_calls") or [] if isinstance(tc, dict)}
                for j in range(first_assistant + 1, len(msgs)):
                    m = msgs[j]
                    if m.get("role") == "tool" and (not ids or m.get("tool_call_id") in ids):
                        self.slot_idx = j
                        break
        if self.slot_idx is not None and self.slot_idx < len(msgs) and msgs[self.slot_idx].get("role") == "tool":
            slot = msgs[self.slot_idx]
            slot["content"] = text
            slot["_summary_slot"] = True
            slot["_archived"] = True
            return
        # fallback: section inside the initial user task (replace, never duplicate)
        head = msgs[1]
        marker = "\n\n[HISTORY SUMMARY]"
        base = head.get("content")
        if isinstance(base, str):
            base = base.split(marker, 1)[0]
            head["content"] = f"{base}{marker}\n{self.summary_text}"
        elif isinstance(base, list):
            base = [b for b in base if not (isinstance(b, dict) and b.get("_summary"))]
            base.append({"type": "text", "text": f"{marker}\n{self.summary_text}", "_summary": True})
            head["content"] = base


def build_policy(cfg: MemoryPolicyConfig) -> _Policy | None:
    """Return the policy object, or None for ``ledger`` (existing code path)."""
    # `adaptive` returns None for the same reason `ledger` does: both run the
    # six-phase path in the hook. The difference is that `adaptive` gates it
    # on headroom, which the hook decides per iteration — a _Policy object
    # cannot express that, because returning one makes the hook bypass the
    # phases entirely rather than choose.
    if cfg.name in ("ledger", "adaptive"):
        return None
    return {"full": FullPolicy, "fifo": FifoPolicy, "summary": SummaryPolicy,
            "ledger_noevict": LedgerNoEvictPolicy}[cfg.name](cfg)
