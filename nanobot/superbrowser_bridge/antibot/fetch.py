"""Unified, engine-pluggable web fetch.

Generalizes the Tier 2→3→4 ladder in ``fetch_auto`` into a single fetch that
also has a Tier-1 plain-HTTP fast path and Jina reader as an engine. Whatever
engine succeeds, the HTML (or Jina markdown) flows through ONE extraction
pipeline (``antibot.extract.extract``), so the agent gets consistently clean
content — fit/raw markdown, numbered citations, scored images, structured data
— no matter which engine fetched the page.

Engine ladder (``engine="auto"``):
    plain (httpx) → impersonate (curl_cffi) → browser (patchright) → jina → archive

Block detection runs on each engine's raw HTML and drives escalation; the
extraction pass runs ONCE, only on the accepted (non-blocked) body.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import httpx

from . import bot_detect
from . import rate_limit as _rl
from .extract import ExtractResult, extract
from .extract import markdown as _markdown
from .fetch_archive import fetch_archive
from .fetch_impersonate import fetch_impersonate
from .fetch_undetected import fetch_undetected

logger = logging.getLogger(__name__)

Engine = Literal["auto", "plain", "impersonate", "browser", "jina", "archive"]

_AUTO_ORDER = ("plain", "impersonate", "browser", "jina", "archive")
_ENGINE_TIER = {"plain": 1, "impersonate": 2, "browser": 3, "jina": 3, "archive": 4}
# Map engines onto the routing learning's tier vocabulary (it knows 2-4 only).
_ROUTING_TIER = {"plain": 2, "impersonate": 2, "browser": 3, "archive": 4}

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _domain_of(url: str) -> str:
    return urlparse(url).hostname or ""


def _starting_tier(domain: str) -> int:
    """1 by default (try the cheap plain fetch first); skip the cheap engines
    only when the per-domain learning says this host needs Tier 3+."""
    if not domain:
        return 1
    try:
        from superbrowser_bridge.routing import choose_starting_tier

        t = choose_starting_tier(domain)
    except Exception:
        return 1
    return t if (t and t >= 3) else 1


def _build_ladder(engine: str, max_tier: int, render: Optional[bool], domain: str) -> list[str]:
    if engine and engine != "auto":
        return [engine]
    if render is True:
        order = ["browser", "jina", "archive"]
    elif render is False:
        order = ["plain", "impersonate", "archive"]
    else:
        order = list(_AUTO_ORDER)
    order = [e for e in order if _ENGINE_TIER[e] <= max_tier]
    start_tier = _starting_tier(domain)
    if start_tier > 1:
        order = [e for e in order if _ENGINE_TIER[e] >= start_tier or e in ("jina", "archive")]
    if not order:
        order = [e for e in _AUTO_ORDER if _ENGINE_TIER[e] <= max_tier]
    return order


async def _fetch_plain(url: str, *, timeout_s: float) -> dict[str, Any]:
    """Tier-1 fast path: a bare httpx GET with a realistic UA. No proxy/cookie
    machinery (that's Tier 2+)."""
    t0 = time.time()
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout_s, headers=headers
        ) as client:
            r = await client.get(url)
        html = r.text
        status = r.status_code
        final_url = str(r.url)
    except Exception as exc:  # noqa: BLE001
        return {
            "html": "", "status": 0, "tier_used": 1, "block_class": "exception",
            "reason": str(exc)[:200], "elapsed_ms": int((time.time() - t0) * 1000),
            "final_url": url,
        }
    verdict = bot_detect.detect(html, status)
    return {
        "html": html, "status": status, "tier_used": 1,
        "block_class": verdict.klass if verdict.blocked else "",
        "reason": verdict.reason if verdict.blocked else "",
        "elapsed_ms": int((time.time() - t0) * 1000), "final_url": final_url,
    }


async def _fetch_jina(url: str, *, timeout_s: float) -> dict[str, Any]:
    """Jina reader engine — returns server-rendered MARKDOWN (no `html` key)."""
    t0 = time.time()
    headers = {"Accept": "application/json"}
    api = os.environ.get("JINA_API_KEY")
    if api:
        headers["Authorization"] = f"Bearer {api}"
    try:
        async with httpx.AsyncClient(timeout=timeout_s, headers=headers) as client:
            r = await client.get(f"https://r.jina.ai/{url}")
        if r.status_code != 200:
            return {
                "markdown": "", "status": r.status_code, "tier_used": 3, "engine": "jina",
                "block_class": "jina_error", "reason": f"jina status {r.status_code}",
                "elapsed_ms": int((time.time() - t0) * 1000), "final_url": url,
            }
        data = r.json()
        d = data.get("data", {}) if isinstance(data, dict) else {}
        content = (d.get("content") or "").strip()
        title = (d.get("title") or "").strip()
        md = f"# {title}\n\n{content}" if title else content
        return {
            "markdown": md, "status": 200, "tier_used": 3, "engine": "jina", "source": "jina",
            "block_class": "" if md else "empty", "reason": "",
            "elapsed_ms": int((time.time() - t0) * 1000), "final_url": d.get("url") or url,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "markdown": "", "status": 0, "tier_used": 3, "engine": "jina",
            "block_class": "exception", "reason": str(exc)[:200],
            "elapsed_ms": int((time.time() - t0) * 1000), "final_url": url,
        }


async def _run_engine(engine: str, url: str, *, timeout_s: float, headless: bool) -> dict[str, Any]:
    if engine == "plain":
        return await _fetch_plain(url, timeout_s=min(timeout_s, 20.0))
    if engine == "impersonate":
        return await fetch_impersonate(
            url, warmup_homepage=True, max_retries=1, timeout_s=min(timeout_s, 25.0)
        )
    if engine == "browser":
        return await fetch_undetected(
            url, warmup_homepage=True, headless=headless, timeout_s=timeout_s
        )
    if engine == "jina":
        return await _fetch_jina(url, timeout_s=min(timeout_s, 30.0))
    if engine == "archive":
        return await fetch_archive(url)
    raise ValueError(f"unknown engine: {engine!r}")


def _record_routing(domain: str, success: Optional[dict], attempts: list[dict]) -> None:
    try:
        from superbrowser_bridge.routing import _record_routing_outcome

        if success is not None:
            tier = _ROUTING_TIER.get(success.get("engine", ""))
            if tier:
                _record_routing_outcome(domain, "browser", True, tier=tier, block_class="")
        elif attempts:
            last = attempts[-1]
            tier = _ROUTING_TIER.get(last.get("engine", ""))
            if tier:
                _record_routing_outcome(
                    domain, "browser", False, tier=tier,
                    block_class=str(last.get("block_class", "")),
                )
    except Exception:
        pass


async def fetch(
    url: str,
    *,
    query: Optional[str] = None,
    engine: Engine = "auto",
    max_tier: int = 4,
    timeout_s: float = 45.0,
    headless: bool = True,
    render: Optional[bool] = None,
    score_images: bool = True,
    want_html: bool = False,
) -> dict[str, Any]:
    """Fetch ``url`` and return a rich, engine-agnostic result.

    Returns a dict: ``raw_markdown, fit_markdown, references, media, structured,
    engine_used, tier_used, attempts, block_class, reason, status, source,
    captured_at, final_url, html (only if want_html), elapsed_ms``.
    """
    start = time.time()
    domain = _domain_of(url)
    rate = _rl.default()
    ladder = _build_ladder(engine, max_tier, render, domain)

    attempts: list[dict] = []
    last_block_class = ""
    last_reason = ""
    success: Optional[dict] = None

    for eng in ladder:
        await rate.wait_if_needed(url)
        t0 = time.time()
        try:
            result = await _run_engine(eng, url, timeout_s=timeout_s, headless=headless)
        except Exception as exc:  # noqa: BLE001
            logger.debug("engine %s raised: %s", eng, exc)
            attempts.append({
                "engine": eng, "tier": _ENGINE_TIER[eng], "status": 0,
                "block_class": "exception", "reason": str(exc)[:200],
                "elapsed_ms": int((time.time() - t0) * 1000),
            })
            rate.observe(url, None)
            continue
        result.setdefault("engine", eng)
        status = result.get("status", 0)
        block_class = result.get("block_class", "")
        reason = result.get("reason", "")
        attempts.append({
            "engine": eng, "tier": _ENGINE_TIER[eng], "status": status,
            "block_class": block_class, "reason": reason[:200],
            "elapsed_ms": result.get("elapsed_ms", int((time.time() - t0) * 1000)),
        })
        ok = rate.observe(url, status if status else None)
        body = (result.get("html") or result.get("markdown") or "").strip()
        if not block_class and body:
            success = result
            break
        last_block_class = block_class
        last_reason = reason
        if not ok:
            logger.debug("rate-limit budget exhausted for %s at engine %s", domain, eng)
            break

    # --- single extraction pass on the accepted body -------------------------
    er = ExtractResult()
    engine_used = None
    tier_used = 0
    status = 0
    source: Optional[str] = None
    captured_at: Optional[str] = None
    final_url = url
    html_out = ""

    if success is not None:
        engine_used = success.get("engine")
        tier_used = success.get("tier_used", 0)
        status = success.get("status", 0)
        source = success.get("source")
        captured_at = success.get("captured_at")
        final_url = success.get("final_url") or url
        if "markdown" in success and "html" not in success:
            # Jina path: already-clean markdown. Still apply citations.
            md = success.get("markdown", "")
            text, refs = _markdown.convert_links_to_citations(md, final_url)
            er = ExtractResult(
                raw_markdown=text, fit_markdown=text, references=refs,
                word_count=len(text.split()),
            )
        else:
            html_out = success.get("html", "")
            er = extract(html_out, url=final_url, query=query, score_images=score_images)

    _record_routing(domain, success, attempts)

    return {
        "raw_markdown": er.raw_markdown,
        "fit_markdown": er.fit_markdown,
        "references": er.references,
        "media": er.media,
        "structured": er.structured,
        "engine_used": engine_used,
        "tier_used": tier_used,
        "attempts": attempts,
        "block_class": last_block_class,
        "reason": last_reason,
        "status": status,
        "source": source,
        "captured_at": captured_at,
        "final_url": final_url,
        "html": html_out if want_html else "",
        "elapsed_ms": int((time.time() - start) * 1000),
    }
