"""The config-path → env-var projection table and its (de)stringifiers.

The table itself is data (``envmap.json``) shared verbatim with the TS engine
loader (``src/config/envmap.ts``, marker-wrapped copy) and guarded by
``scripts/check_config_parity.sh``. Only the interpretation of ``kind`` lives
in code, once per language.

Group-skip semantics: a rule whose env keys are a tuple (e.g. ``engine.token``
→ TOKEN + SUPERBROWSER_TOKEN, ``t3.xvfbDisplay`` → T3_XVFB_DISPLAY + DISPLAY)
fills either ALL of its keys or NONE — if any of them is already set in the
environment, the whole rule is skipped. Filling half a pair would desync
values the code expects to match (mismatched tokens break bridge auth; a
desktop session's DISPLAY must never be repointed at Xvfb).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any

AUTO_SENTINEL = "auto"

_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off")


@dataclass(frozen=True, slots=True)
class EnvRule:
    path: str
    env: tuple[str, ...]
    kind: str
    secret: bool = False


def _load_rules() -> tuple[EnvRule, ...]:
    raw = json.loads(resources.files(__package__).joinpath("envmap.json").read_text())
    return tuple(
        EnvRule(path=r["path"], env=tuple(r["env"]), kind=r["kind"], secret=bool(r.get("secret", False)))
        for r in raw
    )


ENV_RULES: tuple[EnvRule, ...] = _load_rules()

SECRET_PATHS: frozenset[str] = frozenset(r.path for r in ENV_RULES if r.secret)


def dig(data: Mapping[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def stringify(rule: EnvRule, value: Any, *, chrome_finder: Callable[[], str | None]) -> str | None:
    """Config value → env string, or None to skip the rule."""
    if value is None:
        return None
    kind = rule.kind
    if kind == "str":
        if isinstance(value, bool):
            return None
        return str(value)
    if kind == "int":
        if isinstance(value, bool):
            return None
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return None
    if kind == "boolWord":
        parsed = _as_bool(value)
        return None if parsed is None else ("true" if parsed else "false")
    if kind == "boolFlag":
        parsed = _as_bool(value)
        return None if parsed is None else ("1" if parsed else "0")
    if kind == "listComma":
        if isinstance(value, str):
            return value or None
        if isinstance(value, (list, tuple)):
            joined = ",".join(str(item).strip() for item in value if str(item).strip())
            return joined or None
        return None
    if kind == "chromeAuto":
        if not isinstance(value, str) or not value:
            return None
        if value == AUTO_SENTINEL:
            return chrome_finder()
        return value
    return None


def parse_env_value(rule: EnvRule, text: str) -> Any:
    """Env string → config value (used by ``import-env``)."""
    kind = rule.kind
    if kind == "int":
        try:
            return int(text)
        except ValueError:
            return text
    if kind in ("boolWord", "boolFlag"):
        lowered = text.strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        return text
    if kind == "listComma":
        return [part.strip() for part in text.split(",") if part.strip()]
    return text


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
    return None
