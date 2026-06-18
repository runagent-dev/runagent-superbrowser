# SuperBrowser SDK — examples

Runnable examples for the `runagent_superbrowser` Python SDK. Each script is
self-contained and prints what it's doing.

Full SDK guide: [`../docs/sdk.md`](../docs/sdk.md).

## Setup

```bash
# Option A — installed
pip install runagent-superbrowser
patchright install chromium          # stealth Chromium (only needed for browser mode)

# Option B — from this checkout (no install)
#   the scripts auto-add ./nanobot to sys.path if the package isn't installed,
#   so you can just run them — but you still need the deps:
pip install -r requirements.txt

# Configure the model + API key once (writes ~/.nanobot/config.json):
nanobot onboard
```

Every example needs an LLM (the orchestrator is an agent) — set it up with
`nanobot onboard`, or via `.env` (`LLM_MODEL` + your provider key).

## The examples

| File | Mode | Needs the TS engine (`:3100`)? | Shows |
|---|---|---|---|
| [`01_quickstart_fetch.py`](01_quickstart_fetch.py) | `fetch` | **no** | the simplest call — read-only, in-process |
| [`02_auto_mode.py`](02_auto_mode.py) | `auto` | only if it picks the browser | letting the agent decide + `res.classification` |
| [`03_browser_mode.py`](03_browser_mode.py) | `browser` | **yes** | a real browser session + `auto_start_server` |
| [`04_structured_output.py`](04_structured_output.py) | `fetch` | no | typed results via a pydantic `output_schema` |
| [`05_async.py`](05_async.py) | `fetch` | no | `arun()` + running several tasks concurrently |

Start with `01` — it needs nothing but an API key:

```bash
python examples/01_quickstart_fetch.py
```

## The browser engine

`03_browser_mode.py` needs the TypeScript engine. Either start it first…

```bash
npm run dev        # in this checkout (tsx watch, no build step) — serves :3100
# or: superbrowser http   (after `npm i -g runagent-superbrowser` / `npm run build`)
```

…or let the SDK start and stop it (the example uses `auto_start_server=True`).

## CLI

The same thing without writing Python:

```bash
superbrowser-run "what's the top story on hacker news?" --mode fetch
superbrowser-run "book the cheapest DAC->BKK flight" --mode browser --auto-start-server
```
