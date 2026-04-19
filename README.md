# SuperBrowser

![SuperBrowser logo](assets/icons/futuristic-runner-search-logo.png)

**A headless browser built for AI agents — that doesn't lie, doesn't give up, and knows when to ask a human.**

SuperBrowser lets any AI agent drive a real Chromium browser through simple HTTP APIs. But the part that matters isn't the APIs — it's what sits underneath them. A **tier-transparent anti-bot pipeline** (Puppeteer → undetected Chromium → archive) that auto-escalates when a site blocks you, a **vision + bbox + click** loop the agent reasons over instead of raw pixels, and a set of **hallucination guards** that refuse to let the agent fabricate failures, retype into the same field, or pivot to Google Search when the target gets hard.

You keep your agent. SuperBrowser keeps it honest.

---

## What makes this exceptional

Most headless browsers give you a `page.goto()` and call it a day. Anti-bot? "Use residential proxies." Captchas? "Integrate 2captcha." Vision? "Feed screenshots to GPT-4V." That's not automation — that's a bill of materials.

SuperBrowser owns the hard parts:

**The anti-bot ladder, built from scratch.**
Five tiers in one surface. The agent doesn't pick the tier — it calls `fetch_auto` or `browser_open(tier="auto")` and SuperBrowser reads the per-domain learning, runs the cheapest working tier, and escalates automatically on detected blocks. No commercial unlockers, no framework dependencies. Patterns ported from crawl4ai and crawlee-python, **re-implemented in-repo so you own the pipeline**.

```
Tier 0  Direct HTTP (httpx + Jina Reader)                 $0       0.3s   — naive
Tier 1  Puppeteer + 17-patch stealth + cookie jar         $0.005   3–8s   — CF Managed Challenge, JS SPAs
Tier 2  curl_cffi (Chrome TLS/JA3) + session pool         $0       1s     — TLS gating, moderate CF, header blocks
Tier 3  patchright (undetected Chromium) + stealth        $0       5–15s  — Akamai BM, DataDome, PerimeterX, Kasada
Tier 4  Wayback CDX archive                               $0       1–3s  — stale fallback when live blocked
Tier 5  Human handoff (live view)                         user     —      — anything
```

Seamless escalation: when Tier 1 hits a 403, `browser_escalate` exports cookies + localStorage + URL to a fresh Tier 3 patchright session and the LLM's next `browser_click`, `browser_type`, `browser_screenshot` all work transparently on the new backend. **Same tool surface, either backend.**

**A typed block classifier that actually classifies.**
Every response passes through a detector that names the protection: Akamai reference-numbers, Cloudflare cf_chl challenge forms, PerimeterX `_pxAppId`, DataDome captcha delivery domains, Kasada SDK, Imperva Incapsula, Sucuri firewall. The agent sees `block_class="akamai"` not "something went wrong." Learnings are recorded per domain per tier — `lowest_successful_tier` tells the next run exactly where to start.

**Vision that the brain reasons over, not pays for.**
Gemini is called once per screenshot; the brain (Claude, GPT, whatever) sees `[V1] button "Sign in" (450,200 → 650,260) ← matches intent` — a ranked, intent-relevant, coordinate-stable bbox list. `browser_click_at(vision_index="V1")` resolves the label to the actual interactive element via DOM snapping, not guessed pixels. Vision is **prefetched** in the background after every click/type/scroll/navigate, so the next screenshot returns instantly from cache instead of waiting 3–8 s for a fresh Gemini call.

**Guards that stop the three most destructive LLM failure modes.**

1. **Dead-type refusal.** LLM types "khulna" → dropdown appears → LLM misses it → tries to type "khulna, Bangladesh" → field becomes `khulnakhulnabangladesh`. SuperBrowser catches the second type to the same field within 12 s, rejects it, and returns the live autocomplete suggestions inline with exact click coordinates. No more concatenation garbage.

2. **Evidence-based escalation.** LLM calls `browser_escalate(reason="403 Forbidden")` after a `browser_wait_for` timeout even though no 403 ever happened. SuperBrowser validates: if `network_blocked=False`, no observed 4xx/5xx, and no captcha flagged by vision, the escalation is refused with a message telling the agent to take a screenshot or extend the wait timeout. **The LLM can't fabricate a failure the tools didn't see.**

3. **Search-escape blocking.** When the target site is auto-pinned (gozayaan.com, louisvuitton.com, whatever), SuperBrowser blocks any navigation to `google.com/search`, `/images`, `/maps` — the "let me check Google" detour the LLM uses when a site is annoying. OAuth and CDN traffic on Google stays allowed; search does not. The agent stays on the target domain and solves the problem there.

**Human-in-the-loop that survives cold starts.**
A live-view URL the human opens in their own browser, 2 FPS screencast with click forwarding, works across both Tier 1 (TS server on :3100) and Tier 3 (Python aiohttp on :3101). Bot-protection cookies (`cf_clearance`, `_abck`, `ak_bmsc`, `datadome`) persist to a shared jar scoped by task + domain — when the human clears the challenge, the next task doesn't re-prompt them. Webhook hook for WhatsApp / Slack / Telegram bridges. Handoff ledger prevents double-prompting the same user on the same domain within 15 minutes.

**Navigate that doesn't trip sensors.**
Cold `page.goto()` with no Referer, no mouse motion, and two identical GETs within 2 s is a textbook bot signature. SuperBrowser's navigate sets a proper Referer (previous URL as the implicit click-through), adds small humanizing jitter, skips warmup when the target IS the homepage (was firing two identical navs in a row), and pins a real Chrome UA on the HTTP layer (patchright only patched JS-side `navigator.userAgent` — the actual header still leaked `HeadlessChrome`). After navigation, a challenge-wait loop gives Cloudflare "Just a moment" and DataDome auto-verify interstitials a chance to clear before we return.

---

## Where this is going

The near-term target is the **serverless, stateful browser** — deployed on [RunAgent Cloud](https://runagent.cloud) and controlled from whichever chat app the user already lives in. No new UI to learn. You send a message, the browser does the work, and when it needs you (captcha, OTP, a decision), it messages you back with a link you tap.

```
  You on WhatsApp / Telegram / Slack
          │
          │   "find me a black dress like this photo, size M, ships to Dhaka"
          ▼
  RunAgent Cloud  (stateful-serverless — cookies, memory, handoff ledger,
          │       fingerprint continuity survive cold starts)
          │
          ▼
  SuperBrowser  →  any site, auto-escalating through the tier ladder
          │
          │   hits a captcha → pushes a "tap to solve" link back to WhatsApp
          │   you tap once → session resumes on the same cookies
          ▼
  Shortlist delivered as a WhatsApp message with images + links
```

Every piece of this already exists: tier-aware learnings persist per domain, the cookie jar is keyed on `SUPERBROWSER_TASK_ID + domain`, the handoff ledger is cross-session, `HANDOFF_WEBHOOK_URL` lets any messenger bridge receive handoff events, and the WebSocket pushes `awaiting_human` with snapshot replay for late subscribers. What's next is the managed cloud deployment and first-party messaging bridges.

---

## Quick Start

```bash
git clone https://github.com/runagent-dev/superbrowser.git
cd superbrowser
npm install && npm run build
cp .env.example .env     # optional — defaults work for local dev
npm start                # TS server on :3100
```

For the Python tier + vision loop (nanobot integration):

```bash
cd nanobot
pip install -e .
patchright install chromium     # one-time browser install
```

```bash
# Take a screenshot
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

The technical reference (every endpoint, every config env var, every tool schema) will live in a separate docs site. This README is the pitch; that's the manual.

---

## Using it with an AI agent

Four ways in; pick whichever fits.

```python
# 1. HTTP APIs — any language, any framework
httpx.post("http://localhost:3100/session/create", json={"url": "https://example.com"})
httpx.post(f"http://localhost:3100/session/{sid}/click", json={"index": 1})
```

```python
# 2. Full Puppeteer script — one call, real page object
httpx.post(f"http://localhost:3100/session/{sid}/script",
    json={"code": "await page.type('#q','...'); await page.click('#go'); return page.title();"})
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
# 4. Autonomous agent built in — fire and forget
curl -X POST http://localhost:3100/task -d '{"task": "find trending repos on GitHub"}'
```

Real-time events on `ws://localhost:3100/ws/session/:id` — subscribe from a chat-app bridge and you'll receive `awaiting_human` / `captcha_active` / `captcha_done` with snapshot replay.

---

## Architecture at a glance

SuperBrowser is two processes that share a session ID namespace:

```
┌──────────────────────────────────────────────────────────────────┐
│  TS server (Node) — port 3100                                    │
│   • Puppeteer engine + 17-patch stealth                          │
│   • Per-session HTTP API (click/type/screenshot/...)             │
│   • Tier 1 interactive sessions                                  │
│   • Live-view UI for Tier 1 human handoff                        │
└──────────────────────────────────────────────────────────────────┘
                             │
                             │  shared cookie jar on disk
                             │
┌──────────────────────────────────────────────────────────────────┐
│  Python bridge (nanobot/superbrowser_bridge) — worker process    │
│   • antibot/ — tier-aware routing, block classifier, proxy tiers │
│   • interactive_session.py — Tier 3 patchright session manager   │
│   • HTTP intercept in session_tools → routes t3-* session IDs    │
│   • t3_viewer on :3101 — live view for Tier 3 human handoff      │
│   • vision_agent — Gemini bbox labeling + prefetch + cache       │
│   • worker_hook — dead-type guard, evidence-based escalation,    │
│                    auto-escalation on block, domain pinning      │
└──────────────────────────────────────────────────────────────────┘
```

Session IDs: `session-<uuid>` = Tier 1 (TS owns it). `t3-session-<uuid>` = Tier 3 (Python owns it). Every `browser_*` tool transparently routes based on prefix — the agent calls the same tool either way.

---

## Running it in production

SuperBrowser is a stateful HTTP server. Deploy how it fits your stack:

- **Self-hosted** — `npm start` on any machine with Chromium, plus `python -m nanobot` for the Python tier.
- **Docker** — `docker compose up -d`. Mount `~/.superbrowser/cookie-jar/` as a volume if you want the jar to survive restarts.
- **Kubernetes** — one pod per instance; mount a PVC for the cookie jar and handoff ledger.
- **[RunAgent Cloud](https://runagent.cloud)** — the target home. Stateful-serverless with managed proxy pool, chat-app bridges, shared jar storage. Part of the RunAgent super-agent family (OpenClaw, PicoClaw, ZeroClaw).

---

## The short list of things nobody else does

- **One tool surface, two backends.** `browser_click_at(vision_index="V3")` works on Puppeteer and on undetected Chromium, transparently. Mid-task escalation between tiers preserves cookies + URL + localStorage.
- **Vision prefetched after every mutating action.** Screenshots return instantly with bboxes already labeled — no 8-second Gemini wait in the tool's critical path.
- **Hallucination guards at the tool layer.** The agent can't fabricate a 403, can't retype into a field with an open dropdown, can't pivot to Google Search mid-task.
- **Typed block classification + tier-aware learnings.** The system remembers that gozayaan.com needs Tier 3, louisvuitton.com needs residential proxies, browser-use.github.io needs canvas CAPTCHA OCR — per-domain, per-tier, per-outcome.
- **Human handoff that survives cold starts.** Cookies persist, ledger prevents double-prompting, webhook lets any messenger bot receive the "tap to solve" link.

---

## Contributing

Issues and PRs welcome. For substantial changes (new tier, new block classifier pattern, new messaging bridge, new captcha strategy), open an issue first so we can agree on the shape.

```bash
npm test                  # TS side — agent executor, DOM, actions
npx tsc --noEmit          # type check

cd nanobot && source venv/bin/activate
python -m pytest tests/ -q
```

Technical documentation (full endpoint reference, every env var, every tool schema, architectural deep-dives) will live at a separate docs site once the surface stabilizes.

---

## License

MIT.
