# SuperBrowser

![SuperBrowser logo](assets/icons/futuristic-runner-search-logo.png)

A headless browser built for AI agents. It sees, decides, and acts on web pages autonomously.

SuperBrowser gives any AI agent full browser control through simple HTTP APIs. Open a session, get a screenshot, click a button, fill a form, run a Puppeteer script, read the results — every action returns a screenshot so your agent always knows what's on screen.

Self-host it on your own machine, run it in Docker, or deploy it however you want. It's a standalone server with no external dependencies beyond Chromium.

## How it works

SuperBrowser supports three levels of browser control — pick what fits your task:

```
Your AI Agent (any framework, any language)
    │
    │  ── Step-by-step control ──────────────────────────────────
    │
    ├── POST /session/create  { url: "https://example.com" }
    │   └── returns: screenshot + interactive elements list
    │
    ├── POST /session/:id/click  { index: 3 }
    │   └── returns: updated screenshot
    │
    │  ── Puppeteer script execution ────────────────────────────
    │
    ├── POST /session/:id/script  { code: "
    │       await page.type('#search', 'AI news');
    │       await page.click('#submit');
    │       await page.waitForNavigation();
    │       return await page.title();
    │   " }
    │   └── returns: { success: true, result: "Search Results - AI news" }
    │
    │  ── Autonomous agent ──────────────────────────────────────
    │
    ├── POST /task  { task: "Find trending Python repos on GitHub" }
    │   └── returns: { success: true, finalAnswer: "1. ..." }
    │
    └── DELETE /session/:id
```

**Step-by-step**: Your agent calls click/type/scroll one at a time, sees a screenshot after each action, and decides the next move.

**Puppeteer scripts**: Your agent writes a multi-step Puppeteer script that runs server-side with full `page` API access — `page.goto()`, `page.click()`, `page.type()`, `page.waitForSelector()`, `page.screenshot()`, `page.keyboard`, `page.mouse`, and everything else Puppeteer offers. This is the same approach that powers tools like browserless's `/function` endpoint.

**Autonomous agent**: Fire-and-forget. A built-in Navigator+Planner dual-agent loop handles the entire task, with screenshot vision feedback at every step.

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

### Browser Engine
- Headless Chromium with stealth plugin (puppeteer-extra)
- CDP mouse and keyboard dispatch with 3-tier element coordinate resolution
- Anti-detection: webdriver masking, plugin spoofing, WebGL override, permission patching
- Human-like interaction: Bezier curve mouse movement, variable typing speed, micro-pauses
- Request interception, ad blocking, proxy support
- Dialog handling, file upload, PDF export, download monitoring via CDP events
- Captcha detection (reCAPTCHA, hCaptcha, Cloudflare Turnstile) with external solver support

### Puppeteer Script Execution
- Write full Puppeteer scripts and execute them with the real `page` object
- Full API access: `page.goto()`, `page.click()`, `page.type()`, `page.waitForSelector()`, `page.screenshot()`, `page.keyboard`, `page.mouse`, `page.evaluate()`, and everything else
- Helper utilities: `helpers.sleep(ms)`, `helpers.log(...)`, `helpers.screenshot(path?)`
- Timeout protection (default 60s, max 300s)
- Available via HTTP (`/function`, `/session/:id/script`), WebSocket (`script` command), agent action (`run_script`), and nanobot tool (`browser_run_script`)

### DOM Intelligence
- Interactive element indexing: `[0]<input placeholder="Search"> [1]<button>Go`
- Accessibility tree fallback for complex/ARIA-heavy pages
- Cursor-interactive element detection (finds clickable divs that ARIA misses)
- DOM search via CSS selectors and XPath
- DOM element tracking via hash-based identity (branch path, attributes, xpath) — persists elements across page mutations
- Clean markdown content extraction

### Security
- Token-based authentication (optional, via `TOKEN` env var)
- SSRF protection: blocks localhost, private IPs, cloud metadata, file:// protocol
- URL firewall: configurable allow/deny lists, hard-coded dangerous protocol blocking (`chrome://`, `javascript:`, `data:`, `file://`)
- Content guardrails: prompt injection detection, task override blocking, sensitive data redaction (SSN, credit card patterns)
- Per-IP rate limiting
- Session auto-expiry (30 min idle, 2 hour max lifetime)
- Session cap (default 20 concurrent)
- Request payload limits

### Built-in Agent (optional)
- Dual-agent loop: Navigator executes actions, Planner validates progress
- 33 browser actions with Zod schema validation (including `run_script` for Puppeteer automation)
- Screenshot vision feedback at every step
- Context overflow handling with automatic message compaction
- Step history recording for debugging and task replay
- Typed error hierarchy: AuthError, UrlBlockedError, ScriptTimeoutError, MaxStepsError, etc.
- Human-in-the-loop: ask for credentials, OTP, confirmation before irreversible actions
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

# Run DOM-level JavaScript
r = httpx.post(f"http://localhost:3100/session/{session_id}/evaluate",
    json={"script": "document.querySelectorAll('.result').length"})

# Get page as markdown
r = httpx.get(f"http://localhost:3100/session/{session_id}/markdown")

# Check for captcha
r = httpx.get(f"http://localhost:3100/session/{session_id}/captcha/detect")

# Close session
httpx.delete(f"http://localhost:3100/session/{session_id}")
```

### Puppeteer script execution

Write full Puppeteer scripts and run them within a session. The script receives the `page` object with the complete Puppeteer API:

```python
import httpx

# Create session
r = httpx.post("http://localhost:3100/session/create",
    json={"url": "https://www.astrosage.com/free/free-life-report.asp"},
    headers={"Authorization": "Bearer YOUR_TOKEN"})
session_id = r.json()["sessionId"]

# Run a Puppeteer script — full page API access
r = httpx.post(f"http://localhost:3100/session/{session_id}/script",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={"code": """
        await page.type('#Name', 'John Doe', { delay: 30 });
        await page.select('#sex', 'male');
        await page.type('#Day', '15');
        await page.type('#Month', '06');
        await page.type('#Year', '1990');
        await page.type('#place', 'Delhi', { delay: 200 });
        await helpers.sleep(3000);
        await page.keyboard.press('ArrowDown');
        await helpers.sleep(300);
        await page.keyboard.press('Enter');
        await helpers.sleep(1500);
        await page.click('input[name="submit"]');
        await page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 45000 });
        return await page.title();
    """})

result = r.json()
# { "success": true, "result": "Free Kundli - Janam Kundli", "logs": [], "duration": 12340 }

httpx.delete(f"http://localhost:3100/session/{session_id}")
```

The script can also be run as a one-shot (no session) via `POST /function`:

```python
r = httpx.post("http://localhost:3100/function",
    headers={"Authorization": "Bearer YOUR_TOKEN"},
    json={
        "url": "https://example.com",
        "code": "return await page.title();"
    })
```

### With nanobot

SuperBrowser ships with [nanobot](https://github.com/HKUDS/nanobot) integration — 25 registered tools that give the nanobot agent full browser control with screenshots at every step.

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

The nanobot agent has three levels of control:
- **`browser_run_script`** — Write a Puppeteer script for complex multi-step automation (navigate + fill + submit + wait + extract in one call)
- **`browser_eval`** — Quick DOM-level JavaScript for reading values or setting form fields
- **`browser_click` / `browser_type`** — Step-by-step control when you need to observe the page between each action

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

### WebSocket real-time control

For real-time bidirectional control (e.g., from a gateway or UI):

```javascript
const ws = new WebSocket('ws://localhost:3100/ws/session/SESSION_ID');

// Send commands
ws.send(JSON.stringify({ action: 'navigate', data: { url: 'https://example.com' } }));
ws.send(JSON.stringify({ action: 'click', data: { index: 3 } }));
ws.send(JSON.stringify({ action: 'script', data: {
  code: 'await page.type("#q", "hello"); return await page.title();'
} }));

// Receive state updates with screenshots
ws.onmessage = (e) => {
  const { event, data } = JSON.parse(e.data);
  // event: 'state' | 'script_result' | 'eval_result' | 'error' | ...
};
```

## Project Structure

```
src/
├── browser/          # Engine, CDP, stealth, DOM, input, captcha, humanize,
│                     # script-runner, guardrails, firewall, dom-history
├── agent/            # Navigator, Planner, 33 actions, prompts, executor,
│                     # events, errors, human-input, step history
├── llm/              # OpenAI-compatible provider (used by built-in agent only)
├── server/           # HTTP API, WebSocket, MCP server, auth middleware
└── utils/            # Logger, tokens, images

nanobot/              # Python integration (optional)
├── superbrowser_bridge/
│   ├── tools.py           # 8 high-level nanobot tools
│   └── session_tools.py   # 17 step-by-step nanobot tools (incl. run_script, captcha)
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
| POST | `/session/:id/evaluate` | Execute JavaScript in page DOM context |
| POST | `/session/:id/script` | Execute Puppeteer script with full page API (requires TOKEN) |
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
| POST | `/function` | Execute Puppeteer script (requires TOKEN) |
| POST | `/task` | Autonomous agent task (requires LLM key) |
| GET | `/task/:id/history` | Get step-by-step execution history for a task |
| GET | `/health` | Server health + metrics |
| GET | `/metrics` | Session and job metrics |

### Script execution details

Both `/function` and `/session/:id/script` execute Puppeteer code with full `page` API access. The code is the body of an async function that receives:

| Parameter | Type | Description |
|-----------|------|-------------|
| `page` | Puppeteer `Page` | Full API: goto, click, type, waitForSelector, screenshot, evaluate, keyboard, mouse, etc. |
| `context` | `object` | Optional data passed via the request body |
| `helpers.sleep(ms)` | function | Promise-based delay |
| `helpers.log(...args)` | function | Logs returned in response `logs` array |
| `helpers.screenshot(path?)` | function | Returns base64 JPEG screenshot |

**Request body:**
```json
{
  "code": "await page.goto('https://example.com'); return await page.title();",
  "context": { "searchQuery": "hello" },
  "timeout": 60000
}
```

**Response:**
```json
{
  "success": true,
  "result": "Example Domain",
  "logs": [],
  "duration": 1234
}
```

Supported code formats: raw function body (recommended), `async ({ page }) => { ... }`, `async function({ page }) { ... }`, `export default async function({ page }) { ... }`.

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
| `FIREWALL_ALLOW_LIST` | — | Comma-separated allowed domains |
| `FIREWALL_DENY_LIST` | — | Comma-separated blocked domains |
| `OPENAI_API_KEY` | — | Only for built-in `/task` agent |
| `ANTHROPIC_API_KEY` | — | Only for built-in `/task` agent |
| `LLM_MODEL` | `gpt-4o` | Only for built-in `/task` agent |

## Architecture

SuperBrowser is built from patterns found in three production browser automation codebases, with all code written from scratch:

- **[browserless](https://github.com/browserless/browserless)** — stealth, CDP sessions, request interception, goto utility, concurrency limiter, Puppeteer script execution
- **[BrowserOS](https://github.com/anthropics/browseros)** — CDP input dispatch, element coordinate resolution, accessibility tree, cursor detection, console collector
- **[nanobrowser](https://github.com/nicepkg/nanobrowser)** — Navigator+Planner agent loop, DOM element indexing, screenshot feedback, extraction protocol, action schemas, security guardrails, URL firewall, DOM history tracking, error hierarchy

## Deployment

SuperBrowser is designed as middleware. Deploy it however fits your stack:

- **Self-hosted**: `npm start` on any machine with Chromium
- **Docker**: `docker compose up -d`
- **Kubernetes**: Stateless container, one pod per instance
- **Serverless**: Fast cold start, ephemeral by design
- **[RunAgent Cloud](https://runagent.cloud)**: Managed deployment as part of the RunAgent super agent ecosystem (OpenClaw, PicoClaw, ZeroClaw)

## License

MIT
