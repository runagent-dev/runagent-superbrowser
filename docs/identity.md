# Identity persistence — "log in once, stay logged in"

Some sites need a real human login (bank, retailer, dashboard). SuperBrowser
handles this in two moves:

1. **Login handoff** — when a login wall is hit, the agent calls
   `browser_login_handoff`. The user gets a live-view link in their chat
   (WhatsApp/Telegram/Discord, via the gateway) or the terminal banner, logs in
   themselves, and the agent auto-detects completion and resumes.
2. **Identity capture** — on success the full cookie set for that domain is
   saved to the **identity jar**, so the next task on that site starts already
   signed in and skips the login entirely.

## Enabling it

The identity jar is opt-in:

```bash
SUPERBROWSER_IDENTITY_JAR=1
# optional:
SUPERBROWSER_IDENTITY_TTL_DAYS=30       # how long a saved login is reused
SUPERBROWSER_IDENTITY_AUTOSAVE=1        # keep an EXISTING identity fresh post-nav
SUPERBROWSER_IDENTITY_KEY=<32-byte hex> # AES-256-GCM at-rest encryption
```

or in `~/.superbrowser/config.json`:

```json
{ "identities": { "enabled": true, "ttlDays": 30, "autosave": true } }
```

Tier note: **T1** sessions use the identity jar. **T3** (real Chrome) already
persists logins in its per-domain profile under `~/.superbrowser/profiles/`, so
no jar entry is needed there — `browser_forget_site` clears both.

## Tools the agent has

- `browser_login_handoff(session_id)` — hand the browser to the human to log
  in; saves the identity on success.
- `browser_remember_site(session_id)` — explicitly persist the current login.
- `browser_forget_site(domain)` — delete the saved identity, the T3 profile,
  and the bot-protection jar entry for a domain.

Stale logins self-heal: if an identity is loaded but the site still shows a
login wall, the worker is told the identity is stale and re-runs the login
handoff, which overwrites it.

## Endpoints (engine, port 3100)

| Method + path | Purpose |
|---|---|
| `POST /session/:id/identity/save` `{url?}` | Save the current domain's identity → `{saved, domain, cookieCount}` |
| `GET /identity` | List saved identities |
| `DELETE /identity/:domain` | Forget one domain |

All behind the engine's normal `TOKEN` auth (loopback-exempt).

## Threat model

The identity jar stores **live session cookies** — possession of the file is
possession of the login. This is the **same exposure class** as the T3 Chrome
profiles SuperBrowser already stores unencrypted in the same directory; the
identity jar does not add a new one.

- Files are `0600`, the directory `0700`. The boundary is filesystem/volume
  read access.
- On a shared or exposed host, set `SUPERBROWSER_IDENTITY_KEY` (a 32-byte hex
  key) to encrypt the cookie payload with AES-256-GCM. Key management is the
  operator's job (env var, secret store) — lose the key and saved identities
  are unreadable (the agent just re-runs the login handoff).
- Identity cookies are never included in webhook payloads or logs — only counts
  are logged.
- In Docker, the jar lives on the `sb-superbrowser` volume; back it up or wipe
  it like any credential store (`docker volume rm sb-superbrowser`).

Do not enable the identity jar on a multi-tenant host where different users
share one `~/.superbrowser` — run separate instances (`--home`) instead.
