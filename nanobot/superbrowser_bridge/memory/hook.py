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

import hashlib
import re
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext

if TYPE_CHECKING:
    from .memory import Memory


_IMAGE_TYPES = frozenset({"image", "image_url"})
_DEFAULT_KEEP_LAST_SCREENSHOTS = 2
_DEFAULT_KEEP_LAST_FAILURES = 1
_DEFAULT_KEEP_LAST_ELEMENT_LISTS = 1
# Item 2 — strip thinking_blocks past this many assistant messages.
# Each thinking_blocks payload can be 500-2000 tokens; on a 20-iter
# task that's 10-40k tokens of stale reasoning the model would read
# every turn.
_DEFAULT_KEEP_LAST_THINKING = 3
# Item 1 — start gutting old message content once we cross this many
# messages in flight. Below threshold, the existing collapse passes
# (state-block / element-list / failure) already keep growth modest.
_DEFAULT_GUT_THRESHOLD = 30
# Item 1 — past threshold, keep this many recent turns fully intact.
# Each turn = ~2 messages (assistant + tool), so 5 turns ≈ 10 messages.
_DEFAULT_KEEP_RECENT_TURNS = 5

# Element-list tool results carry an [ELEMENTS N shown...] header (verified
# against session_tools/tools/list_elements.py). Once the model has chosen
# a V_n in response to one list, older lists are dead weight.
_ELEMENT_LIST_RE = re.compile(r"\[ELEMENTS\s+\d+\s+shown")

# Pulls the URL from a [SESSION_STATE session_id=... url=... title=...]
# header (verified against session_tools/formatting._format_state). Used
# by failure-collapse to tag the resulting DeadEnd with the page on which
# the failure happened.
_STATE_URL_RE = re.compile(
    r"\[SESSION_STATE\s+session_id=\S+\s+url=(\S+)",
)

# Matches the full [SESSION_STATE ...] block — used by Phase 4 to strip
# stale state blocks from older tool/user messages. The block is multi-
# line (URL + Title + Scroll + Elements + Notices) and ends at a blank
# line or end of text.
_STATE_BLOCK_RE = re.compile(
    r"\[SESSION_STATE\s+session_id=\S+[^\n]*"  # header line
    r"(?:\n(?!\n)[^\n]*)*",  # continuation lines until blank
    re.MULTILINE,
)

_LEDGER_TAG_OPEN = "[Agent Ledger v1]"
_LEDGER_TAG_CLOSE = "[/Agent Ledger v1]"
_LEDGER_PREAMBLE = "\n\n"  # separator from the rest of the system prompt

# Markers that reliably indicate a tool failure in this codebase.
# Source: formatting._build_network_block_message, every tool's failure
# return string, antibot/captcha detection text, verify_action postcondition
# misses, click_at rejection paths in worker_hook, dead-click guard.
# The Phase 1 expansion covers the production failure idioms surfaced by the
# wineaccess.com run (click_at_failed, dead_click, label_mismatch=True,
# VERIFY_MISS, low_reward_band, etc.) — the prior regex missed these and
# the failure-collapse pass never fired in production.
_FAILURE_RE = re.compile(
    r"\[(ERROR|NETWORK_BLOCKED|CF_INTERSTITIAL|WORKER_NO_TOOL_CALLS"
    r"|click_at_failed|click_at rejected|click_blocked|click_silent"
    r"|click_loop_detected|VERIFY_MISS|low_reward_band"
    r"|DOMAIN_PINNED|CAPTCHA DETECTED|transient_rate_limit|t3_open_failed"
    r"|human_handoff_timeout|browser_ask_user_t3_error"
    r"|CF_INTERSTITIAL_STUCK|CF_INTERSTITIAL_PENDING)\b"
    r"|\bFAILED\b|\bdead_click\b|\blabel_mismatch=True\b"
    r"|HTTP\s+(401|403|404|429|451|503)\b",
)
# Intentionally NOT in _FAILURE_RE:
#   - click_escalated     → an escalation strategy succeeded; happy path
#   - human_handoff_cleared → the handoff resolved; happy path
# Matching these would cause failure-collapse to evict successful
# recoveries from the message log and write spurious "recovered"
# dead-ends to the ledger.


def _classify_failure(match: "re.Match[str]") -> str:
    """Bucket a failure-regex match into a short cause label.

    Used by Phase 2 to populate ``DeadEnd.cause`` so the ledger can answer
    questions like "all bot-blocks on this URL" without grep-ing
    descriptions. Buckets are deliberately coarse — finer-grained
    distinctions live in the raw description string.
    """
    token = match.group(1) or ""
    full = match.group(0) or ""
    if token == "NETWORK_BLOCKED" or "HTTP" in full:
        return "network"
    if token.startswith("CF_") or token == "CAPTCHA DETECTED":
        return "bot_block"
    if token == "transient_rate_limit":
        return "rate_limit"
    if token in (
        "click_at_failed",
        "click_blocked",
        "click_silent",
        "click_loop_detected",
        "click_at rejected",
        "low_reward_band",
    ) or "dead_click" in full or "label_mismatch" in full:
        return "stale_selector"
    if token == "VERIFY_MISS":
        return "postcondition_miss"
    if token == "DOMAIN_PINNED":
        return "policy_block"
    if token in ("human_handoff_timeout", "browser_ask_user_t3_error"):
        return "handoff_timeout"
    if token in ("WORKER_NO_TOOL_CALLS",):
        return "worker_idle"
    if token == "t3_open_failed":
        return "tier3_open"
    if token == "ERROR" or "FAILED" in full:
        return "generic"
    return "unknown"


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

    Phase 4 — two-block scheme for cache breakpoint isolation:

    ::

        messages[0]["content"] = [
            {"type": "text", "text": <static prefix>,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": <ledger block>},
        ]

    The static prefix block carries a cache_control marker that we
    place ourselves. The Anthropic provider's ``_apply_cache_control``
    (nanobot/providers/anthropic_provider.py:389-391) ALSO marks
    ``system[-1]`` with cache_control, leaving our block-0 marker
    untouched. Result: two cache breakpoints, where the static prefix
    block survives across iterations even when the ledger block
    changes every turn — fixing the 0% cache regression observed on
    wineaccess.com.

    The first call captures the existing system prompt as the
    "static prefix" — subsequent calls preserve ``content[0]["text"]``
    so the cache key for that block stays stable. The ledger block at
    ``content[1]`` is rewritten on every call.

    Why mutate index 0 rather than insert a new system message:
    the runner's ``_save_turn(skip=...)`` uses a fixed pre-turn skip
    boundary. Inserting a message would shift indices past the
    boundary and either persist our injection or drop a legitimate
    turn message. Mutating ``messages[0]`` keeps every index stable,
    and because nanobot rebuilds the system message from
    ``build_system_prompt`` at the start of each turn, our edits do
    not pollute the next turn either.

    ``_convert_messages`` (anthropic_provider.py:132-133) passes list
    system content through unchanged, so the list-of-blocks shape
    survives provider conversion.

    Returns True if the system message was updated, False if no
    system message exists or its content shape is unsupported.
    """
    if not messages:
        return False
    head = messages[0]
    if head.get("role") != "system":
        return False
    content = head.get("content")

    # Capture the static prefix. Two paths:
    #   First call:    content is a str — the original system prompt.
    #   Later calls:   content is the list we wrote previously; block 0
    #                  holds the static prefix verbatim.
    if isinstance(content, str):
        static_text = content
    elif isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            static_text = first.get("text", "")
        else:
            return False
    else:
        return False

    ledger_block_text = (
        f"{_LEDGER_PREAMBLE}{_LEDGER_TAG_OPEN}\n"
        f"{ledger_text}\n"
        f"{_LEDGER_TAG_CLOSE}"
    )

    head["content"] = [
        {
            "type": "text",
            "text": static_text,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": ledger_block_text,
            # No cache_control here — _apply_cache_control adds one to
            # system[-1] by the provider's own logic, giving us 2
            # breakpoints total. The marker on the dynamic block is a
            # cache miss every turn (ledger changes), but the marker
            # on block 0 stays a cache HIT across turns.
        },
    ]
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
) -> list[dict[str, str]]:
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

    Mutates ``messages`` in place. Returns one dict per collapsed
    failure: ``{"reason": ..., "url": ..., "cause": ...}``. The caller
    feeds these into ``Memory.mark_dead_end(url=..., cause=...)`` so
    the resulting DeadEnd carries state-keyed metadata.

    URL extraction: scans the prior tool message's text for the
    ``[SESSION_STATE ... url=...]`` header (the state block that
    rides alongside most tool results in this codebase). Falls back
    to the empty string if no prior state is visible.
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
    collapsed: list[dict[str, str]] = []
    for i in to_collapse:
        msg = messages[i]
        msg_text = _message_text(msg)
        reason = _extract_failure_snippet(msg_text)
        # URL: try the current message's own state block first; if
        # absent (the failure result might be just an error line),
        # walk back through the messages list looking for the most
        # recent state header.
        url = ""
        url_m = _STATE_URL_RE.search(msg_text)
        if url_m:
            url = url_m.group(1)
        else:
            for j in range(i - 1, -1, -1):
                prev_text = _message_text(messages[j])
                prev_m = _STATE_URL_RE.search(prev_text)
                if prev_m:
                    url = prev_m.group(1)
                    break
        # Cause: re-run the regex on the snippet and classify.
        cause_m = _FAILURE_RE.search(msg_text)
        cause = _classify_failure(cause_m) if cause_m else "unknown"
        replacement = f"[collapsed earlier failure: {reason}]"
        msg["content"] = replacement
        collapsed.append({"reason": reason, "url": url, "cause": cause})
    return collapsed


def _collapse_stale_state_blocks(
    messages: list[dict[str, Any]],
    *,
    keep_last_n: int = 2,
) -> int:
    """Strip ``[SESSION_STATE ...]`` blocks from older tool/user messages.

    The most recent state block per session is what the LLM uses to
    reason about "where am I now". Older blocks are stale by
    construction — the URL/title/scroll they describe is no longer
    the current state. Each block is ~150-300 tokens of dead weight.

    Keep the most recent ``keep_last_n`` messages' state blocks
    verbatim; in older messages, replace each [SESSION_STATE ...]
    block with a single-line marker ``[state from prior turn evicted]``.
    The surrounding text in the message (captions, vision V_n lists,
    tool-specific result content) is preserved.

    Mutates message content strings in place. Returns the number of
    state blocks replaced.
    """
    state_msg_indices: list[int] = []
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") not in ("tool", "user"):
            continue
        text = _message_text(msg)
        if "[SESSION_STATE " in text:
            state_msg_indices.append(i)
    if len(state_msg_indices) <= keep_last_n:
        return 0

    to_strip = state_msg_indices[keep_last_n:]
    n_stripped = 0
    replacement = "[state from prior turn evicted]"
    for i in to_strip:
        msg = messages[i]
        content = msg.get("content")
        if isinstance(content, str):
            new_content, n_sub = _STATE_BLOCK_RE.subn(replacement, content)
            if n_sub:
                msg["content"] = new_content
                n_stripped += n_sub
        elif isinstance(content, list):
            new_blocks: list[dict[str, Any]] = []
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ):
                    new_text, n_sub = _STATE_BLOCK_RE.subn(
                        replacement, block["text"]
                    )
                    if n_sub:
                        new_blocks.append({**block, "text": new_text})
                        n_stripped += n_sub
                        continue
                new_blocks.append(block)
            msg["content"] = new_blocks
    return n_stripped


def _collapse_stale_thinking_blocks(
    messages: list[dict[str, Any]],
    *,
    keep_last_n: int = _DEFAULT_KEEP_LAST_THINKING,
) -> int:
    """Strip ``thinking_blocks`` from older assistant messages.

    Item 2 of the human-memory analogy fix. Each ``thinking_blocks``
    payload can be 500-2000 tokens of extended reasoning. By iter 20
    that accumulates to 10-40k tokens of stale chain-of-thought the
    model is forced to read on every turn — and the model could
    over-index on its own old reasoning instead of trusting the
    structured ledger or current observations.

    Keep ``thinking_blocks`` on the most recent ``keep_last_n``
    assistant messages (so the current iter's own reasoning context
    is preserved). Clear it on older ones. Tool_calls are NOT
    touched — the assistant's actions stay visible; only the
    deliberation around them gets evicted.

    Anthropic accepts assistant messages with empty thinking_blocks
    as long as content or tool_calls is present. Idempotent — clearing
    an already-empty thinking_blocks is a no-op.

    Mutates messages in place. Returns number of messages cleared.
    """
    assistant_indices: list[int] = []
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            assistant_indices.append(i)
    if len(assistant_indices) <= keep_last_n:
        return 0

    to_strip = assistant_indices[keep_last_n:]
    cleared = 0
    for i in to_strip:
        msg = messages[i]
        tb = msg.get("thinking_blocks")
        if tb:
            msg["thinking_blocks"] = []
            cleared += 1
        # reasoning_content is the older sibling field that some models
        # populate instead of thinking_blocks. Strip it too — same
        # rationale: stale CoT, not load-bearing once the action has
        # been taken.
        if msg.get("reasoning_content"):
            msg["reasoning_content"] = ""
    return cleared


def _gut_old_message_content(
    messages: list[dict[str, Any]],
    *,
    threshold: int = _DEFAULT_GUT_THRESHOLD,
    keep_last_turns: int = _DEFAULT_KEEP_RECENT_TURNS,
) -> int:
    """Hard-evict the content of old messages, preserving structure.

    Item 1 of the human-memory analogy fix. Above ``threshold``
    messages in flight, walks the conversation log and guts the
    text/result content of every message older than the last
    ``keep_last_turns`` turns. Preserves:

    - Message count (so ``_save_turn``'s fixed skip boundary stays valid)
    - Assistant ``tool_calls`` with their ids/names/inputs (so
      tool_use ↔ tool_result pairing the Anthropic API requires
      stays consistent)
    - The initial user message at index 1 (the task prompt — the
      model still needs to know what it was asked to do)
    - The system message at index 0 (carries our ledger appendix)

    Evicts:
    - Assistant ``content`` text and ``thinking_blocks`` /
      ``reasoning_content``
    - Tool result message body — replaced with ``[archived]``
    - The bulk of older user messages (rare in superbrowser; included
      defensively)

    The ``_archived`` flag makes the operation idempotent across
    re-runs and across iters — already-archived messages are skipped.

    Why not delete the messages entirely:
    - ``_save_turn(skip=...)`` uses a fixed pre-turn skip boundary
      computed from history length. Deleting messages mid-list shifts
      indices past that boundary and breaks persistence.
    - Anthropic requires tool_use blocks to have matching tool_result
      blocks. Deleting either half orphans the other.
    - This way the message dicts stay in place (and on disk), but their
      payload shrinks from hundreds of tokens to ~5 tokens each.

    Mutates messages in place. Returns count of messages gutted.
    """
    if len(messages) < threshold:
        return 0
    # Keep system (index 0), keep_last_turns turns at the tail, AND the
    # initial user task (index 1). That's at least 2 protected slots
    # plus keep_last_turns*2 message tail.
    keep_tail = keep_last_turns * 2
    # Always leave at least the initial system + initial user untouched.
    if len(messages) <= keep_tail + 2:
        return 0

    start = 2  # 0=system, 1=initial user task
    end = len(messages) - keep_tail  # everything before the keep tail

    gutted = 0
    for i in range(start, end):
        msg = messages[i]
        if msg.get("_archived"):
            continue
        role = msg.get("role")
        if role == "assistant":
            # Strip text + reasoning; keep tool_calls so pairing holds.
            if msg.get("content"):
                msg["content"] = ""
            if msg.get("thinking_blocks"):
                msg["thinking_blocks"] = []
            if msg.get("reasoning_content"):
                msg["reasoning_content"] = ""
            msg["_archived"] = True
            gutted += 1
        elif role == "tool":
            content = msg.get("content")
            if isinstance(content, (str, list)) and content:
                msg["content"] = "[archived]"
                msg["_archived"] = True
                gutted += 1
        elif role == "user" and i > 1:
            # Mid-conversation user messages are rare (worker_hook
            # injects guidance into tool messages instead). Gut
            # defensively if encountered.
            content = msg.get("content")
            if isinstance(content, (str, list)) and content:
                msg["content"] = "[archived]"
                msg["_archived"] = True
                gutted += 1
    return gutted


def _collapse_stale_element_lists(
    messages: list[dict[str, Any]],
    *,
    keep_last_n: int = _DEFAULT_KEEP_LAST_ELEMENT_LISTS,
) -> int:
    """Compact older browser_list_elements tool results to a single line.

    The most recent ``keep_last_n`` element-list tool messages stay
    verbatim. Older ones — which the model has already consumed by
    picking a V_n or DOM index — collapse to::

        [earlier element list collapsed — call browser_list_elements again if needed]

    Element lists routinely run 30-80 rows of bbox/index/role/text data.
    Once the model has acted on them they're pure context bloat.

    Mutates ``messages`` in place. Returns the number of messages
    collapsed (for observability event payload).
    """
    list_indices: list[int] = []
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "tool":
            continue
        text = _message_text(msg)
        if _ELEMENT_LIST_RE.search(text):
            list_indices.append(i)
    if len(list_indices) <= keep_last_n:
        return 0

    to_collapse = list_indices[keep_last_n:]
    replacement = (
        "[earlier element list collapsed — call browser_list_elements "
        "again if needed]"
    )
    for i in to_collapse:
        messages[i]["content"] = replacement
    return len(to_collapse)


class MemoryHook(AgentHook):
    """nanobot-side surface for the memory subsystem.

    Each Memory facade owns one MemoryHook. Construct via
    Memory.attach(bot) - never instantiate directly.
    """

    __slots__ = (
        "memory",
        "keep_last_screenshots",
        "keep_last_failures",
        "keep_last_element_lists",
        "keep_last_state_blocks",
        "keep_last_thinking",
        "gut_threshold",
        "keep_recent_turns",
        "_last_seen_messages",
        "_last_autocompact_hash",
        "_bot",
    )

    def __init__(
        self,
        memory: "Memory",
        *,
        keep_last_screenshots: int = _DEFAULT_KEEP_LAST_SCREENSHOTS,
        keep_last_failures: int = _DEFAULT_KEEP_LAST_FAILURES,
        keep_last_element_lists: int = _DEFAULT_KEEP_LAST_ELEMENT_LISTS,
        keep_last_state_blocks: int = 2,
        keep_last_thinking: int = _DEFAULT_KEEP_LAST_THINKING,
        gut_threshold: int = _DEFAULT_GUT_THRESHOLD,
        keep_recent_turns: int = _DEFAULT_KEEP_RECENT_TURNS,
    ) -> None:
        super().__init__()
        self.memory = memory
        self.keep_last_screenshots = keep_last_screenshots
        self.keep_last_failures = keep_last_failures
        self.keep_last_element_lists = keep_last_element_lists
        self.keep_last_state_blocks = keep_last_state_blocks
        self.keep_last_thinking = keep_last_thinking
        self.gut_threshold = gut_threshold
        self.keep_recent_turns = keep_recent_turns
        # Reference (not copy) to the last messages list this hook saw
        # in after_iteration. Phase 3 uses this so delegation.py's
        # try/finally can hand the final message slice to compact_subgoal
        # without needing access to the worker's run loop internals.
        self._last_seen_messages: list[dict[str, Any]] | None = None
        # Phase 4 — track the last-seen AutoCompact summary so we only
        # ingest novel ones into episodic memory. A SHA1 of the text
        # field is enough; the timestamp inside the summary changes on
        # every AutoCompact write so we can't compare metadata blindly.
        self._last_autocompact_hash: str = ""
        # Bot reference for AutoCompact ingestion (reads session.metadata).
        # Set by Memory.attach via _bind_bot below.
        self._bot: Any | None = None

    def _bind_bot(self, bot: Any) -> None:
        """Called by Memory.attach so AutoCompact ingestion can find the session."""
        self._bot = bot

    def _ingest_autocompact_summary(self) -> None:
        """Detect AutoCompact summaries and fold them into episodic memory.

        AutoCompact writes ``session.metadata["_last_summary"]`` when
        nanobot's token-budget consolidator runs (typically on long
        tasks that exceed the context window). The shape is
        ``{"text": <str>, "last_active": <iso timestamp>}``.

        We track the last-seen summary by SHA1 of its text. When the
        hash changes, we know AutoCompact ran since our last iter and
        we capture the text as a tagged episodic entry — so the
        information AutoCompact captured doesn't vanish from the
        agent's view once the dynamic ledger render obscures it.

        Best-effort: any failure logs and continues. No coordination
        with AutoCompact's own writes — passive observation only.
        """
        bot = self._bot
        if bot is None:
            return
        try:
            session = getattr(bot, "_session", None)
            if session is None:
                return
            meta = getattr(session, "metadata", None) or {}
            summary = meta.get("_last_summary")
            if not isinstance(summary, dict):
                return
            text = summary.get("text") or ""
            if not text:
                return
            h = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
            if h == self._last_autocompact_hash:
                return
            self._last_autocompact_hash = h
            line = f"[AutoCompact @ {summary.get('last_active', '')}] {text[:600]}"
            self.memory.ledger.add_episode(line)
            self.memory.store.append_episode(line, kind="autocompact_summary")
            self.memory.events.log(
                "autocompact_ingested",
                {"chars": len(text), "role": self.memory.role},
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("autocompact ingestion failed: {}", exc)

    async def before_iteration(self, context: AgentHookContext) -> None:
        # Context-pruning passes run before BrowserWorkerHook so the
        # worker hook reads a clean message log. Phase 1 + 4 expanded
        # the lineup to:
        #
        # 1. AutoCompact ingestion (Phase 4) — if nanobot's
        #    consolidator wrote a fresh summary into session.metadata
        #    since last iter, capture it as episodic memory BEFORE we
        #    re-render the ledger so the summary appears in this turn's
        #    injection.
        # 2. Screenshot back-patch - image bytes from older tool messages
        #    become a one-line marker; the most recent two screenshots
        #    stay verbatim. Captions (sibling text blocks) survive.
        #    (Mostly a no-op in superbrowser since vision pipeline
        #    consumes screenshots before they reach messages, but kept
        #    for non-superbrowser callers.)
        # 3. Failure collapse - the most recent failure-bearing tool
        #    message stays verbatim (the worker needs to see it and
        #    react). Older failures, which the worker has already
        #    responded to, collapse to a single-line "[collapsed earlier
        #    failure: ...]" string so the LLM stops pattern-matching on
        #    repeated malformed-call traces.
        # 4. Element-list collapse (Phase 1) — older
        #    browser_list_elements results compact to a single line.
        # 5. State-block collapse (Phase 4) — old [SESSION_STATE...]
        #    blocks become "[state from prior turn evicted]" — they're
        #    the dominant growth driver and stale by construction.
        # 6. Ledger refresh into messages[0] using the two-block list
        #    shape so the static prefix can be cached.
        try:
            self._ingest_autocompact_summary()
        except Exception as exc:
            logger.debug("MemoryHook autocompact-ingest failed: {}", exc)

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
                        "reasons": [c["reason"] for c in collapsed],
                        "causes": [c["cause"] for c in collapsed],
                    },
                )
                # Each collapsed failure becomes a dead-end ledger entry
                # so the orchestrator's render shows the path was tried
                # and failed - even though the tool message that proved
                # it has been condensed away. Phase 2: dead-ends carry
                # the URL where they happened and a coarse cause label
                # for state-keyed retrieval.
                for c in collapsed:
                    try:
                        self.memory.mark_dead_end(
                            c["reason"],
                            url=c.get("url", ""),
                            cause=c.get("cause", "unknown"),
                        )
                    except Exception as exc:
                        logger.debug("mark_dead_end failed: {}", exc)
        except Exception as exc:
            logger.debug("MemoryHook failure-collapse failed: {}", exc)

        # Element-list collapse: older browser_list_elements results are
        # dead weight once the model has acted on them. Mirrors the
        # failure-collapse shape; runs after failure-collapse so a stale
        # list that also happens to contain a failure marker is handled
        # by the failure pass first.
        try:
            collapsed_lists = _collapse_stale_element_lists(
                context.messages,
                keep_last_n=self.keep_last_element_lists,
            )
            if collapsed_lists:
                self.memory.events.log(
                    "element_list_collapsed",
                    {
                        "iter": context.iteration,
                        "role": self.memory.role,
                        "count": collapsed_lists,
                    },
                )
        except Exception as exc:
            logger.debug("MemoryHook element-list-collapse failed: {}", exc)

        # State-block collapse (Phase 4): old [SESSION_STATE ...] blocks
        # describe a URL/title/scroll/element-count snapshot that's
        # stale by construction once newer turns happen. This is the
        # dominant growth driver in long browsing tasks. Keep the
        # most recent ``keep_last_state_blocks`` and strip the rest.
        try:
            stripped_states = _collapse_stale_state_blocks(
                context.messages,
                keep_last_n=self.keep_last_state_blocks,
            )
            if stripped_states:
                self.memory.events.log(
                    "state_block_collapsed",
                    {
                        "iter": context.iteration,
                        "role": self.memory.role,
                        "count": stripped_states,
                    },
                )
        except Exception as exc:
            logger.debug("MemoryHook state-block-collapse failed: {}", exc)

        # Item 2: strip stale thinking_blocks / extended-reasoning text
        # from older assistant messages. Runs every iter so growth is
        # bounded as soon as new assistant messages accumulate. Each
        # stripped block is 500-2000 tokens; the dominant remaining
        # bulk in old assistant messages after state-block-collapse.
        try:
            stripped_thinking = _collapse_stale_thinking_blocks(
                context.messages,
                keep_last_n=self.keep_last_thinking,
            )
            if stripped_thinking:
                self.memory.events.log(
                    "thinking_blocks_stripped",
                    {
                        "iter": context.iteration,
                        "role": self.memory.role,
                        "count": stripped_thinking,
                    },
                )
        except Exception as exc:
            logger.debug("MemoryHook thinking-strip failed: {}", exc)

        # Item 1: hard-evict old message content past the threshold.
        # Most aggressive pass — only fires once we've crossed
        # gut_threshold messages in flight, and only on messages older
        # than the last keep_recent_turns turns. Preserves message
        # count, tool_use/tool_result pairing, and the initial
        # system+user. Models any "older history" as terse markers
        # while the ledger render in messages[0] carries the
        # structured ground truth.
        try:
            gutted = _gut_old_message_content(
                context.messages,
                threshold=self.gut_threshold,
                keep_last_turns=self.keep_recent_turns,
            )
            if gutted:
                self.memory.events.log(
                    "messages_gutted",
                    {
                        "iter": context.iteration,
                        "role": self.memory.role,
                        "count": gutted,
                        "messages_in_flight": len(context.messages),
                    },
                )
                # Add an episodic line so the ledger remembers a chunk
                # of history was archived — gives the model a structured
                # cue that older turns are condensed.
                try:
                    line = (
                        f"[archived {gutted} message(s) at iter "
                        f"{context.iteration}; older actions condensed "
                        f"into markers, ledger remains authoritative]"
                    )
                    self.memory.ledger.add_episode(line)
                    self.memory.store.append_episode(
                        line, kind="auto_archive"
                    )
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("MemoryHook gut-old failed: {}", exc)

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
        # Phase 3: stash a reference to the message list so the worker
        # finally-block (delegation.py) can hand the final slice to
        # compact_subgoal without poking nanobot internals. Reference
        # semantics — the runner mutates this list in-place across the
        # remaining iters, so by the time the finally fires we'll see
        # whatever was current at the worker's last reachable state.
        self._last_seen_messages = context.messages

        # Phase 0 instrumentation: cache stats land in the per-iter event so
        # downstream phases can verify the cache breakpoint and pruning
        # passes are actually moving the cached/uncached split. For workers
        # BrowserWorkerHook also logs an "iteration" event with the same
        # fields; the orchestrator role has only this hook, so this is the
        # sole source of cache telemetry on that side.
        try:
            usage = context.usage or {}
            self.memory.events.log(
                "memory_after_iter",
                {
                    "iter": context.iteration,
                    "role": self.memory.role,
                    "tokens_in": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
                    "tokens_out": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
                    "cache_read": usage.get("cache_read_input_tokens") or 0,
                    "cache_creation": usage.get("cache_creation_input_tokens") or 0,
                    "messages": len(context.messages),
                    "tool_calls": len(context.tool_calls) if context.tool_calls else 0,
                },
            )
        except Exception as exc:
            logger.debug("MemoryHook.after_iteration logging failed: {}", exc)
