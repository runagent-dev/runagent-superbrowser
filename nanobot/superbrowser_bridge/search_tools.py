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
import os
import re as _re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

_BASE = Path(__file__).resolve().parent.parent
SEARCH_WORKSPACE = str(_BASE / "workspace_search")

SUPERBROWSER_URL = os.environ.get("SUPERBROWSER_URL", "http://localhost:3100")


# Blocked-content detector lives in routing.py so it's testable without
# pulling in nanobot deps. Re-imported here so local uses don't need to
# know about the split.
from superbrowser_bridge.routing import (  # noqa: F401
    _extract_browser_target,
    _looks_blocked,
)


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
# Browser-backed fallback fetch (Layer 5.2)
# ---------------------------------------------------------------------------

class WebFetchRenderedTool(Tool):
    """Fetch a URL via the stealth browser and return its markdown.

    Use this ONLY when plain web_fetch returned a bot-block stub
    (Cloudflare challenge, 403, 429, JS-only SPA). The render path is
    slower (~3s) and costs more (~$0.005) than web_fetch (~500ms, free),
    so always try web_fetch first and escalate selectively.

    No captcha auto-solve — if the page still shows a captcha after
    stealth navigation, this returns {blocked: true, reason: ...} and
    the worker should pick a different source URL.
    """

    name = "web_fetch_rendered"
    description = (
        "Fetch a URL using a stealth headless browser and return the page's "
        "rendered markdown. ONLY use when plain `web_fetch` returned a block "
        "stub (Cloudflare 'just a moment', 403, 429, empty SPA). Costs ~3s "
        "and ~$0.005 per call — not a default replacement for web_fetch."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL to fetch (must include scheme)"},
            "timeout": {"type": "integer", "description": "Navigation timeout in ms (default 20000, max 45000)", "minimum": 5000, "maximum": 45000},
        },
        "required": ["url"],
    }

    def __init__(self):
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._min_gap = 1.0  # seconds between rendered fetches

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, url: str, timeout: int | None = None, **kw: Any) -> str:
        # Serial + rate-limit so the search worker can't accidentally fire
        # 10 concurrent browser sessions.
        async with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self._min_gap:
                await asyncio.sleep(self._min_gap - elapsed)
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    r = await client.post(
                        f"{SUPERBROWSER_URL}/fetch/rendered",
                        json={"url": url, "timeout": timeout or 20000},
                    )
                    r.raise_for_status()
                    data = r.json()
            except Exception as exc:
                self._last_call = time.monotonic()
                return f"[web_fetch_rendered failed: {exc}]"
            self._last_call = time.monotonic()

        if data.get("blocked"):
            return (
                f"[BLOCKED by stealth-only fetch: {data.get('reason', 'unknown')}]\n"
                f"URL: {data.get('url', url)}\n"
                f"Final URL: {data.get('finalUrl', '')}\n"
                f"Title: {data.get('title', '')}\n"
                f"The stealth browser couldn't bypass this page. Try a different "
                f"source URL from your search results — do NOT retry the same URL."
            )

        title = data.get("title") or ""
        final_url = data.get("finalUrl") or url
        markdown = (data.get("markdown") or "")[:10_000]
        return (
            f"URL: {final_url}\nTitle: {title}\n"
            f"--- rendered markdown ---\n{markdown}"
        )


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
        self._rendered_count = 0
        self._last_queries: list[str] = []
        # Track URLs we already warned about to prevent nagging on every turn.
        self._nudged_urls: set[str] = set()
        # Remember which URLs went through web_fetch_rendered so the
        # orchestrator can later decide whether search-on-this-domain
        # costs a rendered-fallback too often to be worth it (Layer 5.5).
        self.rendered_urls: list[str] = []

    async def after_iteration(self, context: AgentHookContext) -> None:
        guidance_parts: list[str] = []

        # Track tool calls the assistant just made (search, fetch, rendered).
        # Also remember the last web_fetch tool-call so we can pair its
        # arguments with the corresponding tool result message below.
        pending_fetch_url: str | None = None
        if context.messages:
            for msg in context.messages:
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls", []):
                        name = tc.get("function", {}).get("name", "")
                        try:
                            args = _json.loads(tc["function"].get("arguments", "{}"))
                        except Exception:
                            args = {}
                        if name == "web_search":
                            self._search_count += 1
                            q = args.get("query", "")
                            if q:
                                self._last_queries.append(q)
                        elif name == "web_fetch":
                            self._fetch_count += 1
                            pending_fetch_url = args.get("url") or args.get("uri")
                        elif name == "web_fetch_rendered":
                            self._rendered_count += 1
                            url = args.get("url")
                            if url:
                                self.rendered_urls.append(url)

        # --- Blocked-content detector (Layer 5.3) ---
        # If the last web_fetch result looks like a bot-block stub (Cloudflare,
        # 403, JS-only SPA), nudge the worker to retry with web_fetch_rendered.
        # Cap at one nudge per URL so we don't spam.
        if pending_fetch_url and pending_fetch_url not in self._nudged_urls and context.messages:
            for msg in reversed(context.messages):
                if msg.get("role") != "tool":
                    continue
                content = msg.get("content")
                text_blob = ""
                if isinstance(content, str):
                    text_blob = content
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            text_blob += b.get("text") or ""
                if not text_blob:
                    break
                blocked, reason = _looks_blocked(text_blob)
                if blocked:
                    self._nudged_urls.add(pending_fetch_url)
                    guidance_parts.append(
                        f"[GUIDANCE: web_fetch on {pending_fetch_url} returned what "
                        f"looks like a bot-block page (reason: {reason}). Retry the "
                        f"SAME URL with web_fetch_rendered — it uses a stealth browser "
                        f"that bypasses simple bot checks. If rendered fetch ALSO "
                        f"returns blocked, pick a different source URL; do NOT retry "
                        f"the same URL more than twice.]"
                    )
                break

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
                f"[GUIDANCE: {remaining} iterations left. Prioritize extracting the "
                "specific data still missing and return findings with source URLs. "
                "Do NOT fabricate values — if a data point cannot be found, say so honestly.]"
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

from nanobot.agent.tools.schema import BooleanSchema


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
        force=BooleanSchema(
            description=(
                "Override the routing classifier. Use only if the classifier "
                "suggests 'browser' but you KNOW search is sufficient."
            ),
            default=False,
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

    async def execute(self, question: str, search_hints: str | None = None, force: bool = False, **kw: Any) -> str:
        # --- Classifier gate (Layer 2) --------------------------------
        # Import here to avoid a circular import at module load.
        from superbrowser_bridge.orchestrator_tools import (
            _classify_task, _record_routing_outcome, _domain_from_url,
        )
        classification = _classify_task(question, None)
        # Only warn-back when the classifier strongly suggests browser —
        # calling search when browser was optimal is usually still safe
        # (just slower), while calling browser when search was right burns
        # captcha risk, so the asymmetry is intentional.
        if not force and classification["approach"] == "browser" and classification["confidence"] >= 0.8:
            print(f"\n>> delegate_search_task classifier warn-back: {classification}")
            return (
                "[ROUTING WARNING] This task looks like it wants `browser`, "
                f"not `search` (reason: {classification['reason']}). "
                f"Call `delegate_browser_task` with the specific URL instead. "
                f"If you're confident search text is enough, re-call with `force=true`."
            )

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

        # Register the stealth-browser fallback fetcher (Layer 5.2). Used
        # when web_fetch returns a bot-block stub.
        worker._loop.tools.register(WebFetchRenderedTool())

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
4. If web_fetch returns a bot-block stub, retry that URL with web_fetch_rendered
5. Refining your search based on what you find in the pages
6. Synthesizing a clear answer with source URLs

## Tool usage
- `web_search(query)` — free, returns titles + URLs + snippets. Start here.
- `web_fetch(url)` — free, HTTP GET of the page. Primary way to read full text. Fast (~1s) but fails on bot-protected sites (Cloudflare, Mercari, LinkedIn, etc.) — those return stub HTML like "Just a moment" or an empty SPA shell.
- `web_fetch_rendered(url)` — uses a stealth browser, ~3s and ~$0.005 per call. ONLY for retrying URLs where web_fetch returned a block stub (Cloudflare, 403/429, JS-only SPA). Do NOT use as a default — always try web_fetch first.

CRITICAL RULES:
- Call web_search ONE query at a time. Wait for results before searching again.
- Start with BROAD, natural queries (3-7 words). Do NOT use long exact-match phrases.
- After each search, use web_fetch on 2-3 of the most promising result URLs.
- If a [GUIDANCE: ...] message tells you web_fetch looked blocked, FOLLOW IT — retry that URL with web_fetch_rendered once. If rendered fetch is also blocked, pick a DIFFERENT URL from your search results; do not retry the same URL more than twice.
- Read the FULL page content — search snippets alone are NOT enough.
- Use discoveries from pages to refine your next search (names, places, dates found).
- If you cannot find the answer, say so clearly — do NOT guess or hallucinate.
- Include source URLs for every key claim.
- Partial findings are valuable — return them even if incomplete.""")

        prompt = "\n".join(parts)

        # Create guardrail hook
        hook = SearchWorkerHook(max_iterations=max_iterations)

        # Best-effort domain extraction from the question so we can record
        # per-domain routing outcomes. If the question mentions "mercari.com"
        # or "on amazon.com", we record against that domain.
        import re as _re
        domain_match = _re.search(
            r"\b(?:on\s+|at\s+|from\s+)?([a-z0-9-]+\.[a-z]{2,})(?:\s|$|/|\.)",
            question.lower(),
        )
        probed_domain = domain_match.group(1) if domain_match else None

        try:
            result = await worker.run(prompt, session_key=session_key, hooks=[hook])
            content = result.content

            print(f"\n>> Search worker result ({len(content)} chars): {content[:200]}...")

            # Add search stats
            content += (
                f"\n\n[Search Stats: {hook._search_count} searches, "
                f"{hook._fetch_count} pages read]"
            )

            # Record routing outcome (Layer 4). Heuristic success: the
            # worker returned a non-trivial result and didn't explicitly
            # report "cannot find" / "insufficient info" / "prices vary".
            lower_c = (content or "").lower()
            hedge_phrases = (
                "cannot find", "could not find", "couldn't find",
                "insufficient information", "no results found",
                "unable to provide specific", "unable to find specific",
                "prices vary", "varies by", "check the site directly",
                "visit the website", "you should check", "i recommend visiting",
                "exact prices are not", "specific prices are not",
                "not publicly available", "real-time pricing",
            )
            looks_hedged = any(p in lower_c for p in hedge_phrases)
            success = (
                bool(content)
                and len(content.strip()) > 80
                and not looks_hedged
            )
            # Flag whether this run needed the rendered fallback. If the
            # worker hit web_fetch_rendered for a URL matching the probed
            # domain, the domain's search path effectively cost browser
            # money — track it so the routing preference can adapt.
            used_rendered = False
            if probed_domain and hook.rendered_urls:
                used_rendered = any(probed_domain in (u or "") for u in hook.rendered_urls)
            # A run where rendered fetches were blocked on >=2 attempts also
            # counts as a signal that search won't yield real data.
            rendered_blocked_heavy = hook._rendered_count >= 2 and not success

            if probed_domain:
                _record_routing_outcome(
                    probed_domain, "search",
                    success=success, used_rendered=used_rendered,
                )

            # --- Search → Browser escalation (symmetric to Layer 3) --------
            # If the search worker returned a hedged / insufficient answer
            # AND the question mentions a known brand/URL, automatically
            # try delegate_browser_task. Cap at one escalation per task
            # via the `_browser_fallback_done` sentinel. Skip entirely when
            # called as a rescue from the browser side (`_fallback_from_browser`)
            # to avoid ping-pong loops.
            should_escalate = (
                (not success or rendered_blocked_heavy)
                and not kw.get("_browser_fallback_done")
                and not kw.get("_fallback_from_browser")
            )
            if should_escalate:
                target = _extract_browser_target(question)
                if target:
                    print(f"\n>> search insufficient — escalating to browser on {target}")
                    try:
                        # Lazy import to dodge the circular dep — orchestrator_tools
                        # imports from search_tools for its captcha→search rescue.
                        from superbrowser_bridge.orchestrator_tools import (
                            DelegateBrowserTaskTool,
                        )
                        browser_tool = DelegateBrowserTaskTool()
                        browser_result = await browser_tool.execute(
                            instructions=(
                                f"Original research question (search could not answer):\n"
                                f"{question}\n\n"
                                f"Go to {target} and use its own search/listing UI to "
                                f"extract the information. Return concrete values with "
                                f"source URLs — not a summary."
                            ),
                            url=target,
                            force=True,  # classifier already approved search; override
                            _browser_fallback_done=True,
                        )
                        return (
                            f"[Search result was insufficient — auto-escalated to browser on {target}]\n\n"
                            f"{browser_result}\n\n"
                            f"[Original search attempt]\n{content[:600]}"
                        )
                    except Exception as esc_exc:
                        print(f"  [browser escalation failed: {esc_exc}]")

            return content

        except Exception as e:
            if probed_domain:
                _record_routing_outcome(probed_domain, "search", success=False)
            error_msg = f"Search worker failed: {e}"
            print(f"\n>> Search worker error: {error_msg}")
            return error_msg


def register_search_tools(bot: "Nanobot") -> None:
    """Register search delegation tool on the orchestrator."""
    bot._loop.tools.register(DelegateSearchTaskTool())
