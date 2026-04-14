# SuperBrowser

![SuperBrowser logo](assets/icons/futuristic-runner-search-logo.png)

**A headless browser built for AI agents — with a real human in the loop when it matters.**

SuperBrowser lets any AI agent drive a real Chromium browser through simple HTTP APIs. Open a session, take a screenshot, click a button, fill a form, run a Puppeteer script — every action returns state so your agent always knows what's on screen. When the agent hits a wall (captcha, OTP, weird modal), it hands off to a human through a live-view URL, the human clicks once, and the agent resumes on the same session.

Self-host it on your own machine, run it in Docker, or deploy it however you want. The browser server itself has no external dependencies beyond Chromium.

---

## Where this is going

The near-term target is the **serverless, stateful browser** — deployed on [RunAgent Cloud](https://runagent.cloud) and controlled from whichever chat app the user already lives in. No new UI to learn, no desktop client, no web console for day-to-day tasks. You send a message, the browser does the work, and when it needs you (captcha, OTP, a decision), it messages you back with a link you tap.

```
  You on WhatsApp / Telegram / Slack
          │
          │   "find me a black dress like this photo, size M, ships to Dhaka"
          ▼
  RunAgent Cloud  (serverless, stateful — cold-starts preserve cookies,
          │       per-task memory, handoff ledger, fingerprint continuity)
          │
          ▼
  SuperBrowser session  →  Zara, H&M, Amazon, Daraz, ...
          │
          │   hits a captcha → pushes a "tap to solve" link back to WhatsApp
          │   you tap once → session resumes on the same cookies
          ▼
  Shortlist delivered as a WhatsApp message with images + links
```

The pieces making this real already live in the repo: cookie jars keyed on `SUPERBROWSER_TASK_ID + domain` so a warm task doesn't need a second solve, a cross-session handoff ledger so the same human isn't asked twice, a `HANDOFF_WEBHOOK_URL` hook so any messaging bridge can forward the live-view URL, and a WebSocket `awaiting_human` event with snapshot replay so late-connecting clients still catch an in-flight handoff. **The server is already "stateful enough" to survive a cold start on the same task** — what's next is the managed deployment + first-party messaging bridges.

---

```
Your AI Agent (any framework, any language)
    │
    │  ── Step-by-step control ──────────────────────────────────
    ├── POST /session/create   { url: "https://..." }
    ├── POST /session/:id/click, /type, /scroll, /navigate
    ├── GET  /session/:id/state
    │
    │  ── Puppeteer script execution ────────────────────────────
    ├── POST /session/:id/script  { code: "await page.type(...); ..." }
    │
    │  ── Autonomous agent ──────────────────────────────────────
    ├── POST /task  { task: "Find trending repos on GitHub" }
    │
    │  ── Human handoff (built in) ──────────────────────────────
    ├── GET  /session/:id/view        → live-view URL, human solves here
    ├── POST /session/:id/human-input → any client can reply
    └── ws   /ws/session/:id          → push "awaiting_human" events
```

---

## Why SuperBrowser

- **Works on sites that block bots.** Stealth plugin, CDP-level input, human-like mouse curves, per-domain fingerprint continuity, bot-protection cookie persistence across sessions — `cf_clearance` and friends ride along the next time you visit a site a human already cleared.
- **Fast-to-human captcha policy.** When auto-solve fails (at most once under `SUPERBROWSER_CAPTCHA_POLICY=fast_to_human`), the agent immediately surfaces a live-view URL. The human clicks through in their own browser, the agent detects the clearance and resumes on the same session. No more scripted retries that trip site hardening.
- **Cheap vision, expensive brain.** Screenshots are preprocessed by a dedicated Python vision agent (OpenAI / OpenRouter / Gemini) that converts each image into a text summary + bounding boxes. Your expensive reasoning model never pays image tokens. ~60–80% cost drop on observational iterations.
- **Three levels of control.** Step-by-step HTTP APIs for careful agents, full Puppeteer script execution for complex flows, or fire-and-forget autonomous tasks via `/task`.
- **WebSocket events for gateways.** Every session exposes a WebSocket that pushes `captcha_active` / `awaiting_human` / `captcha_done` events, with snapshot replay on connect — drop it behind a WhatsApp, Slack, or Discord bot without polling.
- **Safe by default.** SSRF guards, URL firewall, prompt-injection detection, sensitive-data redaction, per-IP rate limiting, token auth, session auto-expiry.

---

## Quick Start

```bash
git clone https://github.com/runagent-dev/superbrowser.git
cd superbrowser
npm install
npm run build
cp .env.example .env     # optional — defaults work for local dev
npm start
```

Server starts on port 3100. No API keys needed for the browser server itself — every env var in `.env.example` is optional.

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

---

## Features

### Browser Engine
- Headless Chromium with `puppeteer-extra-stealth`
- CDP mouse and keyboard dispatch with 3-tier element coordinate resolution
- Anti-detection: webdriver masking, plugin spoofing, WebGL override, permission patching
- Human-like interaction: Bezier mouse movement, variable typing speed, micro-pauses
- Request interception, ad blocking, proxy and region selection
- Dialog handling, file upload, PDF export, download monitoring

### Puppeteer Script Execution
- Write full Puppeteer scripts and execute them with the real `page` object
- Full API: `page.goto`, `page.click`, `page.type`, `page.waitForSelector`, `page.keyboard`, `page.mouse`, `page.evaluate`, everything
- Helpers: `helpers.sleep(ms)`, `helpers.log(...)`, `helpers.screenshot(path?)`
- Available via HTTP (`/function`, `/session/:id/script`), WebSocket, agent actions, and nanobot tools

### DOM Intelligence
- Interactive-element indexing: `[0]<input placeholder="Search"> [1]<button>Go`
- Accessibility-tree fallback for complex/ARIA-heavy pages
- Cursor-interactive detection (finds clickable divs that ARIA misses)
- DOM history tracking: hash-based element identity survives mutations
- Clean markdown content extraction

### Captcha, Reinvented
- Built-in solvers: Cloudflare Turnstile (DOM-level), reCAPTCHA checkbox auto-pass, 2captcha/anti-captcha token + grid API
- **Fast-to-human policy** (`SUPERBROWSER_CAPTCHA_POLICY=fast_to_human`): one auto attempt, then immediate handoff
- **Per-domain cookie jar** (`SUPERBROWSER_COOKIE_JAR=1`): persists `cf_clearance`, `__cf_bm`, `datadome`, Akamai and Imperva cookies scoped by task+domain; UA-pinned; 7-day TTL
- **15-min handoff ledger**: if the human solved the same domain recently, don't re-prompt them
- **Live-view UI**: self-contained HTML page, 2 FPS screenshot feed + click/type forwarding — works on any phone browser

### Human-in-the-Loop
- `/session/:id/view` — live browser view the human opens in their own browser
- `POST /session/:id/human-input/ask` — blocking RPC any agent can call for credentials/OTP/confirm
- WebSocket push of `awaiting_human` events with snapshot replay for late subscribers
- `HANDOFF_WEBHOOK_URL` env — fire a custom webhook when a human is needed (WhatsApp/Slack/Discord-ready payload with caption + screenshot)

### Vision Preprocessor (new)
Nanobot's brain never sees raw screenshots when this is enabled. A dedicated Python vision agent converts each image into `{summary, relevant_text, bboxes, flags}` text that the brain reasons over.

```
VISION_ENABLED=1
VISION_PROVIDER=openai        # or openrouter | gemini
VISION_MODEL=gpt-4o-mini      # or openai/gpt-4o-mini | gemini-2.0-flash-exp
VISION_API_KEY=<separate key — NOT your brain's key>
```

- Provider-agnostic — same openai SDK routes to OpenAI, OpenRouter, or Gemini's OpenAI-compat endpoint
- Intent hints per tool call (`intent="verify add-to-cart succeeded"`) for task-aware prompts
- TTL-bounded LRU cache keyed on `(session_id, url, dom_hash, intent_bucket)` — ~40% hit rate on chained tool calls
- Lenient pydantic validators: unknown roles, categorical confidences, missing flags all coerce cleanly
- Fall-soft: any provider/parse failure falls back to legacy image blocks automatically

### Security
- Token-based auth (optional, via `TOKEN`)
- SSRF protection: blocks localhost, private IPs, cloud metadata, `file://`
- URL firewall with allow/deny lists, dangerous-protocol blocking
- Prompt-injection detection, task-override blocking, SSN/credit-card redaction
- Per-IP rate limiting, session auto-expiry (30 min idle, 2 hr max), concurrent session cap

### Built-in Agent (optional)
- Dual-agent loop: Navigator executes actions, Planner validates progress
- 33 browser actions with Zod schema validation (includes `run_script`)
- Context-overflow handling with automatic message compaction
- Step-history recording for debugging and task replay
- Typed error hierarchy: `AuthError`, `UrlBlockedError`, `ScriptTimeoutError`, `MaxStepsError`

---

## Usage

Four ways to drive the browser — pick whichever fits:

```python
# 1. HTTP session APIs — any language, any framework
httpx.post("http://localhost:3100/session/create", json={"url": "https://example.com"})
httpx.post(f"http://localhost:3100/session/{sid}/click", json={"index": 1})
```

```python
# 2. Puppeteer script — one call, full page API
httpx.post(f"http://localhost:3100/session/{sid}/script",
    headers={"Authorization": "Bearer TOKEN"},
    json={"code": "await page.type('#q','...'); await page.click('#go'); return page.title();"})
```

```bash
# 3. Autonomous agent — fire-and-forget
curl -X POST http://localhost:3100/task -d '{"task": "find trending repos on GitHub"}'
```

```python
# 4. With nanobot — Python integration, 25 tools, vision preprocessor built in
from nanobot import Nanobot
from superbrowser_bridge.tools import register_all_tools
bot = Nanobot.from_config(workspace="nanobot/workspace")
register_all_tools(bot)
await bot.run("fill the contact form on example.com with name John Doe")
```

Real-time control + feedback events are also available on `ws://localhost:3100/ws/session/:id`. Connect a WebSocket and you'll receive `awaiting_human` / `captcha_active` / `captcha_done` events — this is the plug point for a chat-app bridge.

Full request/response shapes for every endpoint are in the [API Reference](#api-reference) below.

---

## API Reference

### Sessions

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/session/create` | Open session (returns screenshot + elements + viewUrl hints) |
| POST | `/session/:id/navigate` | Navigate to URL (restores per-domain cookies if jar enabled) |
| GET | `/session/:id/screenshot` | JPEG screenshot |
| GET | `/session/:id/state` | DOM tree + screenshot + console errors |
| POST | `/session/:id/click` | Click by index or x,y |
| POST | `/session/:id/type` | Type text |
| POST | `/session/:id/keys` | Send keys (`Enter`, `Tab`, `Control+a`) |
| POST | `/session/:id/scroll` | Scroll up/down/percent |
| POST | `/session/:id/drag` | Mouse drag (useful for slider captchas) |
| POST | `/session/:id/select` | Pick dropdown option |
| POST | `/session/:id/evaluate` | Run JavaScript in page context |
| POST | `/session/:id/script` | Run full Puppeteer script (requires `TOKEN`) |
| POST | `/session/:id/dialog` | Handle alert/confirm/prompt |
| GET | `/session/:id/markdown` | Page content as markdown |
| GET | `/session/:id/pdf` | Page as PDF |
| GET | `/session/:id/captcha/detect` | Returns `{captcha, viewUrl}` — viewUrl is always surfaced when captcha present |
| POST | `/session/:id/captcha/solve` | Run strategy registry (turnstile, 2captcha, checkbox, human-handoff) |
| DELETE | `/session/:id` | Close session |
| GET | `/sessions` | List active sessions |

### Human-in-the-loop

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/session/:id/view` | Self-contained HTML live-view UI |
| GET | `/session/:id/human-input` | Poll pending request |
| POST | `/session/:id/human-input` | Submit reply (`{id, data, cancelled?}`) |
| POST | `/session/:id/human-input/ask` | Server-side blocking ask (used by `browser_ask_user`) |

### Utility

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/screenshot`, `/pdf`, `/content`, `/scrape` | One-shot convenience endpoints |
| POST | `/function` | Execute Puppeteer script (requires `TOKEN`) |
| POST | `/task` | Autonomous agent task (requires LLM key) |
| GET | `/task/:id/history` | Step-by-step execution history |
| GET | `/health`, `/metrics` | Liveness + metrics |
| WS  | `/ws/session/:id` | Real-time control + `feedback` events |

---

## Configuration

### Core server

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3100` | HTTP + WebSocket port |
| `TOKEN` | — | Bearer auth for protected endpoints |
| `HEADLESS` | `true` | Headless mode |
| `PUPPETEER_EXECUTABLE_PATH` | — | Custom Chromium binary |
| `MAX_SESSIONS` | `20` | Concurrent session cap |
| `RATE_LIMIT` | `200` | Requests per IP per minute |
| `TASK_TIMEOUT` | `300000` | Max agent task duration (ms) |
| `SUPERBROWSER_PUBLIC_HOST` | — | Public base URL of the view UI (e.g. `https://browser.example.com`). Required when the server is behind a proxy. |

### Captcha + human handoff

| Variable | Default | Description |
|----------|---------|-------------|
| `CAPTCHA_PROVIDER` | — | `2captcha` or `anticaptcha` |
| `CAPTCHA_API_KEY` | — | Solver API key |
| `SUPERBROWSER_CAPTCHA_POLICY` | — | Set to `fast_to_human` for one-shot-then-human |
| `SUPERBROWSER_MAX_HUMAN_HANDOFFS` | `1` | Per-session handoff budget |
| `SUPERBROWSER_COOKIE_JAR` | — | Set to `1` to persist bot-protection cookies per task+domain |
| `SUPERBROWSER_COOKIE_JAR_PATH` | `~/.superbrowser/cookie-jar/` | Override jar directory |
| `SUPERBROWSER_TASK_ID` | — | Scope key for cookie jar + handoff ledger |
| `HANDOFF_WEBHOOK_URL` | — | Webhook fired on human handoff (WhatsApp/Slack bridge) |

### Vision preprocessor

| Variable | Default | Description |
|----------|---------|-------------|
| `VISION_ENABLED` | — | `1` to enable Python vision middleman |
| `VISION_PROVIDER` | `openai` | `openai`, `openrouter`, or `gemini` |
| `VISION_MODEL` | — | Provider-native model id (e.g. `gpt-4o-mini`) |
| `VISION_API_KEY` | — | Separate from brain's key |
| `VISION_BASE_URL` | — | Optional baseURL override |
| `VISION_CACHE_SIZE` | `200` | LRU entries |
| `VISION_CACHE_TTL_SEC` | `300` | Cache TTL |
| `VISION_MAX_TOKENS` | `1500` | Response cap |
| `VISION_MAX_BBOXES` | `30` | Truncate long bbox lists ranked by intent relevance |
| `VISION_TIMEOUT_MS` | `8000` | Hard timeout per call |

### Built-in agent (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | Required only for `/task` endpoint |
| `LLM_MODEL` | `gpt-4o` | Only for `/task` |

---

## Project Structure

```
src/
├── browser/            # Engine, CDP, stealth, DOM, input, captcha, humanize,
│                       # script-runner, guardrails, firewall, dom-history
│   └── captcha/
│       ├── strategies/ # turnstile, token-external, recaptcha-checkbox,
│       │               # recaptcha-grid-api, human-handoff
│       ├── cookie-jar.ts       # per-domain bot-protection cookie persistence
│       ├── handoff-ledger.ts   # 15-min cross-session "don't re-prompt" memory
│       └── orchestrator.ts     # strategy registry with fast-to-human short-circuit
├── agent/              # Navigator, Planner, actions, prompts, executor,
│                       # events, errors, human-input, feedback bus
├── llm/                # OpenAI-compatible provider (used by built-in /task agent)
├── server/             # HTTP API, WebSocket + feedback bridge, MCP, auth
└── utils/              # Logger, tokens, images

nanobot/
├── superbrowser_bridge/
│   ├── tools.py              # 8 high-level nanobot tools
│   ├── session_tools.py      # 17 step-by-step tools (incl. run_script, captcha)
│   └── worker_hook.py        # loop guidance, fast-to-human enforcement
├── vision_agent/             # Python vision preprocessor
│   ├── providers/            # openai, openrouter, gemini (OpenAI-compat endpoint)
│   ├── schemas.py            # BBox, PageFlags, VisionResponse (pydantic)
│   ├── prompts.py            # system + intent-bucketed user prompts
│   ├── cache.py              # TTL-bounded LRU
│   └── client.py             # VisionAgent orchestrator
└── workspace/SOUL.md         # nanobot agent personality + instructions
```

---

## Architecture

SuperBrowser stitches together patterns from three production browser-automation codebases, written from scratch:

- **[browserless](https://github.com/browserless/browserless)** — stealth, CDP sessions, request interception, goto utility, concurrency limiter, Puppeteer script execution
- **[BrowserOS](https://github.com/anthropics/browseros)** — CDP input dispatch, element coordinate resolution, accessibility tree, cursor detection, console collector
- **[nanobrowser](https://github.com/nicepkg/nanobrowser)** — Navigator+Planner agent loop, DOM element indexing, screenshot feedback, extraction protocol, action schemas, security guardrails, URL firewall, DOM history tracking, error hierarchy

New since the initial release:

- **Fast-to-human captcha policy** — one auto attempt, immediate handoff, cookie persistence, ledger-backed re-prompt avoidance
- **Python vision preprocessor** — cheap-model middleman so the brain never pays image tokens
- **WebSocket feedback bridge** — `captcha_active` / `awaiting_human` / `captcha_done` events with replay-on-connect
- **Webhook-based handoff notification** — plug any messenger bot into `HANDOFF_WEBHOOK_URL`

---

## Roadmap

The top-of-README vision (serverless + chat-app control) is the guiding star. Concrete near-term items:

- **WhatsApp / Telegram / Slack bridges** — first-party messaging front-ends that consume the existing `HANDOFF_WEBHOOK_URL` + WebSocket `awaiting_human` events. The transport is already in place; what's next is the canned bot.
- **RunAgent Cloud managed deployment** — stateful-serverless: cookie jar, handoff ledger, and task-scoped resumption survive cold starts. One click to deploy, pay per active session-minute.
- **Proxy / region stickiness per task** — `cf_clearance` is IP+UA pinned; the cookie jar is only as useful as the proxy that backs it. Pinning proxies per `SUPERBROWSER_TASK_ID` extends jar validity across longer horizons.
- **CDP screencast live view** — upgrade `/session/:id/view` from 2 FPS screenshot polling to a WebSocket CDP screencast with cursor overlay.
- **More vision providers** — native `google-genai` backend for Gemini features the OpenAI-compat endpoint doesn't expose (multi-image batching, structured output schemas).

Contributions welcome on any of these — open an issue first if it's a large change.

---

## Deployment

SuperBrowser is a stateful HTTP server. Deploy it however fits your stack:

- **Self-hosted** — `npm start` on any machine with Chromium
- **Docker** — `docker compose up -d`
- **Kubernetes** — one pod per instance, horizontal scaling; per-task cookie jar + handoff ledger are on disk (mount a PVC if you want them to survive pod restarts)
- **[RunAgent Cloud](https://runagent.cloud)** — the target home: stateful-serverless deployment with managed proxy pool, chat-app bridges, and shared jar storage. Part of the RunAgent super-agent ecosystem (OpenClaw, PicoClaw, ZeroClaw).

---

## Contributing

Issues and PRs welcome. For larger changes (new captcha strategy, new vision provider, new messaging bridge), open an issue first so we can agree on the shape.

Running the test suite:

```bash
npm test                  # vitest — agent executor, DOM, actions
npx tsc --noEmit          # type check
```

Python side:

```bash
source venv/bin/activate
cd nanobot
python -m pytest tests/ -q   # if tests present
```

## License

MIT
