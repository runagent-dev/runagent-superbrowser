# SuperBrowser

An agentic headless browser that sees, thinks, and acts. Built for AI agents that need to browse the web autonomously — fill forms, navigate complex UIs, extract data, and complete multi-step tasks.

SuperBrowser combines a headless Chromium engine with a dual-agent AI loop (Navigator + Planner) and exposes everything through clean APIs. Your AI agent opens a browser, sees screenshots at every step, decides what to do, and acts — just like a human would.

## How it works

```
Your Agent (nanobot, LangChain, custom)
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

Every action returns a screenshot. Your agent sees the page, decides, acts, verifies. When stuck, it takes a screenshot, analyzes, tries a different approach.

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
- [nanobot](https://github.com/HKUDS/nanobot) — 21 registered tools (8 high-level + 13 session-based)
- Any OpenAI-compatible LLM (Claude, GPT-4, Gemini, local models)
- Works with any agent framework via HTTP API

## Quick Start

```bash
# Clone and install
git clone https://github.com/user/runagent-superbrowser.git
cd runagent-superbrowser
npm install

# Set your API key
echo "OPENAI_API_KEY=sk-..." > .env
# or
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

# Build and start
npm run build
npm start
```

The server starts on port 3100. Test it:

```bash
# Take a screenshot
curl -X POST http://localhost:3100/screenshot \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}' \
  --output screenshot.jpg

# Open a session and interact step by step
curl -X POST http://localhost:3100/session/create \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.google.com"}'

# Run an agentic task (Navigator + Planner handle it)
curl -X POST http://localhost:3100/task \
  -H "Content-Type: application/json" \
  -d '{"task": "Search Google for latest AI news and extract the top 3 results"}'
```

## Usage with nanobot

```bash
# Install nanobot
pip install nanobot-ai

# Start SuperBrowser server (terminal 1)
npm start

# Run via nanobot (terminal 2)
cd nanobot
python run.py "Go to github.com and find trending Python repos today"
```

Or programmatically:

```python
from nanobot import Nanobot
from superbrowser_bridge.tools import register_all_tools

bot = Nanobot.from_config(config_path="nanobot/config/config.json")
register_all_tools(bot)

result = await bot.run("Fill the contact form on example.com with name John Doe")
print(result.content)
```

## Usage with any agent

SuperBrowser is a plain HTTP server. Use it from any language or framework:

```python
import httpx

# Open session
r = httpx.post("http://localhost:3100/session/create", json={"url": "https://example.com"})
session = r.json()
session_id = session["sessionId"]
screenshot = session["screenshot"]  # base64 JPEG
elements = session["elements"]     # [0]<input placeholder="Search"> [1]<button>Go</button>

# Type into the first input
r = httpx.post(f"http://localhost:3100/session/{session_id}/type",
    json={"index": 0, "text": "hello world"})
new_screenshot = r.json()["screenshot"]

# Click the button
r = httpx.post(f"http://localhost:3100/session/{session_id}/click",
    json={"index": 1})

# Close
httpx.delete(f"http://localhost:3100/session/{session_id}")
```

## CLI Mode

Run a single task directly:

```bash
npm run build
node build/index.js task "Go to google.com and search for weather in Tokyo"
```

## Docker

```bash
docker compose up -d
```

## Project Structure

```
src/
├── browser/          # Headless engine, CDP, stealth, DOM, input dispatch
├── agent/            # Navigator, Planner, 30 actions, prompts, executor
├── llm/              # OpenAI-compatible LLM provider
├── server/           # HTTP API + MCP server
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

### Session APIs (step-by-step control)

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
| `CORS` | `false` | Enable CORS |
| `PUPPETEER_EXECUTABLE_PATH` | — | Custom Chromium path |

## Architecture

SuperBrowser draws from three production codebases:

- **[browserless](https://github.com/browserless/browserless)** — Stealth plugin, CDP session management, request interception, hooks, concurrency limiter, goto utility with full page setup
- **[BrowserOS](https://github.com/anthropics/browseros)** — CDP input dispatch, 3-tier element coordinate resolution, accessibility tree, cursor detection, console collector, download monitoring, dialog handling
- **[nanobrowser](https://github.com/nicepkg/nanobrowser)** — Dual-agent Navigator+Planner loop, DOM element indexing, screenshot vision feedback, extraction protocol, multi-action DOM stability checks, action schemas

All browser code is written from scratch. No imports from the above — only patterns.

## License

MIT
