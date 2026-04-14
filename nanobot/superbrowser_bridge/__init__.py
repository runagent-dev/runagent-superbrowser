# SuperBrowser nanobot bridge
#
# Load the project-root .env at package-import time so env vars reach the
# Python process regardless of the entry point. Previously only run.py
# called load_dotenv, which meant invocations via `nanobot run`, MCP,
# gateway, or any other launcher silently missed VISION_ENABLED,
# VISION_API_KEY, SUPERBROWSER_*, etc., and the vision preprocessor never
# kicked in — screenshots fell through to the legacy image-blocks path.
#
# load_dotenv defaults to `override=False`, so a shell-exported var wins
# over the file. Failures are swallowed: dotenv is optional and the env
# can still be configured via the shell.
from __future__ import annotations

import os as _os
from pathlib import Path as _Path

try:
    from dotenv import load_dotenv as _load_dotenv

    _env_candidates = [
        _Path(__file__).resolve().parent.parent.parent / ".env",  # repo root
        _Path.cwd() / ".env",
    ]
    for _p in _env_candidates:
        if _p.exists():
            _load_dotenv(_p)
            break
except ImportError:
    # python-dotenv not installed — fine, env must be set in the shell.
    pass

del _os, _Path
