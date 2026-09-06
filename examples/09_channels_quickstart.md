# Channels quickstart — talk to SuperBrowser from WhatsApp

Goal: send a task from your phone and get the answer (plus a screenshot) back.

## 0. Prerequisites

```bash
pip install "runagent-superbrowser[gateway]"   # WhatsApp/Discord SDKs (Telegram is in the base install)
```

An engine must be running for browser tasks: `npm run dev` (from the repo) or
the Docker container, reachable at `http://127.0.0.1:3100`.

## 1. Configure the brain + vision

```bash
superbrowser-config init      # pick a profile, set your LLM key + the Gemini VISION_API_KEY
```

## 2. Enable WhatsApp

```bash
superbrowser-gateway setup    # answer "yes" to WhatsApp; optionally list allowed numbers
```

`allowFrom` is the phone number(s) permitted to command the agent, **digits
only, no `+`** (e.g. `15551234567`). Leave it empty to use the pairing-code
flow (an unknown sender gets a code; you approve it in chat with
`/pairing approve <CODE>` or in the console).

## 3. Link your WhatsApp

```bash
superbrowser-gateway login whatsapp
```

A QR code prints in the terminal — open WhatsApp → **Linked Devices** → **Link a
Device**, and scan it. (The web console at `http://127.0.0.1:8460` shows the
same QR under **Onboarding → WhatsApp** if you prefer scanning from the screen.)

## 4. Run the gateway

```bash
superbrowser-gateway
```

Now message the linked number from an allowed phone:

> what's the top story on Hacker News right now?

You'll get a short answer. For browser tasks, the agent replies with a
screenshot of the result. Ask **"show me"** (or send `/peek`) any time to get
the current browser view.

## Login walls & captchas

If a task needs you to log in (or solve a captcha), you get a **link in the
chat**. Open it, do the thing, and the agent detects completion and resumes.
Logins are remembered (see [docs/identity.md](../docs/identity.md)), so next
time it starts already signed in.

For links to open from your phone when the gateway runs on a remote VM, set
`publicHost` (or `SUPERBROWSER_PUBLIC_HOST`) to a URL that reaches the engine,
behind a tunnel like cloudflared or tailscale — the viewer ports are
loopback-bound by default.

## Telegram / Discord

Same flow without the QR step: get a bot token (Telegram @BotFather; Discord
Developer Portal with **MESSAGE CONTENT INTENT** enabled), then
`superbrowser-gateway setup` and paste the token. Allowlists are user IDs.

## More than one WhatsApp number

One gateway process per number, each with its own data dir and console port:

```bash
superbrowser-gateway --home ~/.sbgw/acct1 --console-port 8460
superbrowser-gateway --home ~/.sbgw/acct2 --console-port 8461
```
