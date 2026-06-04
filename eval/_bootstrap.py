"""Shared bootstrap for the eval harness.

Makes ``superbrowser_bridge`` / ``vision_agent`` importable and loads the
project ``.env`` — exactly what ``nanobot/run.py`` and ``test_superbrowser.py``
do — so every eval entry point sees the same environment the production CLI
sees (vision keys, headless mode, etc.).

This module runs its side effects on import. ``eval/__init__.py`` imports it,
so any ``python -m eval.<module>`` invocation bootstraps before the submodule
body executes.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# eval/ lives at <repo>/eval/ ; the importable tree (superbrowser_bridge,
# vision_agent) is <repo>/nanobot/. Core `nanobot` is pip-installed separately.
REPO_ROOT = Path(__file__).resolve().parent.parent
NANOBOT_TREE = REPO_ROOT / "nanobot"

if str(NANOBOT_TREE) not in sys.path:
    sys.path.insert(0, str(NANOBOT_TREE))

try:  # dotenv optional; env can still be set in the shell.
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("NANOBOT_CONFIG", str(Path.home() / ".nanobot" / "config.json"))
)


def read_active_model(config_path: Path | None = None) -> dict:
    """Read ``agents.defaults.{model,provider,temperature}`` from the active
    nanobot config so a run can be auto-labeled by whatever model the user
    currently has wired in ``~/.nanobot/config.json``.
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    try:
        data = json.loads(path.read_text())
        defaults = data.get("agents", {}).get("defaults", {})
        return {
            "model": defaults.get("model", "unknown"),
            "provider": defaults.get("provider", "unknown"),
            "temperature": defaults.get("temperature"),
        }
    except Exception:
        return {"model": "unknown", "provider": "unknown", "temperature": None}


def slugify(text: str) -> str:
    """Filesystem-safe label, e.g. ``moonshot/kimi-k2`` -> ``moonshot-kimi-k2``."""
    out = "".join(c if c.isalnum() else "-" for c in str(text).lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "unknown"
