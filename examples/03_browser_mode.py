"""03 — Browser mode: a real interactive browser.

`mode="browser"` drives a real headless browser over the TypeScript engine on
:3100 — for anything that clicks, fills forms, logs in, or books. This example
uses `auto_start_server=True` so the SDK starts the engine for you and tears it
down on exit (it only ever stops an engine it started itself).

In THIS source checkout we point auto-start at the no-build watch server
(`npm run dev`). If you've installed the npm package globally
(`npm i -g runagent-superbrowser`) you can drop `server_cmd` and it'll use the
`superbrowser` binary.

Prerequisites:
  - An LLM configured (`nanobot onboard`)
  - Google Chrome installed + `patchright install chromium`
  - Node (for the engine). Either it's already running on :3100, or
    auto_start_server starts it.

Run:
  python examples/03_browser_mode.py
"""

from __future__ import annotations

import pathlib
import sys

try:
    import runagent_superbrowser  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "nanobot"))

from runagent_superbrowser import ServerStartError, ServerUnavailable, SuperBrowser


def main() -> int:
    # `server_cmd` is only needed for the watch-mode dev server in a checkout;
    # omit it if the `superbrowser` binary is on your PATH.
    sb = SuperBrowser(
        auto_start_server=True,
        server_cmd=["npm", "run", "dev"],
        server_start_timeout=60.0,  # the dev server compiles on first boot
    )

    # `with` guarantees the engine is torn down even if the task raises.
    try:
        with sb:
            res = sb.run(
                "Go to https://news.ycombinator.com and tell me the title and "
                "points of the #1 story.",
                url="https://news.ycombinator.com",
                mode="browser",
            )
    except (ServerUnavailable, ServerStartError) as exc:
        print("Could not start/reach the browser engine:\n ", exc)
        print("Start it manually with `npm run dev` and retry, or check Chrome is installed.")
        return 1

    print("success:", res.success)
    print("answer:\n", res.text)
    if not res.success:
        print("error:", res.error)
    return 0 if res.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
