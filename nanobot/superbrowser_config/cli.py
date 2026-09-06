"""``superbrowser-config`` — manage ~/.superbrowser/config.json.

Subcommands:
    init         detection wizard → write a minimal config (0600)
    get/set/unset  read-modify-write one dotted path
    validate     schema + profile lint
    import-env   map an existing .env into config.json (never edits the .env)
    show         file or --effective merged view with per-key provenance
    profile      set the profile field
    path         print the resolved config path

argparse only — this CLI must work on a bare `pip install runagent-superbrowser`
without pulling the gateway extra.
"""

from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
from pathlib import Path
from typing import Any

from .detect import PROFILES, detection_signals
from .envmap import ENV_RULES, dig, parse_env_value
from .loader import config_path, effective, load, resolve_profile
from .redact import is_secret_path, mask
from .schema import validate_config
from .writer import write_config

OK, WARN, BAD = "[ ok ]", "[warn]", "[FAIL]"

_SUGGESTED_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o",
    "gemini": "gemini-2.5-flash",
    "groq": "llama-3.3-70b-versatile",
}


# ----- dotted-path helpers -----


def _set_path(data: dict[str, Any], dotted: str, value: Any) -> None:
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _unset_path(data: dict[str, Any], dotted: str) -> bool:
    parts = dotted.split(".")
    node: Any = data
    parents: list[tuple[dict[str, Any], str]] = []
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False
        parents.append((node, part))
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    del node[parts[-1]]
    for parent, key in reversed(parents):  # prune now-empty sections
        if parent[key] == {}:
            del parent[key]
    return True


def _parse_cli_value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _load_or_empty() -> dict[str, Any]:
    return load() or {"version": 1}


def _dotenv_into_process_env() -> None:
    """Mirror the SDK's .env discovery so `show --effective` reflects reality."""
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path)


# ----- subcommands -----


def cmd_path(_args: argparse.Namespace) -> int:
    print(config_path())
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    raw = load()
    if raw is None:
        print(f"{BAD} no config at {config_path()}", file=sys.stderr)
        return 1
    value = dig(raw, args.key)
    if value is None:
        print("null")
        return 1
    print(json.dumps(value, indent=2))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    data = _load_or_empty()
    value = _parse_cli_value(args.value)
    _set_path(data, args.key, value)
    write_config(config_path(), data)
    shown = mask(value) if is_secret_path(args.key) else json.dumps(value)
    print(f"{OK} {args.key} = {shown}  ({config_path()})")
    return 0


def cmd_unset(args: argparse.Namespace) -> int:
    data = _load_or_empty()
    if not _unset_path(data, args.key):
        print(f"{WARN} {args.key} was not set")
        return 1
    write_config(config_path(), data)
    print(f"{OK} removed {args.key}")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    data = _load_or_empty()
    data["profile"] = args.name
    write_config(config_path(), data)
    print(f"{OK} profile = {args.name}")
    return 0


def cmd_validate(_args: argparse.Namespace) -> int:
    raw = load()
    if raw is None:
        print(f"{WARN} no config at {config_path()} — nothing to validate (zero-config mode)")
        return 0
    _, warnings, errors = validate_config(raw)
    for message in warnings:
        print(f"{WARN} {message}")
    for message in errors:
        print(f"{BAD} {message}")
    if errors:
        return 2
    print(f"{OK} {config_path()} is valid" + (f" ({len(warnings)} warning(s))" if warnings else ""))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    if not args.effective:
        raw = load()
        if raw is None:
            print(f"{WARN} no config at {config_path()}")
            return 1
        if not args.reveal:
            from .redact import redact_config

            raw = redact_config(raw)
        print(json.dumps(raw, indent=2))
        return 0

    _dotenv_into_process_env()
    raw = load() or {}
    profile = resolve_profile(raw)
    print(f"# effective config — profile: {profile}  (file: {config_path()})")
    print(f"# precedence: environment > .env > config > preset")
    for row in effective():
        if row["value"] is None:
            continue
        value = row["value"]
        if row["secret"] and not args.reveal:
            value = mask(value)
        keys = ",".join(row["env"])
        print(f"{keys:42} = {value}   <- {row['source']} ({row['path']})")
    return 0


def cmd_import_env(args: argparse.Namespace) -> int:
    env_path = Path(args.file) if args.file else None
    if env_path is None:
        try:
            from dotenv import find_dotenv

            found = find_dotenv(usecwd=True)
        except ImportError:
            found = ""
        if not found:
            print(f"{BAD} no .env found — pass a path: superbrowser-config import-env path/to/.env", file=sys.stderr)
            return 1
        env_path = Path(found)
    if not env_path.is_file():
        print(f"{BAD} {env_path} does not exist", file=sys.stderr)
        return 1

    try:
        from dotenv import dotenv_values
    except ImportError:
        print(f"{BAD} python-dotenv is required for import-env", file=sys.stderr)
        return 1

    values = {k: v for k, v in dotenv_values(env_path).items() if v not in (None, "")}
    data = _load_or_empty()
    imported: list[str] = []
    consumed: set[str] = set()

    for rule in ENV_RULES:
        key = next((k for k in rule.env if k in values), None)
        if key is None:
            continue
        _set_path(data, rule.path, parse_env_value(rule, values[key]))
        imported.append(f"{key} -> {rule.path}")
        consumed.update(k for k in rule.env if k in values)

    # Conventional brain keys (mirrors _nanobot_config resolution): a bare
    # provider key becomes brain.provider + brain.apiKey when LLM_API_KEY
    # didn't already claim the slot.
    if dig(data, "brain.apiKey") is None:
        for env_key, provider in (
            ("OPENAI_API_KEY", "openai"),
            ("ANTHROPIC_API_KEY", "anthropic"),
            ("GEMINI_API_KEY", "gemini"),
            ("GROQ_API_KEY", "groq"),
        ):
            if env_key in values:
                _set_path(data, "brain.apiKey", values[env_key])
                if dig(data, "brain.provider") is None:
                    _set_path(data, "brain.provider", provider)
                imported.append(f"{env_key} -> brain.apiKey (+provider={provider})")
                consumed.add(env_key)
                break

    leftover = sorted(set(values) - consumed)

    print(f"# importing {env_path} -> {config_path()}")
    for line in imported:
        print(f"{OK} {line}")
    if leftover:
        print(f"{WARN} kept in .env (no config.json mapping): {', '.join(leftover)}")
    if not imported:
        print(f"{WARN} nothing to import")
        return 0
    if args.dry_run:
        print(f"{WARN} dry run — nothing written")
        return 0
    write_config(config_path(), data)
    print(f"{OK} wrote {config_path()}")
    print("#  your .env was NOT modified — env/.env still override config.json;")
    print("#  delete the imported lines from .env whenever you're ready.")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = config_path()
    if path.exists() and not args.force:
        print(f"{BAD} {path} already exists — use `superbrowser-config set ...` or re-run with --force", file=sys.stderr)
        return 1

    signals = detection_signals()
    suggested = args.profile or str(signals["suggestedProfile"])
    interactive = sys.stdin.isatty() and not args.yes

    print("SuperBrowser config wizard")
    print(f"  platform:   {signals['platform']}" + ("  (container)" if signals["inContainer"] else ""))
    print(f"  display:    {signals['display'] or '-'}")
    print(f"  chrome:     {signals['chromePath'] or 'not found'}")
    print(f"  profile:    {suggested} (suggested)")

    profile = suggested
    if interactive:
        answer = input(f"profile [{'/'.join(PROFILES)}] ({suggested}): ").strip()
        if answer:
            if answer not in PROFILES:
                print(f"{BAD} unknown profile {answer!r}", file=sys.stderr)
                return 1
            profile = answer

    data: dict[str, Any] = {"version": 1, "profile": profile}

    if interactive:
        print("\n-- brain LLM (drives the orchestrator; skip to configure later) --")
        provider = input("provider [anthropic/openai/gemini/groq/skip] (anthropic): ").strip().lower() or "anthropic"
        if provider != "skip":
            default_model = _SUGGESTED_MODELS.get(provider, "")
            model = input(f"model ({default_model}): ").strip() or default_model
            api_key = getpass.getpass(f"{provider} API key (hidden; empty to skip): ").strip()
            brain: dict[str, Any] = {"provider": provider}
            if model:
                brain["model"] = model
            if api_key:
                brain["apiKey"] = api_key
            data["brain"] = brain

        print("\n-- vision (Gemini; strongly recommended for browser mode) --")
        vision_key = getpass.getpass("Gemini VISION API key (hidden; empty to skip): ").strip()
        if vision_key:
            data["vision"] = {"enabled": True, "apiKey": vision_key}

        if profile == "vm":
            print("\n-- vm extras --")
            public_host = input("publicHost (URL humans can reach for handoff links; empty to skip): ").strip()
            if public_host:
                data["publicHost"] = public_host
            if input("generate an engine auth token? [Y/n]: ").strip().lower() not in ("n", "no"):
                data.setdefault("engine", {})["token"] = "sb-" + secrets.token_urlsafe(24)

    write_config(path, data)
    print(f"\n{OK} wrote {path} (0600)")
    print("next steps:")
    print("  superbrowser-config show --effective   # see what the engine/SDK will get")
    print("  superbrowser-config validate")
    return 0


# ----- entry -----


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superbrowser-config", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="detection wizard -> write a minimal config")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing config")
    p_init.add_argument("--profile", choices=PROFILES, help="skip detection, use this profile")
    p_init.add_argument("--yes", action="store_true", help="non-interactive: accept suggestions, skip prompts")
    p_init.set_defaults(func=cmd_init)

    p_get = sub.add_parser("get", help="print one value")
    p_get.add_argument("key", help="dotted path, e.g. engine.port")
    p_get.set_defaults(func=cmd_get)

    p_set = sub.add_parser("set", help="set one value (JSON or bare string)")
    p_set.add_argument("key")
    p_set.add_argument("value")
    p_set.set_defaults(func=cmd_set)

    p_unset = sub.add_parser("unset", help="remove one value")
    p_unset.add_argument("key")
    p_unset.set_defaults(func=cmd_unset)

    p_profile = sub.add_parser("profile", help="set the machine profile")
    p_profile.add_argument("name", choices=PROFILES)
    p_profile.set_defaults(func=cmd_profile)

    p_validate = sub.add_parser("validate", help="schema + profile lint")
    p_validate.set_defaults(func=cmd_validate)

    p_show = sub.add_parser("show", help="print the config (secrets masked)")
    p_show.add_argument("--effective", action="store_true", help="merged env+config+preset view with provenance")
    p_show.add_argument("--reveal", action="store_true", help="do not mask secrets")
    p_show.set_defaults(func=cmd_show)

    p_import = sub.add_parser("import-env", help="map an existing .env into config.json")
    p_import.add_argument("file", nargs="?", help=".env path (default: walk up from cwd)")
    p_import.add_argument("--dry-run", action="store_true")
    p_import.set_defaults(func=cmd_import_env)

    p_path = sub.add_parser("path", help="print the resolved config path")
    p_path.set_defaults(func=cmd_path)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
