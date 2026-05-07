"""Learnings read/write tools — `CheckLearningsTool` + `SaveLearningTool`.

Both are read-only with respect to live state — they touch the per-domain
markdown learnings file under `LEARNINGS_DIR`. CheckLearningsTool is
gated by `learning_reads_enabled()`; SaveLearningTool always writes.
"""

from __future__ import annotations

import os
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)

from superbrowser_bridge.routing import (
    _domain_from_url,
    _learnings_path,
    _preferred_approach,
    learning_reads_enabled,
)


@tool_parameters(
    tool_parameters_schema(
        site=StringSchema("Site domain or URL to check learnings for (e.g., 'gozayaan.com')"),
        required=["site"],
    )
)
class CheckLearningsTool(Tool):
    """Check what we've learned about a site from past tasks."""

    name = "check_learnings"
    description = (
        "Learning reads are disabled in this deployment "
        "(LEARNING_READS_ENABLED=0). Skip calling this; proceed straight "
        "to delegate_browser_task or delegate_search_task."
        if not learning_reads_enabled()
        else (
            "Read past learnings for a website. Returns what worked and what failed. "
            "ALWAYS call this before delegate_browser_task to avoid repeating mistakes."
        )
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, site: str, **kw: Any) -> str:
        if not learning_reads_enabled():
            return (
                f"Learning reads disabled (LEARNING_READS_ENABLED=0). "
                f"No prior history consulted for {site}; proceed with the task."
            )
        domain = _domain_from_url(site)
        path = _learnings_path(domain)

        # Always surface the routing preference if we have one — even if
        # no markdown learnings exist yet, past search/browser outcomes
        # are useful for the orchestrator's next decision.
        sections: list[str] = []
        pref = _preferred_approach(domain)
        if pref:
            sections.append(
                f"## Routing preference for {domain}\n"
                f"- Preferred: **{pref['approach']}** (confidence {pref['confidence']:.2f})\n"
                f"- Reason: {pref['reason']}\n"
                f"- Action: call `delegate_{pref['approach']}_task` first. "
                f"The classifier will also enforce this automatically."
            )

        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    md = f.read().strip()
                if md:
                    sections.append(f"## Task learnings for {domain}\n{md}")
            except OSError:
                pass

        if not sections:
            return f"No learnings or routing history found for {domain}. This is the first task on this site."

        # Preface: past learnings are PATTERNS, not verbatim answers.
        # Without this guidance, the LLM copies concrete values (e.g.
        # `makes[]=bmw` from a BMW task) into an unrelated follow-up
        # query (e.g. a Mercedes task on the same site).
        preamble = (
            "## How to use these learnings\n"
            "These are PATTERNS learned from PAST tasks on this site. "
            "They are NOT the answer to the current task. When you see "
            "concrete values in a URL / selector / script (e.g. a "
            "specific brand, year range, price, ID), treat them as "
            "placeholders you MUST replace with values from the CURRENT "
            "user task. If the current task is about a DIFFERENT entity "
            "than a past learning mentions, the URL pattern still "
            "applies — just swap the query params. Do NOT echo concrete "
            "values from these learnings back to the user as if they "
            "were the current task's answer."
        )
        return "\n\n".join([preamble, *sections])


@tool_parameters(
    tool_parameters_schema(
        site=StringSchema("Site domain or URL"),
        problem=StringSchema(
            "One-sentence description of the problem this learning addresses "
            "(e.g. 'Trade-in form has 8 cascading dropdowns that hallucinate "
            "when click-looped'). Required for structured saves.",
            nullable=True,
        ),
        root_cause=StringSchema(
            "Why the naive approach failed (e.g. 'DOM indices renumber on "
            "every pick; vision V-indices stale at 10s'). Required for structured saves.",
            nullable=True,
        ),
        working_recipe=ArraySchema(
            description=(
                "Ordered, executable steps a future worker can follow. "
                "Use placeholders for task-specific values: <brand>, <year>, "
                "etc. At least 2 steps required for structured saves."
            ),
            items=StringSchema(""),
            nullable=True,
        ),
        anti_patterns=ArraySchema(
            description="Optional. Things NOT to do. Each entry one sentence.",
            items=StringSchema(""),
            nullable=True,
        ),
        confidence=NumberSchema(
            description=(
                "0..1. How confident this recipe will work next time. "
                "Structured saves require ≥0.5 — don't save guesses."
            ),
            nullable=True,
        ),
        learning=StringSchema(
            "DEPRECATED free-text path (legacy). Prefer the structured "
            "fields. If only `learning` is provided, the entry is saved "
            "but flagged as low-trust — future workers may skip it.",
            nullable=True,
        ),
        required=["site"],
    )
)
class SaveLearningTool(Tool):
    """Save a learning about a site for future tasks.

    Two modes:
      • STRUCTURED (preferred) — provide problem/root_cause/working_recipe.
        Saved with stable markdown headings so future workers can grep
        deterministic anchors instead of parsing prose.
      • LEGACY free-text — accepted with a low-trust marker so vague
        "FAILED: dropdown reset" entries don't poison the next worker's
        prompt. New writes should always use the structured path.

    Reject criteria for structured saves: empty working_recipe, fewer
    than 2 recipe steps, or confidence < 0.5. The bar exists because
    a vague "save_learning" call costs a future worker context tokens
    and biases its plan — non-actionable learnings are net negative.
    """

    name = "save_learning"
    description = (
        "Save an ACTIONABLE, GENERALIZABLE learning for a site. Prefer "
        "the structured fields (problem, root_cause, working_recipe, "
        "anti_patterns, confidence) — those produce reusable recipes. "
        "Free-text `learning` is accepted but saved as low-trust. "
        "Use placeholders for task-specific values: <brand>, <year>, "
        "<min_price>. Structural bits (param names, selector paths, "
        "wait durations) stay concrete."
    )

    async def execute(
        self,
        site: str,
        problem: str | None = None,
        root_cause: str | None = None,
        working_recipe: list[str] | None = None,
        anti_patterns: list[str] | None = None,
        confidence: float | None = None,
        learning: str | None = None,
        **kw: Any,
    ) -> str:
        domain = _domain_from_url(site)
        path = _learnings_path(domain)

        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Branch: structured if any of problem/root_cause/working_recipe set.
        structured = any(x for x in (problem, root_cause, working_recipe))

        if structured:
            recipe = [str(s).strip() for s in (working_recipe or []) if str(s).strip()]
            if len(recipe) < 2:
                return (
                    "[save_learning_rejected] Structured save needs "
                    "working_recipe with ≥2 ordered steps. Provide concrete "
                    "actions a future worker can execute (e.g. "
                    "'browser_navigate(<base_url>/trade-in)', "
                    "'browser_form_plan(fields=[{label:Brand,value:<brand>}, ...])')."
                )
            try:
                conf = float(confidence) if confidence is not None else 0.5
            except (TypeError, ValueError):
                conf = 0.5
            if conf < 0.5:
                return (
                    f"[save_learning_rejected] confidence={conf:.2f} is below "
                    "the 0.5 threshold. Don't save guesses — verify the "
                    "recipe end-to-end first, or save as anti_patterns instead."
                )

            lines: list[str] = [f"\n### {timestamp}  (confidence={conf:.2f})"]
            if problem:
                lines.append(f"## problem\n{problem.strip()}")
            if root_cause:
                lines.append(f"## root_cause\n{root_cause.strip()}")
            lines.append("## working_recipe")
            lines.extend(f"  {i+1}. {step}" for i, step in enumerate(recipe))
            if anti_patterns:
                lines.append("## anti_patterns")
                lines.extend(f"  - {str(s).strip()}" for s in anti_patterns if str(s).strip())
            entry = "\n".join(lines) + "\n"

            with open(path, "a") as f:
                f.write(entry)
            return f"Learning saved (structured, {len(recipe)} steps) for {domain}."

        # Legacy free-text path. Mark as low-trust so future workers can
        # discount vague entries when they read the file back.
        if not learning or not learning.strip():
            return (
                "[save_learning_rejected] Empty learning. Provide either "
                "structured fields (problem/root_cause/working_recipe) or "
                "a non-empty `learning` string."
            )
        entry = f"\n### {timestamp}  [legacy:low-trust]\n{learning.strip()}\n"
        with open(path, "a") as f:
            f.write(entry)
        return f"Learning saved (legacy free-text, low-trust) for {domain}."
