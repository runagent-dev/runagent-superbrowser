"""Local-agent (Docker) mode — run against the all-in-one container.

The all-in-one image runs the stealth browser engine + the orchestrator,
exposed as a local RunAgent agent server on :8450. The SDK connects with
remote=False + a local agent URL — NO RunAgent API key needed. Under the hood
it reuses RunAgentClient(local=True), the same interface as remote mode.

Prerequisites:
  - pip install 'runagent-superbrowser[remote]'
  - Start the container:
        cp deploy/.env.example deploy/.env   # set LLM_MODEL + OPENAI_API_KEY
        docker compose up -d                 # agent server on :8450

Run:
    python examples/06_local_agent_server.py
    # or point at a different host:
    SUPERBROWSER_LOCAL_AGENT_URL=http://1.2.3.4:8450 python examples/06_local_agent_server.py
"""

import os
import sys

from runagent_superbrowser import SuperBrowser


def main() -> int:
    url = os.environ.get("SUPERBROWSER_LOCAL_AGENT_URL", "http://localhost:8450")

    sb = SuperBrowser(
        remote=False,             # NOT serverless...
        local_agent_url=url,      # ...talk to the local Docker agent server (no api key)
        persistent=True,          # reuse the container's per-user cookies/profiles
    )

    res = sb.run("go to https://www.stubhub.com/ and Book 4 tickets in the upper for any Kevin Hart show in New York in the next three months and view ticket prices with estimated fees.", mode="browser")
    print(res.text)
    if not res.success and res.error:
        print(f"[error] {res.error}", file=sys.stderr)
    return 0 if res.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
