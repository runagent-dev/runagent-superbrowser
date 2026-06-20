# SuperBrowser Python SDK

`from runagent_superbrowser import SuperBrowser` — a single object that turns a
plain-language goal into a result. The system prompting (routing rubric,
anti-fabrication rules, the browser tool ladder) ships inside the package and is
provisioned automatically, so you write *what* you want, not *how* to click.

```bash
pip install runagent-superbrowser
patchright install chromium       # stealth Chromium (browser mode)
nanobot onboard                   # set your model + API key (~/.nanobot/config.json)
```

## Quick start

```python
from runagent_superbrowser import SuperBrowser

sb = SuperBrowser()
res = sb.run("what's the top story on Hacker News right now?")
print(res.text)
```

`run()` is synchronous; use `await sb.arun(...)` inside an event loop (calling
`run()` from a running loop raises with a clear message).

## Modes — the `intelligence` switch

| `mode` | Behaviour | TS engine? |
|---|---|---|
| `"auto"` *(default)* | the agent routes between fetch/search and a real browser using its built-in rubric | only if it chooses the browser |
| `"fetch"` | read-only: HTTP / stealth fetch (curl_cffi, patchright) / search. In-process, fast, no captcha risk | no |
| `"browser"` | a real headless browser: clicks, forms, logins, bookings, pixel inspection | yes |

```python
sb.run("average price of used iPhone 16 Pro on mercari.com", mode="fetch")
sb.run("book a 4-star Sylhet hotel Sun–Thu for 2", url="https://gozayaan.com", mode="browser")
```

Forcing a mode just adjusts which delegation tools the agent sees and adds a
short directive — the bundled SOUL prompt still does the heavy lifting.

## The result object

`run()` / `arun()` return a `RunResult`:

| field | meaning |
|---|---|
| `text` | the final answer (direct, or captured off the `message()` bus) |
| `success` | `True` if a non-empty answer came back with no hard error (`bool(res)` works too) |
| `data` | parsed `output_schema` payload, or `None` |
| `error` | short failure message, or `None` |
| `task_id` | `orch-<hex8>` — ledger lives under `/tmp/superbrowser/<task_id>/` |
| `mode` | the mode it actually ran in |
| `raw_content` | the agent's direct text (often empty when it answers via `message()`) |
| `classification` | for `mode="auto"`: `{"approach","reason","confidence"}` — why it leaned fetch vs browser |

## Structured output

Pass a pydantic model, a `list[Model]`, or a JSON Schema dict. Parsing is
best-effort: the value lands in `res.data`, or `None` if the answer didn't
contain clean JSON. Validation failures fall back to the raw parsed JSON rather
than raising.

```python
from pydantic import BaseModel
class Flight(BaseModel):
    airline: str
    price_usd: float

res = SuperBrowser().run(
    "cheapest 3 one-way flights DAC→SIN on Mar 5",
    mode="auto", output_schema=list[Flight],
)
for f in res.data or []:
    print(f.airline, f.price_usd)
```

## The browser engine

Browser mode needs the TypeScript engine listening on `:3100`. Three ways:

```python
# 1) you start it: `superbrowser http` (or `npm start`) in another shell
SuperBrowser().run("…", mode="browser")

# 2) the SDK starts + stops it (opt-in); context manager guarantees teardown
with SuperBrowser(auto_start_server=True) as sb:
    sb.run("…", mode="browser")

# 3) point at a remote engine
SuperBrowser(server_url="http://10.0.0.5:3100").run("…", mode="browser")
```

If the engine is down and `auto_start_server=False`, browser mode raises
`ServerUnavailable` with a hint. In `auto` mode the SDK never hard-fails on a
missing engine — it lets the agent try fetch/search instead, and only pre-warms
the engine when `auto_start_server=True` and the classifier leans browser.

> `auto_start_server` spawns the npm engine (`superbrowser` on `PATH`, or
> `npm start` from a checkout). A pip-only install has the Python half but not
> the npm engine — install it with `npm i -g runagent-superbrowser`. The SDK
> only ever stops an engine **it** started.

## Remote (serverless) mode

Run the browser on the RunAgent **serverless** engine instead of a local one. The
query is authenticated with your RunAgent API key, routed through the middleware,
and executed in an **on-demand micro-VM** with a **per-user persistent session**
(cookies/profiles survive across runs). Under the hood this reuses the runagent
SDK's remote stack — `RunAgentClient` with `local=False` + `persistent_memory` —
hitting the same `/api/v1/agents/{id}/run` path as any other RunAgent agent.

```python
from runagent_superbrowser import SuperBrowser

sb = SuperBrowser(
    remote=True,                     # execute on serverless via the middleware
    persistent=True,                 # per-user persistent browser session across runs
    agent_id="<browser-agent-id>",   # from your Browser agent page in the dashboard
    api_key="rau_...",               # or set RUNAGENT_API_KEY
)
res = sb.run("find the cheapest 4-star hotel in Sylhet for next weekend")
print(res.text)
```

Everything also resolves from the environment, so this is equivalent:

```bash
export RUNAGENT_API_KEY=rau_...
export SUPERBROWSER_AGENT_ID=<browser-agent-id>
export SUPERBROWSER_REMOTE=1          # or pass remote=True
```
```python
SuperBrowser(persistent=True).run("…")
```

Or from the CLI:

```bash
superbrowser-run "summarize today's top HN story" \
  --remote --persistent --agent-id <browser-agent-id> --api-key rau_...
```

Notes:
- **Local mode is unchanged** — without `remote`, `npm run dev` then `SuperBrowser().run(...)`.
- Remote mode needs the runagent SDK: `pip install 'runagent-superbrowser[remote]'`.
- `output_schema` is not sent in remote mode (the engine returns text); use it
  locally for typed parsing.
- The first call cold-starts a micro-VM (Chromium boot ~10–15s); subsequent calls
  reuse the warm VM until it's idle-reaped.

## Deploy via the runagent CLI (callable from every SDK)

Beyond local mode, you can **deploy SuperBrowser to RunAgent serverless** with the
`runagent` CLI as a standard agent. Once deployed it runs on on-demand micro-VMs
with a per-user persistent session, and is reachable from **any** RunAgent SDK
(Python/TS/Go/Rust/Dart/C#) — not just this Python package. The heavy Node +
Chromium engine is built server-side, so the deploy project is tiny.

```bash
# scaffold a deploy project (or use the repo's deploy/ directory)
runagent init my-browser --from-template superbrowser/default
cd my-browser
cp .env.example .env          # set LLM_MODEL + OPENAI_API_KEY (or ANTHROPIC_API_KEY)
runagent deploy .             # prints your agent_id
```

`.env` is uploaded at deploy and written to `/root/.env` in the VM — that's how
the agent gets its LLM key. Infra (headless, Chromium, persistence) is baked into
the image; you only provide secrets/options (see `.env.example`).

Call the `run` entrypoint from any SDK with `local=false` + `persistent_memory=true`:

```python
from runagent import RunAgentClient
client = RunAgentClient(agent_id="<agent_id>", entrypoint_tag="run",
                        local=False, persistent_memory=True)
print(client.run(task="find the cheapest 4-star hotel in Sylhet this weekend"))
```

This is the generic equivalent of `SuperBrowser(remote=True, persistent=True,
agent_id="<agent_id>")` — use whichever fits your stack.

## Constructor reference

```python
SuperBrowser(
    model=None,                  # override ~/.nanobot/config.json model (best-effort)
    workspace_root=None,         # base for provisioned workspaces ($SUPERBROWSER_WORKSPACE_ROOT)
    server_url=None,             # default http://localhost:3100
    vision=None,                 # True/False toggles VISION_ENABLED; None inherits env
    vision_api_key=None,         # VISION_API_KEY
    auto_start_server=False,     # spawn/teardown the TS engine for browser mode
    server_cmd=None,             # override the spawn argv (default ["superbrowser","http"])
    server_start_timeout=30.0,
    provision_force=False,       # re-copy bundled SOUL.md even if a workspace one exists
    env=None,                    # extra env vars to set before the bridge imports
    # remote (serverless) execution — see "Remote (serverless) mode":
    remote=False,                # route the run through the middleware → serverless engine
    persistent=False,            # remote: per-user persistent browser session
    agent_id=None,               # remote: Browser agent id ($SUPERBROWSER_AGENT_ID)
    api_key=None,                # remote: RunAgent API key ($RUNAGENT_API_KEY)
    user_id=None,                # remote: scope persistence to a user id (optional)
    base_url=None,               # remote: middleware base URL ($RUNAGENT_BASE_URL)
)
```

`run()` / `arun()` accept: `mode`, `url`, `output_schema`, `force_browser`,
`enable_human_handoff` (default `True`; set `False` for unattended runs so a
failed captcha doesn't wait on a human), and `timeout` (seconds).

## Configuration & `.env`

`SuperBrowser()` loads a `.env` file on construction (walking up from the
current directory), so anything you'd otherwise `export` works from `.env`:

```bash
LLM_MODEL=openai/gpt-4o
OPENAI_API_KEY=sk-...
VISION_API_KEY=...                 # cheap vision model for screenshots (browser mode)
SUPERBROWSER_URL=http://localhost:3100
```

Precedence, highest first: **explicit constructor argument → shell env var →
`.env` → built-in default**. So `SuperBrowser(server_url=…)` beats a shell
`SUPERBROWSER_URL`, which beats `.env`. Model selection comes from
`~/.nanobot/config.json` (`nanobot onboard`); your provider key can live in
`.env` and nanobot resolves it. Server, vision, captcha, and stealth knobs are
all plain env vars — see [`.env.example`](../.env.example).

## How the prompting ships

Each agent's system prompt is the `SOUL.md` in its workspace. The SDK provisions
three role workspaces (orchestrator / browser / search) and seeds each with its
bundled prompt on first use. In a source checkout it reuses the in-repo
`nanobot/workspace_<role>/` dirs; once installed it provisions under
`~/.superbrowser/workspaces/<role>/` (override with `workspace_root=` or
`$SUPERBROWSER_WORKSPACE_ROOT`). This is why a `pip install`ed agent behaves like
the dev tree instead of falling back to a generic prompt.
