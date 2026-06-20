"""``superbrowser-run`` console entry point — a thin CLI over ``SuperBrowser``.

    superbrowser-run "what's the top story on hacker news?" --mode fetch
    superbrowser-run "book the cheapest DAC->BKK flight" --mode browser --auto-start-server
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="superbrowser-run",
        description="Run a task with the SuperBrowser agent (fetch, browser, or auto).",
    )
    parser.add_argument("task", nargs="+", help="the goal, in plain language")
    parser.add_argument(
        "--mode",
        choices=["auto", "fetch", "browser"],
        default="auto",
        help="auto = let the agent decide (default); fetch = read-only; browser = interactive",
    )
    parser.add_argument("--url", default=None, help="starting / target URL")
    parser.add_argument(
        "--server-url", default=None, help="TS engine URL (default http://localhost:3100)"
    )
    parser.add_argument(
        "--auto-start-server",
        action="store_true",
        help="spawn the TS browser engine if it isn't already running",
    )
    parser.add_argument("--model", default=None, help="override the model from ~/.nanobot/config.json")
    parser.add_argument("--timeout", type=float, default=None, help="seconds before the task is aborted")
    parser.add_argument(
        "--remote",
        action="store_true",
        help="execute on the RunAgent serverless engine via the middleware (reuses the runagent SDK)",
    )
    parser.add_argument(
        "--persistent",
        action="store_true",
        help="remote: keep a per-user persistent browser session (cookies/profiles) across runs",
    )
    parser.add_argument("--agent-id", default=None, help="remote: Browser agent id (or set SUPERBROWSER_AGENT_ID)")
    parser.add_argument("--api-key", default=None, help="remote: RunAgent API key (or set RUNAGENT_API_KEY)")
    parser.add_argument("--base-url", default=None, help="remote: middleware base URL (or set RUNAGENT_BASE_URL)")
    ns = parser.parse_args(argv)

    from .client import SuperBrowser

    task = " ".join(ns.task)
    sb = SuperBrowser(
        server_url=ns.server_url,
        auto_start_server=ns.auto_start_server,
        model=ns.model,
        remote=ns.remote,
        persistent=ns.persistent,
        agent_id=ns.agent_id,
        api_key=ns.api_key,
        base_url=ns.base_url,
    )
    try:
        res = sb.run(task, mode=ns.mode, url=ns.url, timeout=ns.timeout)
    finally:
        sb.close()

    if res.text:
        print(res.text)
    if not res.success and res.error:
        print(f"[error] {res.error}", file=sys.stderr)
    return 0 if res.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
