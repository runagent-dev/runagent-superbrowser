"""
Search worker tools for the two-agent architecture.

The orchestrator delegates web research to a search worker that uses
web_search + web_fetch with sophisticated query decomposition and
iterative refinement — no browser needed, no CAPTCHA issues.

Includes a rate-limited web_search wrapper to prevent DDG from
being overwhelmed by parallel requests.
"""

from __future__ import annotations

import asyncio
import json as _json
import time
import uuid
from pathlib import Path
from typing import Any

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

_BASE = Path(__file__).resolve().parent.parent
SEARCH_WORKSPACE = str(_BASE / "workspace_search")


# ---------------------------------------------------------------------------
# Rate-limited web_search wrapper — prevents DDG from being overwhelmed
# ---------------------------------------------------------------------------

class RateLimitedWebSearchTool(Tool):
    """Web search wrapper that serializes requests and adds rate limiting.

    DuckDuckGo (the free fallback) gets rate-limited when the LLM fires
    multiple parallel web_search calls. This wrapper ensures only ONE search
    runs at a time with a minimum gap between calls.
    """

    name = "web_search"
    description = (
        "Search the web. Returns titles, URLs, and snippets. "
        "IMPORTANT: Call ONE search at a time — wait for results before searching again. "
        "Use web_fetch to read full page content from result URLs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query — keep it short and natural (3-7 words)"},
            "count": {"type": "integer", "description": "Number of results (1-10)", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }

    def __init__(self, inner_tool: Tool):
        self._inner = inner_tool
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._min_gap = 2.0  # seconds between DDG calls

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        async with self._lock:
            # Enforce minimum gap between searches
            elapsed = time.monotonic() - self._last_call
            if elapsed < self._min_gap:
                await asyncio.sleep(self._min_gap - elapsed)

            result = await self._inner.execute(query=query, count=count, **kwargs)
            self._last_call = time.monotonic()

            # If DDG returned an error or no results, try a simplified query
            if ("Error:" in result or "No results for:" in result) and len(query.split()) > 5:
                # Retry with shorter query: keep first 5 words
                short_query = " ".join(query.split()[:5])
                await asyncio.sleep(self._min_gap)
                retry_result = await self._inner.execute(query=short_query, count=count, **kwargs)
                self._last_call = time.monotonic()
                if "Error:" not in retry_result and "No results for:" not in retry_result:
                    return f"(Simplified query to: {short_query})\n{retry_result}"

            return result


# ---------------------------------------------------------------------------
# Search worker hook — guardrails
# ---------------------------------------------------------------------------

class SearchWorkerHook(AgentHook):
    """Guardrails for the search worker agent.

    Prevents degenerate search patterns:
    - Too many searches without reading pages
    - Repetitive query variations
    - Running out of budget without synthesizing
    """

    def __init__(self, max_iterations: int = 30):
        self.max_iterations = max_iterations
        self._search_count = 0
        self._fetch_count = 0
        self._last_queries: list[str] = []

    async def after_iteration(self, context: AgentHookContext) -> None:
        guidance_parts: list[str] = []

        # Count searches and fetches from tool calls in this iteration
        if context.messages:
            for msg in context.messages:
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls", []):
                        name = tc.get("function", {}).get("name", "")
                        if name == "web_search":
                            self._search_count += 1
                            try:
                                args = _json.loads(tc["function"].get("arguments", "{}"))
                                query = args.get("query", "")
                                if query:
                                    self._last_queries.append(query)
                            except Exception:
                                pass
                        elif name == "web_fetch":
                            self._fetch_count += 1

        # --- Too many searches without reading pages ---
        if self._search_count >= 3 and self._fetch_count == 0:
            guidance_parts.append(
                f"[GUIDANCE: You have done {self._search_count} searches but read "
                "0 pages. STOP searching. Use web_fetch on the most promising "
                "result URLs from your searches NOW. You MUST read actual pages — "
                "search snippets are not enough to answer research questions.]"
            )

        # --- Search budget warning ---
        if self._search_count >= 8:
            guidance_parts.append(
                f"[GUIDANCE: You have used {self._search_count}/10 search queries. "
                "Synthesize what you have found so far. Use remaining searches "
                "ONLY for critical missing information.]"
            )

        # --- Iteration budget warnings ---
        iteration = context.iteration
        remaining = self.max_iterations - iteration - 1

        if remaining <= int(self.max_iterations * 0.2):
            guidance_parts.append(
                f"[GUIDANCE: CRITICAL — only {remaining} iterations left. "
                "Synthesize ALL findings NOW and return your answer with source URLs. "
                "Partial answers with sources are better than no answer.]"
            )
        elif remaining <= int(self.max_iterations * 0.4):
            guidance_parts.append(
                f"[GUIDANCE: {remaining} iterations left. Start synthesizing your "
                "findings. If you have enough information, return your answer now.]"
            )

        # Inject guidance
        if guidance_parts and context.messages:
            guidance_text = "\n" + "\n".join(guidance_parts)
            for i in range(len(context.messages) - 1, -1, -1):
                msg = context.messages[i]
                if msg.get("role") == "tool":
                    if isinstance(msg.get("content"), str):
                        msg["content"] += guidance_text
                    elif isinstance(msg.get("content"), list):
                        msg["content"].append({"type": "text", "text": guidance_text})
                    break


# ---------------------------------------------------------------------------
# Delegate search task tool — spawns search worker
# ---------------------------------------------------------------------------

@tool_parameters(
    tool_parameters_schema(
        question=StringSchema(
            "The research question or information to find. Be specific about "
            "what you need — include all known constraints and what the answer should look like."
        ),
        search_hints=StringSchema(
            "Optional hints for the search worker: suggested search queries, "
            "key terms to focus on, known facts to build from.",
            nullable=True,
        ),
        required=["question"],
    )
)
class DelegateSearchTaskTool(Tool):
    """Delegate a web research task to the search worker agent.

    The search worker uses web_search + web_fetch (API-based, no browser)
    with sophisticated query decomposition and iterative refinement.
    Use this for questions that require finding information on the web.
    """

    name = "delegate_search_task"
    description = (
        "Send a research question to the search worker. The worker searches the web, "
        "reads pages, and returns findings with source URLs. "
        "Use this for ANY task that requires finding information online. "
        "Write the question clearly with all known constraints."
    )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, question: str, search_hints: str | None = None, **kw: Any) -> str:
        from nanobot import Nanobot

        task_id = uuid.uuid4().hex[:8]
        session_key = f"search:{task_id}"
        max_iterations = 30

        print(f"\n>> delegate_search_task(session={session_key})")
        print(f"   Question: {question[:120]}...")

        # Create a FRESH search worker
        worker = Nanobot.from_config(workspace=SEARCH_WORKSPACE)

        # Remove all tools except web_search and web_fetch
        tools_to_remove = [
            "read_file", "write_file", "edit_file",
            "list_dir", "glob", "grep",
            "exec", "spawn", "cron", "message",
        ]
        for name in tools_to_remove:
            worker._loop.tools.unregister(name)

        # Replace web_search with rate-limited version to prevent DDG overload
        original_search = worker._loop.tools._tools.get("web_search")
        if original_search:
            worker._loop.tools.unregister("web_search")
            worker._loop.tools.register(RateLimitedWebSearchTool(original_search))

        worker._loop.max_iterations = max_iterations

        # Build the search prompt
        parts = [f"## Research Question\n{question}"]

        if search_hints:
            parts.append(f"\n## Search Hints (from orchestrator)\n{search_hints}")

        parts.append("""
## Your Task
Find the answer to the research question above by:
1. Decomposing it into key constraints
2. Searching the web from multiple angles (use web_search — ONE query at a time)
3. Reading promising result pages IN FULL (use web_fetch on result URLs)
4. Refining your search based on what you find in the pages
5. Synthesizing a clear answer with source URLs

CRITICAL RULES:
- Call web_search ONE query at a time. Wait for results before searching again.
- Start with BROAD, natural queries (3-7 words). Do NOT use long exact-match phrases.
- After each search, use web_fetch on 2-3 of the most promising result URLs.
- Read the FULL page content — search snippets alone are NOT enough.
- Use discoveries from pages to refine your next search (names, places, dates found).
- If you cannot find the answer, say so clearly — do NOT guess or hallucinate.
- Include source URLs for every key claim.
- Partial findings are valuable — return them even if incomplete.""")

        prompt = "\n".join(parts)

        # Create guardrail hook
        hook = SearchWorkerHook(max_iterations=max_iterations)

        try:
            result = await worker.run(prompt, session_key=session_key, hooks=[hook])
            content = result.content

            print(f"\n>> Search worker result ({len(content)} chars): {content[:200]}...")

            # Add search stats
            content += (
                f"\n\n[Search Stats: {hook._search_count} searches, "
                f"{hook._fetch_count} pages read]"
            )

            return content

        except Exception as e:
            error_msg = f"Search worker failed: {e}"
            print(f"\n>> Search worker error: {error_msg}")
            return error_msg


def register_search_tools(bot: "Nanobot") -> None:
    """Register search delegation tool on the orchestrator."""
    bot._loop.tools.register(DelegateSearchTaskTool())
