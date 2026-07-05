"""07 — Local Docker (all-in-one) runner: browser OR auto mode, your own task.

Runs any task against the local all-in-one container (the orchestrator + stealth
browser engine, exposed as a RunAgent agent server on :8450). Same path as
``06_local_agent_server.py`` — ``remote=False`` + ``local_agent_url`` (NO RunAgent
API key needed) — but you choose the mode and task on the command line.

  - ``--mode auto``    (default) the orchestrator decides fetch vs browser
  - ``--mode browser`` force a real interactive browser session
  - ``--mode fetch``   read-only fetch/search (fast; good for a smoke test)

Prerequisites — start the container first:
    cp deploy/.env.example deploy/.env      # set LLM_MODEL + OPENAI_API_KEY
    docker compose up -d                    # agent server on :8450
  (host :3100 busy from `npm run dev`? run the image directly, publishing only :8450:
    docker run -d --rm --name sb -p 127.0.0.1:8450:8450 --env-file deploy/.env \
      -e SUPERBROWSER_URL=http://127.0.0.1:3100 -e PORT=3100 -e HEADLESS=true \
      --cap-add SYS_ADMIN runagent-superbrowser:all-in-one )

Examples:
    python examples/07_local_docker.py "what's the top story on Hacker News?"
    python examples/07_local_docker.py --mode browser --url https://gozayaan.com/ \
        "Is a one-bedroom room available at a 5-star hotel in Cox's Bazar for Jul 8-10?"
    python examples/07_local_docker.py --mode fetch "who is the CEO of Anthropic?"

    # point at a different host / raise the long-run timeout:
    SUPERBROWSER_LOCAL_AGENT_URL=http://1.2.3.4:8450 \
        python examples/07_local_docker.py --mode browser --timeout 1200 "…"
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

# Import from a source checkout without installing (mirrors the other examples).
try:
    import runagent_superbrowser  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "nanobot"))

from runagent_superbrowser import SuperBrowser


def _raise_local_http_timeout(seconds: int) -> bool:
    """Best-effort: raise runagent's local-run HTTP timeout (default 300s).

    The SDK's local-agent path (``_run_local_agent``) does NOT forward
    ``sb.run(timeout=...)`` to the RunAgent client, and a cold browser run can
    exceed 5 minutes — so without this the request dies at ~300s even though the
    agent is still working. ``run_agent``'s ``timeout_seconds`` default is bound
    at import, so we rebind it on the class. Silent no-op if internals differ.
    """
    try:
        from runagent.constants import DEFAULT_TIMEOUT_SECONDS as _D
        from runagent.sdk import rest_client as _rc

        if seconds <= _D:
            return False
        patched = False
        for obj in vars(_rc).values():
            fn = getattr(obj, "run_agent", None) if isinstance(obj, type) else None
            if fn is not None and getattr(fn, "__defaults__", None):
                fn.__defaults__ = tuple(
                    seconds if d == _D else d for d in fn.__defaults__
                )
                patched = True
        return patched
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run a task against the local Docker all-in-one (browser/auto/fetch)."
    )
    ap.add_argument("task", nargs="+", help="the task / prompt (quote it)")
    ap.add_argument(
        "--mode", choices=["auto", "browser", "fetch"], default="auto",
        help="auto (default) | browser | fetch",
    )
    ap.add_argument("--url", default=None, help="starting URL (browser mode)")
    ap.add_argument(
        "--agent-url",
        default=os.environ.get("SUPERBROWSER_LOCAL_AGENT_URL", "http://localhost:8450"),
        help="the container's agent server (default http://localhost:8450)",
    )
    ap.add_argument(
        "--timeout", type=int, default=900,
        help="local HTTP timeout seconds for the run (default 900; SDK/runagent default is 300)",
    )
    args = ap.parse_args()
    task = " ".join(args.task)

    raised = _raise_local_http_timeout(args.timeout)

    sb = SuperBrowser(
        remote=False,               # not serverless...
        local_agent_url=args.agent_url,  # ...talk to the local Docker agent server (no api key)
        persistent=True,            # reuse the container's per-user cookies / profiles
    )

    print(
        f">> local-docker | mode={args.mode} | agent={args.agent_url} | "
        f"url={args.url or '-'} | http_timeout={'%ss' % args.timeout if raised else '~300s (default)'}"
    )
    print(f">> task: {task}\n")

    res = sb.run(task, mode=args.mode, url=args.url)

    print("success:", res.success)
    if getattr(res, "classification", None) is not None:
        print("classification:", res.classification)  # auto mode: which way it leaned
    print("ran as mode:", getattr(res, "mode", args.mode))
    print("\nanswer:\n", res.text)
    if not res.success and res.error:
        print(f"\n[error] {res.error}", file=sys.stderr)
    return 0 if res.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
