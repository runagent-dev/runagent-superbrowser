"""Machine-profile presets, loaded from the shared ``presets.json``.

``presets.json`` is duplicated (marker-wrapped) in ``src/config/presets.ts``
for the TS engine loader; ``scripts/check_config_parity.sh`` keeps the copies
identical. Edit the JSON here, mirror it there.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any


def _load() -> dict[str, dict[str, Any]]:
    return json.loads(resources.files(__package__).joinpath("presets.json").read_text())


PRESETS: dict[str, dict[str, Any]] = _load()


def preset_for(profile: str) -> dict[str, Any]:
    return PRESETS.get(profile, {})
