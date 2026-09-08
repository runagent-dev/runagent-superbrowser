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

## Research switches (evaluation harness only)

The experiments under `eval/` (see `eval/PROTOCOL.md`) toggle individual mechanisms through env vars.
Every switch reproduces production behaviour when unset, is read per run by the harness, and is
recorded in the run's record. None of them belong in a production `config.json`.

| Env | Values (default first) | Effect |
|---|---|---|
| `SUPERBROWSER_MEMORY_POLICY` | `ledger` \| `full` \| `fifo` \| `summary` \| `ledger_noevict` | Memory-retention policy of the MemoryHook. `ledger` is the six-phase eviction loop + injected Ledger (byte-identical to before). `full` keeps everything, `fifo` keeps the last K turns, `summary` keeps the last K turns plus one regenerated LLM summary of older turns (same host model, booked as usage role `compressor`), `ledger_noevict` injects the Ledger without evicting. |
| `SUPERBROWSER_MEMORY_RECENT_K` | `5` | Verbatim recent window in assistant-anchored turns (also Phase 6's `keep_last_turns`). |
| `SUPERBROWSER_MEMORY_KEEP_SCREENSHOTS` | `2` \| `all` | Screenshots kept verbatim in the live context. |
| `SUPERBROWSER_MEMORY_BUDGET_TOKENS` | unset | History budget: caps the summary and, for `ledger`, shrinks the rendered Ledger (facts → dead-ends → checkpoints → episodic) to fit. |
| `ABLATE_DEAD_END_MEMORY` | `0` \| `1` | `1`: failures are not recorded as dead-ends, no `DEAD_ENDS` sections, no `[DEAD_ENDS_HERE]` injections, no cross-task dead targets. |
| `ABLATE_VISION_REUSE` | `0` \| `1` | `1`: no background vision prefetch, vision cache TTL 0, vision epoch expires after every mutating turn, no `[CACHED VISION]` piggyback (`FRESH_VISION_SECONDS=0`). |
| `FRESH_VISION_SECONDS` | `10` | Age limit for piggybacking the last vision response onto mutating-tool results (previously hardcoded). |
| `ABLATE_CLICK_LADDER` | `0` \| `1` | `1`: no js/keyboard escalation after a silent primary click (`CLICK_LADDER_AUTO=0`). The TypeScript selector cascade is gated separately by `SUPERBROWSER_CLICK_TIERS=tier1`. |
| `SUPERBROWSER_SNAP_STRATEGY` | `chevron` \| `center` \| `dom_alt` | Sub-element resolver used by the bbox snapper (TypeScript server and the T3 mirror; the server reads it at startup). `center` = naive area snapper, `dom_alt` = DOM-name-first without chevron heuristics. |
| `SUPERBROWSER_TOPOLOGY` | `orchestrator` \| `flat` | Read by the eval runner only: `flat` drives the browser worker directly (same tools, memory hook, budgets; no orchestrator). |
| `SUPERBROWSER_EVAL_DISTRACTOR_TOKENS` | `0` | Memory-pressure ladder: appends a deterministic, marker-free pseudo-DOM block of this many tokens to every tool result. |
| `SUPERBROWSER_CROSS_TASK_MEMORY` | `1` \| `0` | `0`: no site-model ingest at goal time and no site-model merge at task end (paired arms cannot seed each other). |
| `SUPERBROWSER_SITE_MODELS_DIR` | `/tmp/superbrowser/site_models` | Location of the cross-task site-model store. |
| `SUPERBROWSER_TRACE_VISION` / `SUPERBROWSER_TRACE_CLICKS` / `SUPERBROWSER_TRACE_SCREENSHOTS` / `SUPERBROWSER_EVAL_CONTEXT_DUMP` | `0` | Per-run JSONL traces next to the task ledger (`vision_calls.jsonl`, `clicks.jsonl`, `screenshots/index.jsonl` + prefetch screenshots, `live_context.jsonl.gz` + `context_size` events). |
| `SUPERBROWSER_EVAL_WEBJUDGE_MODEL` / `_API_KEY` / `_BASE_URL` | `gpt-4o` / shared judge key / OpenAI | Credentials for the PRIMARY evaluator (Online-Mind2Web's 3-step screenshot judge). Scoped: when `_API_KEY` is set, `_BASE_URL` travels with it. Leave the key unset and WebJudge inherits `SUPERBROWSER_EVAL_JUDGE_API_KEY`/`_BASE_URL`, which silently mismatches if that pair points at another provider. |
| `SUPERBROWSER_EVAL_JUDGE_MODEL` / `_API_KEY` / `_BASE_URL` | `gpt-5.5` / `OPENAI_API_KEY` / OpenAI | Same, for the secondary text-only answer judge (`SUPERBROWSER_EVAL_ANSWER_JUDGE_*` also works and takes precedence). Key precedence for either judge: scoped pair > shared `SUPERBROWSER_EVAL_JUDGE_*` pair > `OPENAI_API_KEY` > `~/.nanobot/config.json` providers. |
