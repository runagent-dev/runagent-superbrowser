<div align="center">

<img src="assets/icons/futuristic-runner-search-logo.png" width="160" alt="SuperBrowser" />

# SuperBrowser

**The browser your agent won't get blocked on.**

[Quick start](#quick-start) · [Examples](#examples) · [What it does](#what-it-does) · [Config](#configuration)

</div>

---

Give your agent a real browser. SuperBrowser handles the parts that break LLMs in the wild — captchas, Cloudflare, autocomplete dropdowns, "let me Google that" drift — so your prompt can stay focused on the task.

```python
from nanobot import Nanobot
from superbrowser_bridge.tools import register_all_tools

bot = Nanobot.from_config(workspace="nanobot/workspace")
register_all_tools(bot)

await bot.run("find me a black summer dress under $80 on zara.com, size M, ships to Dhaka")
```

---

## Drive it from a chat app

Plug SuperBrowser into **WhatsApp, Telegram, Discord, Slack** (also DingTalk, Lark, QQ — the SDKs are already vendored). Type a task in chat, the agent runs it in the cloud, and when it hits a captcha **your phone buzzes with a live-view link** — tap, swipe, the session resumes on the same cookies.

```
You (WhatsApp):  "book me a Khulna hotel under $40/night, check-in Apr 23"

  SuperBrowser:  Searching gozayaan.com...
                 Filtering 4-star, under $40...
                 [hit a captcha] → tap here: https://browser.runagent.cloud/v/abc
                                ↑ you tap once, swipe slider, done

  SuperBrowser:  Found 3 hotels. Top pick: Hotel Castle Salam,
                 $34/night, 4.2★. Want me to book?

You (WhatsApp):  "yes, my card on file"
```

Wire it up in one env var:

```bash
HANDOFF_WEBHOOK_URL=https://your-bot.example.com/webhooks/handoff
```

The webhook receives `{viewUrl, captchaType, pageTitle, screenshot, caption}` — forward that to whichever messenger SDK you're using. WebSocket events (`awaiting_human`, `captcha_active`, `captcha_done`) push updates with snapshot replay so late subscribers see the same state. Cookies persist per task, so the human only solves once per site.

---

## What it does

- **Runs through captchas.** Cloudflare, Akamai, DataDome, PerimeterX, Kasada. Auto-pass on warm profiles, Turnstile token solvers, vision-based slider / jigsaw / rotation solvers, or hand off to a human via a live-view URL.
- **Doesn't get fingerprinted.** Per-domain persistent Chrome profiles. First visit takes the hit, every visit after looks like a returning user.
- **Picks the cheapest engine that works.** httpx for plain pages, Puppeteer for SPAs, curl_cffi for TLS-blocked APIs, undetected Chromium for the hard targets, Wayback as a fallback. One tool call, the router does the rest.
- **Stops LLM failure patterns at the tool layer.** No more `"khulnakhulna, bangladesh"` from a missed autocomplete. No more "let me check Google" mid-task. No more re-typing into a closed dropdown.
- **Keeps your reasoning model cheap.** A dedicated tiny vision model labels screenshots into `[V1]`, `[V2]` boxes. Your expensive LLM never sees raw pixels.
- **Hands off to a human when stuck.** Live-view URL fires through a webhook to WhatsApp / Slack / Telegram. User taps once, session resumes on the same cookies.

---

## Quick start

### Prerequisites

- **Node 20+** and **Python 3.11+**
- **Chrome** (real, not bundled Chromium — fingerprint targets need it)
- **Xvfb** (only if you run headful in a container — required for the hardest CF targets)

### 1. Install Chrome

```bash
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
  | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
  > /etc/apt/sources.list.d/google-chrome.list
sudo apt update && sudo apt install -y google-chrome-stable
# binary lands at /usr/bin/google-chrome-stable — that's what CHROME_PATH points to
```

On a server / container, also grab Xvfb for headful mode:

```bash
sudo apt install -y xvfb
```

### 2. Clone + build the TS server

```bash
git clone https://github.com/runagent-dev/superbrowser.git
cd superbrowser
npm install && npm run build
cp .env.example .env       # then edit keys you care about
npm start                  # runs on :3100
```

That's the whole TS side. No API key needed.

### 3. Python bridge (for captcha-solving, Tier 3, nanobot tools)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
patchright install chromium        # downloads patchright's Chromium
playwright install-deps chromium   # apt deps Chromium needs (libnss3 etc.)
```

### Or just Docker

```bash
docker compose up -d
```

The Docker image bakes Chrome, Xvfb, Node, and Python in. Mount `~/.superbrowser/profiles/` and `~/.superbrowser/cookie-jar/` as volumes if you want state to survive restarts.

---

## Examples

**Screenshot anything.**

```bash
curl -X POST http://localhost:3100/screenshot \
  -d '{"url": "https://example.com"}' \
  -H "Content-Type: application/json" --output shot.jpg
```

**Drive a session from any language.**

```python
import httpx
r = httpx.post("http://localhost:3100/session/create", json={"url": "https://news.ycombinator.com"})
sid = r.json()["sessionId"]
httpx.post(f"http://localhost:3100/session/{sid}/click", json={"index": 1})
```

**Write a Puppeteer script on the live page.**

```python
httpx.post(f"http://localhost:3100/session/{sid}/script", json={
    "code": "await page.type('#q', 'agents'); await page.click('#search'); return page.title();"
})
```

**Let the autonomous agent do it.**

```bash
curl -X POST http://localhost:3100/task \
  -d '{"task": "find trending Python repos this week on GitHub"}' \
  -H "Content-Type: application/json"
```

**Tasks that actually work:**

```python
await bot.run("book a 3-day stay in Khulna on gozayaan.com, check-in April 23, 1 adult, under $40/night")
await bot.run("find the cheapest flight DAC → SIN on March 5, one-way, list top 3 airlines")
await bot.run("get the IRA contribution limit for someone 45yo earning $120k from the Chase calculator")
await bot.run("compare the iPhone 16 Pro on amazon.com vs. apple.com — price, ship date, return policy")
await bot.run("download the latest 10-K filing for NVDA from the SEC EDGAR site")
```

When SuperBrowser hits a captcha it can't auto-solve, your phone buzzes with a link. Tap, swipe, done.

---

## Configuration

Zero config required. The knobs that matter most:

| Variable | What it does |
|---|---|
| `T3_PERSIST_PROFILE=1` | Persistent per-domain Chrome profiles. **Turn this on.** First visit solves the captcha, every visit after looks like a returning user. |
| `HANDOFF_WEBHOOK_URL` | Fires when a human is needed. Point it at your WhatsApp / Slack / Telegram bridge. |
| `CAPTCHA_API_KEY` + `CAPTCHA_PROVIDER` | 2captcha / anticaptcha / nopecha for Turnstile auto-solve. |
| `VISION_API_KEY` + `VISION_MODEL` | Cheap dedicated vision model. Keeps image tokens off your reasoning LLM bill. |
| `PROXY_POOL` + `PROXY_POOL_RESIDENTIAL` | Datacenter + residential pools. Hardened domains auto-promote to residential. |
| `TOKEN` | Bearer auth. Set this for anything not on localhost. |
| `SUPERBROWSER_TASK_ID` | Scope key for the cookie jar. Pass a stable ID for warm starts. |

Full reference: [`.env.example`](.env.example). Deep dive on Tier-3 stealth + persistent profiles: [`STEALTH.md`](STEALTH.md).

---

## Where this is going

[RunAgent Cloud](https://runagent.cloud) — a managed serverless deployment with per-user persistent profiles surviving cold starts, controlled via chat apps. The pieces are already here; the cloud substrate is what's next.

---

## License

MIT.
