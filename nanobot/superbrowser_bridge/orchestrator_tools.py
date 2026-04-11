"""
Orchestrator tools for the two-agent architecture.

The orchestrator delegates browser work to a fresh browser worker instance,
manages site-specific learnings, and never touches browser tools directly.
"""

from __future__ import annotations

import json as _json
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

# Where the browser worker workspace lives (relative to this file)
_BASE = Path(__file__).resolve().parent.parent
BROWSER_WORKSPACE = str(_BASE / "workspace_browser")
LEARNINGS_DIR = str(_BASE / "workspace_orchestrator" / "learnings")


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

    async def execute(self, instructions: str, url: str | None = None, **kw: Any) -> str:
        from nanobot import Nanobot
        from superbrowser_bridge.session_tools import BrowserSessionState, register_session_tools
        from superbrowser_bridge.worker_hook import BrowserWorkerHook

        task_id = uuid.uuid4().hex[:8]
        session_key = f"worker:{task_id}"

        # Research tasks need more iterations: search + visit 3-5 pages + refine
        _research_kw = ("search", "research", "find information", "google", "look up", "investigate")
        is_research = any(k in instructions.lower() for k in _research_kw)
        max_iterations = 40 if is_research else 25

        print(f"\n>> delegate_browser_task(session={session_key})")
        print(f"   Instructions: {instructions[:120]}...")

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
        worker_state.MAX_SCREENSHOTS = 2
        worker_state.task_id = task_id
        register_session_tools(worker, worker_state)

        # Create mid-session guardrail hook
        worker_hook = BrowserWorkerHook(worker_state, max_iterations=max_iterations)

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

        # Check for checkpoint from a previous failed worker attempt.
        # Only resume if the checkpoint domain matches the current task domain
        # to prevent stale checkpoints from unrelated tasks.
        last_checkpoint_path = "/tmp/superbrowser/last_checkpoint.json"
        if os.path.exists(last_checkpoint_path):
            try:
                with open(last_checkpoint_path) as f:
                    checkpoint = _json.load(f)
                cp_url = checkpoint.get("url", "")
                cp_domain = _domain_from_url(cp_url) if cp_url else ""
                # Only inject checkpoint if it's for the same domain as the current task
                if cp_url and domain and cp_domain == domain:
                    parts.append(
                        f"\n## Resume From Checkpoint\n"
                        f"A previous worker made progress to: {cp_url}\n"
                        f"Start from this URL instead of the beginning. "
                        f"Call browser_open with this URL directly.\n"
                        f"Do NOT repeat the steps that led to this point."
                    )
                else:
                    # Stale checkpoint from a different domain — remove it
                    os.remove(last_checkpoint_path)
            except (ValueError, KeyError):
                pass

        # Enforce workflow IN the prompt itself (not just SOUL.md)
        if is_research:
            parts.append("""
## Required Execution Plan — WEB RESEARCH (follow this order)
Step 1: browser_open(url="https://www.google.com/search?q=...") — search Google with a natural, broad query
  Do NOT over-specify with many quoted exact-match phrases. Use natural search terms.
Step 2: browser_get_markdown(session_id) — read the search results, identify promising links
Step 3: For each promising result (visit 3-5 pages):
  - browser_navigate(session_id, url="result-page-url") — ONLY URLs from search results, never fabricated
  - browser_get_markdown(session_id) — extract relevant content (FREE, no budget cost)
  - Note findings and source URL
Step 4: If results are insufficient, do another Google search with refined/alternative terms:
  - browser_navigate(session_id, url="https://www.google.com/search?q=different+query")
  - Repeat Steps 2-3
Step 5: browser_close(session_id) — close when done

KEY FEATURES:
- browser_get_markdown is FREE — use it liberally to read pages
- browser_navigate moves between pages without opening new sessions
- Every action returns updated interactive elements automatically

CRITICAL RULES:
- NEVER fabricate URLs — only visit URLs found in Google search results
- Search snippets are NOT sufficient — you MUST visit actual pages to read full content
- Use BROAD natural queries first, then narrow. Do NOT put all constraints in one query.
- You have {max_iter} iterations total. After iteration 30, return whatever data you have.
- If you see [GUIDANCE: ...] messages, follow them IMMEDIATELY.
- Return ALL findings with source URLs. Partial results are better than no results.""".format(max_iter=max_iterations))
        else:
            parts.append("""
## Required Execution Plan (follow this order)
Step 1: browser_open(url) — open the target page (returns screenshot + interactive elements list)
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
- Partial results are better than no results.""".format(max_iter=max_iterations))

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

            return content

        except Exception as e:
            error_msg = f"Browser worker failed: {e}"
            print(f"\n>> Worker error: {error_msg}")
            return error_msg


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
    """Register orchestrator-specific tools (delegation + learnings + search)."""
    from superbrowser_bridge.search_tools import register_search_tools

    tools = [
        DelegateBrowserTaskTool(),
        CheckLearningsTool(),
        SaveLearningTool(),
    ]
    for tool in tools:
        bot._loop.tools.register(tool)

    # Register the search delegation tool
    register_search_tools(bot)

    # Remove direct web search tools — orchestrator must delegate ALL web
    # research to the search worker (API-based) or browser worker (browser-based).
    for name in ("web_search", "web_fetch"):
        bot._loop.tools.unregister(name)
