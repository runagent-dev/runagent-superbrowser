"""
Nanobot tools that use the SuperBrowser library directly.

Instead of MCP, these tools import nanobot's Tool base class
and call the SuperBrowser HTTP API for browser automation.
Register them with: bot._loop.tools.register(tool_instance)
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

SUPERBROWSER_URL = "http://localhost:3100"


@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("What to do on the website (e.g., 'Find pricing and extract plan details')"),
        url=StringSchema("Starting URL (optional — will search Google if not provided)", nullable=True),
        required=["task"],
    )
)
class BrowseWebsiteTool(Tool):
    """Browse a website and perform actions interactively."""

    name = "browse_website"
    description = (
        "Browse a website and perform interactive actions. "
        "Handles navigation, clicking, form filling, scrolling, "
        "content extraction, and multi-step workflows. "
        "Returns the result of the browsing task."
    )

    async def execute(self, task: str, url: str | None = None, **kwargs: Any) -> str:
        payload: dict[str, Any] = {"task": task}
        if url:
            payload["url"] = url

        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/task", json=payload)
            r.raise_for_status()
            result = r.json()

        if result.get("success"):
            return result.get("finalAnswer", "Task completed successfully")
        return f"Task failed: {result.get('error', 'Unknown error')}"


@tool_parameters(
    tool_parameters_schema(
        url=StringSchema("URL of the form page"),
        form_data=ObjectSchema(description="Field label/name → value pairs to fill"),
        submit=BooleanSchema(description="Whether to submit after filling (default: true)", default=True),
        required=["url", "form_data"],
    )
)
class FillFormTool(Tool):
    """Navigate to a page and fill a form with provided data."""

    name = "fill_form"
    description = (
        "Navigate to a web form and fill it with the provided data. "
        "Supports text inputs, selects, checkboxes, file uploads. "
        "Can optionally submit after filling."
    )

    async def execute(self, url: str, form_data: dict, submit: bool = True, **kwargs: Any) -> str:
        task = f"Fill the form with: {json.dumps(form_data)}"
        if submit:
            task += " and submit it"

        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/task",
                json={"task": task, "url": url},
            )
            r.raise_for_status()
            result = r.json()

        if result.get("success"):
            return result.get("finalAnswer", "Form filled successfully")
        return f"Form fill failed: {result.get('error', 'Unknown error')}"


@tool_parameters(
    tool_parameters_schema(
        url=StringSchema("URL to screenshot"),
        required=["url"],
    )
)
class TakeScreenshotTool(Tool):
    """Take a screenshot of a web page."""

    name = "take_screenshot"
    description = "Navigate to a URL and take a screenshot of the page."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, url: str, **kwargs: Any) -> Any:
        from nanobot.utils.helpers import build_image_content_blocks

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/screenshot",
                json={"url": url},
            )
            r.raise_for_status()

        return build_image_content_blocks(
            r.content, "image/jpeg", url, f"Screenshot of {url}"
        )


@tool_parameters(
    tool_parameters_schema(
        url=StringSchema("URL to extract content from"),
        goal=StringSchema("What information to extract"),
        required=["url", "goal"],
    )
)
class ExtractContentTool(Tool):
    """Extract specific information from a web page."""

    name = "extract_content"
    description = (
        "Navigate to a URL and extract specific information. "
        "The agent will browse the page, scroll, and gather the requested data."
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, url: str, goal: str, **kwargs: Any) -> str:
        task = f"Extract the following from this page: {goal}"
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/task",
                json={"task": task, "url": url},
            )
            r.raise_for_status()
            result = r.json()

        if result.get("success"):
            return result.get("finalAnswer", "No content extracted")
        return f"Extraction failed: {result.get('error', 'Unknown error')}"


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema("What to search for and act on"),
        required=["query"],
    )
)
class SearchAndActTool(Tool):
    """Search Google and interact with results to complete a task."""

    name = "search_and_act"
    description = (
        "Search Google for a query and interact with the results. "
        "Goes beyond simple search — navigates to result pages, "
        "extracts information, fills forms, etc."
    )

    async def execute(self, query: str, **kwargs: Any) -> str:
        task = f'Search Google for "{query}" and complete the task'
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/task", json={"task": task})
            r.raise_for_status()
            result = r.json()

        if result.get("success"):
            return result.get("finalAnswer", "Search completed")
        return f"Search failed: {result.get('error', 'Unknown error')}"


@tool_parameters(
    tool_parameters_schema(
        url=StringSchema("URL of the page"),
        link_description=StringSchema("Description of which link/button to click"),
        required=["url", "link_description"],
    )
)
class DownloadFileTool(Tool):
    """Download a file from a web page."""

    name = "download_file"
    description = (
        "Navigate to a page, find a download link by description, "
        "click it, and save the file."
    )

    async def execute(self, url: str, link_description: str, **kwargs: Any) -> str:
        task = f"Find and click the download link: {link_description}"
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/task",
                json={"task": task, "url": url},
            )
            r.raise_for_status()
            result = r.json()

        if result.get("success"):
            return result.get("finalAnswer", "Download completed")
        return f"Download failed: {result.get('error', 'Unknown error')}"


@tool_parameters(
    tool_parameters_schema(
        url=StringSchema("URL to export as PDF"),
        output_path=StringSchema("Where to save the PDF", nullable=True),
        required=["url"],
    )
)
class ExportPdfTool(Tool):
    """Export a web page as PDF."""

    name = "export_pdf"
    description = "Navigate to a URL and export the page as a PDF file."

    async def execute(self, url: str, output_path: str | None = None, **kwargs: Any) -> str:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/pdf",
                json={"url": url},
            )
            r.raise_for_status()

        save_path = output_path or "/tmp/superbrowser/downloads/page.pdf"
        import os
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(r.content)

        size_kb = len(r.content) / 1024
        return f"PDF saved to {save_path} ({size_kb:.1f} KB)"


@tool_parameters(
    tool_parameters_schema(
        url=StringSchema("URL to run the script on"),
        script=StringSchema("JavaScript code to evaluate"),
        required=["url", "script"],
    )
)
class EvaluateScriptTool(Tool):
    """Run JavaScript on a web page and return the result."""

    name = "evaluate_script"
    description = "Navigate to a URL and execute a JavaScript snippet in the page context."

    async def execute(self, url: str, script: str, **kwargs: Any) -> str:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/function",
                json={"url": url, "code": f"return {script}"},
            )
            r.raise_for_status()

        return f"Script result: {r.text[:2000]}"


def register_high_level_tools(bot: "Nanobot") -> None:
    """Register high-level tools that delegate to the executor."""
    tools = [
        BrowseWebsiteTool(),
        FillFormTool(),
        TakeScreenshotTool(),
        ExtractContentTool(),
        SearchAndActTool(),
        DownloadFileTool(),
        ExportPdfTool(),
        EvaluateScriptTool(),
    ]
    for tool in tools:
        bot._loop.tools.register(tool)


def register_all_tools(bot: "Nanobot") -> None:
    """Register ALL SuperBrowser tools — both high-level and session-based.

    High-level tools (browse_website, fill_form, etc.):
      Delegate entire tasks to the built-in Navigator+Planner agent loop.

    Session tools (browser_open, browser_click, browser_screenshot, etc.):
      Give nanobot step-by-step control. The agent SEES screenshots and
      decides what to do next — just like Claude Code did with browserless.

    Usage:
        from nanobot import Nanobot
        from superbrowser_bridge.tools import register_all_tools

        bot = Nanobot.from_config(config_path="config/config.json")
        register_all_tools(bot)
        result = await bot.run("Go to irctc.co.in and search for trains from Delhi to Mumbai")
    """
    # High-level (fire-and-forget to executor)
    register_high_level_tools(bot)

    # Low-level session tools (step-by-step with screenshots)
    from superbrowser_bridge.session_tools import register_session_tools
    register_session_tools(bot)
