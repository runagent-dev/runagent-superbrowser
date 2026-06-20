"""Bridge ``web_fetch`` — the search worker's primary fetch.

Supersedes nanobot-core ``web_fetch`` (Jina reader -> readability) with the
engine-pluggable ``antibot.fetch`` pipeline: plain HTTP -> TLS impersonation ->
stealth browser -> Jina -> archive, all feeding one extraction pass. Returns
clean markdown with numbered citations, scored images, and structured data.

Because the pipeline already escalates through the stealth browser and archive
internally, a genuine block here means "try a different URL" — not a retry with
``web_fetch_rendered`` (which would re-render the same dead page).
"""

from __future__ import annotations

import json as _json
from typing import Any

from nanobot.agent.tools.base import Tool

_MAX_BODY_CHARS = 12_000
_MAX_REFS_CHARS = 2_000
_MAX_MEDIA = 6


def _format_success(result: dict) -> str:
    fit = result.get("fit_markdown") or result.get("raw_markdown") or ""
    refs = result.get("references") or ""
    media = result.get("media") or []
    structured = result.get("structured") or {}
    meta = structured.get("meta") or {}

    header: dict[str, Any] = {
        "engine_used": result.get("engine_used"),
        "tier_used": result.get("tier_used"),
        "status": result.get("status"),
        "final_url": result.get("final_url"),
    }
    if meta.get("title"):
        header["title"] = meta["title"]
    if result.get("source"):
        header["source"] = result.get("source")
    if result.get("captured_at"):
        header["captured_at"] = result.get("captured_at")
    if structured.get("json_ld"):
        header["json_ld_types"] = [
            d.get("@type") for d in structured["json_ld"] if isinstance(d, dict) and d.get("@type")
        ][:5]
    if media:
        header["images"] = [
            {"src": m.get("src"), "alt": m.get("alt"), "score": m.get("score")}
            for m in media[:_MAX_MEDIA]
        ]

    body = fit[:_MAX_BODY_CHARS]
    if len(fit) > _MAX_BODY_CHARS:
        body += (
            f"\n<!-- truncated, {len(fit)} chars total; pass query=\"...\" to focus "
            f"on the relevant passages -->"
        )
    out = _json.dumps(header, indent=2, default=str) + "\n\n---\n" + body
    if refs:
        out += "\n" + refs[:_MAX_REFS_CHARS]
    return out


class BridgeWebFetchTool(Tool):
    """Engine-pluggable web fetch — clean markdown + citations + images."""

    name = "web_fetch"
    description = (
        "Fetch a URL and return clean markdown + numbered citations + scored "
        "images + structured data. Engine-pluggable and SELF-ESCALATING: plain "
        "HTTP -> TLS impersonation -> stealth browser -> Jina reader -> archive, "
        "all feeding one extraction pipeline. Handles most bot-protected sites "
        "automatically — you rarely need web_fetch_rendered. Pass query=\"...\" to "
        "get only the passages relevant to your question (much cheaper)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL to fetch (must include scheme)."},
            "query": {
                "type": "string",
                "description": (
                    "Optional question. Returns only the BM25-relevant passages — "
                    "much cheaper for 'find X' than reading the whole page."
                ),
            },
            "engine": {
                "type": "string",
                "enum": ["auto", "plain", "impersonate", "browser", "jina", "archive"],
                "description": "Force a specific engine. Default 'auto' walks the ladder.",
            },
            "max_tier": {
                "type": "integer",
                "description": "Ceiling: 2=fast HTTP only, 3=allow stealth browser, 4=also archive. Default 4.",
                "minimum": 1,
                "maximum": 4,
            },
            "timeout": {
                "type": "integer",
                "description": "Per-engine timeout in ms (default 45000).",
                "minimum": 5000,
                "maximum": 90000,
            },
        },
        "required": ["url"],
    }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        url: str,
        query: str | None = None,
        engine: str = "auto",
        max_tier: int = 4,
        timeout: int | None = None,
        **_kw: Any,
    ) -> str:
        # Lazy import so registering the tool doesn't pull in curl_cffi/patchright.
        from .antibot.fetch import fetch as _fetch

        result = await _fetch(
            url,
            query=query,
            engine=engine or "auto",
            max_tier=int(max_tier or 4),
            timeout_s=float(timeout or 45000) / 1000.0,
        )
        fit = (result.get("fit_markdown") or result.get("raw_markdown") or "").strip()
        if not fit:
            bc = result.get("block_class") or "unknown"
            reason = result.get("reason") or ""
            return (
                f"[web_fetch could not retrieve {url} after trying HTTP, TLS "
                f"impersonation, stealth browser, and archive (last block: {bc} "
                f"{reason}). The pipeline already rendered this page — do NOT retry "
                f"with web_fetch_rendered; pick a DIFFERENT source URL from your "
                f"search results.]"
            )
        return _format_success(result)
