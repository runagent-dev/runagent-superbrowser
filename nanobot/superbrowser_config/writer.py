"""Atomic config writes with restrictive permissions.

The file can hold API keys, so every writer (CLI, gateway, console) goes
through here: parent dir 0700, file 0600, tmp + ``os.replace`` so a crashed
write never truncates the previous config.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_config(path: Path, data: dict[str, Any]) -> None:
    path = path.expanduser()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass  # e.g. Windows or a mount that rejects chmod — best effort

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
