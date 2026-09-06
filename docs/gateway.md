# Chat gateway — WhatsApp / Telegram / Discord + web console

The gateway lets people command SuperBrowser from a messaging app and get
answers (with screenshots) back. It runs the **existing** orchestrator on a
message bus — each chat is a persistent conversation — so follow-ups, `/stop`,
and progress updates work out of the box. It is a **separate, opt-in package**
(`superbrowser_gateway`) that depends on the current system without changing
it; the SDK, `npm run dev`, and Docker all behave identically whether or not
the gateway runs.

## Install & run

```bash
pip install "runagent-superbrowser[gateway]"   # Telegram works on the base install
superbrowser-config init                        # brain LLM key + Gemini VISION key
superbrowser-gateway setup                       # enable channels
superbrowser-gateway login whatsapp              # scan the QR (WhatsApp only)
superbrowser-gateway                             # run; console at http://127.0.0.1:8460
```

A browser engine (`npm run dev` or Docker) must be reachable at
`http://127.0.0.1:3100` for browser tasks.

See [examples/09_channels_quickstart.md](../examples/09_channels_quickstart.md)
for the end-to-end WhatsApp walkthrough.

## What the user can do in chat

- Ask a task in plain language → get the answer, plus a screenshot for browser
  tasks.
- **"show me"** / `/peek` → the current browser view.
- `/stop` → cancel the running task (built into nanobot).
- `/pairing approve <CODE>` → approve a new sender (or use the console).
- Login walls / captchas → a link arrives in the chat; solve it, the agent
  resumes automatically and remembers the login.

## Configuration

Channel enablement/tokens/allowlists live in nanobot's config
(`~/.nanobot/config.json`, `channels` section — canonical **camelCase**); the
gateway merges them there from your `~/.superbrowser/config.json` `gateway`
section on startup. You normally edit them through `superbrowser-gateway setup`
or the console, not by hand.

```jsonc
// ~/.superbrowser/config.json
{
  "gateway": {
    "enabled": true,
    "port": 8460,
    "owners": { "whatsapp": ["15550000000"] },   // notified of pairings / handoff fallbacks
    "channels": {
      "whatsapp": { "enabled": true, "allowFrom": ["15551234567"] },   // digits only, no +
      "telegram": { "enabled": false, "token": "", "allowFrom": [] },  // user IDs
      "discord":  { "enabled": false, "token": "", "allowFrom": [] }   // user IDs
    },
    "progress":    { "minIntervalS": 30, "keepaliveS": 90 },
    "screenshots": { "attachFinal": true, "maxSide": 1600, "jpegQuality": 80 }
  }
}
```

Allowlist precedence (from nanobot): `"*"` (open) > exact ID match > pairing
approval > deny. An unknown DM sender is auto-issued a pairing code.

## Multiple WhatsApp numbers

nanobot's WhatsApp is one account per process, so run one gateway per number,
each isolated with `--home` (its own config, WhatsApp session DB, pairing
store, workspaces) and its own console port:

```bash
superbrowser-gateway --home ~/.sbgw/acct1 --console-port 8460
superbrowser-gateway --home ~/.sbgw/acct2 --console-port 8461
```

A systemd template (`superbrowser-gateway@.service`) can template the `--home`
per instance.

## Docker

The gateway is off by default in the image. To enable it:

```bash
docker compose build --build-arg GATEWAY_EXTRAS=gateway   # bake WhatsApp/Discord SDKs
# uncomment GATEWAY_ENABLED=1 and the 8460 port mapping in docker-compose.yml
docker compose up -d
docker compose exec superbrowser superbrowser-gateway setup   # configure channels
```

The console/config persist on the `sb-superbrowser` volume.

## Human-handoff links behind a firewall

The engine's live-view ports (`:3100`, `:3101`) are loopback-bound. For a link
to open from a phone when the gateway runs on a remote VM, set `publicHost`
(or `SUPERBROWSER_PUBLIC_HOST`) to a URL that reaches the engine — front it with
a TLS tunnel (cloudflared, tailscale funnel). `superbrowser-doctor` warns when a
handoff webhook is configured but `publicHost` is unset or loopback.

## How it stays out of the main system's way

- New package `superbrowser_gateway/` — imports only public seams of
  `runagent_superbrowser` / `superbrowser_bridge` and nanobot.
- Two message buses (channel edge ↔ orchestrator) with a thin middleware
  between them; the orchestrator is the unchanged `build_orchestrator()`.
- The orchestrator workspace is isolated under the gateway data dir, so its
  chat-framing `AGENTS.md` never leaks into plain SDK runs.
- Everything the engine needs is additive and default-off; with the gateway
  not running, nothing about the current flows changes.
