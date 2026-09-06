"""``superbrowser-doctor`` — verify the Python side of a SuperBrowser install.

Checks the things that commonly go wrong when bringing SuperBrowser up on a new
machine, and tells you exactly what to run next. Safe to run repeatedly; it only
reads state and (with --fix) runs the patchright browser download.

    superbrowser-doctor          # report
    superbrowser-doctor --fix    # also run `patchright install chromium`

Exits non-zero if a *critical* check fails (Python version, nanobot import) so
it can gate a startup script.
"""

import argparse
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

OK = "\033[32m✓\033[0m"
WARN = "\033[33m!\033[0m"
BAD = "\033[31m✗\033[0m"
SERVER_URL = os.environ.get("SUPERBROWSER_URL", "http://localhost:3100")


def _print(symbol: str, msg: str) -> None:
    print(f" {symbol} {msg}")


def _module_installed(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _check_python() -> bool:
    v = sys.version_info
    if v >= (3, 11):
        _print(OK, f"Python {v.major}.{v.minor}.{v.micro}")
        return True
    _print(BAD, f"Python {v.major}.{v.minor} — need 3.11+. Install from python.org or your package manager.")
    return False


def _check_nanobot() -> bool:
    if not _module_installed("nanobot"):
        _print(BAD, "nanobot-ai not importable — run: pip install runagent-superbrowser  (or: pip install -r requirements.txt)")
        return False
    try:
        from importlib.metadata import version

        _print(OK, f"nanobot-ai {version('nanobot-ai')}")
    except Exception:
        _print(OK, "nanobot importable")
    cfg = Path.home() / ".nanobot" / "config.json"
    if cfg.exists():
        _print(OK, f"nanobot config at {cfg}")
    else:
        _print(WARN, f"no {cfg} — run `nanobot onboard` to set your LLM provider/keys")
    return True


def _check_bridge_deps() -> None:
    for mod, hint in [
        ("httpx", "httpx"),
        ("curl_cffi", "curl_cffi"),
        ("patchright", "patchright"),
        ("playwright_stealth", "playwright-stealth"),
        ("PIL", "pillow"),
    ]:
        if _module_installed(mod):
            _print(OK, f"{hint}")
        else:
            _print(WARN, f"{hint} missing — pip install runagent-superbrowser")


def _patchright_browser_present() -> bool:
    """Heuristic: patchright/playwright store browsers under ms-playwright."""
    candidates = [
        Path.home() / ".cache" / "ms-playwright",  # Linux
        Path.home() / "Library" / "Caches" / "ms-playwright",  # macOS
        Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright",  # Windows
    ]
    for root in candidates:
        try:
            if root.exists() and any(root.glob("chromium-*")):
                return True
        except OSError:
            continue
    return False


def _check_browser(fix: bool) -> None:
    if _patchright_browser_present():
        _print(OK, "patchright Chromium installed")
        return
    if fix and shutil.which("patchright"):
        _print(WARN, "patchright Chromium missing — installing…")
        subprocess.run(["patchright", "install", "chromium"], check=False)
        if platform.system() == "Linux" and shutil.which("playwright"):
            subprocess.run(["playwright", "install-deps", "chromium"], check=False)
    else:
        tip = "patchright install chromium"
        if platform.system() == "Linux":
            tip += " && playwright install-deps chromium"
        _print(WARN, f"patchright Chromium not found — run: {tip}  (or rerun with --fix)")


def _check_server() -> None:
    try:
        with urllib.request.urlopen(f"{SERVER_URL}/health", timeout=3) as r:  # noqa: S310 - localhost
            if r.status == 200:
                _print(OK, f"TS engine reachable at {SERVER_URL}")
                return
    except Exception:
        pass
    _print(WARN, f"TS engine not reachable at {SERVER_URL} — start it with `superbrowser` (or `npm start`)")


def _check_config() -> None:
    """Report the unified config.json + detected machine profile (additive)."""
    try:
        from superbrowser_config import config_path, detect_profile, load, validate_config
    except Exception:  # noqa: BLE001 - config package optional/older install
        return
    raw = load()
    if raw is None:
        _print(OK, f"config: none at {config_path()} (zero-config mode; `superbrowser-config init` to create)")
    else:
        _model, warnings, errors = validate_config(raw)
        if errors:
            _print(WARN, f"config invalid: {'; '.join(errors)}")
        elif warnings:
            _print(WARN, f"config: {'; '.join(warnings)}")
        else:
            _print(OK, f"config valid at {config_path()}")
    try:
        _print(OK, f"machine profile: {detect_profile()}")
    except Exception:  # noqa: BLE001
        pass


def _check_gateway() -> None:
    """Report chat-gateway readiness when it's enabled (additive)."""
    try:
        from superbrowser_config import load
    except Exception:  # noqa: BLE001
        return
    gw = ((load() or {}).get("gateway") or {})
    if not gw.get("enabled"):
        return
    try:
        import superbrowser_gateway  # noqa: F401

        _print(OK, "gateway package importable")
    except Exception:  # noqa: BLE001
        _print(WARN, 'gateway enabled but not importable — pip install "runagent-superbrowser[gateway]"')
        return
    channels = gw.get("channels") or {}
    enabled = [name for name, section in channels.items() if isinstance(section, dict) and section.get("enabled")]
    _print(OK if enabled else WARN, f"gateway channels enabled: {', '.join(enabled) or 'none'}")
    import importlib.util as _u

    if "whatsapp" in enabled and _u.find_spec("neonize") is None:
        _print(WARN, 'whatsapp enabled but neonize missing — pip install "runagent-superbrowser[gateway]"')
    if "discord" in enabled and _u.find_spec("discord") is None:
        _print(WARN, 'discord enabled but discord.py missing — pip install "runagent-superbrowser[gateway]"')


def main() -> None:
    parser = argparse.ArgumentParser(prog="superbrowser-doctor", description=__doc__)
    parser.add_argument("--fix", action="store_true", help="install the patchright Chromium if missing")
    args = parser.parse_args()

    print("SuperBrowser doctor — Python side\n")
    critical_ok = True
    critical_ok &= _check_python()
    critical_ok &= _check_nanobot()
    _check_config()
    _check_bridge_deps()
    _check_browser(args.fix)
    _check_server()
    _check_gateway()

    print()
    if critical_ok:
        print("Looks good. Start the engine with `superbrowser`, then `superbrowser-agent \"<task>\"`.")
    else:
        print("Some critical checks failed — see ✗ above.")
    raise SystemExit(0 if critical_ok else 1)


if __name__ == "__main__":
    main()
