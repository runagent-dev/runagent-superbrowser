"""Assist ledger — one JSON file per request under ~/.superbrowser/human-assist/.

Sits BESIDE the TS captcha handoff-ledger.json (which keeps its 15-min
"recently solved" dedupe for the strategy ladder); this ledger covers ALL
assist types and powers dedupe, reminders, and the console inbox. Atomic
writes, terminal entries pruned after 24h.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .models import TERMINAL_STATES, HumanAssistRequest

_PRUNE_AFTER_S = 24 * 3600.0
DEDUPE_WINDOW_S = float(os.environ.get("SUPERBROWSER_ASSIST_DEDUPE_S", "900"))


class AssistStore:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(
            os.environ.get("SUPERBROWSER_ASSIST_DIR", "~/.superbrowser/human-assist")
        ).expanduser()

    def save(self, request: HumanAssistRequest) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        path = self.root / f"{request.id}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(request.to_dict(), indent=2))
        os.replace(tmp, path)
        self._prune()

    def load(self, request_id: str) -> HumanAssistRequest | None:
        path = self.root / f"{request_id}.json"
        try:
            return HumanAssistRequest.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError):
            return None

    def list(self) -> list[HumanAssistRequest]:
        if not self.root.is_dir():
            return []
        out: list[HumanAssistRequest] = []
        for path in sorted(self.root.glob("assist-*.json")):
            try:
                out.append(HumanAssistRequest.from_dict(json.loads(path.read_text())))
            except (OSError, ValueError):
                continue
        return out

    def recent_duplicate(
        self, *, session_id: str, domain: str, assist_type: str, window_s: float = DEDUPE_WINDOW_S
    ) -> HumanAssistRequest | None:
        """A same-(session, domain, type) request inside the dedupe window —
        used to avoid nagging the human twice for the same wall."""
        cutoff = time.time() - window_s
        for request in self.list():
            if (
                request.session_id == session_id
                and request.domain == domain
                and request.type == assist_type
                and request.created_at >= cutoff
                and request.state not in ("expired", "cancelled")
            ):
                return request
        return None

    def _prune(self) -> None:
        cutoff = time.time() - _PRUNE_AFTER_S
        for path in self.root.glob("assist-*.json"):
            try:
                data = json.loads(path.read_text())
                if data.get("state") in TERMINAL_STATES and float(data.get("createdAt", 0)) < cutoff:
                    path.unlink()
            except (OSError, ValueError):
                continue


_default: AssistStore | None = None


def default_store() -> AssistStore:
    global _default
    if _default is None:
        _default = AssistStore()
    return _default
