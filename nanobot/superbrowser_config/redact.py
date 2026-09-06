"""Secret redaction for CLI output and the console API.

Secret-bearing paths come from the ``secret: true`` flags in ``envmap.json``
plus name heuristics for keys the map doesn't cover (e.g. gateway.token,
which is intentionally not env-projected).
"""

from __future__ import annotations

from typing import Any

from .envmap import SECRET_PATHS

_SECRET_NAME_FRAGMENTS = ("apikey", "api_key", "token", "secret", "password", "encryptionkey")


def is_secret_path(dotted: str) -> bool:
    if dotted in SECRET_PATHS:
        return True
    leaf = dotted.rsplit(".", 1)[-1].lower()
    return any(fragment in leaf for fragment in _SECRET_NAME_FRAGMENTS)


def mask(value: Any) -> str:
    text = str(value)
    if len(text) <= 4:
        return "****"
    return f"****{text[-4:]}"


def redact_config(data: Any, prefix: str = "") -> Any:
    """Deep copy with secrets replaced by ``{"set": true, "last4": "..."}``."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            dotted = f"{prefix}.{key}" if prefix else key
            if is_secret_path(dotted) and isinstance(value, (str, int, float)) and str(value):
                out[key] = {"set": True, "last4": str(value)[-4:]}
            else:
                out[key] = redact_config(value, dotted)
        return out
    if isinstance(data, list):
        return [redact_config(item, prefix) for item in data]
    return data
