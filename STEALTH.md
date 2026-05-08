# T3 Stealth & Persistent Profile

Reference for how the real-browser fingerprint path works in this repo
and what you need to wire up when moving it to serverless micro-VMs
with per-user (e.g. WhatsApp phone-number) persistent profiles.

Last updated 2026-04-21, covers the upgrades shipped in the
`rad/bot_protection` branch commits through today.

---

## 1. What's on this box

Nothing new was `apt install`'d during this work — both were already
present:

| Binary | Location | Source |
|---|---|---|
| Chrome for Testing v146.0.7680.153 | `/usr/bin/google-chrome-stable` → symlink → `/root/.cache/puppeteer/chrome/linux-146.0.7680.153/chrome-linux64/chrome` | Pre-installed by Puppeteer (TS side) |
| Xvfb | `/usr/bin/Xvfb` | Pre-installed in the base VM |

**Chrome for Testing (CfT) vs Google Chrome Stable.** CfT is the build
Puppeteer/Playwright auto-download for automation. It's close to the
consumer Chrome but not identical — missing codecs, different about
page, slightly different UA. For the hardest Imperva/Cloudflare
targets you'd swap in consumer Chrome via Google's apt repo:

```bash
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
  | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
  https://dl.google.com/linux/chrome/deb/ stable main" \
  > /etc/apt/sources.list.d/google-chrome.list
apt update && apt install -y google-chrome-stable
```

After that `/usr/bin/google-chrome` is the consumer build; point
`CHROME_PATH` at it. The `google-chrome-stable` path then belongs to
the real consumer binary, not the CfT symlink.

---

## 2. Environment variables

All live in `runagent-superbrowser/.env`. Everything works as before
when unset — these are additive.

### Chrome binary

| Var | Default | Purpose |
|---|---|---|
| `CHROME_PATH` | unset | Absolute path to real Chrome binary. `executable_path=` is forwarded to `chromium.launch()`. Preferred over bundled Chromium. |
| `CHROME_CHANNEL` | unset | Playwright-native channel selector: `chrome`, `chrome-beta`, `msedge`. Use when Chrome is registered on the OS. |

Both read at launch time in both `antibot/interactive_session.py`
(interactive T3 session, used by `browser_open`) and
`antibot/fetch_undetected.py` (read-only T3 fetch).

### Persistent profile

| Var | Default | Purpose |
|---|---|---|
| `T3_PERSIST_PROFILE` | `0` | Set `1` to switch from `chromium.launch + new_context` to `launch_persistent_context` per session. |
| `T3_PROFILE_ROOT` | `~/.superbrowser/profiles` | Parent directory holding per-domain profile subdirs. |
| `T3_PROFILE_MAX_MB` | `200` | Per-profile size cap. On session open, if `du -s <profile>` exceeds this the profile is evicted (rm -rf) and rebuilt empty. |

### Xvfb / headful

| Var | Default | Purpose |
|---|---|---|
| `T3_HEADLESS` | `1` | Set `0` to launch Chrome in headful mode. Required for the hardest CF targets (real viewport pixels, real GPU). |
| `T3_AUTO_XVFB` | `1` | Auto-spawn `Xvfb :99 -screen 0 1920x1080x24` when `T3_HEADLESS=0` and `DISPLAY` is unset. Falls back to headless with a warning if `Xvfb` isn't installed. |
| `T3_XVFB_DISPLAY` | `:99` | Display number Xvfb binds to (and `DISPLAY=` gets set to). |

### HTTP/2 (pre-existing)

| Var | Default | Purpose |
|---|---|---|
| `T3_DISABLE_HTTP2` | `1` | Forces `--disable-http2` in the launch args. Needed for Imperva-fronted hosts that reject non-allowlisted HTTP/2 frames (cars.com class). Set `0` to re-enable HTTP/2. |

### CF solver tuning (pre-existing)

| Var | Default | Purpose |
|---|---|---|
| `T3_CF_WAIT_S` | `30.0` | Seconds to wait during navigate-time CF auto-pass loop. |
| `T3_CF_SOLVER_WAIT_S` | `60.0` | Seconds the dedicated CF solver (`solve_cf_interstitial`) waits. Double the navigate-time budget so the tool call is a meaningful second bite. |

---

## 3. Persistent profile — how it works

### Per-session flow

1. `browser_open(url=...)` — domain extracted from URL.
2. `_resolve_profile_dir(domain)` in `antibot/interactive_session.py`:
   - Path: `<T3_PROFILE_ROOT>/<domain-safe>/`
   - `_domain_safe()` lowercases, strips `www.`, replaces non-alphanum with `_`.
   - `mkdir -p` the directory.
   - `du`-walk it. If > `T3_PROFILE_MAX_MB`, `rm -rf` and recreate empty.
3. Session-open path branches on `T3_PERSIST_PROFILE`:
   - **Off:** `chromium.launch()` returns a shared `self._browser`, then `browser.new_context(viewport=..., locale=..., user_agent=..., ...)` per session (ephemeral).
   - **On:** `chromium.launch_persistent_context(user_data_dir=<profile>, viewport=..., locale=..., user_agent=..., args=[...], executable_path=$CHROME_PATH, channel=$CHROME_CHANNEL, ...)` returns a `BrowserContext` with its own embedded Browser. That Browser is stored on `_ManagedSession.persistent_browser`.
4. Stealth is applied to the context (same 13 flags either way — see `interactive_session.py:~235`).
5. Cookie jar replay fires (still useful as a belt-and-suspenders layer even with persistent profiles).
6. Session close: `context.close()` AND `persistent_browser.close()` when set.

### What's persisted

Everything Chromium writes under the user-data-dir:

- Cookies (including `cf_clearance`, `__cf_bm`, site login cookies)
- `localStorage` / `sessionStorage`
- IndexedDB
- Service worker registrations + caches
- Permissions grants (geolocation, notifications)
- HSTS state
- The browser's own entropy/pref state that fingerprinting APIs read

This is what makes CF auto-pass: a returning-visitor profile with a
valid `cf_clearance` cookie + plausible localStorage + service-worker
state is indistinguishable from a real repeat user.

### First visit bootstrap

A brand-new profile still has to pass CF once. Flow:

1. `browser_open` → CF interstitial.
2. `browser_solve_captcha(method='auto')` auto-routes to
   `_solve_via_cf_wait` → up to 60 s humanized mouse/wheel on the
   challenge page.
3. CF scores the session, stamps `cf_clearance`, redirects.
4. The persistent profile captures the cookie. From this point on the
   profile is "warm".

Manual pre-solve also works: spin up a session with
`T3_PERSIST_PROFILE=1` + `T3_HEADLESS=0` + the live viewer URL, click
through once yourself, close. All future automated sessions on that
domain reuse the cf_clearance.

### Per-domain isolation

One profile per domain — a cars.com task and a trip.com task never
share state. Prevents:

- Cross-site cookie leakage flagged by privacy frameworks
- One domain's localStorage showing up as a canary on an unrelated site
- Profile bloat from every domain piling into one directory

Downside: a multi-domain task (e.g. checkout via Stripe-hosted page
embedded on a merchant site) splits across two profiles. Not a
problem in practice because the embedded iframe uses the merchant
profile's context — Stripe's cookies land there, not in a separate
profile.

---

## 4. Code map

| File | What it does |
|---|---|
| `nanobot/superbrowser_bridge/antibot/interactive_session.py` | T3SessionManager. Both launch paths, profile resolver, xvfb helper, CF wait loop, retry path (persistent sessions skip the retry). |
| `nanobot/superbrowser_bridge/antibot/fetch_undetected.py` | Tier-3 read-only fetch. Same `CHROME_PATH`/`CHROME_CHANNEL` support. |
| `nanobot/superbrowser_bridge/antibot/cookie_jar.py` | Independent cookie store at `~/.superbrowser/cookie-jar/<host>.json`. Used as a pre-seed layer even in persistent-profile mode. |
| `nanobot/superbrowser_bridge/antibot/captcha/solve_cf.py` | `solve_cf_interstitial` wraps `T3SessionManager._wait_for_cf_clear` for the tool-surface path. Records per-domain failure streak + demotes proxy tier on timeout. |
| `nanobot/superbrowser_bridge/routing.py` | `record_cf_failure(domain)`, `record_cf_success(domain)`, `needs_headful(domain)`. Sticky `needs_headful=true` after 2 consecutive failures. |

Key functions worth knowing by name:

- `_resolve_profile_dir(domain) -> Path` — **the seam** for a future object-storage adapter. Swap this one function to download from S3 on start and upload on close; the rest of the stack is unaffected.
- `_domain_safe(domain) -> str` — profile-dir naming rule.
- `_wait_for_cf_clear(sid, *, timeout_s, origin_url) -> dict` — reusable CF wait. Called by both `_goto_with_warmup` (navigate-time) and `solve_cf_interstitial` (tool-call).
- `_maybe_start_xvfb(headless)` — idempotent xvfb bootstrap.

---

## 5. Serverless / WhatsApp plan (for later)

The current persistent-profile path writes to local disk. For a
per-WhatsApp-user serverless setup where each message may land on a
different micro-VM, you need an object-storage adapter. The whole
change is contained to `_resolve_profile_dir`.

### Proposed interface

```python
# _resolve_profile_dir becomes:
async def _resolve_profile_dir(
    domain: str,
    *,
    user_key: str = "",   # e.g. the WhatsApp phone number
) -> Path:
    local = _profile_root() / _domain_safe(domain) / user_key
    local.mkdir(parents=True, exist_ok=True)
    await _profile_store.pull(user_key, domain, local)
    return local
```

And on session close:

```python
# In T3SessionManager.close(), after context.close():
if s.persistent_browser is not None:
    await _profile_store.push(s.user_key, s.domain, s.profile_dir)
```

`_profile_store` is an ABC with two implementations:

- `LocalProfileStore` — no-op pull/push (current behavior)
- `S3ProfileStore` / `GCSProfileStore` — tars the profile dir, uploads
  under `s3://<bucket>/<user_key>/<domain>.tar.gz`

### Sizing

Empty Chrome profile: ~4 MB. After one task: ~15–30 MB. After
hundreds of visits to a site: 50–150 MB. Tar.gz compresses ~3-4×.

Cold-start cost per session:

| Step | Time |
|---|---|
| S3 GetObject (20 MB tar.gz) | 100–300 ms |
| Untar | 50–150 ms |
| Chrome launch via launch_persistent_context | 300–600 ms |
| Stealth apply + page new_page | 100–200 ms |
| **Total** | **~0.5–1.3 s extra per cold VM** |

Hot VM reuse (same VM serves multiple tasks): no S3 round-trip after
the first task — profile stays on local disk.

### User isolation

Key profiles by `(phone_number, domain)` not just domain:

- Two WhatsApp users hitting cars.com should NOT share a profile —
  CF would see them as the same "returning user", which is weird if
  they're on different proxies, and a cookie collision could leak
  account state.
- Same user across VMs → same profile → same fingerprint → CF sees
  coherent returning-user behaviour.

Profile path layout:

```
~/.superbrowser/profiles/<phone>/<domain>/
```

`_domain_safe(domain)` still applies to the domain segment. Phone
number also wants a safe-name transform (strip `+`, etc.).

### TTL / garbage collection

- CF's `cf_clearance` usually expires in ~30 min (`Max-Age` on the
  cookie). A profile older than 30 min effectively starts cold on
  that site but still has localStorage/fingerprint continuity, which
  is still worth something.
- GC policy: delete profiles untouched for > 7 days from the object
  store. The `last-modified` timestamp on the S3 object serves as the
  LRU key.

### Concurrent writes

Two tasks for the same user on the same domain racing to push the
same tarball → last-write-wins is acceptable (both ran with real
user intent, either profile state is valid). If you want stronger
semantics, use an S3 lock object or DynamoDB conditional writes — but
start without.

---

## 6. Known behavior / gotchas

- **First visit always faces CF.** The profile bootstraps on first
  visit. Budget ~60 s for the first automated task on a new domain.
- **Stale `cf_clearance` can be rejected.** If the cookie is from a
  previous session and CF has since rotated its challenge, the page
  still shows "Just a moment..." even though the cookie is present.
  The wait loop handles this: cookie-match exit is gated on
  `not looks_challenge` (title doesn't say "Just a moment..."). The
  wait loop keeps humanizing until a fresh clearance is stamped OR
  the timeout fires.
- **Bot-management cookies ≠ clearance.** `__cf_bm`, `_abck`,
  `ak_bmsc`, `incap_ses_` are set on every request including during
  challenges. Only `cf_clearance`, `datadome`, `reese84` actually
  mean the challenge passed. The solver's `clearance_cookie_names`
  set is narrow for this reason.
- **Persistent sessions skip the CF retry dance.** The CF-block-rebuild
  path in `open()` calls `_rebuild_context_for_retry` which wipes UA
  and restarts — that's counter-productive when a persistent profile
  is the whole point. Rebuild is skipped when
  `_ManagedSession.persistent_browser` is set.
- **T1 → T3 auto-escalation.** When `BrowserOpenTool` is called with
  `tier='auto'` and T1 returns 401/403/429/502/503, the tool closes
  the T1 session, records a failure via `_record_routing_outcome`,
  and re-opens on T3 within the same call. Next task on that domain
  starts on T3 directly because `choose_starting_tier` sees the
  recorded T1 fail.
- **Auto-escalation ladder** (inside `solve_cf_interstitial` on
  timeout): records per-domain streak via
  `routing.record_cf_failure(domain)`, and calls
  `proxy_tiers.default().demote(domain)` so the next session uses
  residential if `PROXY_POOL_RESIDENTIAL` is set.

---

## 7. Quick reference — current .env block

```bash
CHROME_PATH=/usr/bin/google-chrome-stable
T3_PERSIST_PROFILE=1
# T3_PROFILE_ROOT=/root/.superbrowser/profiles
# T3_PROFILE_MAX_MB=200
# T3_HEADLESS=0
# T3_AUTO_XVFB=1
# T3_XVFB_DISPLAY=:99
```

Commented vars are at their defaults — uncomment to override.

## 8. Diagnostic log

T3 dispatcher exceptions are written to `/tmp/superbrowser/t3_errors.log`
with full traceback per failure. When something goes wrong, `tail -80`
that file first.

Also logged to stdout during captcha flows:

- `[auto-route] detect -> type='cf_interstitial' present=True notes=[...]`
- `[cf_wait] entering solver for session=...`
- `[cf_wait] result: solved=True durationMs=... iterations=... cookies=[...]`
