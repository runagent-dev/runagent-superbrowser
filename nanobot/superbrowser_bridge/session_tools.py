"""
Low-level session-based browser tools for nanobot.

These give the nanobot super agent step-by-step control:
  1. Open session → sees screenshot + DOM state
  2. Navigate / click / type / scroll → sees updated screenshot
  3. If stuck → take screenshot, analyze, try different approach
  4. Execute script if needed → sees result
  5. Repeat until task done
  6. Close session

The nanobot agent SEES every screenshot and decides what to do next,
just like Claude Code did with browserless — but fully autonomous.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)

SUPERBROWSER_URL = "http://localhost:3100"


def _build_image_blocks(screenshot_b64: str, caption: str) -> list[dict]:
    """Build content blocks with image for the agent to see."""
    return [
        {"type": "text", "text": caption},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
        },
    ]


def _format_state(data: dict) -> str:
    """Format state response as text for the agent."""
    parts = []
    if data.get("url"):
        parts.append(f"URL: {data['url']}")
    if data.get("title"):
        parts.append(f"Title: {data['title']}")
    if data.get("scrollInfo"):
        si = data["scrollInfo"]
        parts.append(f"Scroll: {si.get('scrollY', 0)}/{si.get('scrollHeight', 0)} (viewport: {si.get('viewportHeight', 0)})")
    if data.get("elements"):
        parts.append(f"\nInteractive elements:\n{data['elements']}")
    if data.get("consoleErrors"):
        parts.append(f"\nConsole errors: {data['consoleErrors']}")
    if data.get("pendingDialogs"):
        parts.append(f"\nPending dialogs: {data['pendingDialogs']}")
    return "\n".join(parts)


@tool_parameters(
    tool_parameters_schema(
        url=StringSchema("URL to open (optional)", nullable=True),
        required=[],
    )
)
class BrowserOpenTool(Tool):
    """Open a browser session. Returns screenshot + interactive elements."""

    name = "browser_open"
    description = (
        "Open a new browser session. Returns a screenshot of the page and "
        "a list of interactive elements you can interact with. "
        "Use the returned session_id for all subsequent browser actions."
    )

    async def execute(self, url: str | None = None, **kw: Any) -> Any:
        payload: dict[str, Any] = {}
        if url:
            payload["url"] = url

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/create", json=payload)
            r.raise_for_status()
            data = r.json()

        caption = _format_state(data)
        caption = f"Session: {data['sessionId']}\n{caption}"

        if data.get("screenshot"):
            return _build_image_blocks(data["screenshot"], caption)
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID from browser_open"),
        url=StringSchema("URL to navigate to"),
        required=["session_id", "url"],
    )
)
class BrowserNavigateTool(Tool):
    """Navigate to a URL in an open session. Returns screenshot + state."""

    name = "browser_navigate"
    description = (
        "Navigate to a URL in an open browser session. "
        "Returns updated screenshot and interactive elements."
    )

    async def execute(self, session_id: str, url: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/navigate",
                json={"url": url},
            )
            r.raise_for_status()
            data = r.json()

        caption = _format_state(data)
        if data.get("screenshot"):
            return _build_image_blocks(data["screenshot"], caption)
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        required=["session_id"],
    )
)
class BrowserScreenshotTool(Tool):
    """Take a screenshot of the current page. Use this to SEE what's on screen."""

    name = "browser_screenshot"
    description = (
        "Take a screenshot of the current browser page. "
        "Use this whenever you need to see the current state, "
        "verify an action worked, or understand the page layout."
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true"},
            )
            r.raise_for_status()
            data = r.json()

        caption = _format_state(data)
        if data.get("screenshot"):
            return _build_image_blocks(data["screenshot"], caption)
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index from the interactive elements list"),
        button=StringSchema("Mouse button: left, right, middle (default: left)", nullable=True),
        required=["session_id", "index"],
    )
)
class BrowserClickTool(Tool):
    """Click an interactive element by its [index]. Returns updated screenshot."""

    name = "browser_click"
    description = (
        "Click on an interactive element by its [index] number. "
        "Returns updated screenshot showing the result of the click."
    )

    async def execute(self, session_id: str, index: int, button: str | None = None, **kw: Any) -> Any:
        payload: dict[str, Any] = {"index": index}
        if button:
            payload["button"] = button

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/click",
                json=payload,
            )
            r.raise_for_status()
            data = r.json()

        caption = f"Clicked element [{index}]\n"
        caption += _format_state(data)
        if data.get("screenshot"):
            return _build_image_blocks(data["screenshot"], caption)
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        x=NumberSchema(description="X coordinate"),
        y=NumberSchema(description="Y coordinate"),
        required=["session_id", "x", "y"],
    )
)
class BrowserClickAtTool(Tool):
    """Click at specific page coordinates. Use when element index doesn't work."""

    name = "browser_click_at"
    description = (
        "Click at specific x,y coordinates on the page. "
        "Use this when clicking by element index fails or for custom UI elements."
    )

    async def execute(self, session_id: str, x: float, y: float, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/click",
                json={"x": x, "y": y},
            )
            r.raise_for_status()
            data = r.json()

        caption = f"Clicked at ({x}, {y})\n" + _format_state(data)
        if data.get("screenshot"):
            return _build_image_blocks(data["screenshot"], caption)
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index to type into"),
        text=StringSchema("Text to type"),
        clear=BooleanSchema(description="Clear field first (default: true)", default=True),
        required=["session_id", "index", "text"],
    )
)
class BrowserTypeTool(Tool):
    """Type text into a form field. Returns updated screenshot."""

    name = "browser_type"
    description = (
        "Type text into an input field by its [index] number. "
        "Clears existing content by default. Returns updated screenshot."
    )

    async def execute(self, session_id: str, index: int, text: str, clear: bool = True, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/type",
                json={"index": index, "text": text, "clear": clear},
            )
            r.raise_for_status()
            data = r.json()

        caption = f"Typed \"{text}\" into [{index}]\n" + _format_state(data)
        if data.get("screenshot"):
            return _build_image_blocks(data["screenshot"], caption)
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        keys=StringSchema("Keys to send (e.g. Enter, ArrowDown, Control+a, Tab)"),
        required=["session_id", "keys"],
    )
)
class BrowserKeysTool(Tool):
    """Send keyboard keys. Use for Enter, Tab, ArrowDown, keyboard shortcuts."""

    name = "browser_keys"
    description = (
        "Send keyboard keys or shortcuts. Examples: "
        "'Enter', 'Tab', 'ArrowDown', 'Control+a', 'Escape'. "
        "Returns updated screenshot."
    )

    async def execute(self, session_id: str, keys: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/keys",
                json={"keys": keys},
            )
            r.raise_for_status()
            data = r.json()

        caption = f"Sent keys: {keys}\n" + _format_state(data)
        if data.get("screenshot"):
            return _build_image_blocks(data["screenshot"], caption)
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        direction=StringSchema("Scroll direction: up or down (default: down)", nullable=True),
        percent=NumberSchema(description="Scroll to exact percentage 0-100 (overrides direction)", nullable=True),
        required=["session_id"],
    )
)
class BrowserScrollTool(Tool):
    """Scroll the page. Returns updated screenshot."""

    name = "browser_scroll"
    description = (
        "Scroll the page up or down, or to a specific percentage. "
        "Returns updated screenshot showing new content."
    )

    async def execute(self, session_id: str, direction: str | None = None, percent: float | None = None, **kw: Any) -> Any:
        payload: dict[str, Any] = {}
        if percent is not None:
            payload["percent"] = percent
        else:
            payload["direction"] = direction or "down"

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/scroll",
                json=payload,
            )
            r.raise_for_status()
            data = r.json()

        action = f"Scrolled to {percent}%" if percent is not None else f"Scrolled {direction or 'down'}"
        caption = f"{action}\n" + _format_state(data)
        if data.get("screenshot"):
            return _build_image_blocks(data["screenshot"], caption)
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index of the select/dropdown"),
        value=StringSchema("Option value or visible text to select"),
        required=["session_id", "index", "value"],
    )
)
class BrowserSelectTool(Tool):
    """Select a dropdown option. Returns updated screenshot."""

    name = "browser_select"
    description = "Select an option in a dropdown by value. Returns updated screenshot."

    async def execute(self, session_id: str, index: int, value: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/select",
                json={"index": index, "value": value},
            )
            r.raise_for_status()
            data = r.json()

        caption = f"Selected \"{value}\" in [{index}]"
        if data.get("screenshot"):
            return _build_image_blocks(data["screenshot"], caption)
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        script=StringSchema("JavaScript code to execute in the page"),
        required=["session_id", "script"],
    )
)
class BrowserEvalTool(Tool):
    """Execute JavaScript in the page. Use for complex interactions or data extraction."""

    name = "browser_eval"
    description = (
        "Execute a JavaScript snippet in the browser page. "
        "Use this for complex interactions, reading page data, "
        "or automating tasks that standard actions can't handle. "
        "Returns the script result."
    )

    async def execute(self, session_id: str, script: str, **kw: Any) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": script},
            )
            r.raise_for_status()
            data = r.json()

        result = data.get("result")
        if isinstance(result, dict) or isinstance(result, list):
            return json.dumps(result, indent=2, ensure_ascii=False)[:5000]
        return str(result)[:5000]


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        required=["session_id"],
    )
)
class BrowserGetMarkdownTool(Tool):
    """Extract the page content as clean readable markdown."""

    name = "browser_get_markdown"
    description = (
        "Extract the current page content as clean markdown text. "
        "Useful for reading articles, product details, search results, etc."
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/markdown")
            r.raise_for_status()
            data = r.json()

        return data.get("content", "No content extracted")[:10000]


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        accept=BooleanSchema(description="Accept (true) or dismiss (false) the dialog"),
        text=StringSchema("Text to enter for prompt dialogs", nullable=True),
        required=["session_id", "accept"],
    )
)
class BrowserDialogTool(Tool):
    """Handle a pending alert/confirm/prompt dialog."""

    name = "browser_dialog"
    description = "Accept or dismiss a pending JavaScript dialog (alert, confirm, prompt)."

    async def execute(self, session_id: str, accept: bool, text: str | None = None, **kw: Any) -> str:
        payload: dict[str, Any] = {"accept": accept}
        if text:
            payload["text"] = text

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/dialog",
                json=payload,
            )
            r.raise_for_status()

        return f"Dialog {'accepted' if accept else 'dismissed'}"


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        required=["session_id"],
    )
)
class BrowserCloseTool(Tool):
    """Close a browser session when done."""

    name = "browser_close"
    description = "Close the browser session and free resources. Always close when done."

    async def execute(self, session_id: str, **kw: Any) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(f"{SUPERBROWSER_URL}/session/{session_id}")
            r.raise_for_status()

        return "Session closed"


def register_session_tools(bot: "Nanobot") -> None:
    """Register all low-level session tools with a nanobot instance.

    These give the agent step-by-step browser control with screenshots:

        bot = Nanobot.from_config(...)
        register_session_tools(bot)

        # Now the agent can:
        # 1. browser_open → sees screenshot + elements
        # 2. browser_click [3] → sees result screenshot
        # 3. browser_type [5] "hello" → sees result
        # 4. browser_screenshot → verify state
        # 5. browser_eval "document.title" → run JS
        # 6. browser_close → cleanup
    """
    tools = [
        BrowserOpenTool(),
        BrowserNavigateTool(),
        BrowserScreenshotTool(),
        BrowserClickTool(),
        BrowserClickAtTool(),
        BrowserTypeTool(),
        BrowserKeysTool(),
        BrowserScrollTool(),
        BrowserSelectTool(),
        BrowserEvalTool(),
        BrowserGetMarkdownTool(),
        BrowserDialogTool(),
        BrowserCloseTool(),
    ]
    for tool in tools:
        bot._loop.tools.register(tool)
