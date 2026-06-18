"""01 — Quick start (fetch mode).

The simplest possible SuperBrowser call. `mode="fetch"` is read-only and runs
fully in-process (HTTP / stealth fetch / search) — no browser engine, no
captcha risk, nothing to start first.

Prerequisites:
  - An LLM configured (`nanobot onboard`, or LLM_MODEL + provider key in .env)

Run:
  python examples/01_quickstart_fetch.py
"""

from __future__ import annotations

# --- run from a source checkout without installing -------------------------
import pathlib
import sys

try:
    import runagent_superbrowser  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "nanobot"))
# ---------------------------------------------------------------------------

from runagent_superbrowser import SuperBrowser


def main() -> int:
    sb = SuperBrowser()

    # A terse goal — no "please fetch the page and then…". The bundled system
    # prompt handles the how; you say the what.
    res = sb.run("What is the top story on Hacker News right now?", mode="fetch")

    print("success:", res.success)
    print("answer:\n", res.text)
    if not res.success:
        print("error:", res.error)
    return 0 if res.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
