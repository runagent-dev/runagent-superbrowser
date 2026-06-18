"""runagent_superbrowser — the SuperBrowser Python SDK.

Two kinds of browser automation behind one object:

* lightweight *fetch* (HTTP / stealth fetch / search), and
* full *browser* interaction (a real headless browser).

``mode="auto"`` lets the agent decide between them; ``mode="fetch"`` /
``mode="browser"`` force one. The system prompting ships with the package, so
you write terse goals, not step-by-step instructions.

    from runagent_superbrowser import SuperBrowser
    res = SuperBrowser().run("what's the top story on hacker news?", mode="fetch")
    print(res.text)
"""

from __future__ import annotations

from .client import SuperBrowser
from .modes import Mode
from .result import RunResult
from .server import ServerStartError, ServerUnavailable

try:  # match the installed distribution version when available
    from importlib.metadata import version as _version

    __version__ = _version("runagent-superbrowser")
except Exception:  # noqa: BLE001 - source checkout / not installed
    __version__ = "0.1.0"

__all__ = [
    "Mode",
    "RunResult",
    "ServerStartError",
    "ServerUnavailable",
    "SuperBrowser",
    "__version__",
]
