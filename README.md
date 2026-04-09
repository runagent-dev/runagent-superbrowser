# SuperBrowser

An agentic headless browser that sees, thinks, and acts. Part of the [RunAgent](https://github.com/runagent-dev/runagent) super agent ecosystem.

SuperBrowser runs as a serverless micro VM on RunAgent Cloud. Any super agent — OpenClaw, PicoClaw, ZeroClaw, or your own — can spin up an isolated browser instance on demand, browse the web autonomously, and shut it down when done. No infrastructure to manage.

It combines a headless Chromium engine with a dual-agent AI loop (Navigator + Planner) and exposes everything through clean APIs. The agent opens a browser, sees screenshots at every step, decides what to do, and acts.

## RunAgent Ecosystem

```
┌─────────────────────────────────────────────────┐
│                 RunAgent Cloud                  │
│                                                 │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐   │
│  │ OpenClaw  │  │ PicoClaw  │  │ ZeroClaw  │   │
│  │  Agent    │  │  Agent    │  │  Agent    │   │
│  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘   │
│        │              │              │          │
│        └──────────┬───┴──────────────┘          │
│                   │                             │
│           ┌───────▼────────┐                    │
│           │  SuperBrowser  │  ← serverless      │
│           │   micro VM     │    micro VM         │
│           │                │    per request      │
│           │  Chromium +    │                    │
│           │  Navigator +   │                    │
│           │  Planner       │                    │
│           └────────────────┘                    │
└─────────────────────────────────────────────────┘
```

Any claw agent sends a task (or controls step-by-step via session APIs). SuperBrowser spins up, does the job, returns results, shuts down. Each instance is fully isolated.

## How it works

```
Any Agent (OpenClaw, PicoClaw, ZeroClaw, nanobot, custom)
    │
    ├── browser_open("https://irctc.co.in")     → sees screenshot + interactive elements
    ├── browser_type([3], "Delhi")               → sees autocomplete appear
    ├── browser_keys("ArrowDown")                → sees suggestion highlighted
    ├── browser_keys("Enter")                    → sees "Delhi" selected
    ├── browser_click([8])                       → sees search results
    ├── browser_screenshot()                     → verify current state
    ├── browser_eval("document.title")           → run JS when needed
    └── browser_close()                          → cleanup
```

Every action returns a screenshot. The agent sees the page, decides, acts, verifies. When stuck, it takes a screenshot, analyzes, tries a different approach.

## Features

**Browser Engine**
- Headless Chromium with puppeteer-extra stealth plugin
- CDP mouse/keyboard dispatch for precise input (3-tier coordinate resolution)
- Anti-detection: webdriver hiding, plugin spoofing, WebGL masking, permission patching
- Request interception, ad blocking, proxy support
- Dialog handling, file upload, PDF export, download monitoring

**Agent System**
- 30 browser actions (click, type, scroll, navigate, drag, dropdowns, DOM search, JS eval, and more)
- Dual-agent loop: Navigator executes actions, Planner validates and strategizes
- Screenshot vision feedback at every step
- DOM tree with indexed interactive elements (`[1]<button>Submit</button>`)
- Accessibility tree fallback for complex pages
- Cursor-interactive element detection for custom components
- Context overflow handling with automatic message compaction

**APIs**
- Session-based step-by-step control (13 endpoints)
- High-level fire-and-forget tasks (Navigator+Planner handles everything)
- Browserless-compatible screenshot/PDF/content/scrape/function APIs
- Concurrency limiter with queue management
- MCP server for tool discovery

**Integrations**
- RunAgent super agents: OpenClaw, PicoClaw, ZeroClaw
- [nanobot](https://github.com/HKUDS/nanobot) — 21 registered tools (8 high-level + 13 session-based)
- Any OpenAI-compatible LLM (Claude, GPT-4, Gemini, local models)
- Any agent framework via HTTP API

## Quick Start

### Self-hosted

```bash
git clone https://github.com/user/runagent-superbrowser.git
cd runagent-superbrowser
npm install

echo "OPENAI_API_KEY=sk-..." > .env
npm run build
npm start
```

### RunAgent Cloud

```bash
# Deploy as a serverless micro VM
runagent deploy superbrowser

# Your claw agents can now use it automatically
```

### Docker

```bash
docker compose up -d
```

## Usage

### From any claw agent

SuperBrowser is a plain HTTP server. Any agent connects via session APIs:

```python
import httpx

# Open a browser session
r = httpx.post("http://superbrowser:3100/session/create",
    json={"url": "https://example.com"})
session_id = r.json()["sessionId"]
screenshot = r.json()["screenshot"]   # base64 JPEG — agent sees the page
elements = r.json()["elements"]       # [0]<input placeholder="Search"> [1]<button>Go

# Type into a field
r = httpx.post(f"http://superbrowser:3100/session/{session_id}/type",
    json={"index": 0, "text": "hello world"})
new_screenshot = r.json()["screenshot"]  # agent sees the result

# Click a button
r = httpx.post(f"http://superbrowser:3100/session/{session_id}/click",
    json={"index": 1})

# Run JavaScript if needed
r = httpx.post(f"http://superbrowser:3100/session/{session_id}/evaluate",
    json={"script": "document.querySelectorAll('.result').length"})

# Close when done
httpx.delete(f"http://superbrowser:3100/session/{session_id}")
```

### Fire-and-forget (let Navigator+Planner handle it)

```bash
curl -X POST http://localhost:3100/task \
  -H "Content-Type: application/json" \
  -d '{"task": "Search Google for latest AI news and extract the top 3 results"}'
```

### With nanobot

```python
from nanobot import Nanobot
from superbrowser_bridge.tools import register_all_tools

bot = Nanobot.from_config(config_path="nanobot/config/config.json")
register_all_tools(bot)

result = await bot.run("Fill the contact form on example.com with name John Doe")
print(result.content)
```

### CLI

```bash
node build/index.js task "Go to google.com and search for weather in Tokyo"
```

## Project Structure

```
src/
├── browser/          # Headless engine, CDP, stealth, DOM, input dispatch
├── agent/            # Navigator, Planner, 30 actions, prompts, executor
├── llm/              # OpenAI-compatible LLM provider
├── server/           # HTTP API (session + high-level) + MCP server
└── utils/            # Logger, tokens, images

nanobot/              # Python integration
├── superbrowser_bridge/
│   ├── tools.py           # 8 high-level nanobot tools
│   └── session_tools.py   # 13 step-by-step nanobot tools
├── config/config.json
├── workspace/SOUL.md
└── run.py
```

## API Reference

### Session APIs

Step-by-step browser control. Each action returns a screenshot so the agent can see and decide.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/session/create` | Open browser session |
| POST | `/session/:id/navigate` | Navigate to URL |
| GET | `/session/:id/screenshot` | Take screenshot |
| GET | `/session/:id/state` | Get DOM tree + screenshot |
| POST | `/session/:id/click` | Click element by index or coordinates |
| POST | `/session/:id/type` | Type text into field |
| POST | `/session/:id/keys` | Send keyboard keys |
| POST | `/session/:id/scroll` | Scroll page |
| POST | `/session/:id/select` | Select dropdown option |
| POST | `/session/:id/evaluate` | Execute JavaScript |
| POST | `/session/:id/dialog` | Handle alert/confirm/prompt |
| GET | `/session/:id/markdown` | Extract page as markdown |
| DELETE | `/session/:id` | Close session |

### High-level APIs

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/task` | Execute agentic browser task |
| POST | `/screenshot` | Screenshot with full page setup options |
| POST | `/pdf` | Export page as PDF |
| POST | `/content` | Get rendered HTML |
| POST | `/scrape` | Scrape elements with debug data |
| POST | `/function` | Execute arbitrary puppeteer code |
| GET | `/health` | Health check with metrics |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3100` | Server port |
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `LLM_MODEL` | `gpt-4o` | Model for Navigator/Planner |
| `HEADLESS` | `true` | Run browser headlessly |
| `DOWNLOAD_DIR` | `/tmp/superbrowser/downloads` | Download directory |
| `CONCURRENT` | `10` | Max concurrent sessions |
| `QUEUED` | `10` | Max queued requests |
| `TIMEOUT` | `60000` | Request timeout (ms) |
| `PUPPETEER_EXECUTABLE_PATH` | — | Custom Chromium path |



## License

MIT
