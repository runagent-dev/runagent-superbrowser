"""On-demand interactive-element inspection.

After the _format_state rewrite, the canonical state block reports
only a count of interactive elements ("Elements: 47 interactive"). The
full list is no longer pinned into every tool result. When the worker
needs the actual elements - to decide what to click, what V_n maps to
what role - it calls browser_list_elements explicitly.

The element list comes from the same /state endpoint that already
populates state.element_fingerprints. Calling this tool is cheap
(no screenshot, no vision) and the cached fingerprint map is
refreshed as a side effect.
"""

from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)

from ..formatting import _fetch_elements
from ..state import BrowserSessionState


_DEFAULT_LIMIT = 80


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        filter=StringSchema(
            "Optional substring filter - only elements whose text "
            "contains this substring (case-insensitive) are returned.",
            nullable=True,
        ),
        limit=IntegerSchema(
            "Maximum number of elements to return. Default 80, max 500. "
            "Use a smaller value when you only need a quick scan.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserListElementsTool(Tool):
    name = "browser_list_elements"
    description = (
        "List interactive elements on the current page (buttons, links, "
        "inputs). Use this when the canonical state block reports "
        "'Elements: N interactive' but you need to see the actual list "
        "to choose a V_n target. Cheap - no screenshot, no vision. "
        "Optional 'filter' does a case-insensitive substring match."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        filter: str | None = None,
        limit: int | None = None,
        **kw: Any,
    ) -> str:
        elements = await _fetch_elements(session_id, self.s)
        if not elements:
            return "No interactive elements on the current page."

        lines = [line for line in elements.splitlines() if line.strip()]
        total = len(lines)

        if filter:
            needle = filter.casefold()
            lines = [line for line in lines if needle in line.casefold()]

        cap = min(max(limit or _DEFAULT_LIMIT, 1), 500)
        truncated = len(lines) > cap
        if truncated:
            lines = lines[:cap]

        header_bits = [f"{len(lines)} shown"]
        if filter:
            header_bits.append(f"matching '{filter}'")
        header_bits.append(f"of {total} total")
        if truncated:
            header_bits.append(f"truncated at limit={cap}")
        header = f"[ELEMENTS {' '.join(header_bits)}]"

        return header + "\n" + "\n".join(lines)
