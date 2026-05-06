"""Planner agent — supervises the worker by decomposing tasks and
re-planning on stalls.

Phase 6 of the v3 refactor. The Planner is a third role between the
Orchestrator and the Worker:

  * Orchestrator → Planner (task start)
      Planner emits a 2-6 step ``TaskBrief`` from the raw query.
  * Worker hits a stall (no checklist progress for N turns)
      → Worker hook calls ``Planner.replan(...)``
      → Planner returns a revised brief or "abandon" verdict
      → Worker hook installs the revised brief and injects a
        ``[PLANNER_GUIDANCE]`` block on the next tool result.

Design constraints:

  * **Stateless across calls.** Each call rebuilds the prompt from
    inputs. No conversation history, so the Planner can't bloat its
    own context.
  * **Text-only.** No screenshots — only summary text from the vision
    pipeline. Forces a clean handoff contract and keeps cost down.
  * **Cheap model.** Defaults to Gemini Flash. Override via
    ``PLANNER_MODEL`` / ``PLANNER_API_KEY``.
  * **Best-effort.** If the API call fails or the response doesn't
    parse, returns an empty list — the caller (orchestrator) falls
    back to ``heuristic_decompose``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Optional


def _log(msg: str) -> None:
    sys.stderr.write(f"[planner] {msg}\n")


_SYSTEM_PROMPT = """You decompose a user's browser-automation task into \
2 to 6 verifiable, ordered steps. Each step must be:

  - small enough to be checked from page state alone (URL, visible \
text, presence of a button)
  - phrased as the *outcome* the worker should achieve, not as a \
specific click. The worker is a separate agent that picks the actual \
clicks/inputs from a vision-bbox list. You are NOT a click-emitter.
  - independent of preceding steps' implementation details (don't \
write "click the second link" — write "open the search results page \
for X").

Output strict JSON of the form:

  {
    "steps": [
      {"label": "open google.com",          "kind": "navigation"},
      {"label": "search for headphones",   "kind": "action"},
      {"label": "filter to under $50",      "kind": "filter"},
      {"label": "sort by rating",           "kind": "filter"},
      {"label": "extract top 3 listings",   "kind": "extraction"}
    ]
  }

Rules:
  * Allowed `kind` values: filter, action, extraction, navigation, \
verification.
  * 2 to 6 steps. Refuse to emit more — long lists indicate the task \
should be sub-decomposed at a higher level, not crammed into one \
plan.
  * Use plain English in labels; the worker shows them to a vision \
agent that picks bboxes by label match.
  * Don't include implementation hints ("via API", "with JS"). The \
worker decides how.
  * Don't restate the same step twice. If the user repeats an idea, \
collapse it.
  * No JSON outside the top-level object — no commentary, no fence."""


_REPLAN_SYSTEM_PROMPT = _SYSTEM_PROMPT + """

You are now in REPLAN mode. The worker stalled. Read:
  - the user's original task
  - the prior plan + which steps are done / open
  - the last few worker actions and their results
  - a short summary of what the page looks like right now

Then emit a REVISED plan that takes into account where the worker is \
stuck. Drop steps that are no longer reachable. Add steps that the \
worker missed. Reorder if necessary. Honour the same output schema.

If the task is genuinely unreachable from the current page, return:

  {"abandon": true, "reason": "<one short sentence>"}"""


@dataclass
class PlannerResult:
    """Outcome of a Planner call.

    * ``steps`` populated → install as new TaskBrief.
    * ``abandon=True`` → surface to the user; worker stops.
    * Empty + ``abandon=False`` → caller falls back (orchestrator uses
      ``heuristic_decompose``; replan caller leaves brief unchanged).
    """

    steps: list[dict]
    abandon: bool = False
    reason: str = ""
    raw: str = ""  # for telemetry / debugging


class PlannerAgent:
    """Stateless task decomposer + re-planner.

    Construct once per process; the underlying SDK client handles
    pooling. Calls are short, so concurrent requests are safe.
    """

    _DEFAULT_MODEL = "gemini-2.5-flash"
    _DEFAULT_BASE_URL = (
        "https://generativelanguage.googleapis.com/v1beta/openai"
    )
    _DEFAULT_TIMEOUT_S = 12.0

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self.model = (
            model
            or os.environ.get("PLANNER_MODEL")
            or os.environ.get("VISION_MODEL")
            or self._DEFAULT_MODEL
        )
        self.api_key = (
            api_key
            or os.environ.get("PLANNER_API_KEY")
            or os.environ.get("VISION_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        self.base_url = (
            base_url
            or os.environ.get("PLANNER_BASE_URL")
            or self._DEFAULT_BASE_URL
        )
        self.timeout_s = float(
            timeout_s
            or os.environ.get("PLANNER_TIMEOUT_S")
            or self._DEFAULT_TIMEOUT_S
        )
        self._client = None  # lazy

    @property
    def enabled(self) -> bool:
        """Returns True iff the planner has the credentials it needs.

        Callers (delegate_browser, worker_hook) check this and fall
        through to the heuristic / leave-brief-unchanged paths when
        the planner is not configured. Don't treat absence as an error
        — it's a valid mode for environments that don't want the extra
        LLM call."""
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover
                _log(f"openai SDK missing: {exc!r}")
                return None
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout_s,
            )
        return self._client

    async def decompose(self, task: str) -> PlannerResult:
        """Decompose a free-text task into a TaskBrief-compatible list.

        Returns ``PlannerResult.steps == []`` on any failure mode (no
        credentials, API error, malformed JSON, schema violation). The
        orchestrator should treat empty as "use heuristic instead".
        """
        if not self.enabled:
            return PlannerResult(steps=[], reason="planner_disabled")
        task_clean = (task or "").strip()
        if len(task_clean) < 8:
            # Not worth the LLM call — single-word "tasks" don't
            # benefit from decomposition.
            return PlannerResult(steps=[], reason="task_too_short")

        return await self._call(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=f"Task:\n{task_clean}\n\nReturn the JSON object now.",
        )

    async def replan(
        self,
        *,
        task: str,
        brief_render: str,
        recent_steps: list[dict],
        last_vision_summary: str,
        stall_reason: str = "no_progress",
    ) -> PlannerResult:
        """Produce a revised plan for a stalled worker.

        Args:
            task: original free-text task.
            brief_render: ``state.task_brief.render_for_prompt()`` —
                the [BRIEF] block the worker sees.
            recent_steps: last 5-10 entries from
                ``state.step_history`` (tool, args, result).
            last_vision_summary: ``vision_response.summary`` text from
                the most recent screenshot. NEVER pass an image.
            stall_reason: short label that goes into the prompt for
                debugging context (no_progress, cursor_failures,
                regression, etc.).
        """
        if not self.enabled:
            return PlannerResult(steps=[], reason="planner_disabled")
        steps_block = "\n".join(
            f"  - {s.get('tool', '?')}({(s.get('args') or '')[:60]}) → {(s.get('result') or '')[:80]}"
            for s in recent_steps[-10:]
        )
        user_prompt = (
            f"Task:\n{task.strip()}\n\n"
            f"Current brief:\n{brief_render.strip()}\n\n"
            f"Recent worker actions:\n{steps_block or '(none)'}\n\n"
            f"Page summary:\n{(last_vision_summary or '(no recent vision)').strip()[:500]}\n\n"
            f"Stall reason: {stall_reason}\n\n"
            "Return the revised JSON plan, or {\"abandon\": true, \"reason\": \"...\"} "
            "if the goal is unreachable."
        )
        return await self._call(
            system_prompt=_REPLAN_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

    async def _call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> PlannerResult:
        client = self._get_client()
        if client is None:
            return PlannerResult(steps=[], reason="no_client")
        try:
            completion = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=800,
            )
            raw = (completion.choices[0].message.content or "").strip()
        except Exception as exc:
            _log(f"call failed: {exc!r}")
            return PlannerResult(steps=[], reason=f"call_error: {exc!r}"[:120])
        return self._parse(raw)

    @staticmethod
    def _parse(raw: str) -> PlannerResult:
        if not raw:
            return PlannerResult(steps=[], reason="empty_response", raw=raw)
        # Some Gemini revs wrap the JSON in ```json fences even with
        # response_format=json_object. Strip those defensively.
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            _log(f"json parse failed: {exc!r}; raw={raw[:200]!r}")
            return PlannerResult(steps=[], reason="json_parse_error", raw=raw)
        if not isinstance(obj, dict):
            return PlannerResult(steps=[], reason="not_dict", raw=raw)
        if obj.get("abandon"):
            return PlannerResult(
                steps=[],
                abandon=True,
                reason=str(obj.get("reason") or "unreachable")[:200],
                raw=raw,
            )
        steps_in = obj.get("steps")
        if not isinstance(steps_in, list) or not steps_in:
            return PlannerResult(steps=[], reason="no_steps_field", raw=raw)
        steps_out: list[dict] = []
        valid_kinds = {"filter", "action", "extraction", "navigation", "verification"}
        for s in steps_in[:6]:
            if not isinstance(s, dict):
                continue
            label = str(s.get("label") or "").strip()
            if not label:
                continue
            kind = str(s.get("kind") or "filter").strip().lower()
            if kind not in valid_kinds:
                kind = "filter"
            steps_out.append({
                "label": label[:120],
                "kind": kind,
                # Predicates are manual — the brain marks via browser_brief_mark.
                # Future work: let Planner emit URL-pattern predicates so
                # filter steps auto-flip on navigation.
                "predicate": {"manual": True},
            })
        if len(steps_out) < 2:
            return PlannerResult(
                steps=[],
                reason=f"too_few_steps ({len(steps_out)})",
                raw=raw,
            )
        return PlannerResult(steps=steps_out, raw=raw)


# Process-wide singleton accessor — mirrors VisionAgent's pattern.
_INSTANCE: Optional[PlannerAgent] = None


def default_planner() -> PlannerAgent:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = PlannerAgent()
    return _INSTANCE
