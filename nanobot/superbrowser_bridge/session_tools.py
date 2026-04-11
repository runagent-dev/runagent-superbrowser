"""
Low-level session-based browser tools for nanobot.

State is encapsulated in BrowserSessionState — not module globals.
This allows multiple Nanobot instances (e.g., orchestrator + browser worker)
to have isolated state in the same process.
"""

from __future__ import annotations

import json
import time
import os
import base64
from datetime import datetime
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

from superbrowser_bridge.loop_detector import ActionLoopDetector

SUPERBROWSER_URL = "http://localhost:3100"
SCREENSHOT_DIR = os.environ.get("SUPERBROWSER_SCREENSHOT_DIR", "/tmp/superbrowser/screenshots")


class BrowserSessionState:
    """Per-instance state for browser session tools.

    Each Nanobot instance that registers browser tools gets its own state.
    This prevents multi-agent setups from sharing globals.
    """

    MAX_SCREENSHOTS = 3
    MAX_CLICK_AT = 3

    def __init__(self):
        self.screenshot_budget = self.MAX_SCREENSHOTS
        self.vision_calls = 0
        self.text_calls = 0
        self.start_time = 0.0
        self.sessions_opened = 0
        self.activity_log: list[str] = []
        # Per-session (reset on each browser_open)
        self.step_counter = 0
        self.click_at_count = 0
        self.action_count = 0
        self.actions_since_screenshot = 0

        # Checkpoint & URL tracking
        self.task_id: str = ""
        self.checkpoints: list[dict] = []
        self.current_url: str = ""
        self.best_checkpoint_url: str = ""
        self.url_visit_counts: dict[str, int] = {}
        self.regression_count: int = 0
        self.screenshotted_urls: set[str] = set()
        self.last_screenshot_url: str = ""
        self.step_history: list[dict] = []
        # Track consecutive click-type tool calls for loop detection
        self.consecutive_click_calls: int = 0
        # Smart loop detection (ported from browser-use)
        self.loop_detector = ActionLoopDetector()

    def reset_per_session(self):
        """Reset per-session counters. Budget is NOT reset."""
        self.step_counter = 0
        self.click_at_count = 0
        self.action_count = 0
        self.actions_since_screenshot = 0

    def init_if_needed(self):
        if self.start_time == 0.0:
            self.start_time = time.time()

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize URL for comparison (strip trailing slash, fragment)."""
        if not url:
            return ""
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, ""))

    def record_url(self, url: str) -> None:
        """Track a URL visit. Updates current_url and visit counts."""
        if not url:
            return
        norm = self._normalize_url(url)
        self.current_url = url
        self.url_visit_counts[norm] = self.url_visit_counts.get(norm, 0) + 1

    def record_checkpoint(self, url: str, title: str, action: str) -> None:
        """Record a progress checkpoint (successful meaningful step)."""
        self.checkpoints.append({
            "url": url, "title": title, "action": action,
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        if url and url != self.best_checkpoint_url:
            self.best_checkpoint_url = url

    def is_regression(self, url: str) -> bool:
        """Check if navigating to url is going backward."""
        if not url or not self.best_checkpoint_url:
            return False
        norm = self._normalize_url(url)
        best_norm = self._normalize_url(self.best_checkpoint_url)
        # Regression = revisiting an earlier URL when we've been deeper
        if norm == best_norm:
            return False
        return self.url_visit_counts.get(norm, 0) > 0

    def should_allow_screenshot(self, url: str) -> tuple[bool, str]:
        """Check if a screenshot should be allowed. Returns (allowed, reason).

        Vision-on-demand: allows screenshots when stuck (loop detected or
        many actions since last screenshot) even if URL was already captured.
        """
        if self.screenshot_budget <= 0:
            return False, "[Screenshot budget exhausted] Use browser_get_markdown or browser_eval instead."
        if self.actions_since_screenshot == 0:
            return False, "[No actions since last screenshot — reuse previous. Use browser_get_markdown to re-read content.]"
        # Vision-on-demand: skip URL dedup when stuck
        is_stuck = self.loop_detector.is_looping or self.actions_since_screenshot >= 8
        norm = self._normalize_url(url)
        if norm and norm in self.screenshotted_urls and not is_stuck:
            return False, f"[Screenshot already exists for this URL. Use browser_get_markdown or browser_eval to read page state instead.]"
        return True, ""

    def record_step(self, tool_name: str, args_summary: str, result_summary: str) -> None:
        """Record a step in the structured step history."""
        self.step_history.append({
            "tool": tool_name,
            "args": args_summary,
            "result": result_summary[:200],
            "url": self.current_url,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

    def get_last_checkpoint(self) -> dict | None:
        """Return the most recent checkpoint."""
        return self.checkpoints[-1] if self.checkpoints else None

    def export_step_history(self) -> str:
        """Export structured step history and checkpoint to disk."""
        lines = ["## Step History"]
        for i, step in enumerate(self.step_history, 1):
            lines.append(f"{i}. [{step['time']}] {step['tool']}({step['args']}) → {step['result']}")
            if step.get("url"):
                lines.append(f"   URL: {step['url']}")

        if self.checkpoints:
            lines.append("\n## Checkpoints (progress markers)")
            for cp in self.checkpoints:
                lines.append(f"- [{cp['time']}] {cp['action']} → {cp['url']}")

        lines.append(f"\n## Best checkpoint URL: {self.best_checkpoint_url or 'none'}")
        lines.append(f"## Regressions detected: {self.regression_count}")

        content = "\n".join(lines)

        # Write to task-specific directory
        task_dir = f"/tmp/superbrowser/{self.task_id}" if self.task_id else "/tmp/superbrowser"
        os.makedirs(task_dir, exist_ok=True)
        step_path = os.path.join(task_dir, "step_history.md")
        with open(step_path, "w") as f:
            f.write(content)
        print(f"  [step history saved: {step_path}]")

        # Save checkpoint as JSON for re-delegation
        if self.best_checkpoint_url:
            import json as _json
            checkpoint_data = {
                "url": self.best_checkpoint_url,
                "title": self.checkpoints[-1].get("title", "") if self.checkpoints else "",
                "regressions": self.regression_count,
            }
            cp_path = os.path.join(task_dir, "checkpoint.json")
            with open(cp_path, "w") as f:
                _json.dump(checkpoint_data, f)
            print(f"  [checkpoint saved: {cp_path}]")

        return content

    def log_activity(self, action: str, result: str = "ok"):
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {action}"
        if result != "ok":
            entry += f" → {result}"
        self.activity_log.append(entry)
        if len(self.activity_log) > 30:
            self.activity_log.pop(0)

    def get_activity_summary(self) -> str:
        if not self.activity_log:
            return ""
        lines = "\n".join(self.activity_log[-15:])
        return (
            f"\n--- Previous activity (DO NOT repeat failed approaches) ---\n"
            f"{lines}\n"
            f"--- Screenshots remaining: {self.screenshot_budget}/{self.MAX_SCREENSHOTS} | Sessions opened: {self.sessions_opened} ---"
        )

    def print_summary(self):
        elapsed = time.time() - self.start_time if self.start_time else 0
        used = self.MAX_SCREENSHOTS - self.screenshot_budget
        print(f"\n  [Session Summary]")
        print(f"  Duration: {elapsed:.1f}s | Sessions: {self.sessions_opened}")
        print(f"  Vision calls: {self.vision_calls} | Text calls: {self.text_calls} | Screenshots: {used}/{self.MAX_SCREENSHOTS}")
        est = self.vision_calls * 0.03 + self.text_calls * 0.002
        print(f"  Estimated cost: ~${est:.3f}")

    def export_activity_log(self) -> str:
        """Export structured activity log to disk for the orchestrator to read."""
        elapsed = time.time() - self.start_time if self.start_time else 0
        used = self.MAX_SCREENSHOTS - self.screenshot_budget

        lines = [
            f"## Browser Worker Activity",
            f"Duration: {elapsed:.1f}s | Screenshots: {used}/{self.MAX_SCREENSHOTS} | Tool calls: {self.vision_calls + self.text_calls}",
            "",
            "### Actions",
        ]
        lines.extend(self.activity_log)
        content = "\n".join(lines)

        # Write to disk so orchestrator/subagent can read it
        activity_path = "/tmp/superbrowser/last_activity.md"
        os.makedirs(os.path.dirname(activity_path), exist_ok=True)
        with open(activity_path, "w") as f:
            f.write(content)
        print(f"  [activity log saved: {activity_path}]")
        return content

    def save_screenshot(self, b64: str, label: str = "") -> str:
        self.step_counter += 1
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        fn = f"{self.step_counter:03d}-{label}.jpg" if label else f"{self.step_counter:03d}.jpg"
        path = os.path.join(SCREENSHOT_DIR, fn)
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"  [screenshot saved: {path}]")
        return path

    def build_image_blocks(self, b64: str, caption: str) -> list[dict]:
        self.vision_calls += 1
        self.actions_since_screenshot = 0
        label = caption.split("\n")[0][:30].replace(" ", "-").replace("/", "_")
        self.save_screenshot(b64, label)
        return [
            {"type": "text", "text": caption},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]

    def build_text_only(self, data: dict, prefix: str = "") -> str:
        self.action_count += 1
        self.text_calls += 1
        self.actions_since_screenshot += 1
        parts = [prefix]
        if data.get("url"):
            parts.append(f"Page: {data['url']}")
        if data.get("title"):
            parts.append(f"Title: {data['title']}")
        result = " | ".join(p for p in parts if p)
        # Auto-include interactive elements so agent knows what's on page
        # (BrowserOS pattern: every action returns updated element snapshot)
        if data.get("elements"):
            result += f"\n\nInteractive elements:\n{data['elements']}"
        else:
            result += "\n\n[No interactive elements found on page. Use browser_get_markdown to read page text, or browser_eval to inspect DOM.]"
        if data.get("consoleErrors"):
            result += f"\nConsole errors: {data['consoleErrors']}"
        if data.get("pendingDialogs"):
            result += f"\nPending dialogs: {data['pendingDialogs']}"
        if self.action_count >= 5:
            result += "\n\n[HINT: Use browser_run_script to batch remaining steps into ONE script.]"
        # Nudge the LLM to produce a non-empty response
        result += f"\n\n[Step {self.action_count}: Analyze the result above and decide your next action.]"
        return result


async def _fetch_elements(session_id: str) -> str:
    """Fetch current interactive elements without vision (cheap, no screenshot).

    This is the key BrowserOS pattern: every action gets a fresh element snapshot
    so the agent always knows what's on the page without wasting a screenshot.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "false"},
            )
            r.raise_for_status()
            data = r.json()
        return data.get("elements", "")
    except Exception:
        return ""


def _format_state(data: dict) -> str:
    parts = []
    if data.get("url"):
        parts.append(f"URL: {data['url']}")
    if data.get("title"):
        parts.append(f"Title: {data['title']}")
    if data.get("cookiesLoaded"):
        parts.append(f"Cookies: {data['cookiesLoaded']} auto-loaded (do NOT call browser_load_cookies)")
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


# ── Tool classes — each holds a reference to shared BrowserSessionState ──

@tool_parameters(
    tool_parameters_schema(
        url=StringSchema("URL to open (optional)", nullable=True),
        region=StringSchema("Region code for geo-restricted sites (e.g., 'bd', 'in')", nullable=True),
        proxy=StringSchema("Direct proxy URL (e.g., 'socks5://proxy:1080')", nullable=True),
        required=[],
    )
)
class BrowserOpenTool(Tool):
    name = "browser_open"
    description = (
        "Open a new browser session. Returns a screenshot and interactive elements. "
        "For geo-restricted sites, pass region='bd' (Bangladesh), 'in' (India), etc."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    MAX_OPENS = 3  # Hard cap on browser_open calls per worker

    async def execute(self, url: str | None = None, region: str | None = None, proxy: str | None = None, **kw: Any) -> Any:
        self.s.init_if_needed()
        self.s.reset_per_session()
        self.s.sessions_opened += 1

        # Hard cap: prevent browser_open loop
        if self.s.sessions_opened > self.MAX_OPENS:
            msg = (
                f"[ERROR: browser_open called {self.s.sessions_opened} times — limit is {self.MAX_OPENS}. "
                "Opening new sessions repeatedly will NOT fix the problem. "
                "Use the session you already have. Tools available: "
                "browser_run_script, browser_eval, browser_get_markdown, browser_click, browser_type. "
                "If the page requires login, report that back to the orchestrator instead of retrying.]"
            )
            self.s.log_activity("browser_open BLOCKED", f"exceeded {self.MAX_OPENS} limit")
            return msg

        print(f"\n>> browser_open(url={url}, region={region}) [session #{self.s.sessions_opened}, screenshots left: {self.s.screenshot_budget}]")

        payload: dict[str, Any] = {}
        if url:
            payload["url"] = url
        if region:
            payload["region"] = region
        if proxy:
            payload["proxy"] = proxy

        # After the first open, skip vision to preserve screenshot budget for actual work
        if self.s.sessions_opened > 1:
            payload["vision"] = False

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/create", json=payload)
            r.raise_for_status()
            data = r.json()

        actual_url = data.get("url", url or "")
        self.s.log_activity(f"browser_open({url or 'blank'})", f"session={data.get('sessionId', '?')}")
        self.s.record_url(actual_url)
        self.s.record_checkpoint(actual_url, data.get("title", ""), f"browser_open({url or 'blank'})")
        self.s.record_step("browser_open", url or "blank", f"session={data.get('sessionId', '?')}")
        self.s.consecutive_click_calls = 0

        caption = _format_state(data)
        caption = f"Session: {data['sessionId']}\n{caption}"

        # Show previous activity so agent knows what was already tried
        if self.s.sessions_opened > 1:
            activity = self.s.get_activity_summary()
            if activity:
                caption += activity
            caption += (
                "\n\n[WARNING: You already opened a session before. Do NOT call browser_open again. "
                "Work within THIS session using browser_run_script, browser_click, browser_type, "
                "browser_eval, or browser_get_markdown. If login is required, report back to orchestrator.]"
            )

        if data.get("screenshot") and self.s.screenshot_budget > 0:
            self.s.screenshot_budget -= 1
            if actual_url:
                self.s.screenshotted_urls.add(self.s._normalize_url(actual_url))
            return self.s.build_image_blocks(data["screenshot"], caption)
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID from browser_open"),
        url=StringSchema("URL to navigate to"),
        required=["session_id", "url"],
    )
)
class BrowserNavigateTool(Tool):
    name = "browser_navigate"
    description = "Navigate to a URL in an open browser session."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, url: str, **kw: Any) -> Any:
        print(f"\n>> browser_navigate({url})")
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0
        self.s.loop_detector.record_action("browser_navigate", {"url": url})

        # Detect regression before navigating
        regression = self.s.is_regression(url)
        if regression:
            self.s.regression_count += 1

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/navigate", json={"url": url})
            r.raise_for_status()
            data = r.json()

        actual_url = data.get("url", url)
        self.s.log_activity(f"navigate({url})", f"title={data.get('title', '?')}")
        self.s.record_url(actual_url)
        self.s.record_step("browser_navigate", url, f"title={data.get('title', '?')}")

        caption = _format_state(data)

        if regression:
            caption += "\n[WARNING: You already visited this URL. Fix your approach on the CURRENT page instead of going backward. Do NOT restart from the beginning.]"

        if data.get("screenshot") and self.s.screenshot_budget > 0:
            self.s.screenshot_budget -= 1
            if actual_url:
                self.s.screenshotted_urls.add(self.s._normalize_url(actual_url))
            return self.s.build_image_blocks(data["screenshot"], caption)
        return caption


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserScreenshotTool(Tool):
    name = "browser_screenshot"
    description = "Take a screenshot. COSTS MONEY. Use browser_get_markdown or browser_eval to verify instead."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> Any:
        allowed, reason = self.s.should_allow_screenshot(self.s.current_url)
        if not allowed:
            self.s.log_activity("screenshot(BLOCKED)", reason[:60])
            return reason

        self.s.screenshot_budget -= 1
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/state", params={"vision": "true"})
            r.raise_for_status()
            data = r.json()

        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.screenshotted_urls.add(self.s._normalize_url(actual_url))
        self.s.log_activity(f"screenshot({actual_url[:50] if actual_url else '?'})")
        self.s.record_step("browser_screenshot", "", f"url={actual_url[:60] if actual_url else '?'}")
        caption = _format_state(data)
        caption += f"\n[Screenshots remaining: {self.s.screenshot_budget}]"
        if data.get("screenshot"):
            return self.s.build_image_blocks(data["screenshot"], caption)
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index"),
        button=StringSchema("Mouse button: left, right, middle", nullable=True),
        required=["session_id", "index"],
    )
)
class BrowserClickTool(Tool):
    name = "browser_click"
    description = "Click an interactive element by its [index] number."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, button: str | None = None, **kw: Any) -> Any:
        print(f"\n>> browser_click([{index}])")
        self.s.consecutive_click_calls += 1
        self.s.loop_detector.record_action("browser_click", {"index": index})
        payload: dict[str, Any] = {"index": index}
        if button:
            payload["button"] = button

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/click", json=payload)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            # Fallback: try JS click via eval (BrowserOS pattern: CDP click → JS click fallback)
            print(f"  [click failed, trying JS fallback: {e}]")
            try:
                js_click = f"document.querySelectorAll('[data-index=\"{index}\"], [highlight-index=\"{index}\"]')[0]?.click(); 'clicked via JS'"
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.post(
                        f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                        json={"script": js_click},
                    )
                    r.raise_for_status()
                data = r.json()
                # Fetch state after JS click
                elements = await _fetch_elements(session_id)
                self.s.log_activity(f"click([{index}])(JS fallback)", "ok")
                self.s.record_step("browser_click", f"index={index}(JS)", "JS fallback succeeded")
                result = f"Clicked [{index}] (JS fallback)"
                if elements:
                    result += f"\n\nInteractive elements:\n{elements}"
                return result
            except Exception as e2:
                self.s.log_activity(f"click([{index}])(FAILED)", str(e2)[:60])
                return f"Click failed: {e}. JS fallback also failed: {e2}"

        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.record_url(actual_url)
        self.s.log_activity(f"click([{index}])", f"url={actual_url[:50] if actual_url else '?'}")
        self.s.record_step("browser_click", f"index={index}", f"url={actual_url[:60] if actual_url else '?'}")
        return self.s.build_text_only(data, f"Clicked [{index}]")


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        x=NumberSchema(description="X coordinate"),
        y=NumberSchema(description="Y coordinate"),
        required=["session_id", "x", "y"],
    )
)
class BrowserClickAtTool(Tool):
    name = "browser_click_at"
    description = "Click at x,y coordinates. LAST RESORT — prefer browser_run_script."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, x: float, y: float, **kw: Any) -> Any:
        self.s.click_at_count += 1
        self.s.consecutive_click_calls += 1
        self.s.loop_detector.record_action("browser_click_at", {"x": x, "y": y})
        if self.s.click_at_count > self.s.MAX_CLICK_AT:
            return f"[BLOCKED] browser_click_at used {self.s.click_at_count} times. Use browser_run_script instead."
        print(f"\n>> browser_click_at({x}, {y})")
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/click", json={"x": x, "y": y})
            r.raise_for_status()
            data = r.json()
        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.record_url(actual_url)
        self.s.record_step("browser_click_at", f"x={x},y={y}", f"url={actual_url[:60] if actual_url else '?'}")
        return self.s.build_text_only(data, f"Clicked at ({x}, {y})")


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index"),
        text=StringSchema("Text to type"),
        clear=BooleanSchema(description="Clear field first (default: true)", default=True),
        required=["session_id", "index", "text"],
    )
)
class BrowserTypeTool(Tool):
    name = "browser_type"
    description = "Type text into an input field by its [index] number."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, text: str, clear: bool = True, **kw: Any) -> Any:
        print(f'\n>> browser_type([{index}], "{text}")')
        self.s.consecutive_click_calls += 1  # type is also step-by-step
        self.s.loop_detector.record_action("browser_type", {"index": index, "text": text})
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/type", json={"index": index, "text": text, "clear": clear})
            r.raise_for_status()
            data = r.json()
        self.s.record_step("browser_type", f'index={index}, text="{text[:30]}"', "ok")
        return self.s.build_text_only(data, f'Typed "{text}" into [{index}]')


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        keys=StringSchema("Keys to send (e.g. Enter, ArrowDown, Tab)"),
        required=["session_id", "keys"],
    )
)
class BrowserKeysTool(Tool):
    name = "browser_keys"
    description = "Send keyboard keys or shortcuts."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, keys: str, **kw: Any) -> Any:
        print(f"\n>> browser_keys({keys})")
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/keys", json={"keys": keys})
            r.raise_for_status()
            data = r.json()
        # Fetch updated elements after key press (e.g., Enter may submit form)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id)
            if elements:
                data["elements"] = elements
        return self.s.build_text_only(data, f"Sent keys: {keys}")


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        direction=StringSchema("Scroll direction: up or down", nullable=True),
        percent=NumberSchema(description="Scroll to exact percentage 0-100", nullable=True),
        required=["session_id"],
    )
)
class BrowserScrollTool(Tool):
    name = "browser_scroll"
    description = "Scroll the page up or down, or to a specific percentage."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, direction: str | None = None, percent: float | None = None, **kw: Any) -> Any:
        print(f"\n>> browser_scroll({direction or f'{percent}%'})")
        self.s.loop_detector.record_action("browser_scroll", {"direction": direction or "down"})
        payload: dict[str, Any] = {}
        if percent is not None:
            payload["percent"] = percent
        else:
            payload["direction"] = direction or "down"
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/scroll", json=payload)
            r.raise_for_status()
            data = r.json()
        # Fetch updated elements after scroll (new elements may be visible)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id)
            if elements:
                data["elements"] = elements
        action = f"Scrolled to {percent}%" if percent is not None else f"Scrolled {direction or 'down'}"
        return self.s.build_text_only(data, action)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index of the select/dropdown"),
        value=StringSchema("Option value or visible text to select"),
        required=["session_id", "index", "value"],
    )
)
class BrowserSelectTool(Tool):
    name = "browser_select"
    description = "Select an option in a dropdown by value."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, value: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/select", json={"index": index, "value": value})
            r.raise_for_status()
            data = r.json()
        # Fetch updated elements after selection (may trigger form changes)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id)
            if elements:
                data["elements"] = elements
        return self.s.build_text_only(data, f'Selected "{value}" in [{index}]')


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        script=StringSchema("JavaScript code to execute in the page"),
        required=["session_id", "script"],
    )
)
class BrowserEvalTool(Tool):
    name = "browser_eval"
    description = "Execute JavaScript in the browser page. FREE — no screenshot cost."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, script: str, **kw: Any) -> str:
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0  # eval resets click loop tracking
        print(f"\n>> browser_eval({script[:60]}...)")
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/evaluate", json={"script": script})
            r.raise_for_status()
            data = r.json()
        result = data.get("result")
        result_str = json.dumps(result, indent=2, ensure_ascii=False)[:5000] if isinstance(result, (dict, list)) else str(result)[:5000]
        self.s.log_activity(f"eval({script[:40]}...)", result_str[:60])
        self.s.record_step("browser_eval", script[:60], result_str[:100])
        return result_str


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        script=StringSchema(
            "Puppeteer script body. Variables: page (Puppeteer Page), context, helpers (sleep, screenshot, log)."
        ),
        context=ObjectSchema(description="Optional context data", nullable=True),
        timeout=IntegerSchema(description="Script timeout in ms (default: 60000)", nullable=True),
        required=["session_id", "script"],
    )
)
class BrowserRunScriptTool(Tool):
    name = "browser_run_script"
    description = (
        "Execute a Puppeteer script with full page API access. "
        "Use for complex multi-step automation."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, script: str, context: dict | None = None, timeout: int | None = None, **kw: Any) -> str:
        print(f"\n>> browser_run_script({script[:80]}...)")
        self.s.consecutive_click_calls = 0  # script execution resets click loop tracking
        self.s.loop_detector.record_action("browser_run_script", {"script": script})
        payload: dict[str, Any] = {"code": script}
        if context:
            payload["context"] = context
        if timeout:
            payload["timeout"] = timeout

        client_timeout = max(120.0, (timeout or 60000) / 1000 + 10)
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/script", json=payload)
            r.raise_for_status()
            data = r.json()

        self.s.actions_since_screenshot += 1

        if not data.get("success"):
            error = data.get("error", "Unknown error")
            self.s.log_activity("run_script(FAILED)", error[:100])
            self.s.record_step("browser_run_script", script[:60], f"FAILED: {error[:100]}")
            # Fetch current elements so agent can see what's on the page and fix the script
            elements = await _fetch_elements(session_id)
            tip = "\n[TIP: Fix the script and retry in this SAME session. Do NOT navigate back to the start.]"
            if elements:
                tip += f"\n\nCurrent interactive elements:\n{elements}"
            return f"Script error: {error}{tip}"

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
        self.s.log_activity(f"run_script(ok, {duration}ms)", str(result)[:60] if result else "void")
        self.s.record_step("browser_run_script", script[:60], str(result)[:100] if result else "void")
        self.s.record_checkpoint(self.s.current_url, "", f"run_script(ok, {duration}ms)")

        # Auto-include updated elements so agent sees current page state
        elements = await _fetch_elements(session_id)
        if elements:
            parts.append(f"\nInteractive elements:\n{elements}")

        return "\n".join(parts)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        text=StringSchema("Text to wait for on the page", nullable=True),
        selector=StringSchema("CSS selector to wait for", nullable=True),
        timeout=IntegerSchema(description="Max wait time in seconds (default: 10)", nullable=True),
        required=["session_id"],
    )
)
class BrowserWaitForTool(Tool):
    name = "browser_wait_for"
    description = (
        "Wait for text or a CSS selector to appear on the page. "
        "Much better than blind helpers.sleep() — polls efficiently until the condition is met. "
        "Provide either 'text' or 'selector' (not both). FREE — no screenshot cost."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        text: str | None = None,
        selector: str | None = None,
        timeout: int | None = None,
        **kw: Any,
    ) -> str:
        if not text and not selector:
            return "Error: provide either 'text' or 'selector' parameter."

        timeout_s = timeout or 10
        label = f'text="{text}"' if text else f'selector="{selector}"'
        print(f"\n>> browser_wait_for({label}, timeout={timeout_s}s)")

        if text:
            script = f"""
                const deadline = Date.now() + {timeout_s * 1000};
                while (Date.now() < deadline) {{
                    if (document.body.innerText.includes({json.dumps(text)})) {{
                        return {{found: true, title: document.title, url: location.href}};
                    }}
                    await new Promise(r => setTimeout(r, 500));
                }}
                return {{found: false, title: document.title, url: location.href, bodyPreview: document.body.innerText.substring(0, 200)}};
            """
        else:
            script = f"""
                const deadline = Date.now() + {timeout_s * 1000};
                while (Date.now() < deadline) {{
                    if (document.querySelector({json.dumps(selector)})) {{
                        return {{found: true, title: document.title, url: location.href}};
                    }}
                    await new Promise(r => setTimeout(r, 500));
                }}
                return {{found: false, title: document.title, url: location.href, bodyPreview: document.body.innerText.substring(0, 200)}};
            """

        client_timeout = max(30.0, timeout_s + 10)
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/script",
                json={"code": script, "timeout": timeout_s * 1000 + 5000},
            )
            r.raise_for_status()
            data = r.json()

        if not data.get("success"):
            self.s.log_activity(f"wait_for({label})", f"script error: {data.get('error', '?')[:60]}")
            return f"Wait failed (script error): {data.get('error', 'unknown')}"

        result = data.get("result", {})
        if result.get("found"):
            self.s.log_activity(f"wait_for({label})", "found")
            # Fetch updated elements
            elements = await _fetch_elements(session_id)
            response = f"Found! Page: {result.get('url', '?')} | Title: {result.get('title', '?')}"
            if elements:
                response += f"\n\nInteractive elements:\n{elements}"
            return response
        else:
            self.s.log_activity(f"wait_for({label})", f"timeout after {timeout_s}s")
            return (
                f"Not found after {timeout_s}s. "
                f"Page: {result.get('url', '?')} | Title: {result.get('title', '?')}\n"
                f"Page preview: {result.get('bodyPreview', 'N/A')}"
            )


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserGetMarkdownTool(Tool):
    name = "browser_get_markdown"
    description = "Extract page content as markdown. FREE — no screenshot cost."

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
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/dialog", json=payload)
            r.raise_for_status()
        return f"Dialog {'accepted' if accept else 'dismissed'}"


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserCloseTool(Tool):
    name = "browser_close"
    description = "Close the browser session and free resources. Always close when done."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, **kw: Any) -> str:
        print(f"\n>> browser_close({session_id})")
        self.s.log_activity(f"close({session_id})")
        self.s.print_summary()
        self.s.export_activity_log()
        self.s.export_step_history()
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(f"{SUPERBROWSER_URL}/session/{session_id}")
            r.raise_for_status()
        used = self.s.MAX_SCREENSHOTS - self.s.screenshot_budget
        return f"Session closed. Vision: {self.s.vision_calls}, Text: {self.s.text_calls}, Screenshots: {used}/{self.s.MAX_SCREENSHOTS}, Regressions: {self.s.regression_count}"


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserDetectCaptchaTool(Tool):
    name = "browser_detect_captcha"
    description = "Check if the page has a captcha."

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
            return "No captcha detected."
        return f"Captcha detected: type={captcha['type']}, siteKey={captcha.get('siteKey', 'N/A')}"


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserCaptchaScreenshotTool(Tool):
    name = "browser_captcha_screenshot"
    description = "Take a close-up screenshot of the captcha area."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/captcha/screenshot")
            if r.status_code == 404:
                return "No captcha area found."
            r.raise_for_status()
        b64 = base64.b64encode(r.content).decode()
        return self.s.build_image_blocks(b64, "Captcha area — analyze to solve")


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        method=StringSchema("Solving method: 'auto', 'token', 'ai_vision', 'grid'", nullable=True),
        provider=StringSchema("Captcha solver: '2captcha' or 'anticaptcha'", nullable=True),
        api_key=StringSchema("API key for solver service", nullable=True),
        required=["session_id"],
    )
)
class BrowserSolveCaptchaTool(Tool):
    name = "browser_solve_captcha"
    description = "Solve a detected captcha automatically."

    async def execute(self, session_id: str, method: str | None = None, provider: str | None = None, api_key: str | None = None, **kw: Any) -> str:
        payload: dict[str, Any] = {}
        if method:
            payload["method"] = method
        if provider:
            payload["provider"] = provider
        if api_key:
            payload["apiKey"] = api_key
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/captcha/solve", json=payload)
            r.raise_for_status()
            data = r.json()
        if data.get("solved"):
            return f"Captcha solved via {data.get('method', '?')} ({data.get('attempts', 1)} attempt(s))"
        return f"Captcha not solved: {data.get('error', 'all methods failed')}"


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        question=StringSchema("What to ask the user"),
        input_type=StringSchema("Type: credentials, captcha, confirmation, otp, text, choice", nullable=True),
        required=["session_id", "question"],
    )
)
class BrowserAskUserTool(Tool):
    name = "browser_ask_user"
    description = "Ask the user a question and wait for their response."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, question: str, input_type: str | None = None, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/state", params={"vision": "true"})
            r.raise_for_status()
            data = r.json()
        caption = f"[Browser needs your input]\n\n{question}"
        if data.get("url"):
            caption += f"\nCurrent page: {data['url']}"
        if data.get("screenshot"):
            return self.s.build_image_blocks(data["screenshot"], caption)
        return caption


COOKIE_DIR = os.environ.get("COOKIE_DIR", "/tmp/superbrowser/cookies")


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        domain=StringSchema("Domain to save cookies for (e.g., 'openrouter.ai'). If omitted, saves all cookies.", nullable=True),
        required=["session_id"],
    )
)
class BrowserSaveCookiesTool(Tool):
    name = "browser_save_cookies"
    description = (
        "Save browser cookies to disk for future sessions. "
        "Call this after a successful login to persist authentication. "
        "Cookies are saved per-domain and auto-loaded in future sessions."
    )

    async def execute(self, session_id: str, domain: str | None = None, **kw: Any) -> str:
        print(f"\n>> browser_save_cookies(domain={domain})")
        # Get cookies from the session
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/cookies")
            r.raise_for_status()
            data = r.json()

        cookies = data.get("cookies", [])
        if not cookies:
            return "No cookies found in this session."

        # Filter by domain if specified
        if domain:
            domain_clean = domain.replace("www.", "").lower()
            cookies = [c for c in cookies if domain_clean in (c.get("domain", "").replace(".", "").lower())]
            if not cookies:
                return f"No cookies found for domain '{domain}'."

        # Group by domain and save
        os.makedirs(COOKIE_DIR, exist_ok=True)
        by_domain: dict[str, list] = {}
        for c in cookies:
            d = (c.get("domain", "")).lstrip(".").lower()
            if not d:
                continue
            if d not in by_domain:
                by_domain[d] = []
            by_domain[d].append(c)

        saved_domains = []
        for d, domain_cookies in by_domain.items():
            safe_name = d.replace("/", "_").replace(":", "_")
            cookie_path = os.path.join(COOKIE_DIR, f"{safe_name}.json")
            with open(cookie_path, "w") as f:
                json.dump(domain_cookies, f, indent=2)
            saved_domains.append(d)

        return f"Saved {len(cookies)} cookies for domains: {', '.join(saved_domains)}. These will be auto-loaded in future sessions."


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        domain=StringSchema("Domain to load cookies for (e.g., 'openrouter.ai')"),
        required=["session_id", "domain"],
    )
)
class BrowserLoadCookiesTool(Tool):
    name = "browser_load_cookies"
    description = (
        "Load saved cookies into the browser session to restore authentication. "
        "Call this right after browser_open and BEFORE navigating to the site. "
        "Cookies are loaded from disk (saved by a previous browser_save_cookies call)."
    )

    async def execute(self, session_id: str, domain: str, **kw: Any) -> str:
        print(f"\n>> browser_load_cookies(domain={domain})")
        # Try to load from disk
        safe_name = domain.replace("/", "_").replace(":", "_").lower()
        cookie_path = os.path.join(COOKIE_DIR, f"{safe_name}.json")

        if not os.path.exists(cookie_path):
            # Try the SuperBrowser cookie API
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.get(f"{SUPERBROWSER_URL}/cookies/load/{safe_name}")
                    r.raise_for_status()
                    data = r.json()
                    cookies = data.get("cookies", [])
                    if cookies:
                        # Inject into session
                        async with httpx.AsyncClient(timeout=10.0) as client2:
                            r2 = await client2.post(
                                f"{SUPERBROWSER_URL}/session/{session_id}/cookies",
                                json={"cookies": cookies},
                            )
                            r2.raise_for_status()
                        return f"Loaded {len(cookies)} cookies for {domain} from server cookie store."
            except Exception:
                pass
            return f"No saved cookies found for '{domain}'. User needs to log in first."

        with open(cookie_path) as f:
            cookies = json.load(f)

        if not cookies:
            return f"Cookie file exists but is empty for '{domain}'."

        # Inject cookies into the session
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/cookies",
                json={"cookies": cookies},
            )
            r.raise_for_status()

        return f"Loaded {len(cookies)} cookies for {domain}. Navigate to the site now — you should be authenticated."


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        url=StringSchema("URL of the site to log into"),
        required=["session_id", "url"],
    )
)
class BrowserAuthSetupTool(Tool):
    name = "browser_auth_setup"
    description = (
        "ONLY for bot protection: Cloudflare challenge, PerimeterX 'Press & Hold', hCaptcha, "
        "reCAPTCHA. Hands the browser to the user to solve, then returns updated page state. "
        "NEVER use for: cookie consent, country selectors, age gates, newsletter popups, "
        "login forms, paywalls, or any normal overlay — click those yourself with browser_click. "
        "This tool BLOCKS until done. After it returns, CONTINUE in the SAME session."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, url: str, **kw: Any) -> Any:
        import asyncio

        print(f"\n>> browser_auth_setup(url={url}, session={session_id})")

        # GUARD: Check if the page actually has bot protection before handing to user.
        # If it just has normal overlays, REFUSE and tell the worker to click through.
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/state")
                r.raise_for_status()
                guard_data = r.json()
            page_title = (guard_data.get("title", "") or "").lower()
            page_elements = (guard_data.get("elements", "") or "").lower()
            page_url = (guard_data.get("url", "") or "").lower()

            # Actual bot protection indicators
            is_bot_protection = (
                "just a moment" in page_title
                or "verify you are human" in page_title
                or "attention required" in page_title
                or "checking your browser" in page_title
                or "press & hold" in page_elements
                or "press and hold" in page_elements
                or "challenges.cloudflare.com" in page_url
                or "cdn-cgi/challenge-platform" in page_url
                or ("access denied" in page_elements and "reference id" in page_elements)
                or ("access to this page has been denied" in page_elements and "reference id" in page_elements)
            )

            if not is_bot_protection:
                # This is NOT bot protection — it's a normal overlay or website issue.
                # Tell the worker to handle it itself.
                print(f">> browser_auth_setup BLOCKED — page is not bot protection (title: {page_title[:60]})")

                # Try server-side dismiss one more time
                try:
                    await client.post(
                        f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                        json={"expression": "window.__dismissAttempted = true"},
                        timeout=5.0,
                    )
                except Exception:
                    pass

                overlay_hints = []
                if "accept" in page_elements or "cookie" in page_elements or "consent" in page_elements:
                    overlay_hints.append("cookie consent — look for an 'Accept' or 'I agree' button and click it")
                if "stay" in page_elements or "country" in page_elements or "location" in page_elements:
                    overlay_hints.append("country/locale selector — look for 'Yes, stay' or 'Continue' button and click it")
                if "newsletter" in page_elements or "subscribe" in page_elements or "email" in page_elements:
                    overlay_hints.append("newsletter popup — look for a close/X button or 'No thanks' and click it")
                if "age" in page_elements or "18" in page_elements or "21" in page_elements:
                    overlay_hints.append("age gate — look for 'I am over 18' or 'Enter' button and click it")

                hint_text = ""
                if overlay_hints:
                    hint_text = "\nDetected overlays:\n" + "\n".join(f"  - {h}" for h in overlay_hints)

                return (
                    f"[REFUSED: This is NOT bot protection — do NOT call browser_auth_setup for normal overlays.]\n"
                    f"The page has a normal website overlay/modal. Handle it yourself:\n"
                    f"1. Look at the elements list for buttons like 'Accept', 'Yes, stay', 'Continue', 'Close', 'No thanks'\n"
                    f"2. Use browser_click(index=N, session_id='{session_id}') to click the right button\n"
                    f"3. Then continue with your task in this same session\n"
                    f"{hint_text}"
                )
        except Exception:
            pass  # If guard check fails, fall through to normal auth flow

        # Get the current page state before handing to user (to detect changes)
        initial_title = ""
        initial_url = ""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/state")
                r.raise_for_status()
                initial_data = r.json()
                initial_title = initial_data.get("title", "")
                initial_url = initial_data.get("url", "")
        except Exception:
            pass

        auth_url = f"http://localhost:3100/auth-ui/{session_id}"
        print(f">> Waiting for user to complete auth at: {auth_url}")

        # Tell the user to solve the challenge — this message goes to the LLM output
        # which the orchestrator relays to the user
        user_msg = (
            f"[HUMAN ACTION REQUIRED]\n\n"
            f"The browser hit a bot protection screen. Please solve it:\n"
            f"  {auth_url}\n\n"
            f"1. Click the link above to see the live browser\n"
            f"2. Solve the captcha / Press & Hold / log in\n"
            f"3. Click 'Save Cookies & Done' when the real page loads\n\n"
            f"Waiting for you to finish..."
        )

        # Poll until the user signals done (via auth-ui button) or page changes
        max_wait = 180  # 3 minutes for user to solve
        poll_interval = 2  # check every 2 seconds
        start = time.time()

        while time.time() - start < max_wait:
            await asyncio.sleep(poll_interval)

            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    # Check the human-done signal first (fastest path)
                    try:
                        hd = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/human-done")
                        if hd.status_code == 200 and hd.json().get("done"):
                            # User clicked "Save Cookies & Done" — page is reloading
                            # Wait a moment for the page reload to complete
                            await asyncio.sleep(3)
                            # Fetch updated state
                            r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/state")
                            r.raise_for_status()
                            current = r.json()
                            current_title = current.get("title", "")
                            current_url = current.get("url", "")

                            elapsed = int(time.time() - start)
                            print(f">> User signaled done in {elapsed}s — page: {current_title}")
                            # Fall through to the success handler below
                            title_changed = True
                            url_changed = True
                            still_blocked = False
                        else:
                            # Also check page state for changes
                            r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/state")
                            r.raise_for_status()
                            current = r.json()
                            current_title = current.get("title", "")
                            current_url = current.get("url", "")
                            title_changed = current_title and current_title != initial_title
                            url_changed = current_url and current_url != initial_url
                            lower_title = current_title.lower()
                            elements_text = (current.get("elements", "") or "").lower()
                            still_blocked = (
                                "press & hold" in elements_text
                                or "press and hold" in elements_text
                                or "just a moment" in lower_title
                                or "access denied" in lower_title
                                or "verify you are human" in lower_title
                                or "access to this page has been denied" in elements_text
                            )
                    except Exception:
                        continue

                if (title_changed or url_changed) and not still_blocked:
                    elapsed = int(time.time() - start)
                    print(f">> User completed auth in {elapsed}s — page changed to: {current_title}")

                    # Auto-save cookies for this domain
                    try:
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            cr = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/cookies")
                            cr.raise_for_status()
                            cookies = cr.json().get("cookies", [])
                            if cookies:
                                await client.post(
                                    f"{SUPERBROWSER_URL}/cookies/save",
                                    json={"cookies": cookies},
                                )
                                print(f">> Auto-saved {len(cookies)} cookies after auth")
                    except Exception:
                        pass

                    # Return updated page state — worker continues in THIS session
                    self.s.record_url(current_url)
                    self.s.record_step("browser_auth_setup", url, f"user solved, now on: {current_url}")
                    self.s.log_activity("auth_complete", f"{current_url} ({elapsed}s)")

                    # Get full state with elements for the worker to use
                    try:
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            r = await client.get(
                                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                                params={"vision": "true"},
                            )
                            r.raise_for_status()
                            data = r.json()
                        caption = f"[Auth complete — continue in this session]\n"
                        caption += f"Session: {session_id}\n"
                        caption += _format_state(data)
                        if data.get("screenshot") and self.s.screenshot_budget > 0:
                            self.s.screenshot_budget -= 1
                            return self.s.build_image_blocks(data["screenshot"], caption)
                        return caption
                    except Exception:
                        return f"[Auth complete] Page is now: {current_title} ({current_url}). Continue using session {session_id}."

            except Exception:
                pass  # Network errors during polling — just retry

        # Timeout — user didn't solve in time
        return (
            f"[Auth timeout — user did not complete within {max_wait}s]\n"
            f"Auth link was: {auth_url}\n"
            f"Report this to the orchestrator."
        )


def register_session_tools(bot: "Nanobot", state: BrowserSessionState | None = None) -> BrowserSessionState:
    """Register all browser session tools with a nanobot instance.

    Args:
        bot: The Nanobot instance to register tools on.
        state: Optional shared state. If None, creates a new one.

    Returns:
        The BrowserSessionState used (for external access if needed).
    """
    if state is None:
        state = BrowserSessionState()

    tools = [
        BrowserOpenTool(state),
        BrowserNavigateTool(state),
        BrowserScreenshotTool(state),
        BrowserClickTool(state),
        BrowserClickAtTool(state),
        BrowserTypeTool(state),
        BrowserKeysTool(state),
        BrowserScrollTool(state),
        BrowserSelectTool(state),
        BrowserEvalTool(state),
        BrowserRunScriptTool(state),
        BrowserWaitForTool(state),
        BrowserGetMarkdownTool(),        # stateless
        BrowserDialogTool(),             # stateless
        BrowserDetectCaptchaTool(),      # stateless
        BrowserCaptchaScreenshotTool(state),
        BrowserSolveCaptchaTool(),       # stateless
        BrowserAskUserTool(state),
        BrowserCloseTool(state),
        BrowserSaveCookiesTool(),        # stateless
        BrowserLoadCookiesTool(),        # stateless
        BrowserAuthSetupTool(state),     # needs state for session continuity
    ]
    for tool in tools:
        bot._loop.tools.register(tool)
    return state
