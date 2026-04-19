"""Nanobot Tool wrappers for the three antibot fetchers.

These expose the Tier 2 / Tier 3 / Tier 4 fetchers to the orchestrator
LLM through nanobot's Tool framework. Each returns a formatted string
(the Tool contract) that includes the status, tier, block verdict, and
the fetched HTML (truncated to keep responses bounded).
"""

from __future__ import annotations

import json as _json
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)

from .fetch_archive import fetch_archive as _archive
from .fetch_auto import fetch_auto as _auto
from .fetch_impersonate import fetch_impersonate as _impersonate
from .fetch_undetected import fetch_undetected as _undetected
from . import content as _content

# Keep tool responses bounded. The agent can ask for more by extracting
# specific sections; the full page stays in the jar/returned HTML.
_MAX_HTML_CHARS = 32_000


def _format_response(meta: dict, html: str) -> str:
    """Return a human/LLM-friendly string that leads with meta and ends with HTML."""
    summary = {k: v for k, v in meta.items() if k != "html"}
    summary_str = _json.dumps(summary, indent=2, default=str)
    truncated = html[:_MAX_HTML_CHARS]
    if len(html) > _MAX_HTML_CHARS:
        truncated += f"\n<!-- truncated, total {len(html)} chars -->"
    return f"{summary_str}\n\n---\n{truncated}"


def _postprocess(html: str, query: str | None, markdown: bool) -> str:
    """Apply BM25 filter, markdown conversion, or return raw HTML."""
    if not html:
        return ""
    if query:
        return _content.bm25_filter(html, query, top_k=20)
    if markdown:
        return _content.to_markdown(html)
    return html


@tool_parameters(
    tool_parameters_schema(
        url=StringSchema(
            "URL to fetch. Must be http(s). "
            "Use for read-only content extraction on sites that block naive "
            "HTTP (Cloudflare Turnstile, moderate header-based blocks, TLS "
            "fingerprint gating). Does NOT render JavaScript."
        ),
        profile=StringSchema(
            "Chrome profile to impersonate (default: chrome124_mac).",
            enum=(
                "chrome124_mac", "chrome124_linux",
                "chrome125_mac", "chrome125_linux",
                "chrome126_mac", "chrome126_linux",
            ),
            nullable=True,
        ),
        warmup_homepage=BooleanSchema(
            description=(
                "If true (default), pre-visit the root of the domain to "
                "collect clearance cookies (_abck / cf_clearance / datadome) "
                "before fetching the target URL. Skip this only on domains "
                "you know accept deep links without warmup."
            ),
            default=True,
        ),
        max_retries=IntegerSchema(
            2,
            description="Retries after a detected block (default 2).",
        ),
        timeout_s=NumberSchema(
            30.0,
            description="Per-request timeout in seconds (default 30).",
        ),
        referer=StringSchema(
            "Optional Referer header. Omit unless the site checks referer.",
            nullable=True,
        ),
        query=StringSchema(
            "Optional query string. If set, the response is BM25-filtered to "
            "the top-scoring passages instead of returning the full HTML. "
            "Dramatically reduces token usage for 'find X' tasks.",
            nullable=True,
        ),
        markdown=BooleanSchema(
            description=(
                "If true (and no query), convert the HTML to clean markdown "
                "(headings, paragraphs, links, code blocks) with boilerplate "
                "nav/footer/aside stripped. Much cheaper for the LLM to read."
            ),
            default=False,
        ),
        required=["url"],
    )
)
class FetchImpersonateTool(Tool):
    """Tier 2: fetch via curl_cffi with Chrome TLS impersonation.

    Use BEFORE Tier 3 (browser) when you just need the HTML of a page.
    Much faster (~0.5-2s vs 5-15s) and free. Won't defeat Akamai Bot
    Manager or sophisticated JS challenges — fall through to
    `fetch_undetected` on block.
    """

    name = "fetch_impersonate"
    description = (
        "Tier-2 anti-bot fetch: curl_cffi with Chrome TLS/JA3 impersonation, "
        "realistic headers, session pool, tiered proxy, cookie-jar reuse. "
        "Defeats TLS fingerprinting, moderate CF, header-based blocks. "
        "Read-only HTML, no JS rendering. Fast (~1s). Escalate to "
        "fetch_undetected if this returns block_class != ''."
    )

    async def execute(
        self,
        url: str,
        profile: str | None = None,
        warmup_homepage: bool = True,
        max_retries: int = 2,
        timeout_s: float = 30,
        referer: str | None = None,
        query: str | None = None,
        markdown: bool = False,
        **_kw: Any,
    ) -> str:
        result = await _impersonate(
            url,
            profile=profile or "chrome124_mac",  # type: ignore[arg-type]
            warmup_homepage=warmup_homepage,
            max_retries=max_retries,
            timeout_s=float(timeout_s),
            referer=referer,
        )
        html = result.pop("html", "")
        body = _postprocess(html, query, markdown)
        result["structured"] = _content.extract_meta_title_description(html)
        return _format_response(result, body)


@tool_parameters(
    tool_parameters_schema(
        url=StringSchema(
            "URL to fetch. Launches an undetected Chromium browser, renders "
            "JS, simulates user input, and collects the final DOM HTML."
        ),
        warmup_homepage=BooleanSchema(
            description=(
                "If true (default), navigate to the domain root first and "
                "pause 2-4s so Akamai / Cloudflare set clearance cookies "
                "before the target URL is hit."
            ),
            default=True,
        ),
        simulate_user=BooleanSchema(
            description=(
                "If true (default), perform 2-3 mouse moves + a wheel scroll "
                "before reading the DOM. Helps with sensor-scoring protections "
                "that weight 'no input events' as a bot signal."
            ),
            default=True,
        ),
        remove_overlays=BooleanSchema(
            description="Strip common GDPR/consent overlays post-load.",
            default=True,
        ),
        headless=BooleanSchema(
            description=(
                "Chromium headless mode. Default true. Set false only if you "
                "have a display (xvfb) — hardened Akamai targets occasionally "
                "fingerprint the no-display indicator."
            ),
            default=True,
        ),
        timeout_s=NumberSchema(
            45.0,
            description="Total navigation timeout in seconds (default 45).",
        ),
        wait_for_selector=StringSchema(
            "Optional CSS selector to wait for after navigation (useful for "
            "SPA content that arrives post-load via XHR).",
            nullable=True,
        ),
        screenshot=BooleanSchema(
            description=(
                "If true, return a base64-encoded PNG screenshot alongside "
                "the HTML. Useful when the vision_agent needs to inspect "
                "pixels that aren't present in the DOM (charts, maps)."
            ),
            default=False,
        ),
        query=StringSchema(
            "Optional query; BM25-filter the returned content.",
            nullable=True,
        ),
        markdown=BooleanSchema(
            description="Convert HTML to clean markdown when no query is set.",
            default=False,
        ),
        required=["url"],
    )
)
class FetchUndetectedTool(Tool):
    """Tier 3: fetch via patchright (undetected Chromium) + playwright-stealth.

    Use when Tier 2 reports a block. Heavier (~5-15s, ~180MB browser)
    but defeats Akamai Bot Manager, DataDome, PerimeterX, Kasada, and
    hardened Cloudflare. Read-only HTML, no interactive flows.
    """

    name = "fetch_undetected"
    description = (
        "Tier-3 anti-bot fetch: patchright (undetected Chromium) + "
        "playwright-stealth + simulate_user + homepage warmup + overlay "
        "removal + cookie-jar reuse. Defeats Akamai BM, DataDome, "
        "PerimeterX, Kasada, hardened CF. Read-only HTML (JS fully "
        "rendered). Fall through to fetch_archive if this also blocks."
    )

    async def execute(
        self,
        url: str,
        warmup_homepage: bool = True,
        simulate_user: bool = True,
        remove_overlays: bool = True,
        headless: bool = True,
        timeout_s: float = 45,
        wait_for_selector: str | None = None,
        screenshot: bool = False,
        query: str | None = None,
        markdown: bool = False,
        **_kw: Any,
    ) -> str:
        result = await _undetected(
            url,
            warmup_homepage=warmup_homepage,
            simulate_user=simulate_user,
            remove_overlays=remove_overlays,
            headless=headless,
            timeout_s=float(timeout_s),
            wait_for_selector=wait_for_selector,
            screenshot=screenshot,
        )
        html = result.pop("html", "")
        body = _postprocess(html, query, markdown)
        result["structured"] = {
            "json_ld": _content.extract_json_ld(html),
            "meta": _content.extract_meta_title_description(html),
        }
        return _format_response(result, body)


@tool_parameters(
    tool_parameters_schema(
        url=StringSchema(
            "URL to look up in public web archives. Returns STALE content "
            "(last snapshot, possibly months old). Disclose staleness to the "
            "user — never present archive data as live."
        ),
        required=["url"],
    )
)
class FetchArchiveTool(Tool):
    """Tier 4: archive fallback (Wayback Machine + Google Cache)."""

    name = "fetch_archive"
    description = (
        "Tier-4 fallback: retrieve a stale snapshot of the URL from "
        "Wayback Machine (archive.org) or Google Cache. Free, ~1-3s. "
        "Acceptable for catalog/description queries; UNACCEPTABLE for "
        "prices, inventory, or any time-sensitive data. Always disclose "
        "captured_at to the user."
    )

    async def execute(self, url: str, **_kw: Any) -> str:
        result = await _archive(url)
        html = result.pop("html", "")
        return _format_response(result, html)


@tool_parameters(
    tool_parameters_schema(
        url=StringSchema(
            "URL to fetch. The tool reads the per-domain learning, picks the "
            "cheapest tier known to work, runs it, escalates on block, and "
            "records the new outcome. PREFER THIS over picking a tier yourself."
        ),
        query=StringSchema(
            "Optional natural-language query. When set, the response is "
            "BM25-filtered to the top-scoring passages so the agent spends "
            "tokens only on the relevant part of the page.",
            nullable=True,
        ),
        markdown=BooleanSchema(
            description=(
                "If true (and no query), return clean markdown (headings, "
                "paragraphs, links, lists) with nav/footer/aside stripped."
            ),
            default=False,
        ),
        max_tier=IntegerSchema(
            4,
            description=(
                "Ceiling on the ladder. 2 = fast HTTP only (no browser). "
                "3 = allow undetected Chromium. 4 = also try archive. "
                "Default 4."
            ),
        ),
        timeout_s=NumberSchema(
            45.0,
            description="Per-tier timeout in seconds (default 45).",
        ),
        required=["url"],
    )
)
class FetchAutoTool(Tool):
    """Adaptive anti-bot fetch — walks the tier ladder automatically.

    This is the primary read-only fetch tool. It:
      1. Reads the learning system's `lowest_successful_tier` for the
         domain (default Tier 2 for unseen hosts).
      2. Runs that tier; on block, escalates one tier and retries.
      3. Applies rate limiting per domain (exponential backoff on 429/503).
      4. Records the outcome so the next call starts at the right tier.
      5. Optionally BM25-filters the result or converts to markdown.

    Use for any pure data-extraction task where you don't need to click
    or fill anything. Do NOT use for interactive flows — those still
    need `delegate_browser_task`.
    """

    name = "fetch_auto"
    description = (
        "Adaptive anti-bot fetch that walks Tier 2 → 3 → 4 automatically, "
        "escalating on detected blocks. Reads per-domain learnings to skip "
        "tiers known to fail. Supports BM25 query filtering + markdown "
        "output. Preferred read-only fetch for any data-extraction task."
    )

    async def execute(
        self,
        url: str,
        query: str | None = None,
        markdown: bool = False,
        max_tier: int = 4,
        timeout_s: float = 45,
        **_kw: Any,
    ) -> str:
        result = await _auto(
            url,
            query=query,
            markdown=markdown,
            max_tier=int(max_tier),
            timeout_s=float(timeout_s),
        )
        content = result.pop("content", "")
        # Drop the raw HTML from the summary — the formatted body already has it.
        result.pop("html", None)
        return _format_response(result, content)


__all__ = [
    "FetchImpersonateTool",
    "FetchUndetectedTool",
    "FetchArchiveTool",
    "FetchAutoTool",
]
