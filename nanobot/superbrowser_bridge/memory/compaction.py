"""Subgoal-boundary compaction.

When a subgoal completes, the messages produced during it are
redundant with the ledger entries the worker accumulated (facts,
checkpoints, recent outcomes, dead-ends). Instead of letting them sit
in context, we hand the slice to nanobot's ``Consolidator.archive``
which produces a short LLM-generated summary and appends it to
``history.jsonl``. The summary is also folded into the ledger's
``episodic`` list so it's grep-able by ``memory_recall``.

This module is the bridge to nanobot's existing consolidator - no
new summarization logic; we just call into the right primitive at the
right semantic boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from .memory import Memory


class SubgoalCompactor:
    """Run Consolidator.archive at subgoal boundaries.

    Constructed by ``Memory.attach`` once a bot reference is available.
    The orchestrator calls ``Memory.compact_subgoal(messages)`` (or
    ``end_subgoal`` for the basic-mutation-only path) and the
    compactor handles the message-slice -> archive -> ledger flow.
    """

    __slots__ = ("memory", "_bot")

    def __init__(self, memory: "Memory", bot: Any) -> None:
        self.memory = memory
        self._bot = bot

    def _consolidator(self) -> Any | None:
        # Reach through nanobot's loop. Best-effort: if the attribute
        # path changes in a future nanobot release, return None and let
        # the caller continue with the basic-mutation path.
        try:
            loop = getattr(self._bot, "_loop", None)
            if loop is None:
                return None
            return getattr(loop, "auto_compact", None) or getattr(
                loop, "consolidator", None
            )
        except Exception:  # pragma: no cover - defensive
            return None

    async def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        success: bool,
        summary_hint: str | None = None,
    ) -> str | None:
        """Archive ``messages`` and fold the LLM summary into episodic.

        ``messages`` should be the subgoal-scoped slice
        ``context.messages[subgoal_message_floor:current]``. The
        Consolidator's archive method returns either the summary
        string or ``None``; on success we append a tagged episodic
        entry so ``memory_recall`` can find it.
        """
        subgoal_name = self.memory.ledger.subgoal or "(unknown)"
        consolidator = self._consolidator()
        archived: str | None = None

        if consolidator is not None and messages:
            try:
                archive_fn = getattr(consolidator, "archive", None)
                if archive_fn is not None:
                    archived = await archive_fn(messages)
            except Exception as exc:
                logger.debug("SubgoalCompactor.archive failed: {}", exc)

        body = archived or summary_hint or "(no LLM summary)"
        line = f"[subgoal={subgoal_name} success={success}] {body}"
        self.memory.ledger.add_episode(line)
        self.memory.store.append_episode(line, kind="subgoal_summary")
        self.memory.events.log(
            "subgoal_compacted",
            {
                "subgoal": subgoal_name,
                "messages": len(messages),
                "archived": bool(archived),
                "success": success,
            },
        )
        return archived
