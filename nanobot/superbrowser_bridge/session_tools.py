"""
Low-level session-based browser tools for nanobot.

Script-first, vision-fallback architecture:
  1. Open session → sees DOM state (+ optional screenshot)
  2. Inspect DOM with browser_eval → find selectors
  3. Execute script with browser_run_script → do all actions at once
  4. Verify with browser_get_markdown or browser_eval → check result via DOM
  5. Screenshot ONLY if DOM state is ambiguous (max 3 per session)
  6. Close session

The nanobot agent is the single brain — no inner LLM loop.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

import os
import base64
from datetime import datetime

SUPERBROWSER_URL = "http://localhost:3100"
SCREENSHOT_DIR = os.environ.get("SUPERBROWSER_SCREENSHOT_DIR", "/tmp/superbrowser/screenshots")

# Auto-incrementing step counter for screenshot filenames
_step_counter = 0
_click_at_count = 0
MAX_INDIVIDUAL_CLICKS = 3  # After 3 click_at calls, force scripts

# Screenshot budget — hard limit per session to control vision API costs
MAX_SCREENSHOTS_PER_SESSION = 3
_screenshot_budget = MAX_SCREENSHOTS_PER_SESSION

# Cost tracking
_vision_calls = 0
_text_calls = 0
_session_start_time = 0.0


def _reset_session_counters():
    """Reset all per-session counters when a new session opens."""
    global _step_counter, _click_at_count, _screenshot_budget
    global _vision_calls, _text_calls, _session_start_time
    _step_counter = 0
    _click_at_count = 0
    _screenshot_budget = MAX_SCREENSHOTS_PER_SESSION
    _vision_calls = 0
    _text_calls = 0
    _session_start_time = time.time()


def _log_session_summary():
    """Print cost summary when session closes."""
    elapsed = time.time() - _session_start_time if _session_start_time else 0
    screenshots_used = MAX_SCREENSHOTS_PER_SESSION - _screenshot_budget
    print(f"\n  [Session Summary]")
    print(f"  Duration: {elapsed:.1f}s")
    print(f"  Vision API calls (screenshots sent to LLM): {_vision_calls}")
    print(f"  Text-only calls: {_text_calls}")
    print(f"  Screenshots used: {screenshots_used}/{MAX_SCREENSHOTS_PER_SESSION}")
    est_cost = _vision_calls * 0.03 + _text_calls * 0.002
    print(f"  Estimated tool cost: ~${est_cost:.3f}")


def _save_screenshot(screenshot_b64: str, label: str = "") -> str:
    """Save screenshot to disk so the user can see what's happening."""
    global _step_counter
    _step_counter += 1
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    filename = f"{_step_counter:03d}-{label}.jpg" if label else f"{_step_counter:03d}.jpg"
    filepath = os.path.join(SCREENSHOT_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(base64.b64decode(screenshot_b64))
    print(f"  [screenshot saved: {filepath}]")
    return filepath


def _build_image_blocks(screenshot_b64: str, caption: str) -> list[dict]:
    """Build content blocks WITH image — use only when screenshot is needed."""
    global _vision_calls
    _vision_calls += 1
    label = caption.split("\n")[0][:30].replace(" ", "-").replace("/", "_")
    _save_screenshot(screenshot_b64, label)

    return [
        {"type": "text", "text": caption},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
        },
    ]


_action_count = 0

def _build_text_only(data: dict, prefix: str = "") -> str:
    """Build minimal text response — just confirmation, no element list, no screenshot to LLM."""
    global _action_count, _text_calls
    _action_count += 1
    _text_calls += 1
    parts = [prefix]
    if data.get("url"):
        parts.append(f"Page: {data['url']}")
    result = " | ".join(p for p in parts if p)
    # After 3 individual actions, nudge toward scripts
    if _action_count >= 3:
        result += " | HINT: You've done multiple individual actions. Use browser_run_script to batch the remaining steps into ONE script instead of clicking one at a time."
    return result


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
        region=StringSchema(
            "Region code for geo-restricted sites (e.g., 'bd' for Bangladesh, 'in' for India). "
            "Routes through a regional proxy if configured. Use when a site is geo-blocked.",
            nullable=True,
        ),
        proxy=StringSchema("Direct proxy URL (e.g., 'socks5://proxy:1080'). Overrides region.", nullable=True),
        required=[],
    )
)
class BrowserOpenTool(Tool):
    """Open a browser session. Returns screenshot + interactive elements."""

    name = "browser_open"

    @property
    def exclusive(self) -> bool:
        return True

    description = (
        "Open a new browser session. Returns a screenshot of the page and "
        "a list of interactive elements you can interact with. "
        "Use the returned session_id for all subsequent browser actions. "
        "For geo-restricted sites, pass region='bd' (Bangladesh), 'in' (India), etc. "
        "to route through a regional proxy."
    )

    async def execute(self, url: str | None = None, region: str | None = None, proxy: str | None = None, **kw: Any) -> Any:
        global _action_count, _screenshot_budget
        _action_count = 0
        _reset_session_counters()
        print(f"\n>> browser_open(url={url}, region={region})")
        payload: dict[str, Any] = {}
        if url:
            payload["url"] = url
        if region:
            payload["region"] = region
        if proxy:
            payload["proxy"] = proxy

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/create", json=payload)
            r.raise_for_status()
            data = r.json()

        caption = _format_state(data)
        caption = f"Session: {data['sessionId']}\n{caption}"

        if data.get("screenshot"):
            _screenshot_budget -= 1
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

    @property
    def exclusive(self) -> bool:
        return True

    description = (
        "Navigate to a URL in an open browser session. "
        "Returns updated screenshot and interactive elements."
    )

    async def execute(self, session_id: str, url: str, **kw: Any) -> Any:
        global _screenshot_budget
        print(f"\n>> browser_navigate({url})")
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/navigate",
                json={"url": url},
            )
            r.raise_for_status()
            data = r.json()

        caption = _format_state(data)
        if data.get("screenshot"):
            _screenshot_budget -= 1
            return _build_image_blocks(data["screenshot"], caption)
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        required=["session_id"],
    )
)
class BrowserScreenshotTool(Tool):
    """Take a screenshot of the current page to see what's on screen."""

    name = "browser_screenshot"
    description = (
        "Take a screenshot of the current browser page. "
        "Use to see current state, verify script results, or understand page layout."
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> Any:
        global _screenshot_budget
        if _screenshot_budget <= 0:
            return (
                f"[Screenshot budget exhausted] Max {MAX_SCREENSHOTS_PER_SESSION} screenshots per session reached. "
                "Use browser_get_markdown or browser_eval to inspect page state instead. "
                "Example: browser_eval(session_id, 'document.body.innerText.substring(0, 3000)')"
            )
        _screenshot_budget -= 1
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true"},
            )
            r.raise_for_status()
            data = r.json()

        remaining = _screenshot_budget
        caption = _format_state(data)
        if remaining == 0:
            caption += f"\n[Last screenshot — budget exhausted. Use browser_get_markdown or browser_eval for further verification.]"
        else:
            caption += f"\n[Screenshots remaining: {remaining}]"
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
    """Click an interactive element by its [index]."""

    name = "browser_click"

    @property
    def exclusive(self) -> bool:
        return True
    description = (
        "Click on an interactive element by its [index] number. "
        "Returns updated screenshot showing the result of the click."
    )

    async def execute(self, session_id: str, index: int, button: str | None = None, **kw: Any) -> Any:
        print(f"\n>> browser_click([{index}])")
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

        return _build_text_only(data, f"Clicked [{index}]")


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        x=NumberSchema(description="X coordinate"),
        y=NumberSchema(description="Y coordinate"),
        required=["session_id", "x", "y"],
    )
)
class BrowserClickAtTool(Tool):
    """Click at x,y coordinates. AVOID — prefer browser_run_script with page.click('selector') instead."""

    name = "browser_click_at"
    description = (
        "Click at specific x,y coordinates. LAST RESORT ONLY. "
        "Prefer browser_run_script with page.click('selector') or "
        "browser_click with element index. Coordinate clicking is unreliable."
    )

    async def execute(self, session_id: str, x: float, y: float, **kw: Any) -> Any:
        global _click_at_count
        _click_at_count += 1
        if _click_at_count > MAX_INDIVIDUAL_CLICKS:
            return (
                f"[BLOCKED] browser_click_at used {_click_at_count} times (max {MAX_INDIVIDUAL_CLICKS}). "
                "Switch to browser_run_script for remaining interactions. Example:\n"
                "browser_run_script(session_id, `\n"
                "  await page.click('#fromCity');\n"
                "  await page.type('#fromCity input', 'Dhaka');\n"
                "  await helpers.sleep(1000);\n"
                "  await page.click('.suggestion-item');\n"
                "`)"
            )
        print(f"\n>> browser_click_at({x}, {y})")
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/click",
                json={"x": x, "y": y},
            )
            r.raise_for_status()
            data = r.json()

        return _build_text_only(data, f"Clicked at ({x}, {y})")


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
    """Type text into a form field."""

    name = "browser_type"

    @property
    def exclusive(self) -> bool:
        return True
    description = (
        "Type text into an input field by its [index] number. "
        "Clears existing content by default. Returns updated screenshot."
    )

    async def execute(self, session_id: str, index: int, text: str, clear: bool = True, **kw: Any) -> Any:
        print(f'\n>> browser_type([{index}], "{text}")')
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/type",
                json={"index": index, "text": text, "clear": clear},
            )
            r.raise_for_status()
            data = r.json()

        return _build_text_only(data, f'Typed "{text}" into [{index}]')


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        keys=StringSchema("Keys to send (e.g. Enter, ArrowDown, Control+a, Tab)"),
        required=["session_id", "keys"],
    )
)
class BrowserKeysTool(Tool):
    """Send keyboard keys (Enter, Tab, ArrowDown, Control+a, etc)."""

    name = "browser_keys"

    @property
    def exclusive(self) -> bool:
        return True
    description = (
        "Send keyboard keys or shortcuts. Examples: "
        "'Enter', 'Tab', 'ArrowDown', 'Control+a', 'Escape'. "
        "Returns updated screenshot."
    )

    async def execute(self, session_id: str, keys: str, **kw: Any) -> Any:
        print(f"\n>> browser_keys({keys})")
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/keys",
                json={"keys": keys},
            )
            r.raise_for_status()
            data = r.json()

        return _build_text_only(data, f"Sent keys: {keys}")


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        direction=StringSchema("Scroll direction: up or down (default: down)", nullable=True),
        percent=NumberSchema(description="Scroll to exact percentage 0-100 (overrides direction)", nullable=True),
        required=["session_id"],
    )
)
class BrowserScrollTool(Tool):
    """Scroll the page up or down."""

    name = "browser_scroll"

    @property
    def exclusive(self) -> bool:
        return True
    description = (
        "Scroll the page up or down, or to a specific percentage. "
        "Returns updated screenshot showing new content."
    )

    async def execute(self, session_id: str, direction: str | None = None, percent: float | None = None, **kw: Any) -> Any:
        print(f"\n>> browser_scroll({direction or f'{percent}%'})")
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
        return _build_text_only(data, action)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index of the select/dropdown"),
        value=StringSchema("Option value or visible text to select"),
        required=["session_id", "index", "value"],
    )
)
class BrowserSelectTool(Tool):
    """Select a dropdown option."""

    name = "browser_select"

    @property
    def exclusive(self) -> bool:
        return True
    description = "Select an option in a dropdown by value. Returns updated screenshot."

    async def execute(self, session_id: str, index: int, value: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/select",
                json={"index": index, "value": value},
            )
            r.raise_for_status()
            data = r.json()

        return _build_text_only(data, f'Selected "{value}" in [{index}]')


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
        print(f"\n>> browser_eval({script[:60]}...)")
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
        script=StringSchema(
            "Puppeteer script body with full page API access. "
            "Available variables: page (Puppeteer Page), context (optional data), "
            "helpers (sleep, screenshot, log). "
            "Example: await page.goto('https://example.com'); "
            "await page.type('#search', 'hello'); "
            "await page.click('#submit'); "
            "return await page.title();"
        ),
        context=ObjectSchema(
            description="Optional context data passed to the script",
            nullable=True,
        ),
        timeout=IntegerSchema(
            description="Script timeout in ms (default: 60000, max: 300000)",
            nullable=True,
        ),
        required=["session_id", "script"],
    )
)
class BrowserRunScriptTool(Tool):
    """Execute a full Puppeteer script with page API access.

    Unlike browser_eval (which runs DOM-level JavaScript like document.querySelector),
    this tool runs Node.js-level Puppeteer code with full access to:
    - page.goto(), page.click(), page.type(), page.waitForSelector()
    - page.screenshot(), page.pdf(), page.evaluate()
    - page.keyboard, page.mouse
    - helpers.sleep(), helpers.screenshot(), helpers.log()

    Use for complex multi-step browser automation.
    """

    name = "browser_run_script"

    @property
    def exclusive(self) -> bool:
        return True

    description = (
        "Execute a Puppeteer script with full page API access. "
        "Unlike browser_eval (DOM-only JS), this gives access to "
        "page.goto(), page.click(), page.type(), page.waitForSelector(), "
        "page.screenshot(), and all Puppeteer methods. "
        "Use for complex multi-step automation scripts."
    )

    async def execute(
        self,
        session_id: str,
        script: str,
        context: dict | None = None,
        timeout: int | None = None,
        **kw: Any,
    ) -> str:
        print(f"\n>> browser_run_script({script[:80]}...)")
        payload: dict[str, Any] = {"code": script}
        if context:
            payload["context"] = context
        if timeout:
            payload["timeout"] = timeout

        client_timeout = max(120.0, (timeout or 60000) / 1000 + 10)
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/script",
                json=payload,
            )
            r.raise_for_status()
            data = r.json()

        if not data.get("success"):
            return f"Script error: {data.get('error', 'Unknown error')}"

        parts = []
        result = data.get("result")
        if result is not None:
            if isinstance(result, (dict, list)):
                parts.append(f"Result: {json.dumps(result, indent=2, ensure_ascii=False)[:5000]}")
            else:
                parts.append(f"Result: {str(result)[:5000]}")

        logs = data.get("logs", [])
        if logs:
            parts.append("Logs:\n" + "\n".join(logs[:20]))

        duration = data.get("duration", 0)
        parts.append(f"Duration: {duration}ms")

        return "\n".join(parts)


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
        print(f"\n>> browser_close({session_id})")
        _log_session_summary()
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(f"{SUPERBROWSER_URL}/session/{session_id}")
            r.raise_for_status()

        screenshots_used = MAX_SCREENSHOTS_PER_SESSION - _screenshot_budget
        return f"Session closed. Stats: {_vision_calls} vision calls, {_text_calls} text calls, {screenshots_used} screenshots used."


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        required=["session_id"],
    )
)
class BrowserDetectCaptchaTool(Tool):
    """Detect if the current page has a captcha."""

    name = "browser_detect_captcha"
    description = (
        "Check if the page has a captcha (reCAPTCHA, hCaptcha, Cloudflare Turnstile). "
        "Use this when a page seems blocked or asks for verification."
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/captcha/detect")
            r.raise_for_status()
            data = r.json()

        captcha = data.get("captcha")
        if not captcha:
            return "No captcha detected on this page."
        return f"Captcha detected: type={captcha['type']}, siteKey={captcha.get('siteKey', 'N/A')}"


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        required=["session_id"],
    )
)
class BrowserCaptchaScreenshotTool(Tool):
    """Take a close-up screenshot of the captcha for analysis/solving."""

    name = "browser_captcha_screenshot"
    description = (
        "Take a close-up screenshot of the captcha area. "
        "Use this to see the captcha image and attempt to solve it visually."
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/captcha/screenshot")
            if r.status_code == 404:
                return "No captcha area found on the page."
            r.raise_for_status()

        import base64
        b64 = base64.b64encode(r.content).decode()
        return _build_image_blocks(b64, "Captcha area — analyze this to solve")


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        method=StringSchema(
            "Solving method: 'auto' (try all), 'token' (inject token), "
            "'ai_vision' (LLM analyzes image grid), 'grid' (2captcha grid API). "
            "Default: 'auto'",
            nullable=True,
        ),
        provider=StringSchema("Captcha solver: '2captcha' or 'anticaptcha'", nullable=True),
        api_key=StringSchema("API key for the solver service", nullable=True),
        required=["session_id"],
    )
)
class BrowserSolveCaptchaTool(Tool):
    """Solve a captcha using multiple strategies.

    Methods (tried in order when method='auto'):
    1. Token injection — send siteKey to 2captcha, get token, inject it (works 95%)
    2. AI vision — screenshot the image grid, ask LLM which tiles match, click them
    3. 2captcha grid — send grid screenshot to 2captcha, get tile indices, click them
    4. Manual wait — wait for human to solve

    For image grid captchas ("select all traffic lights"), use method='auto' or 'ai_vision'.
    """

    name = "browser_solve_captcha"
    description = (
        "Solve a detected captcha automatically. Supports token injection, "
        "AI vision (analyzes image grid tiles like 'select traffic lights'), "
        "and 2captcha grid API. Use method='auto' to try all strategies."
    )

    async def execute(
        self,
        session_id: str,
        method: str | None = None,
        provider: str | None = None,
        api_key: str | None = None,
        **kw: Any,
    ) -> str:
        payload: dict[str, Any] = {}
        if method:
            payload["method"] = method
        if provider:
            payload["provider"] = provider
        if api_key:
            payload["apiKey"] = api_key

        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/captcha/solve",
                json=payload,
            )
            r.raise_for_status()
            data = r.json()

        if data.get("solved"):
            solve_method = data.get("method", "unknown")
            attempts = data.get("attempts", 1)
            captcha_type = data.get("captcha", {}).get("type", "unknown")
            return f"Captcha solved via {solve_method} method ({attempts} attempt(s), type: {captcha_type})"
        return f"Captcha not solved: {data.get('error', 'all methods failed')}. Use browser_ask_user for manual solving."


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        question=StringSchema("What to ask the user"),
        input_type=StringSchema(
            "Type of input needed: credentials, captcha, confirmation, otp, card, text, choice",
            nullable=True,
        ),
        required=["session_id", "question"],
    )
)
class BrowserAskUserTool(Tool):
    """Ask the user for input. Use when you need credentials, OTP, confirmation, etc.

    The question and a screenshot of the current page are sent to the user.
    This tool blocks until the user responds via their messaging channel.
    """

    name = "browser_ask_user"
    description = (
        "Ask the user a question and wait for their response. "
        "Use when you need: login credentials, OTP/2FA code, captcha help, "
        "payment confirmation, or any information you don't have. "
        "Sends the user a screenshot of the current page for context."
    )

    async def execute(self, session_id: str, question: str, input_type: str | None = None, **kw: Any) -> Any:
        # Get current screenshot for context
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true"},
            )
            r.raise_for_status()
            data = r.json()

        screenshot = data.get("screenshot")

        # Build response with screenshot so the user sees the page
        parts = [f"[Browser needs your input]\n\n{question}"]
        if data.get("url"):
            parts.append(f"\nCurrent page: {data['url']}")

        caption = "\n".join(parts)

        if screenshot:
            return _build_image_blocks(screenshot, caption)
        return caption


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
        BrowserRunScriptTool(),
        BrowserGetMarkdownTool(),
        BrowserDialogTool(),
        BrowserDetectCaptchaTool(),
        BrowserCaptchaScreenshotTool(),
        BrowserSolveCaptchaTool(),
        BrowserAskUserTool(),
        BrowserCloseTool(),
    ]
    for tool in tools:
        bot._loop.tools.register(tool)
