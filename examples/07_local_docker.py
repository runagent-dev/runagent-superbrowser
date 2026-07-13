"""07 — Local Docker (all-in-one): a real interactive browser, via the container.

Same as `03_browser_mode.py`, but instead of auto-starting the engine it talks to
the all-in-one Docker container (orchestrator + stealth browser engine, exposed
as a RunAgent agent server on :8450). No RunAgent API key needed — `remote=False`
+ `local_agent_url`. Just edit the task / url / mode below and run.

Prerequisites — start the container first (rebuild after any Dockerfile change):
    docker compose up -d --build            # agent server on :8450

  To make Docker behave EXACTLY like `npm run dev`, give the container the same
  brain as your host — deliver your nanobot config verbatim, leave LLM_MODEL unset:
    echo "NANOBOT_CONFIG_JSON_B64=$(base64 -w0 ~/.nanobot/config.json)" >> deploy/.env
  (Already set if you kept the provided deploy/.env. Stop `npm run dev` first if it's
  holding :3100 — the container publishes the viewer there.)

Run:
  python examples/07_local_docker.py
"""

from __future__ import annotations

import pathlib
import sys

try:
    import runagent_superbrowser  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "nanobot"))

from runagent_superbrowser import SuperBrowser


def _raise_local_http_timeout(seconds: int = 900) -> None:
    """Raise runagent's local-run HTTP timeout (default 300s) so a cold browser
    run doesn't die at ~5 min while the agent is still working. Best-effort; the
    SDK's local-agent path doesn't forward `sb.run(timeout=...)`, so we rebind the
    default. Silent no-op if internals differ. You don't need to touch this."""
    try:
        from runagent.constants import DEFAULT_TIMEOUT_SECONDS as _D
        from runagent.sdk import rest_client as _rc
        if seconds <= _D:
            return
        for obj in vars(_rc).values():
            fn = getattr(obj, "run_agent", None) if isinstance(obj, type) else None
            if fn is not None and getattr(fn, "__defaults__", None):
                fn.__defaults__ = tuple(seconds if d == _D else d for d in fn.__defaults__)
    except Exception:
        pass


def main() -> int:
    _raise_local_http_timeout(900)  # allow long browser runs

    sb = SuperBrowser(
        remote=False,                              # not serverless...
        local_agent_url="http://localhost:8450",   # ...the local Docker agent server (no api key)
        persistent=True,                           # reuse the container's cookies / T3 profiles
    )

    res = sb.run(
        "Is there a one-bedroom room available at a five-star hotel in Sylhet on https://gozayaan.com/ from July 16 to 22 july. Find me three hotel suggestions.",
        url="https://gozayaan.com/",
        mode="browser",  # also "auto" (orchestrator decides) or "fetch" (read-only, fast)
    )

    print("success:", res.success)
    print("answer:\n", res.text)
    if not res.success:
        print("error:", res.error)
    return 0 if res.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
