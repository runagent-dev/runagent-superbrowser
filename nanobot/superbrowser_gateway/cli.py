"""``superbrowser-gateway`` — chat with SuperBrowser over WhatsApp/Telegram/Discord.

    superbrowser-gateway                       # run (foreground)
    superbrowser-gateway setup                 # enable channels interactively
    superbrowser-gateway login whatsapp        # terminal QR pairing
    superbrowser-gateway status                # ping the running gateway
    superbrowser-gateway --home ~/.sbgw/acct2 --console-port 8461   # second WhatsApp number

Multi-number: one gateway process per WhatsApp account. ``--home`` isolates
the nanobot config/data dir (WhatsApp session DB, pairing store, sessions) and
the orchestrator workspaces, so instances never share state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _bootstrap_env(home: str | None) -> Path | None:
    """Config projection + --home isolation. Must run before nanobot/bridge imports."""
    try:
        from superbrowser_config import apply_to_env

        apply_to_env()
    except Exception:  # noqa: BLE001 - fail-open like every other entry point
        pass

    if not home:
        return None
    home_path = Path(home).expanduser().resolve()
    home_path.mkdir(parents=True, exist_ok=True)
    from nanobot.config.loader import set_config_path

    set_config_path(home_path / "config.json")  # all nanobot data dirs follow
    os.environ.setdefault("SUPERBROWSER_WORKSPACE_ROOT", str(home_path / "workspaces"))
    return home_path


def cmd_run(ns: argparse.Namespace) -> int:
    home = _bootstrap_env(ns.home)
    from .app import main_async_entry
    from .config import load_settings

    try:
        settings = load_settings(home=home)
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    if ns.console_port:
        settings.port = ns.console_port
    if ns.bind:
        settings.bind = ns.bind
    if ns.engine_url:
        settings.engine_url = ns.engine_url.rstrip("/")

    print(f"superbrowser-gateway: engine={settings.engine_url} console=http://{settings.bind}:{settings.port}")
    try:
        main_async_entry(settings, homed=home is not None)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 - startup failures get a friendly line
        message = str(exc)
        if "provider" in message.lower() or "api key" in message.lower() or "config" in message.lower():
            print(
                f"[FAIL] gateway could not start: {exc}\n"
                "       No usable LLM brain? Set brain.provider/model/apiKey via "
                "`superbrowser-config init`, or LLM_PROVIDER/LLM_MODEL/LLM_API_KEY in .env, "
                "or run `nanobot onboard`.",
                file=sys.stderr,
            )
        else:
            print(f"[FAIL] gateway could not start: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_setup(ns: argparse.Namespace) -> int:
    home = _bootstrap_env(ns.home)
    from superbrowser_config import config_path, load, write_config

    from .config import load_settings
    from .nanobot_channels import sync_channels_into_nanobot_config

    data = load() or {"version": 1}
    gateway = data.setdefault("gateway", {})
    gateway["enabled"] = True
    channels = gateway.setdefault("channels", {})

    interactive = sys.stdin.isatty() and not ns.yes
    if interactive:
        print("SuperBrowser gateway setup — enable the channels you want:")
        if _ask_yes("  WhatsApp (QR pairing)?"):
            allow = input("    allowed numbers, comma-separated, digits only (empty = pairing-code flow): ").strip()
            section = channels.setdefault("whatsapp", {})
            section["enabled"] = True
            if allow:
                section["allowFrom"] = [a.strip().lstrip("+") for a in allow.split(",") if a.strip()]
        if _ask_yes("  Telegram (bot token from @BotFather)?"):
            token = input("    bot token: ").strip()
            section = channels.setdefault("telegram", {})
            section["enabled"] = bool(token)
            if token:
                section["token"] = token
            allow = input("    allowed user IDs, comma-separated (empty = pairing-code flow): ").strip()
            if allow:
                section["allowFrom"] = [a.strip() for a in allow.split(",") if a.strip()]
        if _ask_yes("  Discord (bot token)?"):
            token = input("    bot token: ").strip()
            section = channels.setdefault("discord", {})
            section["enabled"] = bool(token)
            if token:
                section["token"] = token
    else:
        print("(non-interactive: enabling nothing new — edit gateway.channels in the config)")

    write_config(config_path(), data)
    print(f"[ ok ] wrote {config_path()}")

    settings = load_settings(home=home)
    path = sync_channels_into_nanobot_config(settings, homed=home is not None)
    print(f"[ ok ] channels merged into {path}")
    if channels.get("whatsapp", {}).get("enabled"):
        print("next: superbrowser-gateway login whatsapp   (scan the QR)")
    print("then: superbrowser-gateway")
    return 0


def cmd_login(ns: argparse.Namespace) -> int:
    home = _bootstrap_env(ns.home)
    if ns.channel != "whatsapp":
        print(f"[FAIL] interactive login is only needed for whatsapp (got {ns.channel!r})", file=sys.stderr)
        return 2
    from .config import load_settings
    from .nanobot_channels import sync_channels_into_nanobot_config

    settings = load_settings(home=home)
    sync_channels_into_nanobot_config(settings, homed=home is not None)

    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import load_config

    config = load_config()
    section = getattr(config.channels, "whatsapp", None)
    if section is None or not (
        section.get("enabled") if isinstance(section, dict) else getattr(section, "enabled", False)
    ):
        print("[FAIL] whatsapp channel is not enabled — run `superbrowser-gateway setup` first", file=sys.stderr)
        return 2
    try:
        from nanobot.channels.whatsapp import WhatsAppChannel
    except Exception as exc:  # noqa: BLE001 - neonize extra missing
        print(
            f"[FAIL] WhatsApp support not installed ({exc}).\n"
            '       pip install "runagent-superbrowser[gateway]"',
            file=sys.stderr,
        )
        return 2

    channel = WhatsAppChannel(section, MessageBus())
    ok = asyncio.run(channel.login(force=ns.force))
    print("[ ok ] WhatsApp linked" if ok else "[FAIL] login did not complete")
    return 0 if ok else 1


def cmd_status(ns: argparse.Namespace) -> int:
    _bootstrap_env(ns.home)
    from .config import load_settings

    settings = load_settings()
    url = f"http://{settings.bind if settings.bind != '0.0.0.0' else '127.0.0.1'}:{settings.port}/api/health"
    try:
        import urllib.request

        request = urllib.request.Request(url)
        if settings.token:
            request.add_header("Authorization", f"Bearer {settings.token}")
        with urllib.request.urlopen(request, timeout=5) as resp:
            print(json.dumps(json.load(resp), indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] gateway not reachable at {url}: {exc}", file=sys.stderr)
        return 1


def _ask_yes(prompt: str) -> bool:
    return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superbrowser-gateway", description=__doc__.split("\n")[0])
    parser.add_argument("--home", default=None, help="isolated data dir for this instance (multi-number)")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="run the gateway (default)")
    p_setup = sub.add_parser("setup", help="enable channels interactively")
    p_setup.add_argument("--yes", action="store_true", help="non-interactive (no prompts)")
    p_login = sub.add_parser("login", help="interactive channel login (WhatsApp QR)")
    p_login.add_argument("channel", nargs="?", default="whatsapp")
    p_login.add_argument("--force", action="store_true", help="wipe the session and pair fresh")
    sub.add_parser("status", help="ping the running gateway")

    for p in (parser, p_run):
        p.add_argument("--console-port", type=int, default=None, help="HTTP surface port (default 8460)")
        p.add_argument("--bind", default=None, help="HTTP surface bind address (default 127.0.0.1)")
        p.add_argument("--engine-url", default=None, help="TS engine URL (default http://127.0.0.1:3100)")

    parser.set_defaults(func=cmd_run)
    p_run.set_defaults(func=cmd_run)
    p_setup.set_defaults(func=cmd_setup)
    p_login.set_defaults(func=cmd_login)
    sub.choices["status"].set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    # subcommands without their own copies of the run flags
    for attr in ("console_port", "bind", "engine_url", "yes", "force"):
        if not hasattr(ns, attr):
            setattr(ns, attr, None)
    return int(ns.func(ns))


if __name__ == "__main__":
    raise SystemExit(main())
