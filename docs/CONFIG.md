# Configuration

SuperBrowser reads its settings from environment variables. You can set them
however you like — a shell, a `.env` file, or a single **`~/.superbrowser/config.json`**
managed by `superbrowser-config`. All three coexist.

## Precedence

```
process env  >  .env  >  ~/.superbrowser/config.json  >  profile preset  >  code defaults
```

The config file is **projected into unset environment variables** at startup —
it never overrides something you already set in the shell or `.env`. This means
adopting config.json changes nothing about existing setups: with no file, or
with keys you haven't set, behavior is exactly as before.

## `superbrowser-config`

```bash
superbrowser-config init                 # detection wizard → writes a minimal file (0600)
superbrowser-config set engine.port 3200
superbrowser-config get brain.model
superbrowser-config show --effective     # what the engine/SDK actually receive, with provenance
superbrowser-config validate
superbrowser-config import-env [.env]    # migrate an existing .env (prints a diff; leaves .env intact)
superbrowser-config path
```

`show --effective` labels each value's source (`environment` / `config` /
`preset`), so "why is my config.json ignored?" is answerable at a glance — a
shell/`.env` value shadowing it shows as `environment`.

## Machine profiles

`profile` (`local` / `vm` / `docker`) supplies sensible presets so a laptop
config is tiny. Auto-detected when unset (container → docker; desktop session →
local; headless Linux → vm).

| | local (laptop) | vm (Hetzner-style) | docker |
|---|---|---|---|
| Chrome | auto-detected | `/usr/bin/google-chrome-stable` | baked |
| T3 display | real display (no Xvfb) | Xvfb `:99` | baked |
| concurrency | light (3/5/5) | heavy (10/10/20) | heavy |
| publicHost | localhost links | **required** for handoff links | host-forwarded |

A laptop's whole config can be:

```json
{
  "version": 1,
  "profile": "local",
  "brain":  { "provider": "anthropic", "model": "claude-sonnet-5", "apiKey": "sk-ant-…" },
  "vision": { "apiKey": "AIza…" }
}
```

## Schema (top-level sections)

- `engine` — `port`, `url`, `token`, `headless`, `chromePath` (`"auto"` to
  detect), `downloadDir`, `concurrency{…}`, `cors`, `firewall`
- `brain` — `provider`, `model`, `apiKey`, `baseUrl` (the LLM_* contract)
- `vision` — the Gemini preprocessor: `enabled`, `provider`, `model`, `apiKey`
  (**separate key from the brain**), `cacheTtlSec`, …
- `captcha` — `provider`, `apiKey` (2captcha/anticaptcha)
- `antibot` — `cookieJar`, `captchaPolicy`, `maxHumanHandoffs`, `proxyPool`, …
- `t3` — patchright/real-Chrome tier: `chromePath`, `persistProfile`, `headless`,
  `autoXvfb`, `xvfbDisplay`, `viewerPort`, …
- `publicHost` — base URL for live-view / handoff links behind a proxy
- `handoff` — `webhookUrl`, timeouts
- `identities` — login persistence (`enabled`, `ttlDays`, `autosave`,
  `encryptionKey`) — see [identity.md](identity.md)
- `gateway` — chat channels + console — see [gateway.md](gateway.md) (read
  directly by the gateway; not env-projected)

Field names are camelCase. `superbrowser-config validate` checks the schema and
flags profile-specific issues (e.g. `vm` without `publicHost`, a non-loopback
gateway bind without a token).

## Secrets

Keys in config.json are fine for a single-user machine (the file is `0600`, and
`~/.nanobot/config.json` already stores your LLM key the same way). On shared or
CI hosts, prefer env / `deploy/.env` and keep only non-secret knobs in the file.
The console and API always return secrets redacted (`{set, last4}`).

## The engine's env knobs

The TS engine still reads its `PORT`, `HEADLESS`, `TOKEN`, `T3_*`, `VISION_*`,
etc. from the environment — config.json simply fills those in. The full env
reference is `.env.example`. Dead knobs `BROWSER_PROFILE`/`COOKIE_DIR` were
removed; `~/.superbrowser/` is the single data umbrella (see
[data-layout.md](data-layout.md)).
