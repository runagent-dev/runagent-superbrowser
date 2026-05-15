"""Memory facade - the public surface of the memory subsystem.

`Memory(task_id, session_key=..., role=...)` is what external code
(run.py, delegation.py, worker tools) holds onto. Internally it
composes:

- Ledger        - in-memory dataclasses (semantic state)
- LedgerStore   - JSONL persistence under /tmp/superbrowser/{task_id}/memory/
- MemoryHook    - nanobot AgentHook bridge
- EventLog      - append-only observability sink

Roles:
- "orchestrator" - long-lived session, full ledger view, recall tools
- "worker"       - short-lived per-subgoal session, sliced ledger view,
                   no recall tools (returns role-error if called)

Both roles share the same on-disk store under the task_id directory.
The orchestrator and worker Memory instances coordinate through the
JSONL files; last-writer-wins is fine because worker writes steps
and orchestrator writes plan/facts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from .compaction import SubgoalCompactor
from .hook import MemoryHook
from .ledger import Checkpoint, DeadEnd, Fact, Ledger, StepOutcome
from .store import EventLog, LedgerStore

if TYPE_CHECKING:
    from nanobot import Nanobot


Role = Literal["orchestrator", "worker"]


class Memory:
    """The "what does the agent know" layer.

    Lifecycle:
        memory = Memory(task_id, session_key=..., role=...)
        memory_hook = memory.attach(bot)
        await bot.run(task, session_key=..., hooks=[memory_hook, ...])

    For the worker-side, hooks=[memory.attach(worker_bot), worker_hook].
    MemoryHook runs before BrowserWorkerHook so context.messages is
    already cleaned up by the time worker_hook reads it.
    """

    def __init__(
        self,
        task_id: str,
        *,
        session_key: str,
        role: Role = "orchestrator",
    ) -> None:
        self.task_id = task_id
        self.session_key = session_key
        self.role: Role = role
        self.events = EventLog(task_id)
        self.store = LedgerStore(task_id)
        # Hydrate from disk if a ledger.json exists for this task_id;
        # this is what makes resumption work across process restarts.
        loaded = self.store.load()
        self.ledger: Ledger = loaded if loaded is not None else Ledger()
        # Bot reference and subgoal compactor are wired in ``attach``.
        self._bot: Any | None = None
        self._compactor: SubgoalCompactor | None = None

    # ----- nanobot integration -----

    def attach(self, bot: "Nanobot") -> MemoryHook:
        """Wire Memory into a nanobot instance.

        Side effects:
        - When ``role == "orchestrator"``, registers the four recall
          tools (memory_recall / _remember / _note / _forget) on
          ``bot._loop.tools``. Workers never get these tools - their
          context stays focused on the current subgoal and they
          escalate to the orchestrator if they need older context.
        - Logs the attach event to ``events.jsonl``.

        Returns the MemoryHook the caller must pass to
        ``bot.run(hooks=[...])`` - composes with BrowserWorkerHook.
        """
        if self.role == "orchestrator" and bot is not None:
            from .tools import recall_tool_classes

            for cls in recall_tool_classes():
                try:
                    bot._loop.tools.register(cls(self))
                except Exception:  # pragma: no cover - best effort
                    pass

        if bot is not None:
            self._bot = bot
            self._compactor = SubgoalCompactor(self, bot)

        self.events.log(
            "memory_attach",
            {
                "role": self.role,
                "session_key": self.session_key,
                "resumed": self.ledger.step_count > 0,
            },
        )
        return MemoryHook(self)

    # ----- plan / structure -----

    def set_goal(self, goal: str) -> None:
        self.ledger.goal = goal
        self._persist()

    def set_plan(self, subgoals: list[str]) -> None:
        self.ledger.plan = list(subgoals)
        self._persist()

    def begin_subgoal(self, subgoal: str, *, message_floor: int = -1) -> None:
        """Mark the start of a new subgoal.

        ``message_floor`` records the index in ``context.messages`` at
        which this subgoal's iterations begin. The subgoal compactor
        (step 10) uses it to slice messages for archival.
        """
        self.ledger.subgoal = subgoal
        if message_floor >= 0:
            self.ledger.subgoal_message_floor = message_floor
        self._persist()
        self.events.log(
            "subgoal_begin",
            {"subgoal": subgoal, "message_floor": message_floor},
        )

    def end_subgoal(self, *, success: bool, summary: str | None = None) -> None:
        """Mark the current subgoal complete (basic mutation only).

        Records a summary line in episodic, clears the active subgoal,
        and persists. For full compaction (LLM-driven message-slice
        summarization via nanobot's Consolidator), use
        ``compact_subgoal`` which additionally archives the bounded
        message slice.
        """
        if summary:
            line = f"[subgoal={self.ledger.subgoal} success={success}] {summary}"
            self.ledger.add_episode(line)
            self.store.append_episode(line, kind="subgoal_summary")
        self.events.log(
            "subgoal_end",
            {"subgoal": self.ledger.subgoal, "success": success},
        )
        self.ledger.subgoal = ""
        self.ledger.subgoal_message_floor = -1
        self._persist()

    async def compact_subgoal(
        self,
        messages: list[dict[str, Any]],
        *,
        success: bool,
        summary_hint: str | None = None,
        clear_subgoal: bool = True,
    ) -> str | None:
        """Archive the subgoal-scoped message slice and clear subgoal state.

        Hands the slice to ``SubgoalCompactor`` which invokes nanobot's
        Consolidator. The returned archive summary (or ``summary_hint``
        if archival was unavailable) is appended to episodic memory.

        Pass ``messages`` as
        ``context.messages[ledger.subgoal_message_floor : len(...)]``
        from whichever hook or tool is in a position to slice. Set
        ``clear_subgoal=False`` to compact without ending the subgoal
        (rare - used when consolidating mid-subgoal to free context).
        """
        archived: str | None = None
        if self._compactor is not None:
            archived = await self._compactor.compact(
                messages,
                success=success,
                summary_hint=summary_hint,
            )
        elif summary_hint:
            # No compactor (Memory attached without a bot reference).
            # Still capture the hint so end_subgoal-equivalent semantics
            # apply.
            line = (
                f"[subgoal={self.ledger.subgoal or '(unknown)'} "
                f"success={success}] {summary_hint}"
            )
            self.ledger.add_episode(line)
            self.store.append_episode(line, kind="subgoal_summary")

        if clear_subgoal:
            self.ledger.subgoal = ""
            self.ledger.subgoal_message_floor = -1
            self._persist()
        return archived

    # ----- facts / events -----

    def remember(
        self,
        key: str,
        value: str,
        *,
        source_step: int = -1,
        confidence: float = 1.0,
        subgoal: str | None = None,
    ) -> None:
        if source_step < 0:
            source_step = self.ledger.step_count
        if subgoal is None:
            subgoal = self.ledger.subgoal or None
        fact = Fact(
            key=key,
            value=value,
            source_step=source_step,
            confidence=confidence,
            subgoal=subgoal,
        )
        self.ledger.add_fact(fact)
        self.store.append_fact_action(key, value, "remember")
        self._persist()

    def forget(self, key: str) -> bool:
        removed = self.ledger.remove_fact(key)
        if removed:
            self.store.append_fact_action(key, None, "forget")
            self._persist()
        return removed

    def note(self, text: str) -> None:
        if not text:
            return
        self.ledger.add_episode(text)
        self.store.append_episode(text, kind="note")
        self._persist()

    def log_activity(self, entry: str) -> None:
        """Append a bounded HH:MM:SS audit entry.

        Migrates ``BrowserSessionState.log_activity`` - the bounded
        debug trail (max 30 entries) the worker uses for
        get_activity_summary. Stays in the ledger for cross-restart
        availability.
        """
        if not entry:
            return
        self.ledger.add_activity(entry)
        self._persist()

    def update_current_url(self, url: str) -> None:
        """Mirror navigation state into the ledger.

        Navigation mechanics (regression detection, visit counts) still
        live in BrowserSessionState. We mirror the URL here so the
        rendered ledger and resumption flows can show "where are we
        right now" without reaching back into state.py.
        """
        if url and url != self.ledger.current_url:
            self.ledger.current_url = url
            self._persist()

    def mark_dead_end(
        self, description: str, *, subgoal: str | None = None
    ) -> None:
        if not description:
            return
        if subgoal is None:
            subgoal = self.ledger.subgoal or None
        dead_end = DeadEnd(description=description, subgoal=subgoal)
        self.ledger.add_dead_end(dead_end)
        self._persist()

    def checkpoint(
        self,
        url: str,
        *,
        title: str = "",
        action: str = "",
        subgoal: str | None = None,
    ) -> None:
        if not url:
            return
        if subgoal is None:
            subgoal = self.ledger.subgoal or None
        cp = Checkpoint(url=url, title=title, action=action, subgoal=subgoal)
        self.ledger.add_checkpoint(cp)
        self._persist()

    def record_step(
        self,
        tool: str,
        args: str,
        result: str,
        *,
        success: bool = True,
        url: str = "",
        iteration: int = -1,
    ) -> None:
        outcome = StepOutcome(
            tool=tool,
            args=args,
            result=result,
            url=url,
            success=success,
            subgoal=self.ledger.subgoal or None,
            iteration=iteration,
        )
        self.ledger.append_step(outcome)
        self.store.append_step(outcome)
        # Mutating recent doesn't change anything else in the snapshot
        # we care about, but we still rewrite ledger.json so the
        # bounded recent[] stays in sync after restart.
        self._persist()

    # ----- consumption -----

    def render_for_llm(self, *, role: Role | None = None) -> str:
        return self.ledger.render(role or self.role)

    def recall(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Grep-based lookup across facts, episodes, and steps.

        Scoring is deliberately simple: fact > episodic > step,
        substring match (case-insensitive). The orchestrator reaches
        for this when it needs a piece of context the rendered ledger
        doesn't expose. No embeddings; if the in-memory dict of facts
        and on-disk JSONL of episodes/steps get large enough to make
        grep slow, we'll add an index then.
        """
        if not query:
            return []
        needle = query.casefold()
        scored: list[tuple[float, dict[str, Any]]] = []

        # Score 3.0: facts (high signal - structured kv data).
        for fact in self.ledger.facts.values():
            haystack = f"{fact.key} {fact.value}".casefold()
            if needle in haystack:
                scored.append(
                    (
                        3.0,
                        {
                            "source": "fact",
                            "key": fact.key,
                            "text": f"{fact.key} = {fact.value}",
                            "subgoal": fact.subgoal,
                        },
                    )
                )

        # Score 2.0: episodic memory (subgoal summaries + free notes).
        for ep in self.store.read_episodes():
            text = (ep.get("text") or "")
            if needle in text.casefold():
                scored.append(
                    (
                        2.0,
                        {
                            "source": "episodic",
                            "kind": ep.get("kind", "note"),
                            "text": text[:240],
                        },
                    )
                )

        # Score 1.0: full step history.
        for step in self.store.read_steps():
            haystack = (
                f"{step.get('tool', '')} {step.get('args', '')} "
                f"{step.get('result', '')} {step.get('url', '')}"
            ).casefold()
            if needle in haystack:
                tool = step.get("tool", "?")
                args = step.get("args", "")
                result = (step.get("result") or "")[:120]
                scored.append(
                    (
                        1.0,
                        {
                            "source": "step",
                            "iteration": step.get("iteration", -1),
                            "text": f"{tool}({args}) -> {result}",
                            "url": step.get("url", ""),
                        },
                    )
                )

        scored.sort(key=lambda x: -x[0])
        self.events.log(
            "recall",
            {"query": query, "limit": limit, "hits": len(scored)},
        )
        return [m[1] for m in scored[:limit]]

    # ----- back-compat export -----

    def export_step_history(self) -> None:
        """Write the existing step_history.md / step_history.json artifacts.

        Called at task-end by the orchestrator so learnings_tools and
        other downstream consumers keep working through the cutover
        in step 8.
        """
        self.store.export_step_history(self.ledger)

    # ----- internals -----

    def _persist(self) -> None:
        self.store.save(self.ledger)
