"""Screenshot, markdown extraction, and JS dialog tools.

`BrowserScreenshotTool` is the fork between the vision-preprocessor path
and the legacy raw-image path.
"""

from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    StringSchema,
    tool_parameters_schema,
)

from ..formatting import _fetch_elements, _format_state
from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "Optional hint for the vision agent. Only used when "
            "VISION_ENABLED=1.",
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserScreenshotTool(Tool):
    name = "browser_screenshot"
    description = "Take a screenshot. COSTS MONEY. Use browser_get_markdown or browser_eval to verify instead."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, intent: str | None = None, **kw: Any) -> Any:
        # Peek current page content so dedup keys on (url, content_hash)
        # — a reload or DOM change produces a different hash and unblocks.
        peek_hash = ""
        try:
            peek_elements = await _fetch_elements(session_id, self.s)
            peek_hash = BrowserSessionState.hash_page_content(peek_elements)
        except Exception:
            pass

        allowed, reason = self.s.should_allow_screenshot(self.s.current_url, peek_hash)
        if not allowed:
            self.s.log_activity("screenshot(BLOCKED)", reason[:60])
            return reason

        self.s.screenshot_budget -= 1
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            # bounds=true returns selectorEntries (with x/y/width/height) +
            # devicePixelRatio so we can draw bbox overlays before the
            # screenshot goes to the vision LLM.
            params={"vision": "true", "bounds": "true"},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()

        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.mark_screenshot_taken(
                actual_url,
                self.s.hash_page_content(data.get("elements", "")),
            )
        self.s.log_activity(f"screenshot({actual_url[:50] if actual_url else '?'})")
        self.s.record_step("browser_screenshot", "", f"url={actual_url[:60] if actual_url else '?'}")
        caption = _format_state(data, self.s)
        caption += f"\n[Screenshots remaining: {self.s.screenshot_budget}]"
        if data.get("screenshot"):
            entries = data.get("selectorEntries") or []
            dpr = float(data.get("devicePixelRatio") or 1.0)
            # Rename tagName → tag for the overlay (both naming schemes work
            # but tag is the overlay's canonical key).
            overlay_elements = [
                {
                    "index": e.get("index"),
                    "tag": e.get("tagName") or e.get("tag"),
                    "role": e.get("role") or (e.get("attributes") or {}).get("role"),
                    "bounds": e.get("bounds"),
                }
                for e in entries
                if e.get("bounds") and e.get("index") is not None
            ]
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent=intent or "observe page",
                url=actual_url,
                elements=data.get("elements"),
                elements_with_bounds=overlay_elements,
                device_pixel_ratio=dpr,
                # v2-C: full selectorEntries (with attributes + text)
                # so vision_pipeline can detect chevrons that vision
                # merged into a parent row bbox and inject the missing
                # sub-bbox.
                selector_entries=entries,
            )
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        include_anchors=BooleanSchema(
            description=(
                "When true, each heading is annotated inline with "
                "[@y=N] (its absolute scroll-Y in pixels) and a trailing "
                "[OUTLINE scrollY=N scrollHeight=H vp=V] line is "
                "appended. Use this when you need to approximate-scroll "
                "to a NAMED section (e.g. 'Brand', 'Price'): read the "
                "[@y=N] for that heading, compute pixels = y - scrollY, "
                "then browser_scroll(direction='down', pixels=…). "
                "Vision will finish the fine targeting once you land "
                "in the right neighborhood. Default false (back-compat)."
            ),
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserGetMarkdownTool(Tool):
    name = "browser_get_markdown"
    description = (
        "Extract page content as markdown. FREE — no screenshot cost. "
        "Pass include_anchors=true to also get [@y=N] heading anchors "
        "+ a trailing [OUTLINE …] line for DOM-aware approximate "
        "scrolling: read anchor_y, compute pixels = anchor_y - "
        "scrollY, then browser_scroll(pixels=…). Lets vision finish "
        "the fine targeting after you land near the right section."
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        include_anchors: bool | None = None,
        **kw: Any,
    ) -> str:
        params: dict[str, Any] = {}
        if include_anchors:
            params["include_anchors"] = "true"
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/markdown",
            params=params,
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("content", "No content extracted")[:10000]


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        accept=BooleanSchema(description="Accept (true) or dismiss (false)"),
        text=StringSchema("Text for prompt dialogs", nullable=True),
        required=["session_id", "accept"],
    )
)
class BrowserDialogTool(Tool):
    name = "browser_dialog"
    description = "Accept or dismiss a pending JavaScript dialog."

    async def execute(self, session_id: str, accept: bool, text: str | None = None, **kw: Any) -> str:
        payload: dict[str, Any] = {"accept": accept}
        if text:
            payload["text"] = text
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/dialog",
            json=payload,
            timeout=10.0,
        )
        r.raise_for_status()
        return f"Dialog {'accepted' if accept else 'dismissed'}"
