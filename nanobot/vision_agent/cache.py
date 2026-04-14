"""TTL-bounded LRU cache for VisionResponses.

Keyed on (session_id, url, dom_hash, intent_bucket). In-process, no disk
persistence — vision analyses are stale after minutes anyway, so there's
no point in paying serialization cost.

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


CacheKey = tuple[str, str, str, str]  # (session_id, url, dom_hash, intent_bucket)


@dataclass
class _Entry:
    response: VisionResponse
    stored_at: float


class VisionCache:
    def __init__(self, *, max_size: int = 200, ttl_s: float = 300.0) -> None:
        self._store: "OrderedDict[CacheKey, _Entry]" = OrderedDict()
        self._max = max_size
        self._ttl = ttl_s
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> "VisionCache":
        return cls(
            max_size=int(os.environ.get("VISION_CACHE_SIZE") or "200"),
            ttl_s=float(os.environ.get("VISION_CACHE_TTL_SEC") or "300"),
        )

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
