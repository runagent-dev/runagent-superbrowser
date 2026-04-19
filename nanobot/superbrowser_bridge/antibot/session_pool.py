"""In-process SessionPool with error scoring and rotation.

Pattern ported from crawlee-python
(references:
  /root/agentic-browser/crawlee-python/src/crawlee/sessions/_session.py:22-150
  /root/agentic-browser/crawlee-python/src/crawlee/sessions/_session_pool.py:28-147).
Stdlib-only reimplementation. No pydantic.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Session:
    id: str
    created_at: float
    max_age_s: float
    max_usage: int
    max_error_score: float
    usage_count: int = 0
    error_score: float = 0.0
    cookies: dict[str, str] = field(default_factory=dict)
    profile: str = ""

    def record_success(self) -> None:
        self.usage_count += 1
        # Mirror crawlee's soft-recovery: a success reduces error pressure.
        self.error_score = max(0.0, self.error_score - 0.5)

    def record_failure(self, weight: float = 1.0) -> None:
        self.usage_count += 1
        self.error_score += weight

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) >= self.max_age_s

    def is_over_usage(self) -> bool:
        return self.usage_count >= self.max_usage

    def is_blocked(self) -> bool:
        return self.error_score >= self.max_error_score

    def is_retired(self) -> bool:
        return self.is_expired() or self.is_over_usage() or self.is_blocked()


class SessionPool:
    """Thread-safe pool of Session objects.

    - `acquire()` returns a fresh or reusable session.
    - `release()` puts it back unless it's retired.
    - `rotate()` forcibly retires and returns a new one (used when the
       caller detects a block and wants a clean session immediately).
    """

    def __init__(
        self,
        *,
        max_pool_size: int = 100,
        max_age_s: float = 30 * 60,
        max_usage: int = 50,
        max_error_score: float = 3.0,
        default_profile: str = "chrome124_mac",
    ) -> None:
        self._max = max_pool_size
        self._age = max_age_s
        self._use = max_usage
        self._err = max_error_score
        self._profile = default_profile
        self._lock = threading.Lock()
        self._free: list[Session] = []
        self._busy: dict[str, Session] = {}

    def _new(self, profile: Optional[str] = None) -> Session:
        return Session(
            id=uuid.uuid4().hex,
            created_at=time.time(),
            max_age_s=self._age,
            max_usage=self._use,
            max_error_score=self._err,
            profile=profile or self._profile,
        )

    def acquire(self, *, profile: Optional[str] = None) -> Session:
        with self._lock:
            while self._free:
                s = self._free.pop()
                if s.is_retired():
                    continue
                if profile and s.profile != profile:
                    # Keep it for another caller; pick a fresh matching one.
                    self._free.insert(0, s)
                    continue
                self._busy[s.id] = s
                return s
            s = self._new(profile)
            self._busy[s.id] = s
            return s

    def release(self, session: Session) -> None:
        with self._lock:
            self._busy.pop(session.id, None)
            if session.is_retired():
                return
            if len(self._free) + len(self._busy) >= self._max:
                return  # drop on the floor, pool is full
            self._free.append(session)

    def rotate(self, session: Session, *, profile: Optional[str] = None) -> Session:
        """Retire `session` and return a new one. Used after a detected block."""
        with self._lock:
            self._busy.pop(session.id, None)
            new = self._new(profile or session.profile)
            self._busy[new.id] = new
            return new

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"free": len(self._free), "busy": len(self._busy)}


# Module-level default pool so fetch_impersonate can share one across calls.
_DEFAULT: SessionPool | None = None


def default_pool() -> SessionPool:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = SessionPool()
    return _DEFAULT
