"""
Orchestrator tools for the two-agent architecture.

The orchestrator delegates browser work to a fresh browser worker instance,
manages site-specific learnings, and never touches browser tools directly.
"""

from __future__ import annotations

import json as _json
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from superbrowser_bridge.plan_tracker import PlanTracker

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

# Where the browser worker workspace lives (relative to this file)
_BASE = Path(__file__).resolve().parent.parent
BROWSER_WORKSPACE = str(_BASE / "workspace_browser")
LEARNINGS_DIR = str(_BASE / "workspace_orchestrator" / "learnings")

SUPERBROWSER_URL = os.environ.get("SUPERBROWSER_URL", "http://localhost:3100")


def _domain_from_url(url: str) -> str:
    """Extract domain for learnings filename."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        return parsed.hostname or "unknown"
    except Exception:
        return "unknown"


def _learnings_path(domain: str) -> str:
    os.makedirs(LEARNINGS_DIR, exist_ok=True)
    safe = domain.replace("/", "_").replace(":", "_")
    return os.path.join(LEARNINGS_DIR, f"{safe}.md")


@tool_parameters(
    tool_parameters_schema(
        instructions=StringSchema(
            "Specific browser task instructions for the worker. "
            "Be detailed: include URL, what to click, what to fill, what to extract."
        ),
        url=StringSchema("Starting URL for the task", nullable=True),
        required=["instructions"],
    )
)
class DelegateBrowserTaskTool(Tool):
    """Delegate a browser task to the worker agent.

    Creates a fresh browser worker Nanobot with isolated state,
    runs the task, and returns the result. Each call gets a clean
    session with no history pollution.
    """

    name = "delegate_browser_task"
    description = (
        "Send a browser task to the worker agent. The worker opens a fresh browser, "
        "executes scripts, and returns the result. Write SPECIFIC instructions."
    )

    @property
    def exclusive(self) -> bool:
        return True

    # Bot protection cookie names that indicate a successful bypass
    _PROTECTION_COOKIES = {"cf_clearance", "_px", "_pxhd", "_pxvid", "_pxde", "__cf_bm"}

    async def _warmup_if_needed(self, url: str) -> bool:
        """Pre-solve bot protection (Cloudflare, PerimeterX, etc.) before the worker starts.

        Creates a temporary session, navigates to the URL (which triggers
        waitForCloudflare + waitForPerimeterX server-side), saves cookies,
        and closes the session. Returns True if protection cookies were obtained.
        """
        domain = _domain_from_url(url)
        cookie_dir = os.environ.get("COOKIE_DIR", "/tmp/superbrowser/cookies")
        cookie_path = os.path.join(cookie_dir, f"{domain}.json")

        # Check if we already have protection cookies
        if os.path.exists(cookie_path):
            try:
                with open(cookie_path) as f:
                    cookies = _json.load(f)
                existing = {c.get("name") for c in cookies}
                found = existing & self._PROTECTION_COOKIES
                if found:
                    print(f"[warmup] Protection cookies exist for {domain}: {found}, skipping warm-up")
                    return True
            except (ValueError, KeyError):
                pass

        print(f"[warmup] No protection cookies for {domain}, attempting warm-up...")
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                # Create session + navigate (triggers waitForCloudflare + waitForPerimeterX)
                r = await client.post(
                    f"{SUPERBROWSER_URL}/session/create",
                    json={"url": url, "vision": False},
                )
                data = r.json()
                session_id = data.get("sessionId")
                if not session_id:
                    print(f"[warmup] Failed to create warm-up session")
                    return False

                # Force-save cookies via the cookies API
                cookies = []
                try:
                    cr = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/cookies")
                    cookies = cr.json().get("cookies", [])
                    if cookies:
                        await client.post(
                            f"{SUPERBROWSER_URL}/cookies/save",
                            json={"cookies": cookies},
                        )
                except Exception:
                    pass

                # Close the warm-up session
                await client.delete(f"{SUPERBROWSER_URL}/session/{session_id}")

                cookie_names = {c.get("name") for c in cookies}
                found = cookie_names & self._PROTECTION_COOKIES
                if found:
                    print(f"[warmup] Protection cookies obtained for {domain}: {found}")
                    return True
                else:
                    print(f"[warmup] Warm-up completed, {len(cookies)} cookies saved (no protection cookies found)")
                    return len(cookies) > 0  # Still useful — session cookies etc.

        except Exception as e:
            print(f"[warmup] Warm-up failed: {e}")
            return False

    async def execute(self, instructions: str, url: str | None = None, **kw: Any) -> str:
        from nanobot import Nanobot
        from superbrowser_bridge.session_tools import BrowserSessionState, register_session_tools
        from superbrowser_bridge.worker_hook import BrowserWorkerHook

        task_id = uuid.uuid4().hex[:8]
        session_key = f"worker:{task_id}"
        max_iterations = 25

        print(f"\n>> delegate_browser_task(session={session_key})")
        print(f"   Instructions: {instructions[:120]}...")

        # Pre-solve bot protection (CF, PerimeterX, etc.) before the worker starts
        if url:
            await self._warmup_if_needed(url)

        # Create a FRESH browser worker — isolated state, clean session
        worker = Nanobot.from_config(workspace=BROWSER_WORKSPACE)

        # Remove default nanobot tools — worker only needs browser tools
        default_tools_to_remove = [
            "web_search", "web_fetch",
            "read_file", "write_file", "edit_file",
            "list_dir", "glob", "grep",
            "exec", "spawn", "cron", "message",
        ]
        for name in default_tools_to_remove:
            worker._loop.tools.unregister(name)

        # Register ALL browser tools with isolated state
        worker_state = BrowserSessionState()
        worker_state.MAX_SCREENSHOTS = 4  # base budget; +2 bonus when stuck (vision-on-demand)
        worker_state.task_id = task_id
        register_session_tools(worker, worker_state)

        # Parse task into structured steps for plan-guided execution
        plan_tracker = PlanTracker()
        task_steps = self._parse_task_steps(instructions, url)
        if task_steps:
            plan_tracker.set_plan(task_steps)

        # Create mid-session guardrail hook with plan tracking
        worker_hook = BrowserWorkerHook(
            worker_state, max_iterations=max_iterations, plan=plan_tracker,
        )

        # 25 iterations — enough for: open + inspect + script + fail + retry + verify + close
        worker._loop.max_iterations = max_iterations

        # Build the worker prompt with enforced workflow structure
        parts = []

        if url:
            parts.append(f"Target URL: {url}")

        parts.append(f"\n## Task\n{instructions}")

        # Auto-inject learnings so the worker follows known patterns
        domain = _domain_from_url(url) if url else None
        if domain:
            lpath = _learnings_path(domain)
            if os.path.exists(lpath):
                with open(lpath) as f:
                    learnings = f.read().strip()
                if learnings:
                    parts.append(f"\n## Site Learnings (from past tasks — FOLLOW THESE)\n{learnings}")

        # Note: cookies are auto-loaded by the server at session creation time.
        # No need to tell the worker to call browser_load_cookies manually.
        if domain:
            cookie_dir = os.environ.get("COOKIE_DIR", "/tmp/superbrowser/cookies")
            cookie_path = os.path.join(cookie_dir, f"{domain}.json")
            if os.path.exists(cookie_path):
                parts.append(
                    f"\n## Cookies Auto-Loaded\n"
                    f"Saved cookies for {domain} will be injected automatically when you call browser_open. "
                    f"Do NOT call browser_load_cookies — it is already done. "
                    f"Just open the URL and you should be authenticated."
                )

        # Check for checkpoint from a previous failed worker attempt
        last_checkpoint_path = "/tmp/superbrowser/last_checkpoint.json"
        if os.path.exists(last_checkpoint_path):
            try:
                with open(last_checkpoint_path) as f:
                    checkpoint = _json.load(f)
                if checkpoint.get("url"):
                    parts.append(
                        f"\n## Resume From Checkpoint\n"
                        f"A previous worker made progress to: {checkpoint['url']}\n"
                        f"Start from this URL instead of the beginning. "
                        f"Call browser_open with this URL directly.\n"
                        f"Do NOT repeat the steps that led to this point."
                    )
            except (ValueError, KeyError):
                pass

        # Enforce workflow IN the prompt itself (not just SOUL.md)
        parts.append("""
## Required Execution Plan (follow this order)
Step 1: browser_open(url) — open the TARGET URL (returns screenshot + interactive elements list)
  IMPORTANT: Always use the exact target URL, NEVER navigate to a /sign-in or /login page directly.
  If cookies are loaded, the target URL should work. If it redirects to login, report back — do NOT try to fix it.
  IF YOU SEE A POPUP/OVERLAY (cookie consent, country selector, age gate, newsletter):
    → These are normal website modals. Click them yourself using browser_click or browser_run_script.
    → Cookie consent: click "Accept all", "I agree", "OK" button
    → Country/locale: click "Yes, stay on ...", "Continue", or the confirmation button
    → Age gate: click "I am over 18", "Enter site", "Yes" button
    → Newsletter/promo: click the X/close button, or "No thanks"
    → Do NOT call browser_auth_setup for these. NEVER.
  ONLY call browser_auth_setup for actual BOT PROTECTION:
    - Cloudflare "Just a moment" / "Verify you are human" with NO clickable buttons
    - PerimeterX "Press & Hold" with a reference ID
    - A completely blank page or 403 with a captcha widget
  When you DO call browser_auth_setup, it BLOCKS until the user solves it, then returns updated page state.
  After it returns, CONTINUE in the SAME session — do NOT call browser_open again.
Step 2: Read the elements list from Step 1 — you already have the selectors, no need for a separate browser_eval
Step 3: browser_run_script — write ONE script that does ALL form filling + submission + data extraction
  Use the element indices/selectors from Step 1. Use browser_wait_for instead of blind helpers.sleep().
  For autocomplete: page.type(field, text, {{delay:100}}) then browser_wait_for(text="suggestion") then click suggestion
Step 4: If script failed, read the error AND the updated elements list in the error response. Fix the script and run browser_run_script again IN THE SAME SESSION — do NOT navigate back to the start
Step 5: browser_get_markdown or browser_eval — read the results (or just read the elements returned by Step 3)
Step 6: browser_close

KEY FEATURES:
- Every action (click, type, scroll, run_script) returns updated interactive elements automatically — you don't need screenshots to see what changed
- browser_wait_for(session_id, text="...", timeout=10) waits for content to appear — MUCH better than helpers.sleep()
- browser_click has JS fallback if standard click fails

CRITICAL RULES:
- Prefer browser_run_script for multi-step interactions. Use browser_click/browser_type only for simple one-off actions.
- If a script fails, FIX IT and retry on the current page. Do NOT navigate backward.
- You have {max_iter} iterations total. After iteration 15, use browser_run_script ONLY. After iteration 20, return whatever data you have.
- If you see [GUIDANCE: ...] messages, follow them IMMEDIATELY.
- Partial results are better than no results.
- Do NOT call browser_open more than once. If the page redirects to a login/sign-in page, STOP and report back: "LOGIN REQUIRED: [site] requires authentication." Do NOT retry browser_open — the orchestrator will handle auth.""".format(max_iter=max_iterations))

        prompt = "\n".join(parts)

        try:
            result = await worker.run(prompt, session_key=session_key, hooks=[worker_hook])
            content = result.content

            print(f"\n>> Worker result ({len(content)} chars): {content[:200]}...")

            # Read the activity log the worker saved on close
            task_dir = f"/tmp/superbrowser/{task_id}"
            activity_path = "/tmp/superbrowser/last_activity.md"
            if os.path.exists(activity_path):
                with open(activity_path) as f:
                    activity = f.read().strip()
                if activity:
                    content += f"\n\n[Worker Activity Log]\n{activity}"

            # Read step history for structured analysis
            step_path = os.path.join(task_dir, "step_history.md")
            if os.path.exists(step_path):
                with open(step_path) as f:
                    steps = f.read().strip()
                if steps:
                    content += f"\n\n[Worker Step History]\n{steps}"

            # Copy checkpoint for potential re-delegation
            cp_path = os.path.join(task_dir, "checkpoint.json")
            if os.path.exists(cp_path):
                import shutil
                shutil.copy2(cp_path, "/tmp/superbrowser/last_checkpoint.json")

            # Detect auth/block results from the worker.
            # The worker should now handle captchas INLINE via browser_auth_setup
            # (which blocks until the user solves it). Only fall back to creating
            # a separate auth session if the worker couldn't handle it inline.
            login_keywords = ["login required", "log in", "sign in", "not logged in",
                              "session has expired", "authentication"]
            block_keywords = ["access denied", "403 forbidden", "readtimeout",
                              "press & hold", "press and hold"]
            content_lower = content.lower()
            has_login_signal = any(kw in content_lower for kw in login_keywords)
            has_block_signal = any(kw in content_lower for kw in block_keywords)
            # If the worker already solved auth inline, it won't have these signals
            auth_was_handled = "auth complete" in content_lower or "auth_complete" in content_lower

            if (has_login_signal or has_block_signal) and url and not auth_was_handled:
                domain_check = _domain_from_url(url)
                cookie_dir = os.environ.get("COOKIE_DIR", "/tmp/superbrowser/cookies")
                cookie_path = os.path.join(cookie_dir, f"{domain_check}.json")
                if os.path.exists(cookie_path):
                    content += (
                        f"\n\n[AUTH NOTE: Saved cookies for {domain_check} were auto-loaded but "
                        f"the site still requires login. The cookies may be expired or incomplete. "
                        f"Ask the user to log in again via the auth-ui.]"
                    )
                else:
                    content = await self._create_auth_session(content, url)

            return content

        except Exception as e:
            error_msg = f"Browser worker failed: {e}"
            print(f"\n>> Worker error: {error_msg}")
            return error_msg

    @staticmethod
    def _parse_task_steps(instructions: str, url: str | None) -> list[str]:
        """Parse task instructions into discrete execution steps.

        Extracts numbered steps from the instructions, or generates
        a default step sequence for common task patterns.
        """
        steps = []

        # Try to extract numbered steps from instructions
        # Match patterns like "1. Do X", "1) Do X", "Step 1: Do X"
        numbered = re.findall(
            r'(?:^|\n)\s*(?:step\s*)?(\d+)[.):]\s*(.+?)(?=\n\s*(?:step\s*)?\d+[.):]\s|\n\n|\Z)',
            instructions,
            re.IGNORECASE | re.DOTALL,
        )
        if numbered:
            for _, text in numbered:
                step_text = text.strip().split("\n")[0].strip()  # First line only
                if len(step_text) > 10:  # Skip very short fragments
                    steps.append(step_text)

        if steps:
            # Prepend browser_open step if not already included
            first_lower = steps[0].lower()
            if "open" not in first_lower and "navigate" not in first_lower and "go to" not in first_lower:
                open_step = f"Open {url}" if url else "Open the target URL"
                steps.insert(0, open_step)
            # Append close step
            steps.append("Extract results and close browser")
            return steps

        # No numbered steps found — generate default sequence from task
        # Truncate instructions to first 200 chars for step description
        task_summary = instructions.strip()[:200].split("\n")[0]
        default_steps = []
        if url:
            default_steps.append(f"Open {url}")
        else:
            default_steps.append("Open the target URL")
        default_steps.append(f"Execute task: {task_summary}")
        default_steps.append("Extract results using browser_get_markdown or browser_eval")
        default_steps.append("Close browser and return results")
        return default_steps

    async def _create_auth_session(self, worker_content: str, url: str) -> str:
        """Create a fresh long-lived session for the user to log in manually.

        The worker's session is dead, so we spin up a new one that stays
        alive in the HTTP server's session pool for the user to interact with.
        """
        superbrowser_url = os.environ.get("SUPERBROWSER_URL", "http://localhost:3100")

        # Delete stale cookies for this domain so fresh ones aren't mixed in
        domain = _domain_from_url(url)
        cookie_dir = os.environ.get("COOKIE_DIR", "/tmp/superbrowser/cookies")
        stale_path = os.path.join(cookie_dir, f"{domain}.json")
        if os.path.exists(stale_path):
            os.remove(stale_path)
            print(f"[auth] Deleted stale cookies for {domain}")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    f"{superbrowser_url}/session/create",
                    json={"url": url, "vision": False},
                )
                r.raise_for_status()
                data = r.json()
            session_id = data.get("sessionId", "")
            if session_id:
                auth_url = f"{superbrowser_url}/auth-ui/{session_id}"
                print(f"\n>> Auth session created: {auth_url}")
                return (
                    f"[LOGIN REQUIRED]\n\n"
                    f"The site requires authentication. Open this link to log in:\n\n"
                    f"  >>> {auth_url} <<<\n\n"
                    f"Log in as you normally would, then click 'Save Cookies & Done'.\n"
                    f"After that, tell me to retry and cookies will be loaded automatically.\n\n"
                    f"(Worker note: {worker_content[:300]})"
                )
        except Exception as e:
            print(f"\n>> Failed to create auth session: {e}")

        # Fallback: return original content with a clear note
        return (
            f"{worker_content}\n\n"
            f"[NOTE: Login appears required for {url} but auth session could not be created. "
            f"Try loading cookies manually.]"
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
        "Read past learnings for a website. Returns what worked and what failed. "
        "ALWAYS call this before delegate_browser_task to avoid repeating mistakes."
    )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, site: str, **kw: Any) -> str:
        domain = _domain_from_url(site)
        path = _learnings_path(domain)

        if not os.path.exists(path):
            return f"No learnings found for {domain}. This is the first task on this site."

        with open(path, "r") as f:
            content = f.read().strip()

        if not content:
            return f"No learnings found for {domain}."

        return f"Learnings for {domain}:\n\n{content}"


@tool_parameters(
    tool_parameters_schema(
        site=StringSchema("Site domain or URL"),
        learning=StringSchema(
            "What worked or failed. Be specific: include URLs, selectors, error messages. "
            "Format: '- WORKED: ...' or '- FAILED: ...'"
        ),
        required=["site", "learning"],
    )
)
class SaveLearningTool(Tool):
    """Save a learning about a site for future tasks."""

    name = "save_learning"
    description = (
        "Save ACTIONABLE learnings for a site. Future workers will follow these directly. Include:\n"
        "- WORKING: exact script patterns (browser_run_script code), URL patterns, selectors, wait strategies\n"
        "- FAILED: what was tried and WHY it failed, with 'DO NOT:' instructions\n"
        "Read the [Worker Activity Log] to extract successful patterns. "
        "Write learnings as step-by-step instructions a worker can directly execute."
    )

    async def execute(self, site: str, learning: str, **kw: Any) -> str:
        domain = _domain_from_url(site)
        path = _learnings_path(domain)

        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

        entry = f"\n### {timestamp}\n{learning}\n"

        with open(path, "a") as f:
            f.write(entry)

        return f"Learning saved for {domain}."


def register_orchestrator_tools(bot: "Nanobot") -> None:
    """Register orchestrator-specific tools (delegation + learnings)."""
    tools = [
        DelegateBrowserTaskTool(),
        CheckLearningsTool(),
        SaveLearningTool(),
    ]
    for tool in tools:
        bot._loop.tools.register(tool)
