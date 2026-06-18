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
