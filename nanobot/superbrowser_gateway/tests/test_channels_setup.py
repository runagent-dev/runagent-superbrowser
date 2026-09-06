"""Channel wiring: manager swap + nanobot channels merge (no network, no SDKs required)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from nanobot.bus.queue import MessageBus
from nanobot.config.loader import load_config

from superbrowser_gateway.channels_setup import GatewayChannelManager
from superbrowser_gateway.config import GatewaySettings
from superbrowser_gateway.login_broker import LoginBroker
from superbrowser_gateway.nanobot_channels import sync_channels_into_nanobot_config
from superbrowser_gateway.wa_channel import GatewayWhatsAppChannel


def _config_with_channels(**channels):
    # Start from schema defaults (a non-existent path => Config()), NEVER from
    # the developer's real ~/.nanobot/config.json: a box with WhatsApp enabled
    # there would otherwise leak a live channel into these hermetic tests.
    cfg = load_config(Path("/nonexistent/superbrowser-gateway-test-config.json"))
    for name, section in channels.items():
        cfg.channels.__pydantic_extra__[name] = section
    return cfg


def test_whatsapp_channel_swapped_for_qr_relay():
    cfg = _config_with_channels(whatsapp={"enabled": True, "allowFrom": ["15551234567"]})
    broker = LoginBroker()
    mgr = GatewayChannelManager(cfg, MessageBus(), broker=broker)
    wa = mgr.channels.get("whatsapp")
    assert isinstance(wa, GatewayWhatsAppChannel)
    assert wa._broker is broker
    # camelCase allowFrom parsed into the validated model
    assert wa.config.allow_from == ["15551234567"]
    # progress flags copied from the manager-configured base instance
    assert isinstance(wa.send_progress, bool)


def test_no_whatsapp_no_swap():
    cfg = _config_with_channels(telegram={"enabled": True, "token": "x:y", "allowFrom": ["1"]})
    mgr = GatewayChannelManager(cfg, MessageBus(), broker=LoginBroker())
    # telegram present (SDK is a base dep), no whatsapp to swap
    assert "telegram" in mgr.channels
    assert "whatsapp" not in mgr.channels


def test_sync_channels_merges_camelcase_into_nanobot_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # avoid touching the real ~/.nanobot; homed mode does a local LLM merge only
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"providers": {"openai": {"apiKey": "keep-me"}}}))

    settings = GatewaySettings()
    settings.channels = {
        "whatsapp": {"enabled": True, "allowFrom": ["15551234567"]},
        "telegram": {"enabled": True, "token": "bot:token", "allowFrom": ["999"]},
        "discord": {"enabled": False},
    }
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    sync_channels_into_nanobot_config(settings, config_path=config_path, homed=True)

    data = json.loads(config_path.read_text())
    # existing providers preserved (B64-style content isn't clobbered)
    assert data["providers"]["openai"]["apiKey"] == "keep-me"
    # channels written in canonical camelCase
    assert data["channels"]["whatsapp"] == {"enabled": True, "allowFrom": ["15551234567"]}
    assert data["channels"]["telegram"]["token"] == "bot:token"
    assert data["channels"]["discord"] == {"enabled": False}


def test_sync_preserves_existing_channel_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"channels": {"telegram": {"reactEmoji": "🤖", "enabled": False}}})
    )
    settings = GatewaySettings()
    settings.channels = {"telegram": {"enabled": True, "token": "t"}}
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    sync_channels_into_nanobot_config(settings, config_path=config_path, homed=True)
    tg = json.loads(config_path.read_text())["channels"]["telegram"]
    assert tg["reactEmoji"] == "🤖"  # pre-existing field kept
    assert tg["enabled"] is True and tg["token"] == "t"  # gateway fields merged over


def test_sync_homed_merges_llm_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "config.json"
    settings = GatewaySettings()
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "sk-ant-xyz")
    monkeypatch.setenv("LLM_MODEL", "claude-x")

    sync_channels_into_nanobot_config(settings, config_path=config_path, homed=True)
    data = json.loads(config_path.read_text())
    assert data["providers"]["anthropic"]["apiKey"] == "sk-ant-xyz"
    assert data["agents"]["defaults"]["provider"] == "anthropic"
    assert data["agents"]["defaults"]["model"] == "claude-x"
