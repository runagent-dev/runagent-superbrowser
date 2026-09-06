"""Gateway settings — the ``gateway`` section of ~/.superbrowser/config.json.

This section is deliberately NOT env-projected by superbrowser_config (new
code reads it directly). Env overrides use the ``SB_GATEWAY_*`` prefix so a
Docker/compose deployment can flip individual knobs without editing the file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Base(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")


class ProgressSettings(_Base):
    min_interval_s: float = 30.0
    keepalive_s: float = 90.0
    keepalive_screenshot: bool = False


class ScreenshotSettings(_Base):
    attach_final: bool = True
    max_side: int = 1600
    jpeg_quality: int = 80


class HandoffSettings(_Base):
    reminder_minutes: float = 5.0
    timeout_minutes: float = 30.0


class GatewaySettings(_Base):
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8460
    token: str | None = None
    loopback_bypass: bool = True
    engine_url: str = "http://127.0.0.1:3100"
    max_concurrent_tasks: int = 1
    owners: dict[str, list[str]] = Field(default_factory=dict)
    channels: dict[str, dict[str, Any]] = Field(default_factory=dict)
    progress: ProgressSettings = Field(default_factory=ProgressSettings)
    screenshots: ScreenshotSettings = Field(default_factory=ScreenshotSettings)
    handoff: HandoffSettings = Field(default_factory=HandoffSettings)
    task_timeout_s: float = 0.0
    notify_on_restart: bool = True
    peek_aliases: list[str] = Field(default_factory=lambda: ["/peek", "/shot"])

    # runtime-only (never persisted)
    home: Path | None = Field(default=None, exclude=True)

    @property
    def data_dir(self) -> Path:
        """Where the gateway keeps its own small state files."""
        if self.home is not None:
            return self.home
        return Path("~/.superbrowser/gateway").expanduser()


def load_settings(home: Path | None = None) -> GatewaySettings:
    """Settings = config.json ``gateway`` section, then SB_GATEWAY_* env wins."""
    from superbrowser_config import load as load_product_config

    raw = (load_product_config() or {}).get("gateway") or {}
    settings = GatewaySettings.model_validate(raw)
    settings.home = home

    env = os.environ
    if env.get("SB_GATEWAY_BIND"):
        settings.bind = env["SB_GATEWAY_BIND"]
    if env.get("SB_GATEWAY_CONSOLE_PORT") or env.get("SB_GATEWAY_PORT"):
        settings.port = int(env.get("SB_GATEWAY_CONSOLE_PORT") or env["SB_GATEWAY_PORT"])
    if env.get("SB_GATEWAY_TOKEN"):
        settings.token = env["SB_GATEWAY_TOKEN"]
    # SUPERBROWSER_URL is the established engine-URL contract everywhere else.
    if env.get("SB_GATEWAY_ENGINE_URL") or env.get("SUPERBROWSER_URL"):
        settings.engine_url = (env.get("SB_GATEWAY_ENGINE_URL") or env["SUPERBROWSER_URL"]).rstrip("/")
    if env.get("SB_GATEWAY_MAX_CONCURRENT_TASKS"):
        settings.max_concurrent_tasks = int(env["SB_GATEWAY_MAX_CONCURRENT_TASKS"])

    if settings.bind not in ("127.0.0.1", "localhost", "::1") and not settings.token:
        raise ValueError(
            f"gateway.bind={settings.bind!r} without gateway.token — a non-loopback "
            "bind requires a token (set gateway.token in ~/.superbrowser/config.json "
            "or SB_GATEWAY_TOKEN)."
        )
    return settings
