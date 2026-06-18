"""02 — Auto mode: let the agent decide fetch vs browser.

`mode="auto"` (the default) is the "intelligence" switch — the orchestrator
routes between a lightweight fetch/search and a full browser session on its own,
using its built-in rubric. The result carries `classification`, which tells you
*which way it leaned and why* (this is surfaced for visibility; it doesn't
force the choice).

Auto mode only touches the browser engine if the agent actually picks the
browser, so it's safe to run without one — it'll fall back to fetch/search.

Prerequisites:
  - An LLM configured (`nanobot onboard`)

Run:
  python examples/02_auto_mode.py
"""

from __future__ import annotations

import pathlib
import sys

try:
    import runagent_superbrowser  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "nanobot"))

from runagent_superbrowser import SuperBrowser


def main() -> int:
    sb = SuperBrowser()

    task = "Who is the current CEO of Anthropic, and what year was the company founded?"
    res = sb.run(task, mode="auto")

    # Why the agent leaned the way it did (e.g. {'approach': 'search', ...}).
    print("classification:", res.classification)
    print("ran as mode:", res.mode)
    print("\nanswer:\n", res.text)
    return 0 if res.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
