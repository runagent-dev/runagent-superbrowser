"""config.json → environment projection (the Python loader).

The canonical product config lives at ``~/.superbrowser/config.json``. It is
never read directly by feature code — this loader projects it into **unset**
environment variables right after dotenv runs, so precedence falls out of a
single rule ("fill only unset keys"):

    process env  >  .env  >  config.json explicit  >  profile preset  >  code defaults

Fail-open by design: no file, unparseable file, or a broken install of this
package must never change today's zero-config behavior. The TS engine ships
the same algorithm in ``src/config/apply.ts``.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

from .detect import detect_profile, find_chrome
from .envmap import ENV_RULES, dig, stringify
from .presets import preset_for

GUARD_ENV = "SUPERBROWSER_CONFIG_APPLIED"
CONFIG_PATH_ENV = "SUPERBROWSER_CONFIG"
PROFILE_ENV = "SUPERBROWSER_PROFILE"
CONFIG_VERSION = 1

DEFAULT_CONFIG_PATH = Path("~/.superbrowser/config.json")


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = env.get(CONFIG_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG_PATH.expanduser()


def load(environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """The raw config file as a dict, or None when absent/unreadable."""
    path = config_path(environ)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_profile(raw: Mapping[str, Any] | None, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    profile = env.get(PROFILE_ENV) or (raw or {}).get("profile") or detect_profile(env)
    return str(profile)


def merged_config(
    raw: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """(profile, preset ⊕ explicit-values) — explicit config beats the preset."""
    profile = resolve_profile(raw, environ)
    explicit = {k: v for k, v in raw.items() if k not in ("version", "profile")}
    return profile, deep_merge(preset_for(profile), explicit)


def apply_to_env(environ: MutableMapping[str, str] | None = None) -> bool:
    """Project the config file into unset env vars. Returns True if a file was applied.

    Idempotent per process via the GUARD_ENV marker; the marker is inherited by
    child processes, which is correct — children also inherit the projected env.
    """
    env = os.environ if environ is None else environ
    if env.get(GUARD_ENV) == "1":
        return False
    try:
        applied = _apply(env)
    except Exception as exc:  # noqa: BLE001 - fail-open, never break the host process
        print(f"[superbrowser-config] ignored config error: {exc}", file=sys.stderr)
        applied = False
    env[GUARD_ENV] = "1"
    return applied


def _apply(env: MutableMapping[str, str]) -> bool:
    path = config_path(env)
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print(f"[superbrowser-config] could not read {path}: {exc}", file=sys.stderr)
        return False
    if not isinstance(raw, dict):
        print(f"[superbrowser-config] {path} is not a JSON object; ignoring", file=sys.stderr)
        return False
    version = raw.get("version")
    if version not in (None, CONFIG_VERSION):
        print(
            f"[superbrowser-config] {path} has version {version!r} (this build understands "
            f"{CONFIG_VERSION}); applying best-effort",
            file=sys.stderr,
        )

    _, merged = merged_config(raw, env)
    for rule in ENV_RULES:
        value = dig(merged, rule.path)
        if value is None:
            continue
        text = stringify(rule, value, chrome_finder=find_chrome)
        if text is None:
            continue
        if any(key in env for key in rule.env):
            continue  # group-skip: the environment already owns this rule
        for key in rule.env:
            env[key] = text
    return True


def effective(environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Per-rule effective view with provenance — powers ``show --effective``.

    Provenance is recomputed (not recorded at apply time): a rule whose first
    env key is set to something other than what the config would project is
    "environment"; matching/unset keys attribute to config/preset/unset.
    """
    env = os.environ if environ is None else environ
    raw = load(env) or {}
    profile, merged = merged_config(raw, env)
    explicit = {k: v for k, v in raw.items() if k not in ("version", "profile")}
    preset = preset_for(profile)

    rows: list[dict[str, Any]] = []
    for rule in ENV_RULES:
        value = dig(merged, rule.path)
        projected = None if value is None else stringify(rule, value, chrome_finder=find_chrome)
        env_value = next((env[key] for key in rule.env if key in env), None)

        if env_value is not None:
            source = "environment" if env_value != projected else _config_source(rule.path, explicit, preset)
            final: str | None = env_value
        elif projected is not None:
            source = _config_source(rule.path, explicit, preset)
            final = projected
        else:
            source = "unset"
            final = None

        rows.append(
            {
                "path": rule.path,
                "env": list(rule.env),
                "value": final,
                "source": source,
                "secret": rule.secret,
            }
        )
    return rows


def _config_source(path: str, explicit: Mapping[str, Any], preset: Mapping[str, Any]) -> str:
    if dig(explicit, path) is not None:
        return "config"
    if dig(preset, path) is not None:
        return "preset"
    return "environment"
