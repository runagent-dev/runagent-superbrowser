"""Compose the gateway's channel config into ~/.nanobot/config.json.

Never edits the synced ``_nanobot_config.py`` — imports it. Ordering matters:
``ensure``/``bootstrap`` may REPLACE the whole file (the NANOBOT_CONFIG_JSON_B64
path), so the channels merge always runs AFTER it. In ``--home`` mode the
brain merge is done locally (the bridge helper writes the hardcoded
~/.nanobot path, which a homed instance must not touch).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from .config import GatewaySettings

_MERGE_CHANNELS = ("whatsapp", "telegram", "discord")


def default_nanobot_config_path() -> Path:
    return Path("~/.nanobot/config.json").expanduser()


def sync_channels_into_nanobot_config(
    settings: GatewaySettings,
    *,
    config_path: Path | None = None,
    homed: bool = False,
) -> Path:
    """LLM bootstrap (onboard-wins) + gateway channels merge. Returns the path."""
    path = config_path or default_nanobot_config_path()

    if homed:
        _merge_llm_env_locally(path)
    else:
        try:
            from runagent_superbrowser._nanobot_config import bootstrap_nanobot_config

            bootstrap_nanobot_config()
        except Exception:  # noqa: BLE001 - no LLM config yet is survivable at merge time
            logger.warning("LLM bootstrap failed; continuing with channel merge only")

    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            logger.warning("unreadable {} — rewriting channels section from scratch", path)
            data = {}

    channels = data.setdefault("channels", {})
    for name in _MERGE_CHANNELS:
        section = settings.channels.get(name)
        if not isinstance(section, dict) or not section:
            continue
        existing = channels.get(name)
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(section)
        channels[name] = merged

    _atomic_write(path, data)
    return path


def _merge_llm_env_locally(path: Path) -> None:
    """Minimal LLM_* env → config merge for --home instances.

    Mirrors the spirit of the synced bridge helper (provider/model/apiKey from
    LLM_PROVIDER / LLM_MODEL / LLM_API_KEY or a conventional key) without
    importing its hardcoded ~/.nanobot path.
    """
    provider = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    api_key = (os.environ.get("LLM_API_KEY") or "").strip()
    model = (os.environ.get("LLM_MODEL") or "").strip()
    if not api_key:
        for env_key, conventional in (
            ("OPENAI_API_KEY", "openai"),
            ("ANTHROPIC_API_KEY", "anthropic"),
            ("GEMINI_API_KEY", "gemini"),
            ("GROQ_API_KEY", "groq"),
        ):
            value = (os.environ.get(env_key) or "").strip()
            if value:
                api_key = value
                provider = provider or conventional
                break
    if not (api_key and provider):
        return

    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
    providers = data.setdefault("providers", {})
    entry = providers.setdefault(provider, {})
    entry.setdefault("apiKey", api_key)
    defaults = data.setdefault("agents", {}).setdefault("defaults", {})
    defaults.setdefault("provider", provider)
    if model:
        defaults.setdefault("model", model)
    _atomic_write(path, data)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
