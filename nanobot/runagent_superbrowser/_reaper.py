"""Best-effort cancel-on-exit for REST-transport local-agent runs.

The default local-agent transport is streaming, where a dying client drops the
WebSocket and the server unwinds the task on its own. The REST fallback
(``SUPERBROWSER_RUN_TRANSPORT=rest``) has no such signal — so when a client
uses it against a server that exposes the ``cancel`` entrypoint, this module
registers ``atexit`` + SIGINT/SIGTERM hooks that POST a cancel for every
still-running REST task before the process dies. SIGKILL remains a documented
residual risk of the REST fallback (nothing runs on SIGKILL).

Handlers chain to whatever was installed before them, and are only attached
from the main thread (signal.signal raises elsewhere).
"""

from __future__ import annotations

import atexit
import signal
import threading
from typing import Any

from . import lifecycle

_lock = threading.Lock()
_clients: list[Any] = []
_hooks_installed = False


def install(client: Any) -> None:
    """Track ``client`` for exit-time cancels; install the hooks once."""
    global _hooks_installed
    with _lock:
        if not any(existing is client for existing in _clients):
            _clients.append(client)
        if _hooks_installed:
            return
        _hooks_installed = True

    atexit.register(_cleanup)

    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous = signal.getsignal(sig)
            signal.signal(sig, _make_handler(previous))
        except (ValueError, OSError):  # non-main thread / unsupported platform
            pass


def _cleanup() -> None:
    with _lock:
        clients = list(_clients)
    for record in lifecycle.list_tasks():
        if record.get("state") in ("running", "cancelling") and record.get("transport") == "rest-local":
            for client in clients:
                try:
                    if client.cancel(record["handle"]):
                        break
                except Exception:  # noqa: BLE001 - exiting; nothing to do about it
                    pass


def _make_handler(previous: Any):
    def _handler(signum: int, frame: Any) -> None:
        _cleanup()
        if callable(previous):
            previous(signum, frame)
        else:
            try:
                signal.signal(signum, signal.SIG_DFL)
                signal.raise_signal(signum)
            except (ValueError, OSError):
                raise SystemExit(128 + signum)

    return _handler
