# Contributing

Thanks for helping out. SuperBrowser is two halves that talk over HTTP on
`localhost:3100`:

- **TS browser engine** (`src/` → `build/`) — published to npm as
  `runagent-superbrowser`. Puppeteer + stealth, HTTP + MCP servers.
- **Python agent bridge** (`nanobot/superbrowser_bridge/` + `nanobot/vision_agent/`)
  — published to PyPI as `runagent-superbrowser`. The nanobot orchestrator,
  captcha solving, vision preprocessing.

> **Heads-up on the `nanobot/` folder.** It is *not* the nanobot framework — it's
> this project's Python tree. `from nanobot import Nanobot` resolves to the
> released **`nanobot-ai`** PyPI package; the two importable packages we ship are
> `superbrowser_bridge` and `vision_agent`, which live *under* `nanobot/` but
> install as top-level names. Don't add an `__init__.py` at `nanobot/`'s root.

## Dev setup

The fastest path is the bootstrap installer (it works on a checkout too):

```bash
bash scripts/install.sh          # or scripts/install.ps1 on Windows
```

Or by hand:

```bash
git clone https://github.com/runagent-dev/runagent-superbrowser.git
cd runagent-superbrowser

# TS engine
npm install && npm run build
cp .env.example .env

# Python bridge — requirements.txt is the pinned dev lockfile
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
patchright install chromium
playwright install-deps chromium      # Linux only
```

`requirements.txt` pins the exact tree (including the known-good `nanobot-ai`
build); `pyproject.toml` declares the loose, *released* dependency set that
`pip install runagent-superbrowser` resolves against. Edit both when you change
a direct dependency.

## Running things

```bash
npm start                                   # engine on :3100 (no API key needed)
venv/bin/python nanobot/run.py "find trending Python repos on GitHub"
```

`nanobot/run.py` is a dev shim for the installed `superbrowser-agent` console
script — keep its behavior in sync with `superbrowser_bridge/cli.py`.

## Tests & checks

```bash
npm run build                               # must pass (the CI gate)
npm test                                    # vitest (some tests launch a browser)
node bin/superbrowser-doctor.js             # Node-side environment check

pytest nanobot/superbrowser_bridge/tests    # Python unit tests
superbrowser-doctor                         # Python-side environment check
```

CI (`.github/workflows/ci.yml`) gates on **build + packaging + import-smoke**
across Ubuntu/macOS/Windows; the browser-dependent test suites currently run
**non-blocking** until they're proven hermetic in CI — tighten them as you make
them deterministic.

## Releasing

Versions for npm and PyPI move together. Bump both with one command, then tag:

```bash
node scripts/bump-version.mjs 0.2.0
git commit -am "release: v0.2.0"
git tag v0.2.0 && git push --follow-tags
```

The tag push triggers `.github/workflows/publish.yml` (npm + PyPI + GHCR image +
GitHub Release). Full maintainer checklist and one-time secret/OIDC setup:
[`docs/RELEASING.md`](docs/RELEASING.md).
