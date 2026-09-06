"""Unit tests for the config.json → env projection core."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from superbrowser_config import loader
from superbrowser_config.cli import main as cli_main
from superbrowser_config.envmap import ENV_RULES
from superbrowser_config.loader import GUARD_ENV, apply_to_env, deep_merge, merged_config
from superbrowser_config.redact import redact_config
from superbrowser_config.schema import validate_config
from superbrowser_config.writer import write_config


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def _env_for(tmp_path: Path, data: dict, **extra: str) -> dict[str, str]:
    path = _write(tmp_path, data)
    env = {"SUPERBROWSER_CONFIG": str(path)}
    env.update(extra)
    return env


# ----- apply_to_env -----


def test_no_file_is_a_noop(tmp_path: Path) -> None:
    env = {"SUPERBROWSER_CONFIG": str(tmp_path / "missing.json")}
    assert apply_to_env(env) is False
    assert env[GUARD_ENV] == "1"
    assert set(env) == {"SUPERBROWSER_CONFIG", GUARD_ENV}


def test_broken_json_is_a_noop(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json")
    env = {"SUPERBROWSER_CONFIG": str(path)}
    assert apply_to_env(env) is False
    assert set(env) == {"SUPERBROWSER_CONFIG", GUARD_ENV}


def test_explicit_values_project(tmp_path: Path) -> None:
    env = _env_for(
        tmp_path,
        {
            "version": 1,
            "profile": "docker",  # empty preset -> only explicit values project
            "engine": {"port": 3200, "headless": False},
            "brain": {"provider": "anthropic", "model": "m1", "apiKey": "sk-x"},
            "vision": {"enabled": True, "somOverlay": False},
        },
    )
    assert apply_to_env(env) is True
    assert env["PORT"] == "3200"
    assert env["HEADLESS"] == "false"  # boolWord
    assert env["LLM_PROVIDER"] == "anthropic"
    assert env["LLM_API_KEY"] == "sk-x"
    assert env["VISION_ENABLED"] == "1"  # boolFlag
    assert env["VISION_SOM_OVERLAY"] == "0"


def test_env_wins_over_config(tmp_path: Path) -> None:
    env = _env_for(
        tmp_path,
        {"version": 1, "profile": "docker", "engine": {"port": 3200}},
        PORT="9999",
    )
    apply_to_env(env)
    assert env["PORT"] == "9999"


def test_preset_applies_and_explicit_beats_preset(tmp_path: Path) -> None:
    env = _env_for(
        tmp_path,
        {"version": 1, "profile": "local", "engine": {"concurrency": {"maxConcurrent": 7}}},
    )
    apply_to_env(env)
    assert env["CONCURRENT"] == "7"  # explicit beats the local preset's 3
    assert env["QUEUED"] == "5"  # from the local preset
    assert env["SUPERBROWSER_COOKIE_JAR"] == "1"
    assert env["LEARNING_READS_ENABLED"] == "0"
    assert env["T3_HEADLESS"] == "0"
    assert "T3_XVFB_DISPLAY" not in env  # local preset sets no display


def test_vm_preset_projects_display_pair(tmp_path: Path) -> None:
    env = _env_for(tmp_path, {"version": 1, "profile": "vm"})
    apply_to_env(env)
    assert env["T3_XVFB_DISPLAY"] == ":99"
    assert env["DISPLAY"] == ":99"
    assert env["T3_AUTO_XVFB"] == "1"


def test_group_skip_display(tmp_path: Path) -> None:
    # A real desktop session (DISPLAY already set) must never be repointed.
    env = _env_for(tmp_path, {"version": 1, "profile": "vm"}, DISPLAY=":0")
    apply_to_env(env)
    assert env["DISPLAY"] == ":0"
    assert "T3_XVFB_DISPLAY" not in env


def test_group_skip_token_pair(tmp_path: Path) -> None:
    env = _env_for(
        tmp_path,
        {"version": 1, "profile": "docker", "engine": {"token": "from-config"}},
        TOKEN="from-shell",
    )
    apply_to_env(env)
    assert env["TOKEN"] == "from-shell"
    assert "SUPERBROWSER_TOKEN" not in env  # never desync the pair


def test_token_pair_fills_both(tmp_path: Path) -> None:
    env = _env_for(tmp_path, {"version": 1, "profile": "docker", "engine": {"token": "t1"}})
    apply_to_env(env)
    assert env["TOKEN"] == "t1"
    assert env["SUPERBROWSER_TOKEN"] == "t1"


def test_public_host_fills_both_aliases(tmp_path: Path) -> None:
    env = _env_for(tmp_path, {"version": 1, "profile": "docker", "publicHost": "https://x.example"})
    apply_to_env(env)
    assert env["SUPERBROWSER_PUBLIC_HOST"] == "https://x.example"
    assert env["PUBLIC_BASE_URL"] == "https://x.example"


def test_chrome_auto_resolves_or_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loader, "find_chrome", lambda: "/opt/chrome")
    env = _env_for(tmp_path, {"version": 1, "profile": "docker", "engine": {"chromePath": "auto"}})
    apply_to_env(env)
    assert env["PUPPETEER_EXECUTABLE_PATH"] == "/opt/chrome"

    monkeypatch.setattr(loader, "find_chrome", lambda: None)
    sub = tmp_path / "b"
    sub.mkdir()
    env2 = _env_for(sub, {"version": 1, "profile": "docker", "engine": {"chromePath": "auto"}})
    apply_to_env(env2)
    assert "PUPPETEER_EXECUTABLE_PATH" not in env2  # no chrome found -> leave unset


def test_firewall_list_joined(tmp_path: Path) -> None:
    env = _env_for(
        tmp_path,
        {"version": 1, "profile": "docker", "engine": {"firewall": {"allow": ["a.com", "b.com"]}}},
    )
    apply_to_env(env)
    assert env["FIREWALL_ALLOW_LIST"] == "a.com,b.com"


def test_guard_is_idempotent(tmp_path: Path) -> None:
    env = _env_for(tmp_path, {"version": 1, "profile": "docker", "engine": {"port": 1}})
    assert apply_to_env(env) is True
    env["PORT"] = "changed"
    assert apply_to_env(env) is False  # guard blocks a second application
    assert env["PORT"] == "changed"


def test_profile_env_override(tmp_path: Path) -> None:
    env = _env_for(tmp_path, {"version": 1, "profile": "local"}, SUPERBROWSER_PROFILE="vm")
    apply_to_env(env)
    assert env["CONCURRENT"] == "10"  # vm preset, not local's 3


def test_gateway_section_is_not_projected(tmp_path: Path) -> None:
    env = _env_for(
        tmp_path,
        {"version": 1, "profile": "docker", "gateway": {"enabled": True, "port": 8460}},
    )
    apply_to_env(env)
    assert not any(k.startswith("GATEWAY") for k in env)


# ----- merge/schema/redact -----


def test_deep_merge_nested() -> None:
    merged = deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 3}, "b": 4})
    assert merged == {"a": {"x": 1, "y": 3}, "b": 4}


def test_merged_config_strips_meta(tmp_path: Path) -> None:
    profile, merged = merged_config({"version": 1, "profile": "docker", "engine": {"port": 1}})
    assert profile == "docker"
    assert "version" not in merged and "profile" not in merged


def test_validate_vm_without_public_host_warns() -> None:
    _, warnings, errors = validate_config({"version": 1, "profile": "vm"})
    assert not errors
    assert any("publicHost" in w for w in warnings)


def test_validate_gateway_bind_without_token_errors() -> None:
    _, _, errors = validate_config(
        {"version": 1, "profile": "local", "gateway": {"bind": "0.0.0.0"}}
    )
    assert any("gateway.bind" in e for e in errors)


def test_validate_unknown_profile_errors() -> None:
    _, _, errors = validate_config({"version": 1, "profile": "laptop"})
    assert errors


def test_validate_extra_keys_allowed() -> None:
    config, _, errors = validate_config(
        {"version": 1, "profile": "local", "futureSection": {"x": 1}, "engine": {"futureKey": True}}
    )
    assert not errors and config is not None


def test_redact_config_masks_secrets() -> None:
    redacted = redact_config(
        {"brain": {"apiKey": "sk-abcdef", "model": "m"}, "gateway": {"token": "tok-1234"}}
    )
    assert redacted["brain"]["apiKey"] == {"set": True, "last4": "cdef"}
    assert redacted["brain"]["model"] == "m"
    assert redacted["gateway"]["token"] == {"set": True, "last4": "1234"}


def test_every_rule_path_unique_and_env_unique() -> None:
    paths = [r.path for r in ENV_RULES]
    assert len(paths) == len(set(paths))
    all_envs = [k for r in ENV_RULES for k in r.env]
    assert len(all_envs) == len(set(all_envs))


# ----- writer -----


def test_writer_sets_restrictive_perms(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.json"
    write_config(path, {"version": 1})
    assert json.loads(path.read_text()) == {"version": 1}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


# ----- CLI -----


def test_cli_set_get_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("SUPERBROWSER_CONFIG", str(tmp_path / "config.json"))
    assert cli_main(["set", "engine.port", "3200"]) == 0
    assert cli_main(["get", "engine.port"]) == 0
    assert "3200" in capsys.readouterr().out
    assert cli_main(["unset", "engine.port"]) == 0
    data = json.loads((tmp_path / "config.json").read_text())
    assert "engine" not in data  # empty section pruned


def test_cli_import_env_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("SUPERBROWSER_CONFIG", str(tmp_path / "config.json"))
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "PORT=3100\n"
        "HEADLESS=true\n"
        "VISION_ENABLED=1\n"
        "T3_XVFB_DISPLAY=:99\n"
        "DISPLAY=:99\n"
        "ANTHROPIC_API_KEY=sk-ant-test\n"
        "FIREWALL_ALLOW_LIST=a.com,b.com\n"
        "SUPERBROWSER_TASK_ID=leftover\n"
    )
    assert cli_main(["import-env", str(dotenv)]) == 0
    out = capsys.readouterr().out
    assert "leftover" in out or "SUPERBROWSER_TASK_ID" in out  # unmapped keys reported

    data = json.loads((tmp_path / "config.json").read_text())
    assert data["engine"]["port"] == 3100
    assert data["engine"]["headless"] is True
    assert data["vision"]["enabled"] is True
    assert data["t3"]["xvfbDisplay"] == ":99"
    assert data["brain"]["apiKey"] == "sk-ant-test"
    assert data["brain"]["provider"] == "anthropic"
    assert data["engine"]["firewall"]["allow"] == ["a.com", "b.com"]
    # the imported .env itself is untouched
    assert "PORT=3100" in dotenv.read_text()


def test_cli_validate_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERBROWSER_CONFIG", str(tmp_path / "config.json"))
    write_config(tmp_path / "config.json", {"version": 1, "profile": "local"})
    assert cli_main(["validate"]) == 0


def test_cli_validate_error_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERBROWSER_CONFIG", str(tmp_path / "config.json"))
    write_config(
        tmp_path / "config.json",
        {"version": 1, "profile": "local", "gateway": {"bind": "0.0.0.0"}},
    )
    assert cli_main(["validate"]) == 2


def test_cli_init_non_interactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERBROWSER_CONFIG", str(tmp_path / "config.json"))
    assert cli_main(["init", "--yes", "--profile", "local"]) == 0
    data = json.loads((tmp_path / "config.json").read_text())
    assert data == {"version": 1, "profile": "local"}
    # refuses to clobber without --force
    assert cli_main(["init", "--yes", "--profile", "vm"]) == 1
    assert cli_main(["init", "--yes", "--profile", "vm", "--force"]) == 0


def test_cli_show_masks_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("SUPERBROWSER_CONFIG", str(tmp_path / "config.json"))
    write_config(
        tmp_path / "config.json",
        {"version": 1, "profile": "local", "brain": {"apiKey": "sk-secret-abcd"}},
    )
    assert cli_main(["show"]) == 0
    out = capsys.readouterr().out
    assert "sk-secret-abcd" not in out
    assert "abcd" in out
