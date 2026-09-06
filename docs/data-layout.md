# Data layout — `~/.superbrowser/`

Everything SuperBrowser persists on disk lives under one umbrella directory,
`~/.superbrowser/` (plus the nanobot brain's own `~/.nanobot/`). In Docker both
are named volumes so state survives `docker compose up` recreations.

| Path | What | Written by | Lifetime |
|---|---|---|---|
| `config.json` | The unified product config (see [CONFIG.md](CONFIG.md)) | `superbrowser-config`, the console | until you change it |
| `cookie-jar/<domain>.json` | **Bot-protection cookies only** (`cf_clearance`, `__cf_bm`, `datadome`, Akamai `_abck`, …) | TS engine + Python antibot | 7-day hard TTL |
| `identities/<domain>.json` | **Full login session** (all registrable-domain cookies) — "log in once, stay logged in" | TS identity jar | 30-day TTL (configurable) |
| `profiles/<domain>/` | Full T3 Chrome user-data-dir (real logged-in browser profile) | Python T3 (patchright) | until deleted |
| `human-assist/<id>.json` | Human-assist request ledger (captcha/login/otp/approval) | Python human-assist | pruned 24h after terminal |
| `handoff-ledger.json` | 15-min "recently solved" dedupe for the captcha ladder | TS captcha strategies | rolling 15-min window |
| `domain-stats.json` | Per-domain tier learnings (which tier worked where) | TS | until reset |
| `gateway/` | Channels-gateway state (media, active-turn markers) — or `--home` dir | `superbrowser-gateway` | while the gateway runs |
| `workspaces/<role>/` | Orchestrator/worker nanobot workspaces (SOUL.md, memory) | Python bridge | persistent |

Override the root of individual stores with env vars:
`SUPERBROWSER_COOKIE_JAR_PATH`, `T3_PROFILE_ROOT`, `SUPERBROWSER_ASSIST_DIR`,
`SUPERBROWSER_WORKSPACE_ROOT`, `SUPERBROWSER_HANDOFF_LEDGER`.

## What is (and isn't) here anymore

The old `~/.superbrowser-cookies/` and `~/.superbrowser-profile/` directories
(from the removed `COOKIE_DIR` / `BROWSER_PROFILE` knobs) are **not used by any
current code** and have been dropped from `.env.example`. Everything lives
under `~/.superbrowser/` now.

## Wiping state

- One domain: use the agent's `browser_forget_site` tool, the console's
  Sessions screen, or delete `identities/<domain>.json` + `profiles/<domain>/`
  + `cookie-jar/<domain>.json` by hand.
- Everything (local): `rm -rf ~/.superbrowser`
- Everything (Docker): `docker volume rm sb-superbrowser` (the compose volume).

## Security

`identities/` and `profiles/` hold **live logged-in browser sessions** — treat
them like a password vault. Files are written `0600`, the directory `0700`.
Anyone who can read the volume can hijack those sessions. See
[identity.md](identity.md) for the full threat model and optional at-rest
encryption.
