# Security

## Reporting a vulnerability

Email the maintainers (see the repository contact) with details and a
reproduction. Please do not open a public issue for exploitable bugs.

## Trust model

### Engine auth (`TOKEN`)

- With `TOKEN` unset (default), the engine has **no auth** — it trusts anything
  that can reach the port. Only run it that way on loopback / a trusted network.
- With `TOKEN` set, requests need `Authorization: Bearer <TOKEN>` (or
  `?token=`), and the sensitive routes (`/session/:id/script`, `/function`) are
  unlocked. **Loopback callers are exempt** by default
  (`TOKEN_AUTH_LOOPBACK_BYPASS`) so the in-container bridge works without a
  token — set it to `false` to require the token even locally.
- `/session/:id/script` runs arbitrary Puppeteer code. Never expose the engine
  publicly without a token.

### Data at rest — `~/.superbrowser/`

This directory holds **live browser state**, some of it as sensitive as
passwords. Treat the whole directory as a credential store.

| Path | Sensitivity |
|---|---|
| `identities/<domain>.json` | **Full login sessions** — possession = account access |
| `profiles/<domain>/` | T3 Chrome profiles — full logged-in browsers |
| `cookie-jar/<domain>.json` | Bot-protection cookies (cf_clearance, …) |

- Files are written `0600`, directories `0700`. The security boundary is
  filesystem / Docker-volume read access.
- The identity jar is **opt-in** (`SUPERBROWSER_IDENTITY_JAR=1`) and supports
  optional AES-256-GCM encryption at rest (`SUPERBROWSER_IDENTITY_KEY`). See
  [docs/identity.md](docs/identity.md) for the full threat model.
- On a multi-tenant host, do **not** share one `~/.superbrowser` across users —
  run isolated instances (the gateway's `--home`).
- Wipe with `rm -rf ~/.superbrowser` (local) or `docker volume rm sb-superbrowser`.

### Live-view / handoff links

- The live-view UI (`/session/:id/view`) grants **interactive control** of the
  browser to whoever opens the link. Links carry a session-scoped token; treat
  them as sensitive and short-lived.
- To expose a link to a phone/LAN, set `SUPERBROWSER_PUBLIC_HOST` and front the
  engine with **TLS + the engine `TOKEN`** (e.g. a cloudflared/tailscale
  tunnel). The viewer ports are loopback-bound by default. `superbrowser-doctor`
  warns when a handoff webhook is set but `publicHost` is unset or loopback.

### Chat gateway

- Binds to `127.0.0.1` by default. Binding to `0.0.0.0` **requires a token**
  (a validation error otherwise). Front a public bind with TLS.
- Channel allowlists (`allowFrom`) plus the pairing-approval flow gate who may
  command the agent. Unknown senders get a pairing code, not access.
- Approval gates (`browser_request_approval`, `SUPERBROWSER_REQUIRE_APPROVAL=1`)
  put a human yes/no in front of irreversible actions (checkout, payment).

### Network egress (SSRF)

The engine's `validateUrl` blocks navigation to private/loopback/metadata IPs;
outbound fetches from tools are SSRF-checked. This protects the host from an
agent (or a page) coaxing it into hitting internal services.

## Secrets

Prefer environment / `deploy/.env` for API keys on shared or CI hosts. Keys in
`~/.superbrowser/config.json` are acceptable for single-user machines (the file
is `0600`). The console/API never return secret values — only `{set, last4}`.
