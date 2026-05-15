"""MemoryHook - the integration seam between Memory and nanobot's run loop.

MemoryHook composes with nanobot's AgentHook lifecycle. Its job is to:

- before_iteration:  prepare the message log for the next LLM call
                     (screenshot back-patch, failure collapse in step 5,
                     ledger refresh in step 7).
- after_iteration:   ingest the just-completed step into the ledger,
                     persist to disk, log observability events.

Composition:
  worker:       hooks=[MemoryHook(worker_memory), BrowserWorkerHook(state)]
  orchestrator: hooks=[MemoryHook(orchestrator_memory)]

MemoryHook runs first so the worker hook reads a context that already
has older screenshots evicted and prior-turn failures collapsed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext

if TYPE_CHECKING:
    from .memory import Memory


_IMAGE_TYPES = frozenset({"image", "image_url"})
_DEFAULT_KEEP_LAST_SCREENSHOTS = 2
_DEFAULT_KEEP_LAST_FAILURES = 1

_LEDGER_TAG_OPEN = "[Agent Ledger v1]"
_LEDGER_TAG_CLOSE = "[/Agent Ledger v1]"
_LEDGER_PREAMBLE = "\n\n"  # separator from the rest of the system prompt

# Markers that reliably indicate a tool failure in this codebase.
# Source: formatting._build_network_block_message, every tool's failure
# return string, antibot/captcha detection text. The patterns are
# narrow on purpose: false positives would silently collapse healthy
# tool results.
_FAILURE_RE = re.compile(
    r"\[(ERROR|NETWORK_BLOCKED|CF_INTERSTITIAL|WORKER_NO_TOOL_CALLS)\]?"
    r"|\bFAILED\b"
    r"|\bCF_INTERSTITIAL_STUCK\b"
    r"|\bNETWORK_BLOCKED\b"
    r"|HTTP\s+(401|403|404|429|451|503)\b",
)


def _message_text(msg: dict[str, Any]) -> str:
    """Concatenate every text block in a message's content into a string.

    Tool results in superbrowser arrive both as plain strings and as
    multimodal content lists (text + image blocks). The failure scanner
    needs a single flat string regardless of shape.
    """
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _back_patch_screenshots(
    messages: list[dict[str, Any]],
    *,
    keep_last_n: int = _DEFAULT_KEEP_LAST_SCREENSHOTS,
) -> int:
    """Replace image blocks in older messages with eviction markers.

    Walks `messages` once, identifies every message whose content list
    contains at least one image block, keeps the most recent
    ``keep_last_n`` such messages untouched, and rewrites all older
    messages so each image block becomes a small "[screenshot from
    prior turn evicted]" text block in the same position. The text
    caption that vision_pipeline emits alongside the image (a sibling
    text block in the same content list) is preserved untouched.

    Mutates ``messages`` in place. Returns the count of image blocks
    that were replaced.
    """
    image_msg_indices: list[int] = []
    for i in range(len(messages) - 1, -1, -1):
        content = messages[i].get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(b, dict) and b.get("type") in _IMAGE_TYPES
            for b in content
        ):
            image_msg_indices.append(i)
    if len(image_msg_indices) <= keep_last_n:
        return 0

    # image_msg_indices is end-to-start; skip the newest keep_last_n,
    # back-patch everything older.
    to_evict = image_msg_indices[keep_last_n:]
    n_evicted = 0
    for i in to_evict:
        msg = messages[i]
        new_content: list[dict[str, Any]] = []
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("type") in _IMAGE_TYPES:
                new_content.append(
                    {"type": "text", "text": "[screenshot from prior turn evicted]"}
                )
                n_evicted += 1
            else:
                new_content.append(block)
        msg["content"] = new_content
    return n_evicted


def _refresh_ledger_in_system_message(
    messages: list[dict[str, Any]],
    ledger_text: str,
) -> bool:
    """Embed the rendered ledger inside ``messages[0]``'s system prompt.

    The injection lives between ``[Agent Ledger v1]`` /
    ``[/Agent Ledger v1]`` delimiters appended to the existing system
    message. On every iteration we strip any prior block and write a
    fresh one in its place; the surrounding system prompt is untouched.

    Why mutate index 0 rather than insert a new system message:
    the runner's ``_save_turn(skip=...)`` uses a fixed pre-turn skip
    boundary. Inserting a message would shift indices past the
    boundary and either persist our injection or drop a legitimate
    turn message. Mutating ``messages[0]`` keeps every index stable,
    and because nanobot rebuilds the system message from
    ``build_system_prompt`` at the start of each turn, our edits do
    not pollute the next turn either.

    Returns True if the system message was updated, False if no
    system message exists or its content shape is unsupported (e.g.
    a multimodal content list - rare for system prompts).
    """
    if not messages:
        return False
    head = messages[0]
    if head.get("role") != "system":
        return False
    content = head.get("content")
    if not isinstance(content, str):
        return False

    block = (
        f"{_LEDGER_PREAMBLE}{_LEDGER_TAG_OPEN}\n"
        f"{ledger_text}\n"
        f"{_LEDGER_TAG_CLOSE}"
    )

    start_idx = content.find(_LEDGER_TAG_OPEN)
    if start_idx == -1:
        head["content"] = content + block
        return True

    # Replace existing block; preserve everything before the preamble
    # so the original system prompt is never touched.
    preamble_start = max(0, start_idx - len(_LEDGER_PREAMBLE))
    if content[preamble_start:start_idx] != _LEDGER_PREAMBLE:
        preamble_start = start_idx
    end_idx = content.find(_LEDGER_TAG_CLOSE, start_idx)
    if end_idx == -1:
        head["content"] = content[:preamble_start] + block
        return True
    head["content"] = (
        content[:preamble_start]
        + block
        + content[end_idx + len(_LEDGER_TAG_CLOSE):]
    )
    return True


def _extract_failure_snippet(text: str, *, max_chars: int = 140) -> str:
    """Pull a short single-line excerpt of the failure reason.

    Prefers the line that triggered the failure regex; falls back to
    the first non-empty line. Keeps the result under ``max_chars`` so
    collapsed messages don't leak the bulk back into context.
    """
    if not text:
        return "unknown failure"
    match = _FAILURE_RE.search(text)
    if match:
        start = text.rfind("\n", 0, match.start()) + 1
        end = text.find("\n", match.start())
        end = end if end != -1 else len(text)
        line = text[start:end].strip()
    else:
        line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = line.replace("\n", " ").strip()
    if len(line) > max_chars:
        line = line[: max_chars - 1] + "…"
    return line or "unknown failure"


def _collapse_failed_tool_messages(
    messages: list[dict[str, Any]],
    *,
    keep_last_n: int = _DEFAULT_KEEP_LAST_FAILURES,
) -> list[str]:
    """Compact older failure-bearing tool messages to a single line.

    The most recent ``keep_last_n`` failure-bearing tool messages stay
    verbatim - the worker needs to see its immediate failure once and
    react. Older failures, which the worker has already responded to,
    collapse to::

        [collapsed earlier failure: <one-line reason>]

    The assistant message that issued the tool call is left untouched,
    so the model can still see what was tried; only the bulky result
    payload is condensed. The point is not to hide history but to stop
    rebuilding malformed-call patterns from echoed-back failures.

    Mutates ``messages`` in place. Returns the collapsed reasons (one
    per message) for ledger ingestion.
    """
    failure_indices: list[int] = []
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "tool":
            continue
        text = _message_text(msg)
        if _FAILURE_RE.search(text):
            failure_indices.append(i)
    if len(failure_indices) <= keep_last_n:
        return []

    to_collapse = failure_indices[keep_last_n:]
    collapsed_reasons: list[str] = []
    for i in to_collapse:
        msg = messages[i]
        reason = _extract_failure_snippet(_message_text(msg))
        replacement = f"[collapsed earlier failure: {reason}]"
        msg["content"] = replacement
        collapsed_reasons.append(reason)
    return collapsed_reasons


class MemoryHook(AgentHook):
    """nanobot-side surface for the memory subsystem.

    Each Memory facade owns one MemoryHook. Construct via
    Memory.attach(bot) - never instantiate directly.
    """

    __slots__ = ("memory", "keep_last_screenshots", "keep_last_failures")

    def __init__(
        self,
        memory: "Memory",
        *,
        keep_last_screenshots: int = _DEFAULT_KEEP_LAST_SCREENSHOTS,
        keep_last_failures: int = _DEFAULT_KEEP_LAST_FAILURES,
    ) -> None:
        super().__init__()
        self.memory = memory
        self.keep_last_screenshots = keep_last_screenshots
        self.keep_last_failures = keep_last_failures

    async def before_iteration(self, context: AgentHookContext) -> None:
        # Two context-pruning passes run before BrowserWorkerHook so the
        # worker hook reads a clean message log:
        #
        # 1. Screenshot back-patch - image bytes from older tool messages
        #    become a one-line marker; the most recent two screenshots
        #    stay verbatim. Captions (sibling text blocks) survive.
        #
        # 2. Failure collapse - the most recent failure-bearing tool
        #    message stays verbatim (the worker needs to see it and
        #    react). Older failures, which the worker has already
        #    responded to, collapse to a single-line "[collapsed earlier
        #    failure: ...]" string so the LLM stops pattern-matching on
        #    repeated malformed-call traces.
        #
        # Step 7 will add ledger refresh into session.metadata['_last_summary'].
        try:
            evicted = _back_patch_screenshots(
                context.messages,
                keep_last_n=self.keep_last_screenshots,
            )
            if evicted:
                self.memory.events.log(
                    "screenshot_evicted",
                    {
                        "iter": context.iteration,
                        "role": self.memory.role,
                        "count": evicted,
                    },
                )
        except Exception as exc:
            logger.debug("MemoryHook back-patch failed: {}", exc)

        try:
            collapsed = _collapse_failed_tool_messages(
                context.messages,
                keep_last_n=self.keep_last_failures,
            )
            if collapsed:
                self.memory.events.log(
                    "failures_collapsed",
                    {
                        "iter": context.iteration,
                        "role": self.memory.role,
                        "count": len(collapsed),
                        "reasons": collapsed,
                    },
                )
                # Each collapsed failure becomes a dead-end ledger entry
                # so the orchestrator's render shows the path was tried
                # and failed - even though the tool message that proved
                # it has been condensed away.
                for reason in collapsed:
                    try:
                        self.memory.mark_dead_end(reason)
                    except Exception as exc:
                        logger.debug("mark_dead_end failed: {}", exc)
        except Exception as exc:
            logger.debug("MemoryHook failure-collapse failed: {}", exc)

        # Refresh the ledger block embedded in messages[0]. Runs after
        # back-patch and failure-collapse so the ledger reflects the
        # latest dead-end additions on the same turn.
        try:
            ledger_text = self.memory.render_for_llm()
            ok = _refresh_ledger_in_system_message(context.messages, ledger_text)
            if ok:
                self.memory.events.log(
                    "ledger_injected",
                    {
                        "iter": context.iteration,
                        "role": self.memory.role,
                        "chars": len(ledger_text),
                    },
                )
        except Exception as exc:
            logger.debug("MemoryHook ledger-injection failed: {}", exc)

    async def after_iteration(self, context: AgentHookContext) -> None:
        # Step 6: ingest the just-completed step into Ledger.
        # Step 6: persist ledger to disk.
        try:
            self.memory.events.log(
                "memory_after_iter",
                {"iter": context.iteration, "role": self.memory.role},
            )
        except Exception as exc:
            logger.debug("MemoryHook.after_iteration logging failed: {}", exc)
