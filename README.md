# SuperBrowser

![SuperBrowser logo](assets/icons/futuristic-runner-search-logo.png)

A headless browser for AI agents. HTTP APIs on top of Puppeteer and undetected Chromium, a shared session-ID namespace, a vision + bbox layer for click-by-label, and a handful of guards at the tool layer that prevent common LLM failure patterns.

---

## Overview

SuperBrowser is two cooperating processes:

- A **TypeScript server** (port 3100) that runs Puppeteer + stealth and exposes per-session HTTP endpoints — navigate, click, type, screenshot, etc.
- A **Python bridge** (`nanobot/superbrowser_bridge`) that adds a second backend — an in-process patchright (undetected Chromium) session manager on port 3101 — plus a tiered anti-bot pipeline, the vision agent, and the tool-layer guards.

Session IDs route transparently: `session-<uuid>` → Tier 1 (TS/Puppeteer), `t3-session-<uuid>` → Tier 3 (Python/patchright). The agent calls the same `browser_click`, `browser_type`, `browser_screenshot` regardless of backend.

---

## What's in the box

**A tiered anti-bot pipeline.** Five tiers behind one tool surface. The agent calls `fetch_auto` or `browser_open(tier="auto")` and the router reads per-domain learnings, runs the cheapest tier that has worked before, and escalates on detected blocks.

```
Tier 0  httpx + Jina Reader                                ~0.3s   naive pages
Tier 1  Puppeteer + stealth plugins + cookie jar           ~3–8s   Cloudflare Managed Challenge, JS SPAs
Tier 2  curl_cffi (Chrome TLS impersonation)               ~1s     TLS fingerprint + header blocks
Tier 3  patchright (undetected Chromium) + stealth         ~5–15s  Akamai BM, DataDome, PerimeterX, Kasada
Tier 4  Wayback CDX archive                                ~1–3s   stale fallback when live is blocked
Tier 5  Human handoff via live-view URL                    —       anything
```

Mechanisms for detection + session rotation + proxy tiering + header generation were ported from [crawl4ai](https://github.com/unclecode/crawl4ai) and [crawlee-python](https://github.com/apify/crawlee-python), reimplemented in-repo so there are no framework dependencies to track.

**Block classification.** Each response is scored against a regex catalog for Akamai reference-numbers, Cloudflare `cf_chl_`, PerimeterX `_pxAppId`, DataDome captcha-delivery domains, Kasada SDK markers, Imperva/Incapsula, Sucuri. Outcomes are recorded per domain per tier so `choose_starting_tier(domain)` returns the lowest tier that has succeeded there.

**Tier-transparent escalation.** `browser_escalate` exports cookies, URL, localStorage, sessionStorage from a Tier 1 session, closes it, and opens a Tier 3 session with that state pre-loaded. Subsequent tool calls route to the new backend automatically via the shared session-ID prefix convention.

**Vision + bbox loop.** A dedicated Gemini call labels each screenshot with stable `[V1], [V2], ...` bboxes ranked by (intent-relevant, clickable, confidence). `browser_click_at(vision_index="V3")` resolves the label to a DOM element via point-in-bbox snapping. Vision is prefetched in the background after every mutating action (click/type/scroll/navigate) so the next screenshot call typically hits the cache instead of waiting for a fresh Gemini response.

**Tool-layer guards.** These refuse or warn at the tool level rather than relying on prompting:

- `browser_type` tracks the last type index + text. A second type to the same field within 12s whose text is a superset of the previous (e.g. `"khulna"` → `"khulna, bangladesh"`) is rejected to prevent the field from becoming `"khulnakhulna, bangladesh"` when the agent misses an autocomplete dropdown. After every successful type, a JS probe enumerates visible `[role=listbox] [role=option]` / MUI Autocomplete / generic `.suggestions li` elements and surfaces them inline with click coordinates.
- `browser_escalate` validates that the session is actually blocked (`network_blocked=True`, last observed status ≥400 excluding 404, or vision-flagged captcha) before migrating. Reduces the pattern where a `browser_wait_for` timeout gets recast as "HTTP 403" and triggers a tier migration for no reason. `force=true` bypasses the check.
- `browser_navigate` rejects navigation outside the task-pinned domain, including `google.com/search`, `/images`, `/maps`, or any google.com URL with a `q=` query param — blocks the "let me check Google" pivot. OAuth (`accounts.google.com`) and CDN (`googleapis.com`, `gstatic.com`) URLs on the same hosts remain allowed.

**Navigate humanization.** The Tier 3 navigate path sets Referer to the previous URL when one exists, adds 200–800ms of pre-nav mouse motion + jitter, skips the homepage warmup hop when the target URL already IS the homepage (was firing two identical navigations in 2 seconds), pins a real Chrome User-Agent on the HTTP layer (patchright's stealth only patches `navigator.userAgent` in JS; the HTTP header still leaked `HeadlessChrome` by default), and polls for clearance cookies (`cf_clearance`, `_abck`, `ak_bmsc`, `datadome`) for up to 12s after navigation to let self-clearing challenges finish before returning.

**Human handoff.** Live-view URLs on port 3100 (Tier 1) and port 3101 (Tier 3). Both serve a 2-FPS screencast with click forwarding. Bot-protection cookies (`cf_clearance`, `__cf_bm`, `datadome`, `_abck`, `ak_bmsc`, `bm_sv`, `bm_sz`, Imperva session, PerimeterX) persist to `~/.superbrowser/cookie-jar/<host>.json` scoped by `SUPERBROWSER_TASK_ID`. A handoff ledger tracks recent successful solves per (user, domain) so the same person isn't asked twice within 15 minutes. `HANDOFF_WEBHOOK_URL` lets any messenger bridge (WhatsApp, Slack, Telegram) receive the live-view URL when a handoff fires. WebSocket pushes `awaiting_human` / `captcha_active` / `captcha_done` events with snapshot replay for late subscribers.

---

## Quick start

```bash
git clone https://github.com/runagent-dev/superbrowser.git
cd superbrowser
npm install && npm run build
cp .env.example .env       # optional — defaults work locally
npm start                  # TS server on :3100
```

For the Python bridge and Tier 3:

```bash
cd nanobot
pip install -e .
patchright install chromium
```

```bash
# One-shot screenshot
curl -X POST http://localhost:3100/screenshot \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}' --output shot.jpg

# Open a session
curl -X POST http://localhost:3100/session/create \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.google.com"}'
```

Or with Docker:

```bash
docker compose up -d
```

Full endpoint reference, env var list, and tool schemas will live in a separate docs site as the surface stabilizes.

---

## Usage

Four entry points:

```python
# 1. HTTP — any language
httpx.post("http://localhost:3100/session/create", json={"url": "https://example.com"})
httpx.post(f"http://localhost:3100/session/{sid}/click", json={"index": 1})
```

```python
# 2. Full Puppeteer script — the real page object
httpx.post(f"http://localhost:3100/session/{sid}/script", json={
    "code": "await page.type('#q', '...'); await page.click('#go'); return page.title();"
})
```

```python
# 3. Nanobot integration — the full stack (tier ladder, vision, guards, handoff)
from nanobot import Nanobot
from superbrowser_bridge.tools import register_all_tools

bot = Nanobot.from_config(workspace="nanobot/workspace")
register_all_tools(bot)
await bot.run("find 4-star hotels in Khulna on gozayaan.com, check-in April 23")
```

```bash
# 4. Built-in autonomous agent
curl -X POST http://localhost:3100/task \
  -d '{"task": "find trending repos on GitHub"}'
```

WebSocket at `ws://localhost:3100/ws/session/:id` pushes feedback events (`awaiting_human`, `captcha_active`, `captcha_done`) with snapshot replay — useful for chat-app bridges.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  TS server (Node)  —  :3100                                      │
│   Puppeteer engine + stealth patches                             │
│   Per-session HTTP: click, type, screenshot, navigate, ...       │
│   Tier 1 interactive sessions                                    │
│   Live-view UI for Tier 1 human handoff                          │
└──────────────────────────────────────────────────────────────────┘
                              │
                              │  shared cookie jar on disk
                              │  (SUPERBROWSER_TASK_ID + hostname)
                              │
┌──────────────────────────────────────────────────────────────────┐
│  Python bridge (nanobot/superbrowser_bridge) — worker process    │
│   antibot/              tier routing, block classifier, proxies  │
│   interactive_session.py  Tier 3 patchright session manager      │
│   session_tools.py      HTTP intercept routing t3-* session IDs  │
│   t3_viewer.py          aiohttp live viewer on :3101             │
│   vision_agent/         Gemini bbox labeling + cache + prefetch  │
│   worker_hook.py        dead-type, domain pin, escalate guard,   │
│                          auto-escalation on block                │
└──────────────────────────────────────────────────────────────────┘
```

The TS server and Python bridge coordinate only through the shared cookie jar on disk and the session-ID prefix convention (`session-*` vs `t3-session-*`). Each owns its own browser processes; neither knows about the other's internals.

---

## Direction

The medium-term target is a serverless deployment on [RunAgent Cloud](https://runagent.cloud) controlled via chat-app bridges (WhatsApp, Telegram, Slack). The underlying pieces — task-scoped cookie jar, cross-session handoff ledger, `HANDOFF_WEBHOOK_URL` hook, WebSocket handoff events with replay — are already in place. What's remaining is the managed cloud substrate and the first-party messenger bridges.

```
  User on a chat app
      │  "find me a black dress like this photo, size M, ships to Dhaka"
      ▼
  RunAgent Cloud  (cookies + memory + handoff ledger survive cold starts)
      │
      ▼
  SuperBrowser session
      │
      │  hits a captcha → webhook pushes a live-view link back to the chat
      │  user taps once → session resumes on the same cookies
      ▼
  Shortlist delivered back to the chat
```

---

## Running it

- **Self-hosted.** `npm start` for the TS server plus a Python worker for the bridge + Tier 3.
- **Docker.** `docker compose up -d`. Mount `~/.superbrowser/cookie-jar/` as a volume if you want the jar to survive restarts.
- **Kubernetes.** One pod per instance; PVC for the jar and handoff ledger.
- **[RunAgent Cloud](https://runagent.cloud).** Managed stateful-serverless deployment. Part of the RunAgent family (OpenClaw, PicoClaw, ZeroClaw).

---

## Contributing

Issues and PRs welcome. For substantive changes (new tier, new block-classifier pattern, new captcha strategy, new messenger bridge), open an issue first so the shape is agreed before the PR.

```bash
npm test                  # TS — agent executor, DOM, actions
npx tsc --noEmit          # type check

cd nanobot && source venv/bin/activate
python -m pytest tests/ -q
```

A full technical reference (endpoints, env vars, tool schemas, architectural deep-dives) will live at a separate docs site.

---

## License

MIT.
