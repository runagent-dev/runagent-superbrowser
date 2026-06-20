"""Remote (serverless) mode — run the browser on RunAgent's serverless engine.

The query is authenticated with your RunAgent API key, routed through the
middleware, and executed in an on-demand micro-VM with a per-user persistent
session. This reuses the runagent SDK's remote stack (RunAgentClient).

Prerequisites:
  - A "Browser" agent created + provisioned in the RunAgent dashboard
    (Managed Agents → create → Browser). Copy its agent id from the Browser tab.
  - pip install 'runagent-superbrowser[remote]'
  - Set RUNAGENT_API_KEY (an rau_… key from Settings → API Keys), or pass api_key=...

Run:
    export RUNAGENT_API_KEY=rau_...
    python examples/04_remote_serverless.py <browser-agent-id>
"""

import os
import sys

from runagent_superbrowser import SuperBrowser


def main() -> int:
    agent_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SUPERBROWSER_AGENT_ID")
    if not agent_id:
        print("usage: python 04_remote_serverless.py <browser-agent-id>", file=sys.stderr)
        return 2

    sb = SuperBrowser(
        remote=True,          # execute on the serverless engine via the middleware
        persistent=True,      # per-user persistent browser session across runs
        agent_id=agent_id,
        # api_key=...,        # falls back to RUNAGENT_API_KEY
    )

    res = sb.run("what is the top story on Hacker News right now?", mode="fetch")
    print(res.text)
    if not res.success and res.error:
        print(f"[error] {res.error}", file=sys.stderr)
    return 0 if res.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
