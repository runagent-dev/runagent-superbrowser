"""Tiered proxy rotation with per-domain auto-demotion.

Pattern ported from crawlee-python's `ProxyConfiguration` (reference:
/root/agentic-browser/crawlee-python/src/crawlee/proxy_configuration.py:56-269).
Stdlib-only, thread-safe.

Tier 0: datacenter proxies from `PROXY_POOL` / `PROXY_DEFAULT` (existing env).
Tier 1: residential proxies from `PROXY_POOL_RESIDENTIAL` (new env, optional).
Each tier is a list; we pick round-robin within a tier and demote the
domain to a higher-numbered tier on block verdicts.
"""

from __future__ import annotations

import itertools
import os
import threading
from typing import Optional


def _parse_pool(raw: str) -> list[str]:
    """Parse 'region:url,region:url' or 'url,url' into a flat URL list."""
    if not raw:
        return []
    out: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part and not part.startswith(("http://", "https://", "socks5://", "socks4://")):
            # Drop leading "region:" prefix used by PROXY_POOL.
            _, _, url = part.partition(":")
            part = url.strip()
        if part:
            out.append(part)
    return out


class ProxyTiers:
    def __init__(self, tiers: Optional[list[list[str]]] = None) -> None:
        self._tiers: list[list[str]] = tiers if tiers is not None else self._from_env()
        self._cursors = [itertools.cycle(t) if t else None for t in self._tiers]
        self._domain_tier: dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _from_env() -> list[list[str]]:
        tier0 = _parse_pool(os.environ.get("PROXY_POOL", ""))
        default = os.environ.get("PROXY_DEFAULT", "").strip()
        if default and default not in tier0:
            tier0.append(default)
        tier1 = _parse_pool(os.environ.get("PROXY_POOL_RESIDENTIAL", ""))
        tiers = [tier0, tier1]
        return tiers

    def reload(self) -> None:
        with self._lock:
            self._tiers = self._from_env()
            self._cursors = [itertools.cycle(t) if t else None for t in self._tiers]

    def current_tier(self, domain: str) -> int:
        with self._lock:
            return self._domain_tier.get(domain, 0)

    def pick(self, domain: str) -> Optional[str]:
        """Return a proxy URL for this domain, or None for direct."""
        with self._lock:
            tier = self._domain_tier.get(domain, 0)
            # Skip empty tiers up the ladder.
            while tier < len(self._tiers) and not self._tiers[tier]:
                tier += 1
            if tier >= len(self._tiers):
                return None
            cur = self._cursors[tier]
            return next(cur) if cur else None

    def demote(self, domain: str) -> int:
        """Bump the domain one tier stricter. Returns the new tier index.

        If already at the last non-empty tier, stays put.
        """
        with self._lock:
            cur = self._domain_tier.get(domain, 0)
            for nxt in range(cur + 1, len(self._tiers)):
                if self._tiers[nxt]:
                    self._domain_tier[domain] = nxt
                    return nxt
            # Already at top; record stickiness at the last non-empty index.
            self._domain_tier[domain] = cur
            return cur

    def reset(self, domain: str) -> None:
        with self._lock:
            self._domain_tier.pop(domain, None)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "tier_sizes": [len(t) for t in self._tiers],
                "domain_tiers": dict(self._domain_tier),
            }


_DEFAULT: ProxyTiers | None = None


def default() -> ProxyTiers:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = ProxyTiers()
    return _DEFAULT
