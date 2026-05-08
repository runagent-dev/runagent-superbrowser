"""TTL-bounded LRU cache for VisionResponses.

Keyed on (session_id, url, dom_hash, dom_text_hash, intent_bucket,
subgoal_id). The subgoal_id is included because subgoal-aware vision
returns different bbox emphasis per subgoal even on an identical
screenshot — caching across transitions would defeat the targeting.
The dom_text_hash (Phase 1.2) extends the original dom_hash key:
two pages can share the same structural DOM (same element listing)
but differ in visible content — e.g. a dismissed cookie banner
replaced by an autocomplete dropdown with similar tag structure, or
a search-results page after vs before applying a filter. Without
the text-content hash, the cache hits and serves stale bboxes that
manifest as the vision agent "hallucinating" targets. In-process,
no disk persistence — vision analyses are stale after minutes
anyway, so there's no point in paying serialization cost.

Thread-safe via a single asyncio.Lock because the cache is read and
written from async tool handlers that may interleave.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from dataclasses import dataclass

from .schemas import VisionResponse


# (session_id, url, dom_hash, dom_text_hash, intent_bucket, subgoal_id)
CacheKey = tuple[str, str, str, str, str, str]


@dataclass
class _Entry:
    response: VisionResponse
    stored_at: float


class VisionCache:
    def __init__(self, *, max_size: int = 200, ttl_s: float = 60.0) -> None:
        # TTL default dropped from 300s → 60s: a 5-minute cache made
        # long-running agents see identical bboxes across many
        # iterations when URL + DOM hash didn't change (dynamic content
        # loads, spinners clearing, lazy-rendered widgets appearing
        # with the same container DOM). 60s is short enough that a slow
        # page still refreshes per realistic user pace, long enough
        # that a chained sequence of tool calls (click → wait → verify)
        # within the same page state still hits cache.
        self._store: "OrderedDict[CacheKey, _Entry]" = OrderedDict()
        self._max = max_size
        self._ttl = ttl_s
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> "VisionCache":
        return cls(
            max_size=int(os.environ.get("VISION_CACHE_SIZE") or "200"),
            ttl_s=float(os.environ.get("VISION_CACHE_TTL_SEC") or "60"),
        )

    async def bust(self, key: CacheKey) -> None:
        """Force-remove a key so the next `get()` misses and the caller
        re-runs the vision model. Used by the bridge when the
        dead-click guard detects the agent is stuck — fresh bboxes may
        reveal that the previous pass mislabelled the target."""
        async with self._lock:
            self._store.pop(key, None)

    async def bust_session(self, session_id: str) -> int:
        """Evict every entry for a session. Used by browser_rewind and
        on URL-change boundaries where cached bboxes must not follow
        the worker into a different page state. Returns evicted count."""
        if not session_id:
            return 0
        async with self._lock:
            keys = [k for k in self._store if k and k[0] == session_id]
            for k in keys:
                self._store.pop(k, None)
            return len(keys)

    async def get(self, key: CacheKey) -> VisionResponse | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if (time.monotonic() - entry.stored_at) > self._ttl:
                # Expired — evict and miss.
                self._store.pop(key, None)
                return None
            # Touch for LRU ordering.
            self._store.move_to_end(key)
            # Return a copy with cached=True flipped, so the brain can see
            # the decision without mutating what's stored.
            resp = entry.response.model_copy(update={"cached": True})
            return resp

    async def put(self, key: CacheKey, response: VisionResponse) -> None:
        async with self._lock:
            # Store with cached=False so a first-read returns cached=False
            # and subsequent reads return cached=True (via the get() copy).
            normalized = response.model_copy(update={"cached": False})
            self._store[key] = _Entry(response=normalized, stored_at=time.monotonic())
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()
