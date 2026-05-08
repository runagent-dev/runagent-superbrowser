"""Per-domain rate limiter with exponential backoff on 429/503.

Pattern ported from crawl4ai's `RateLimiter`
(reference: /root/agentic-browser/crawl4ai/crawl4ai/async_dispatcher.py:28-85).
Stdlib-only. Thread-safe via asyncio.Lock (one per domain).
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse


@dataclass
class _DomainState:
    last_request_time: float = 0.0
    current_delay: float = 0.0
    fail_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RateLimiter:
    def __init__(
        self,
        *,
        base_delay: tuple[float, float] = (0.5, 1.5),
        max_delay: float = 60.0,
        max_retries: int = 3,
        rate_limit_codes: tuple[int, ...] = (429, 503),
    ) -> None:
        self._base = base_delay
        self._max = max_delay
        self._max_retries = max_retries
        self._codes = tuple(rate_limit_codes)
        self._domains: dict[str, _DomainState] = {}
        self._domains_lock = asyncio.Lock()

    @staticmethod
    def _domain(url: str) -> str:
        return urlparse(url).netloc or "unknown"

    async def _state_for(self, domain: str) -> _DomainState:
        async with self._domains_lock:
            state = self._domains.get(domain)
            if state is None:
                state = _DomainState()
                self._domains[domain] = state
            return state

    async def wait_if_needed(self, url: str) -> None:
        """Block until the per-domain backoff window has passed."""
        domain = self._domain(url)
        state = await self._state_for(domain)
        async with state.lock:
            now = time.time()
            if state.last_request_time:
                waited = now - state.last_request_time
                to_wait = max(0.0, state.current_delay - waited)
                if to_wait > 0:
                    await asyncio.sleep(to_wait)
            if state.current_delay == 0.0:
                state.current_delay = random.uniform(*self._base)
            state.last_request_time = time.time()

    def observe(self, url: str, status_code: Optional[int]) -> bool:
        """Update delay/fail counters from a response.

        Returns False when max_retries on rate-limit codes has been exceeded,
        signalling the caller should give up on this domain for the tier.
        """
        domain = self._domain(url)
        state = self._domains.get(domain)
        if state is None:
            return True
        if status_code in self._codes:
            state.fail_count += 1
            if state.fail_count > self._max_retries:
                return False
            state.current_delay = min(
                state.current_delay * 2 * random.uniform(0.75, 1.25),
                self._max,
            )
        else:
            # Success or non-rate-limit failure — ease the delay back toward base.
            state.current_delay = max(
                random.uniform(*self._base),
                state.current_delay * 0.75,
            )
            state.fail_count = 0
        return True

    def snapshot(self) -> dict[str, dict]:
        return {
            d: {
                "last_request_time": s.last_request_time,
                "current_delay": round(s.current_delay, 3),
                "fail_count": s.fail_count,
            }
            for d, s in self._domains.items()
        }


_DEFAULT: RateLimiter | None = None


def default() -> RateLimiter:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = RateLimiter()
    return _DEFAULT
