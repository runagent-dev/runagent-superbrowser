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

## Install

> **Run it locally.** Datacenter IPs (Hetzner, AWS, DigitalOcean…) get blocked
> by a lot of sites. On your own machine — macOS, Windows, or Ubuntu — you look
> like a normal visitor. The one-liner below gets you there on any of them.

### Fastest — one-command bootstrap

**macOS / Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/runagent-dev/runagent-superbrowser/main/scripts/install.sh | bash
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/runagent-dev/runagent-superbrowser/main/scripts/install.ps1 | iex
```

It clones the repo, installs Google Chrome (+ Xvfb and the headless system libs
on Linux), sets up a Python venv and the patchright Chromium, builds the TS
engine, and writes a `.env`. The only things it won't silently install are the
**Node 20+ / Python 3.11+ runtimes** — it detects them and prints the right
command for your OS so it never clobbers nvm/pyenv. Pass `--check` to dry-run,
`--yes` for non-interactive (`-Check` / `-Yes` on PowerShell).

Then:

```bash
superbrowser-doctor     # verify Chrome, build, env, server
superbrowser            # start the engine on :3100  (alias: npm start)
```

### From packages

The two halves publish separately — the browser engine to npm, the agent bridge
to PyPI:

```bash
# TS browser engine (HTTP + MCP server):
npm install -g runagent-superbrowser

# Python agent bridge (nanobot tools, captcha solving, Tier-3):
pip install runagent-superbrowser
patchright install chromium          # download the stealth Chromium (pip can't)
playwright install-deps chromium     # Linux only: the apt libs Chromium needs
superbrowser-doctor                  # check Chrome, build, env
```

`puppeteer-core` does **not** bundle a browser — you need real Google Chrome on
the machine. The installer handles it; if you set things up by hand, point
`PUPPETEER_EXECUTABLE_PATH` at the binary:

| OS | Typical Chrome path |
|---|---|
| Ubuntu/Debian | `/usr/bin/google-chrome-stable` |
| macOS | `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` |
| Windows | `C:\Program Files\Google\Chrome\Application\chrome.exe` |

### Or Docker

```bash
docker compose up -d
```

The image bakes Chrome, Xvfb, Node, and Python in. Mount `~/.superbrowser/profiles/`
and `~/.superbrowser/cookie-jar/` as volumes if you want state to survive restarts.

### Per-OS notes

| | Install Chrome | Headless | Extra |
|---|---|---|---|
| **Ubuntu / Debian** | `apt install google-chrome-stable` | `HEADLESS=true` works; headful Tier-3 needs Xvfb | `apt install xvfb` + the lib list (the installer does this) |
| **macOS** | `brew install --cask google-chrome` | headful, no Xvfb | — |
| **Windows** | `winget install Google.Chrome` | headful (`HEADLESS=false`) | no Xvfb/apt needed |

### From source (contributors)

```bash
git clone https://github.com/runagent-dev/runagent-superbrowser.git
cd runagent-superbrowser
npm install && npm run build            # TS engine
cp .env.example .env                    # then edit the keys you care about
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt         # Python bridge (pinned dev lockfile)
patchright install chromium
playwright install-deps chromium        # Linux only
npm start                               # engine on :3100 — no API key needed
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the dev + release workflow.

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
