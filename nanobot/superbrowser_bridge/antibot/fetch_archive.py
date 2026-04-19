"""Tier 4: archive fallback via the Wayback Machine.

Last-ditch retrieval when Tiers 0-3 all fail. Returns stale content. Caller
is responsible for surfacing `captured_at` to the user so they know the
data is not live.

Google Cache was removed in 2024 (the `cache:` operator returns a consent
interstitial, not content) and is NOT used.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import httpx

from . import bot_detect

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
_WAYBACK_REPLAY = "https://web.archive.org/web/{ts}if_/{url}"


async def _try_wayback(client: httpx.AsyncClient, url: str) -> dict | None:
    """Use CDX search to find the most recent 200-OK snapshot.

    The older `archive.org/wayback/available` endpoint silently returns
    `archived_snapshots: {}` for URLs that CDX clearly has — avoid it.
    """
    try:
        r = await client.get(
            _WAYBACK_CDX,
            params={
                "url": url,
                "limit": "-3",  # last 3 rows, newest last
                "output": "json",
                "filter": "statuscode:200",
            },
            timeout=15.0,
        )
        if r.status_code != 200:
            return None
        rows = r.json()
    except Exception as exc:
        logger.debug("wayback CDX failed: %s", exc)
        return None
    # First row is the header; we want the last data row.
    if not isinstance(rows, list) or len(rows) < 2:
        return None
    latest = rows[-1]
    try:
        ts = latest[1]
        orig = latest[2]
    except (IndexError, TypeError):
        return None
    # `if_` flag returns the raw archived HTML without Wayback's toolbar wrapper.
    snap_url = _WAYBACK_REPLAY.format(ts=ts, url=orig)
    try:
        r2 = await client.get(snap_url, timeout=30.0, follow_redirects=True)
    except Exception as exc:
        logger.debug("wayback replay failed: %s", exc)
        return None
    if r2.status_code != 200 or not r2.text:
        return None
    try:
        captured_at = datetime.strptime(ts, "%Y%m%d%H%M%S").isoformat()
    except Exception:
        captured_at = ts
    return {
        "html": r2.text,
        "status": r2.status_code,
        "source": "wayback",
        "captured_at": captured_at,
        "snapshot_url": snap_url,
    }


async def fetch_archive(url: str) -> dict[str, Any]:
    """Return an archived snapshot of `url`, or an empty block verdict if none.

    Returns: {html, status, tier_used: 4, source, captured_at, reason?,
              block_class?, elapsed_ms}
    """
    start = time.time()
    async with httpx.AsyncClient(headers={"User-Agent": _UA}) as client:
        got = await _try_wayback(client, url)
        if got:
            v = bot_detect.detect(got["html"], got["status"])
            if not v.blocked:
                return {
                    "html": got["html"],
                    "status": got["status"],
                    "tier_used": 4,
                    "source": got["source"],
                    "captured_at": got["captured_at"],
                    "snapshot_url": got["snapshot_url"],
                    "block_class": "",
                    "reason": "",
                    "elapsed_ms": int((time.time() - start) * 1000),
                }
    return {
        "html": "",
        "status": 0,
        "tier_used": 4,
        "source": None,
        "captured_at": None,
        "snapshot_url": None,
        "block_class": "empty",
        "reason": "no archive snapshot found",
        "elapsed_ms": int((time.time() - start) * 1000),
    }
