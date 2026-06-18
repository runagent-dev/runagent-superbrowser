"""The "inherent prompting" layer.

The heavy lifting — persona, routing rubric, anti-fabrication rules, the
browser tool ladder — lives in the bundled ``SOUL.md`` prompts. This module is
deliberately *thin*: it wraps the user's terse goal in a small, consistent
envelope (mode directive + goal + optional URL + optional output schema) so a
caller can write ``"cheapest flight DAC→BKK Apr 30 return May 5"`` without any
"please use the browser to…" scaffolding. Duplicating SOUL content here would
fight the system prompt, so we don't.

Also holds the structured-output helpers: render a JSON Schema into the prompt,
and best-effort parse the model's answer back out.
"""

from __future__ import annotations

import json
from typing import Any


def _schema_json(output_schema: Any) -> str:
    """Render ``output_schema`` to a compact JSON Schema string for the prompt.

    Accepts a JSON Schema dict (passed through), a pydantic model class, or any
    type hint (e.g. ``list[Hotel]``, ``dict[str, int]``) via pydantic's
    ``TypeAdapter``.
    """
    if isinstance(output_schema, dict):
        return json.dumps(output_schema, indent=2)
    try:
        from pydantic import TypeAdapter

        return json.dumps(TypeAdapter(output_schema).json_schema(), indent=2)
    except Exception:  # noqa: BLE001 - unschematizable hint; fall back to a label
        return str(output_schema)


def frame_task(
    task: str,
    *,
    mode_directive: str = "",
    url: str | None = None,
    output_schema: Any | None = None,
    force_browser: bool = False,
) -> str:
    """Compose the final orchestrator prompt from a terse goal + options."""
    parts: list[str] = []
    if mode_directive:
        parts.append(mode_directive)
    parts.append(f"Goal: {task.strip()}")
    if url:
        parts.append(f"Target URL: {url}")
    if force_browser and "force=True" not in mode_directive:
        parts.append(
            "If you delegate to the browser, pass force=True to delegate_browser_task."
        )
    if output_schema is not None:
        parts.append(
            "Output format: once you have the answer, end your reply with a single "
            "JSON value (object or array) matching this JSON Schema, in a ```json "
            "code block. Put any prose before it.\n" + _schema_json(output_schema)
        )
    return "\n\n".join(parts)


def _extract_json(text: str) -> str | None:
    """Pull the most likely JSON value out of a free-form answer.

    Prefers a fenced ```json block; otherwise falls back to the last balanced
    ``{...}`` / ``[...]`` span. Best-effort — returns ``None`` if nothing looks
    like JSON.
    """
    if not text:
        return None
    # 1) fenced ```json ... ``` (last one wins — the answer usually trails)
    fence = "```"
    lowered = text.lower()
    idx = lowered.rfind("```json")
    if idx != -1:
        start = text.find("\n", idx)
        if start != -1:
            end = text.find(fence, start + 1)
            if end != -1:
                return text[start + 1 : end].strip()
    # 2) last balanced object/array span
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        end = text.rfind(close_ch)
        start = text.find(open_ch)
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1].strip()
            try:
                json.loads(candidate)
                return candidate
            except Exception:  # noqa: BLE001
                continue
    return None


def parse_output(text: str, output_schema: Any | None) -> Any | None:
    """Best-effort parse of the answer into ``output_schema``. Never raises.

    For a JSON Schema dict, returns the raw parsed JSON. For a pydantic model
    or any type hint (``list[Hotel]`` etc.), validates/coerces via
    ``TypeAdapter`` and returns model instances — falling back to the raw
    parsed JSON if validation fails.
    """
    if output_schema is None or not text:
        return None
    raw = _extract_json(text)
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(output_schema, dict):
        return obj
    try:
        from pydantic import TypeAdapter

        return TypeAdapter(output_schema).validate_python(obj)
    except Exception:  # noqa: BLE001 - return the raw JSON if it won't validate
        return obj
