# SuperBrowser

![SuperBrowser logo](assets/icons/futuristic-runner-search-logo.png)

A headless browser built for AI agents. It sees, decides, and acts on web pages autonomously.

SuperBrowser gives any AI agent full browser control through simple HTTP APIs. Open a session, get a screenshot, click a button, fill a form, read the results — every action returns a screenshot so your agent always knows what's on screen.

Self-host it on your own machine, run it in Docker, or deploy it however you want. It's a standalone server with no external dependencies beyond Chromium.

## How it works

```
Your AI Agent (any framework, any language)
    │
    ├── POST /session/create  { url: "https://example.com" }
    │   └── returns: screenshot + interactive elements list
    │
    ├── POST /session/:id/type  { index: 3, text: "Delhi" }
    │   └── returns: updated screenshot (autocomplete appeared)
    │
    ├── POST /session/:id/keys  { keys: "ArrowDown" }
    │   └── returns: updated screenshot (suggestion highlighted)
    │
    ├── POST /session/:id/click  { index: 8 }
    │   └── returns: updated screenshot (search results)
    │
    ├── GET  /session/:id/state
    │   └── returns: DOM tree + screenshot + console errors
    │
    └── DELETE /session/:id
```

Every action returns a screenshot. Your agent sees the page, decides what to do next, acts, and verifies the result. When stuck, it takes a screenshot, analyzes, tries a different approach.

## Quick Start

```bash
git clone https://github.com/runagent-dev/superbrowser.git
cd superbrowser
npm install
npm run build
npm start
```

Server starts on port 3100. No API keys needed for the browser server itself.

```bash
# Take a screenshot
curl -X POST http://localhost:3100/screenshot \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}' \
  --output screenshot.jpg

# Open a persistent session
curl -X POST http://localhost:3100/session/create \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.google.com"}'
```

### Docker

```bash
docker compose up -d
```

## Features

**Browser Engine**
- Headless Chromium with stealth plugin (puppeteer-extra)
- CDP mouse and keyboard dispatch with 3-tier element coordinate resolution
- Anti-detection: webdriver masking, plugin spoofing, WebGL override, permission patching
- Human-like interaction: Bezier curve mouse movement, variable typing speed, micro-pauses
- Request interception, ad blocking, proxy support
- Dialog handling, file upload, PDF export, download monitoring via CDP events
- Captcha detection (reCAPTCHA, hCaptcha, Cloudflare Turnstile) with external solver support

**DOM Intelligence**
- Interactive element indexing: `[0]<input placeholder="Search"> [1]<button>Go`
- Accessibility tree fallback for complex/ARIA-heavy pages
- Cursor-interactive element detection (finds clickable divs that ARIA misses)
- DOM search via CSS selectors and XPath
- Clean markdown content extraction

**Security**
- Token-based authentication (optional, via `TOKEN` env var)
- SSRF protection: blocks localhost, private IPs, cloud metadata, file:// protocol
- Per-IP rate limiting
- Session auto-expiry (30 min idle, 2 hour max lifetime)
- Session cap (default 20 concurrent)
- Request payload limits

**Built-in Agent (optional)**
- Dual-agent loop: Navigator executes actions, Planner validates progress
- 32 browser actions with Zod schema validation
- Screenshot vision feedback at every step
- Context overflow handling with automatic message compaction
- Requires LLM API key only if you use the `/task` endpoint

## Usage

### From any language — HTTP session APIs

SuperBrowser is a plain HTTP server. Use it from Python, Node.js, Go, Rust, whatever:

```python
import httpx

# Open session
r = httpx.post("http://localhost:3100/session/create",
    json={"url": "https://example.com"})
data = r.json()
session_id = data["sessionId"]
screenshot = data["screenshot"]   # base64 JPEG
elements = data["elements"]       # indexed interactive elements

# Type into the first input
r = httpx.post(f"http://localhost:3100/session/{session_id}/type",
    json={"index": 0, "text": "hello world"})

# Click a button
r = httpx.post(f"http://localhost:3100/session/{session_id}/click",
    json={"index": 1})

# Run JavaScript
r = httpx.post(f"http://localhost:3100/session/{session_id}/evaluate",
    json={"script": "document.querySelectorAll('.result').length"})

# Get page as markdown
r = httpx.get(f"http://localhost:3100/session/{session_id}/markdown")

# Check for captcha
r = httpx.get(f"http://localhost:3100/session/{session_id}/captcha/detect")

# Close session
httpx.delete(f"http://localhost:3100/session/{session_id}")
```

### With nanobot

SuperBrowser ships with [nanobot](https://github.com/HKUDS/nanobot) integration — 24 registered tools that give the nanobot agent full browser control with screenshots at every step.

```bash
# Terminal 1: start SuperBrowser
npm start

# Terminal 2: run via nanobot
cd nanobot
pip install nanobot-ai
python run.py "Go to github.com and find trending Python repos"
```

Or programmatically:

```python
from nanobot import Nanobot
from superbrowser_bridge.tools import register_all_tools

# Uses ~/.nanobot/config.json (set up via `nanobot onboard`)
bot = Nanobot.from_config(workspace="nanobot/workspace")
register_all_tools(bot)

result = await bot.run("Fill the contact form on example.com with name John Doe")
print(result.content)
```

### Built-in agent (optional)

If you set an LLM API key, SuperBrowser can handle tasks autonomously:

```bash
# Set API key for built-in agent
echo "OPENAI_API_KEY=sk-..." >> .env

# Fire-and-forget task
curl -X POST http://localhost:3100/task \
  -H "Content-Type: application/json" \
  -d '{"task": "Search Google for latest AI news and extract the top 3 results"}'

# CLI mode
node build/index.js task "Go to google.com and search for weather in Tokyo"
```

## Project Structure

```
src/
├── browser/          # 22 modules — engine, CDP, stealth, DOM, input, captcha, humanize
├── agent/            # Navigator, Planner, 32 actions, prompts, executor, events
├── llm/              # OpenAI-compatible provider (used by built-in agent only)
├── server/           # HTTP API, MCP server, auth middleware
└── utils/            # Logger, tokens, images

nanobot/              # Python integration (optional)
├── superbrowser_bridge/
│   ├── tools.py           # 8 high-level nanobot tools
│   └── session_tools.py   # 16 step-by-step nanobot tools (with captcha)
├── workspace/SOUL.md      # Agent personality + instructions
└── run.py                 # CLI entry point
```

## API Reference

### Session APIs

Persistent browser sessions with step-by-step control. Every mutation returns an updated screenshot.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/session/create` | Open browser session (returns screenshot + elements) |
| POST | `/session/:id/navigate` | Navigate to URL |
| GET | `/session/:id/screenshot` | Take screenshot (JPEG) |
| GET | `/session/:id/state` | DOM tree + screenshot + console errors |
| POST | `/session/:id/click` | Click element by index or x,y coordinates |
| POST | `/session/:id/type` | Type text into input field |
| POST | `/session/:id/keys` | Send keyboard keys (Enter, Tab, Control+a) |
| POST | `/session/:id/scroll` | Scroll page (up/down/percent) |
| POST | `/session/:id/select` | Select dropdown option |
| POST | `/session/:id/evaluate` | Execute JavaScript in page |
| POST | `/session/:id/dialog` | Handle alert/confirm/prompt |
| GET | `/session/:id/markdown` | Extract page content as markdown |
| GET | `/session/:id/pdf` | Export page as PDF |
| GET | `/session/:id/captcha/detect` | Check for captcha |
| GET | `/session/:id/captcha/screenshot` | Screenshot captcha area |
| POST | `/session/:id/captcha/solve` | Solve via external API |
| DELETE | `/session/:id` | Close session |
| GET | `/sessions` | List active sessions |

### Utility APIs

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/screenshot` | One-shot screenshot (no session needed) |
| POST | `/pdf` | One-shot PDF export |
| POST | `/content` | Get rendered HTML |
| POST | `/scrape` | Scrape elements by CSS selectors |
| POST | `/function` | Execute JavaScript (requires TOKEN) |
| POST | `/task` | Autonomous agent task (requires LLM key) |
| GET | `/health` | Server health + metrics |
| GET | `/metrics` | Session and job metrics |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3100` | Server port |
| `TOKEN` | — | Auth token (if set, all requests require Bearer auth) |
| `HEADLESS` | `true` | Headless mode |
| `DOWNLOAD_DIR` | `/tmp/superbrowser/downloads` | Download directory |
| `CONCURRENT` | `10` | Max concurrent jobs |
| `QUEUED` | `10` | Max queued jobs |
| `TIMEOUT` | `60000` | Job timeout (ms) |
| `MAX_SESSIONS` | `20` | Max concurrent sessions |
| `RATE_LIMIT` | `200` | Requests per IP per minute |
| `TASK_TIMEOUT` | `300000` | Max agent task duration (ms) |
| `CAPTCHA_PROVIDER` | — | Captcha solver (2captcha, anticaptcha) |
| `CAPTCHA_API_KEY` | — | Captcha solver API key |
| `PUPPETEER_EXECUTABLE_PATH` | — | Custom Chromium binary path |
| `CORS` | `false` | Enable CORS |
| `CORS_ALLOW_ORIGIN` | — | Allowed CORS origin |
| `OPENAI_API_KEY` | — | Only for built-in `/task` agent |
| `LLM_MODEL` | `gpt-4o` | Only for built-in `/task` agent |

## Architecture

SuperBrowser is built from patterns found in three production browser automation codebases, with all code written from scratch:

- **[browserless](https://github.com/browserless/browserless)** — stealth, CDP sessions, request interception, goto utility, concurrency limiter, hooks
- **[BrowserOS](https://github.com/anthropics/browseros)** — CDP input dispatch, element coordinate resolution, accessibility tree, cursor detection, console collector
- **[nanobrowser](https://github.com/nicepkg/nanobrowser)** — Navigator+Planner agent loop, DOM element indexing, screenshot feedback, extraction protocol, action schemas

## Deployment

SuperBrowser is designed as middleware. Deploy it however fits your stack:

- **Self-hosted**: `npm start` on any machine with Chromium
- **Docker**: `docker compose up -d`
- **Kubernetes**: Stateless container, one pod per instance
- **Serverless**: Fast cold start, ephemeral by design
- **[RunAgent Cloud](https://runagent.cloud)**: Managed deployment as part of the RunAgent super agent ecosystem (OpenClaw, PicoClaw, ZeroClaw)

## License

MIT
