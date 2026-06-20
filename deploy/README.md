# Deploy SuperBrowser to RunAgent serverless

This directory is the **RunAgent agent manifest** for SuperBrowser. Deploying it
hosts SuperBrowser on the RunAgent serverless platform — on-demand micro-VMs with
a **per-user persistent browser session**, callable from **every** RunAgent SDK.

The heavy Node + Chromium engine is built **server-side** from the `superbrowser`
runtime image, so this directory is intentionally tiny (just the manifest +
entrypoint + your `.env`). Deploy from here so only these files are uploaded —
not the whole repo.

```bash
cd deploy
cp .env.example .env
$EDITOR .env            # set LLM_MODEL + OPENAI_API_KEY (or ANTHROPIC_API_KEY)
runagent deploy .       # prints your agent_id
```

Then call the `run` entrypoint from any SDK with `local=false` +
`persistent_memory=true`:

```python
from runagent import RunAgentClient
client = RunAgentClient(agent_id="<agent_id>", entrypoint_tag="run",
                        local=False, persistent_memory=True)
print(client.run(task="find the cheapest 4-star hotel in Sylhet this weekend"))
```

Python users can also use the convenience wrapper:
`SuperBrowser(remote=True, persistent=True, agent_id="<agent_id>").run("...")`.

Prefer not to clone this repo? `runagent init my-browser --from-template
superbrowser/default` scaffolds the same thing standalone.

For **local** development (no deploy), use `npm run dev` + the Python SDK in
local mode — see the repo root README.
