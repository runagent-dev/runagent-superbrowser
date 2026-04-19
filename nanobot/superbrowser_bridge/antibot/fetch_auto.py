"""fetch_auto: adaptive ladder walker across Tier 2 → 3 → 4.

Reads the per-domain learning (`lowest_successful_tier`) to skip tiers
known to fail on this host, runs the cheapest remaining tier first,
escalates automatically on block, and optionally post-processes the
result with `query` (BM25 filter) or `markdown=True` (clean markdown).

This is the primary tool the orchestrator should call for read-only
page fetches — it collapses the "which tier?" decision into one call.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional
from urllib.parse import urlparse

from . import content as _content
from . import rate_limit as _rl
from .fetch_archive import fetch_archive
from .fetch_impersonate import fetch_impersonate
from .fetch_undetected import fetch_undetected

logger = logging.getLogger(__name__)

_TIER_ORDER = (2, 3, 4)


def _domain_of(url: str) -> str:
    return urlparse(url).hostname or ""


def _starting_tier(domain: str) -> int:
    """Read the routing JSON for this domain's lowest_successful_tier.

    Falls back to Tier 2 (the cheapest anti-bot tier) when no record exists.
    """
    if not domain:
        return 2
    try:
        from superbrowser_bridge.routing import choose_starting_tier
    except Exception:
        return 2
    t = choose_starting_tier(domain)
    if t in _TIER_ORDER:
        return t
    return 2


def _post_process(html: str, query: Optional[str], markdown: bool) -> str:
    if not html:
        return ""
    if query:
        # BM25 always converts its output to filtered markdown-ish text.
        return _content.bm25_filter(html, query, top_k=20)
    if markdown:
        return _content.to_markdown(html)
    return html


async def fetch_auto(
    url: str,
    *,
    query: Optional[str] = None,
    markdown: bool = False,
    max_tier: int = 4,
    headless: bool = True,
    timeout_s: float = 45.0,
) -> dict[str, Any]:
    """Walk the anti-bot ladder automatically; return first success.

    Args:
        url: target URL.
        query: optional query to BM25-filter the returned content by.
        markdown: if True (and no query), return HTML-to-markdown output
                  instead of raw HTML.
        max_tier: hard ceiling; 2 skips the browser, 4 includes archive.
        headless: passed to Tier 3.
        timeout_s: per-tier timeout cap.

    Returns: {
        content: str,              # post-processed (markdown / bm25 / raw html)
        tier_used: int,
        attempts: list[dict],      # per-tier summary: {tier, status, block_class, elapsed_ms}
        block_class: str,          # last-seen block class if we failed
        elapsed_ms: int,
        source: Optional[str],     # "wayback" when Tier 4 was used
        captured_at: Optional[str],
        structured: dict,          # JSON-LD + OG + meta extracted from the HTML
    }
    """
    start = time.time()
    domain = _domain_of(url)
    rate = _rl.default()

    start_tier = _starting_tier(domain)
    tiers = [t for t in _TIER_ORDER if t >= start_tier and t <= max_tier]
    if not tiers:
        tiers = [t for t in _TIER_ORDER if t <= max_tier]

    attempts: list[dict] = []
    last_block_class = ""
    last_reason = ""
    html = ""
    status = 0
    source: Optional[str] = None
    captured_at: Optional[str] = None
    final_tier = 0

    for tier in tiers:
        await rate.wait_if_needed(url)
        t0 = time.time()
        result: dict[str, Any]
        try:
            if tier == 2:
                result = await fetch_impersonate(
                    url, warmup_homepage=True,
                    max_retries=1, timeout_s=min(timeout_s, 25.0),
                )
            elif tier == 3:
                result = await fetch_undetected(
                    url, warmup_homepage=True,
                    headless=headless, timeout_s=timeout_s,
                )
            else:  # tier 4
                result = await fetch_archive(url)
        except Exception as exc:
            logger.debug("tier %d raised: %s", tier, exc)
            attempts.append({
                "tier": tier, "status": 0, "block_class": "exception",
                "reason": str(exc)[:200],
                "elapsed_ms": int((time.time() - t0) * 1000),
            })
            rate.observe(url, None)
            continue

        status = result.get("status", 0)
        html = result.get("html", "")
        block_class = result.get("block_class", "")
        reason = result.get("reason", "")
        attempts.append({
            "tier": tier,
            "status": status,
            "block_class": block_class,
            "reason": reason[:200],
            "elapsed_ms": result.get("elapsed_ms", int((time.time() - t0) * 1000)),
        })
        ok = rate.observe(url, status if status else None)
        if not block_class and html:
            final_tier = tier
            source = result.get("source")
            captured_at = result.get("captured_at")
            break
        last_block_class = block_class
        last_reason = reason
        if not ok:
            # Rate-limit budget exhausted for this domain — stop escalating.
            logger.debug("rate-limit budget exhausted for %s at tier %d", domain, tier)
            break

    structured = {
        "json_ld": _content.extract_json_ld(html),
        "opengraph": _content.extract_opengraph(html),
        "meta": _content.extract_meta_title_description(html),
    }

    post = _post_process(html, query, markdown)

    # Record tier-aware learning.
    try:
        from superbrowser_bridge.routing import _record_routing_outcome
        if final_tier:
            _record_routing_outcome(
                domain, "browser", True,
                tier=final_tier, block_class="",
            )
        else:
            # Record the last attempted tier as a failure so we escalate sooner next time.
            if attempts:
                last = attempts[-1]
                _record_routing_outcome(
                    domain, "browser", False,
                    tier=int(last["tier"]),
                    block_class=str(last.get("block_class", "")),
                )
    except Exception:
        pass

    return {
        "content": post,
        "html": html if not (query or markdown) else "",
        "tier_used": final_tier,
        "attempts": attempts,
        "block_class": last_block_class,
        "reason": last_reason,
        "status": status,
        "source": source,
        "captured_at": captured_at,
        "structured": structured,
        "elapsed_ms": int((time.time() - start) * 1000),
    }
