"""Machine-profile detection + Chrome discovery.

The Chrome candidate list mirrors ``bin/superbrowser-doctor.js`` — keep the two
in sync so "auto" resolves to the same binary the doctor reports.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

Profile = str  # "local" | "vm" | "docker"

PROFILES = ("local", "vm", "docker")


def chrome_candidates() -> list[str]:
    if sys.platform == "darwin":
        return ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    if sys.platform in ("win32", "cygwin"):
        program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        return [
            f"{program_files}\\Google\\Chrome\\Application\\chrome.exe",
            f"{program_files_x86}\\Google\\Chrome\\Application\\chrome.exe",
            f"{local_app_data}\\Google\\Chrome\\Application\\chrome.exe",
        ]
    return ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"]


def find_chrome() -> str | None:
    for candidate in chrome_candidates():
        if candidate and Path(candidate).exists():
            return candidate
    return None


def in_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
    except OSError:
        return False
    return any(marker in cgroup for marker in ("docker", "containerd", "kubepods"))


def detect_profile(environ: Mapping[str, str] | None = None) -> Profile:
    env = os.environ if environ is None else environ
    if in_container():
        return "docker"
    if sys.platform in ("darwin", "win32", "cygwin"):
        return "local"
    # Linux: a desktop session means a laptop/workstation; a bare box is a VM.
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY") or env.get("XDG_CURRENT_DESKTOP"):
        return "local"
    return "vm"


def detection_signals(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Everything the wizard / console shows next to the suggested profile."""
    env = os.environ if environ is None else environ
    chrome = find_chrome()
    return {
        "suggestedProfile": detect_profile(env),
        "platform": sys.platform,
        "inContainer": in_container(),
        "display": env.get("DISPLAY") or env.get("WAYLAND_DISPLAY") or None,
        "chromePath": chrome,
        "chromeFound": chrome is not None,
        "xvfbInstalled": any(
            Path(p, "Xvfb").exists() for p in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin")
        ),
    }
