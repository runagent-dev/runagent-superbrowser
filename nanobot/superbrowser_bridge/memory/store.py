"""Filesystem-backed persistence for the memory subsystem.

Two concerns live here:

- EventLog       - append-only observability sink (token usage,
                   evictions, compactions). One file: events.jsonl.
- LedgerStore    - persistence for the structured Ledger. Four files:
                   ledger.json (snapshot), steps.jsonl (append),
                   episodic.jsonl (append), facts.jsonl (audit).

ledger.json is authoritative state. The JSONL files are append-only
history - they're consumed by recall (step 9) and by the back-compat
export to ``step_history.md`` / ``step_history.json``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from .ledger import Ledger, StepOutcome


_DEFAULT_BASE_DIR = "/tmp/superbrowser"


def memory_dir(task_id: str, *, base_dir: str = _DEFAULT_BASE_DIR) -> Path:
    """Return the per-task memory directory, creating it on demand.

    Empty task_id falls back to a shared "default" subtree so early
    iterations before task_id is set still produce a usable log.
    """
    name = task_id.strip() if task_id else "default"
    path = Path(base_dir) / name / "memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


class EventLog:
    """Append-only JSONL sink for observability events.

    Writes are best-effort: a failure to log MUST NOT crash the agent
    loop. All exceptions are logged and swallowed.
    """

    __slots__ = ("_path",)

    def __init__(self, task_id: str, *, base_dir: str = _DEFAULT_BASE_DIR) -> None:
        self._path = memory_dir(task_id, base_dir=base_dir) / "events.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    def log(self, event_type: str, payload: dict[str, Any]) -> None:
        record = {
            "ts": time.time(),
            "type": event_type,
            **payload,
        }
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            logger.debug("EventLog write failed at {}: {}", self._path, exc)


def count_image_blocks(messages: list[dict[str, Any]]) -> int:
    """Count message content blocks that carry image data.

    Used by the observability stub to track per-iteration image
    pressure - the dominant single cost in vision-heavy tasks. Counts
    both Anthropic-style ``image`` blocks and OpenAI-style
    ``image_url`` blocks across all messages.
    """
    n = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "image" or btype == "image_url":
                n += 1
    return n


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON record as a line. Best-effort: log and continue on error."""
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        logger.debug("JSONL append failed at {}: {}", path, exc)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of dicts. Skips corrupt lines."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Skipping corrupt JSONL line in {}", path)
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError as exc:
        logger.debug("JSONL read failed at {}: {}", path, exc)
    return out


class LedgerStore:
    """Filesystem-backed persistence for the structured Ledger.

    Layout under ``/tmp/superbrowser/{task_id}/memory/``:

    - ``ledger.json``     full snapshot, rewritten on every ``save()``.
    - ``steps.jsonl``     append-only, one StepOutcome per line.
    - ``episodic.jsonl``  append-only, subgoal summaries and notes.
    - ``facts.jsonl``     append-only, audit of remember/forget actions.

    ``load()`` reads only ``ledger.json``. The JSONL files exist for
    recall (step 9) and post-mortem analysis - they are not replayed
    into the Ledger on load.
    """

    __slots__ = ("_dir", "_ledger_json", "_steps", "_episodic", "_facts", "_task_id")

    def __init__(self, task_id: str, *, base_dir: str = _DEFAULT_BASE_DIR) -> None:
        self._task_id = task_id or "default"
        self._dir = memory_dir(task_id, base_dir=base_dir)
        self._ledger_json = self._dir / "ledger.json"
        self._steps = self._dir / "steps.jsonl"
        self._episodic = self._dir / "episodic.jsonl"
        self._facts = self._dir / "facts.jsonl"

    @property
    def dir(self) -> Path:
        return self._dir

    @property
    def task_id(self) -> str:
        return self._task_id

    # ----- snapshot -----

    def load(self) -> "Ledger | None":
        """Load the persisted Ledger snapshot, or None if no file exists.

        Returns None for both "no prior task" and "corrupt snapshot".
        After loading the snapshot the full step list is rehydrated
        from steps.jsonl so callers that read ``ledger.all_steps``
        see the same data they had before the restart.
        """
        from .ledger import Ledger, StepOutcome

        if not self._ledger_json.exists():
            return None
        try:
            with self._ledger_json.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Ledger load failed at {}: {}", self._ledger_json, exc)
            return None
        try:
            ledger = Ledger.from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("Ledger.from_dict failed: {}", exc)
            return None
        # Rehydrate all_steps from the JSONL ground truth.
        try:
            for record in _read_jsonl(self._steps):
                ledger.all_steps.append(StepOutcome.from_dict(record))
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("all_steps rehydration failed: {}", exc)
        return ledger

    def save(self, ledger: "Ledger") -> None:
        """Rewrite ledger.json atomically with the current Ledger state."""
        try:
            tmp = self._ledger_json.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(
                    ledger.to_dict(), f, ensure_ascii=False, indent=2, default=str
                )
            os.replace(tmp, self._ledger_json)
        except OSError as exc:
            logger.debug("Ledger save failed at {}: {}", self._ledger_json, exc)

    # ----- append-only logs -----

    def append_step(self, outcome: "StepOutcome") -> None:
        _append_jsonl(self._steps, outcome.to_dict())

    def append_episode(self, text: str, *, kind: str = "note") -> None:
        if not text:
            return
        _append_jsonl(
            self._episodic,
            {"ts": time.time(), "kind": kind, "text": text},
        )

    def append_fact_action(
        self, key: str, value: str | None, action: str
    ) -> None:
        _append_jsonl(
            self._facts,
            {"ts": time.time(), "action": action, "key": key, "value": value},
        )

    def read_steps(self) -> list[dict[str, Any]]:
        return _read_jsonl(self._steps)

    def read_episodes(self) -> list[dict[str, Any]]:
        return _read_jsonl(self._episodic)

    def read_facts_log(self) -> list[dict[str, Any]]:
        return _read_jsonl(self._facts)

    # ----- back-compat export -----

    def export_step_history(self, ledger: "Ledger") -> tuple[Path, Path]:
        """Write step_history.md and step_history.json next to memory/.

        Mirrors the artifact shape state.export_step_history used to
        produce, so external consumers (orchestrator learnings tools,
        post-mortem scripts) keep working through the migration.
        Returns (md_path, json_path).
        """
        task_dir = self._dir.parent  # /tmp/superbrowser/{task_id}/
        md_path = task_dir / "step_history.md"
        json_path = task_dir / "step_history.json"
        steps = self.read_steps()

        try:
            with md_path.open("w", encoding="utf-8") as f:
                f.write(f"# Step History for task {self._task_id}\n\n")
                if ledger.goal:
                    f.write(f"**Goal:** {ledger.goal}\n\n")
                if ledger.checkpoints:
                    f.write("## Checkpoints\n\n")
                    for c in ledger.checkpoints:
                        title = f' "{c.title}"' if c.title else ""
                        f.write(f"- {c.url}{title}\n")
                    f.write("\n")
                f.write("## Steps\n\n")
                for i, s in enumerate(steps, start=1):
                    tool = s.get("tool", "?")
                    args = s.get("args", "")
                    result = s.get("result", "")
                    url = s.get("url", "")
                    success = "OK" if s.get("success", True) else "FAIL"
                    f.write(f"{i}. [{success}] **{tool}**({args}) at {url}\n")
                    if result:
                        f.write(f"   {result}\n")
        except OSError as exc:
            logger.debug("step_history.md export failed: {}", exc)

        try:
            with json_path.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "task_id": self._task_id,
                        "goal": ledger.goal,
                        "plan": ledger.plan,
                        "subgoal": ledger.subgoal,
                        "checkpoints": [c.to_dict() for c in ledger.checkpoints],
                        "best_checkpoint": (
                            ledger.best_checkpoint.to_dict()
                            if ledger.best_checkpoint
                            else None
                        ),
                        "facts": {k: v.to_dict() for k, v in ledger.facts.items()},
                        "dead_ends": [d.to_dict() for d in ledger.dead_ends],
                        "steps": steps,
                        "episodic": ledger.episodic,
                        "step_count": ledger.step_count,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
        except OSError as exc:
            logger.debug("step_history.json export failed: {}", exc)

        return md_path, json_path
