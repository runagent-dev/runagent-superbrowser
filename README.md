<div align="center">

<img src="assets/icons/futuristic-runner-search-logo.png" width="160" alt="SuperBrowser" />

# SuperBrowser

**The browser your agent won't get blocked on.**

[Quick start](#quick-start) · [Examples](#examples) · [What it does](#what-it-does) · [Config](#configuration)

</div>

---

Give your agent a real browser. SuperBrowser handles the parts that break LLMs in the wild — captchas, Cloudflare, autocomplete dropdowns, "let me Google that" drift — so your prompt can stay focused on the task.

```python
from runagent_superbrowser import SuperBrowser

sb = SuperBrowser()

# One call. The agent decides whether a lightweight fetch or a full browser
# session is the right tool — you just say what you want, no "please click…".
res = sb.run("find me a black summer dress under $80 on zara.com, size M, ships to Dhaka")
print(res.text)
```

---

## Run it locally — or deploy to serverless

- **Local**: `npm run dev` (starts the engine) + the Python SDK below.
- **Serverless (callable from *every* RunAgent SDK)**: deploy with the `runagent` CLI —

  ```bash
  runagent init my-browser --from-template superbrowser/default   # or: cd deploy
  cp .env.example .env     # set LLM_MODEL + OPENAI_API_KEY (or ANTHROPIC_API_KEY)
  runagent deploy .        # prints an agent_id
  ```

  Then call it from Python/TS/Go/Rust/Dart/C# via
  `RunAgentClient(agent_id, "run", local=False, persistent_memory=True).run(task="…")`.
  On-demand micro-VMs, per-user persistent sessions. See
  [deploy/README.md](deploy/README.md) and
  [docs/sdk.md](docs/sdk.md#deploy-via-the-runagent-cli-callable-from-every-sdk).

---

## Python SDK

`pip install runagent-superbrowser` gives you a one-object SDK. Terse goals in,
structured results out — the heavy prompting (routing rules, anti-fabrication,
the browser tool ladder) **ships inside the package**, so you don't hand-write
"please click… please type…".

```python
from runagent_superbrowser import SuperBrowser

sb = SuperBrowser()
res = sb.run("what's the top story on Hacker News right now?")
print(res.text)        # the answer
print(res.success)     # did it work?
```

### Pick how it browses — or let it decide

`mode` is the intelligence switch:

| `mode` | What runs | Needs the engine? |
|---|---|---|
| `"auto"` *(default)* | the agent decides: lightweight fetch/search **or** a real browser | only if it picks the browser |
| `"fetch"` | read-only: HTTP / stealth fetch / search. Fast, no captcha risk | no |
| `"browser"` | a real headless browser — clicks, forms, logins, bookings | yes |

```python
sb.run("average price of used iPhone 16 Pro on mercari.com", mode="fetch")
sb.run("book a 4-star Sylhet hotel, Sun–Thu, 2 adults", url="https://gozayaan.com", mode="browser")
```

In `auto` mode the result tells you which way it leaned, and why:

```python
res = sb.run("cheapest DAC→BKK flight Apr 30, return May 5")
print(res.classification)   # {'approach': 'browser', 'reason': '…', 'confidence': 0.88}
```

### Typed results

Pass a pydantic model (or `list[Model]`, or a JSON Schema dict) and get parsed
data back in `res.data` — best-effort, never raises:

```python
from pydantic import BaseModel
from runagent_superbrowser import SuperBrowser

class Hotel(BaseModel):
    name: str
    price_usd: float

res = SuperBrowser().run(
    "list 4–5 star hotels in Sylhet with nightly prices",
    url="https://gozayaan.com", mode="browser",
    output_schema=list[Hotel],
)
for h in res.data or []:    # list[Hotel]; None if the model didn't return clean JSON
    print(h.name, h.price_usd)
```

### The browser engine

Browser mode needs the TS engine on `:3100`. Start it yourself (`superbrowser http`),
or let the SDK start and stop it for you:

```python
with SuperBrowser(auto_start_server=True) as sb:   # spawns the engine, tears it down on exit
    res = sb.run("…", mode="browser")
```

### Async + CLI

```python
res = await SuperBrowser().arun("…", mode="fetch")
```

```bash
superbrowser-run "what's trending on github this week" --mode fetch
superbrowser-run "book the cheapest DAC→BKK flight" --mode browser --auto-start-server
```

> Model + API keys come from `~/.nanobot/config.json` (`nanobot onboard`); vision
> and server knobs from env / `.env`. Override per-instance with
> `SuperBrowser(model=…, vision=…, server_url=…, workspace_root=…)`.
> Full guide: [`docs/sdk.md`](docs/sdk.md).

The low-level `register_all_tools(bot)` / raw `/session` HTTP API are still there
for advanced use — see [Examples](#examples).

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
the machine. The bootstrap installer handles it; if you set things up by hand on
a **fresh Ubuntu/Debian VM**, Chrome isn't in the default apt repos, so
`apt install google-chrome-stable` fails until you add Google's repo first:

```bash
# add Google's signing key + apt source, then install Chrome
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
  | sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
  | sudo tee /etc/apt/sources.list.d/google-chrome.list
sudo apt update && sudo apt install -y google-chrome-stable
```

Then point `PUPPETEER_EXECUTABLE_PATH` at the binary:

| OS | Typical Chrome path |
|---|---|
| Ubuntu/Debian | `/usr/bin/google-chrome-stable` |
| macOS | `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` |
| Windows | `C:\Program Files\Google\Chrome\Application\chrome.exe` |

### Or Docker (all-in-one agent server)

One container runs the stealth browser engine **and** the agent orchestrator,
exposed as a local RunAgent agent server on `:8450` — no Node/Python/venv on the
host and no RunAgent account or key.

```bash
cp deploy/.env.example deploy/.env   # set LLM_MODEL + OPENAI_API_KEY (or ANTHROPIC_API_KEY)
docker compose up -d                 # ready when :8450/api/v1/health is healthy
```

Then from Python on the host (no API key):

```python
from runagent_superbrowser import SuperBrowser
sb = SuperBrowser(remote=False, local_agent_url="http://localhost:8450")
print(sb.run("what's the top story on Hacker News right now?").text)
```

The image bakes Node, Chromium, patchright's stealth Chromium, and the Python
bridge. Compose persists `~/.superbrowser` (cookies/profiles) and `~/.nanobot`
(orchestrator state) in named volumes across restarts. See
[docs/sdk.md → Local agent server (Docker)](docs/sdk.md) for the full picture
(in-process vs local-agent vs remote).

### Per-OS notes

| | Install Chrome | Headless | Extra |
|---|---|---|---|
| **Ubuntu / Debian** | add Google's apt repo, then `apt install google-chrome-stable` ([snippet above](#from-packages)) | `HEADLESS=true` works; headful Tier-3 needs Xvfb | `apt install xvfb` + the lib list (the installer does this) |
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
