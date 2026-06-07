# Releasing

SuperBrowser publishes two packages from one tag: the **npm** engine and the
**PyPI** bridge (plus a **GHCR** Docker image and a **GitHub Release**). It's all
in `.github/workflows/publish.yml`, triggered by pushing a `v*` tag.

## One-time setup (before the first release)

1. **Reserve the names.** Claim `runagent-superbrowser` on both registries so no
   one squats them:
   - npm: `npm publish` the first version (or `npm publish --dry-run` to rehearse).
   - PyPI: do a TestPyPI upload first (see "Dry runs"), then the first real run claims it.

2. **npm token.** Create an *automation* access token on npmjs.com and add it as
   the repo secret **`NPM_TOKEN`** (Settings → Secrets and variables → Actions).

3. **PyPI Trusted Publisher (OIDC — no token).** On PyPI → your project →
   *Publishing*, add a GitHub publisher:
   - Owner: `runagent-dev`  ·  Repo: `runagent-superbrowser`
   - Workflow: `publish.yml`  ·  Environment: *(leave blank, or `release` if you add one)*

4. **GHCR.** Nothing to configure — the workflow uses the built-in `GITHUB_TOKEN`
   with `packages: write`. (After the first push, set the package visibility to
   public on the org's Packages page if you want it pullable anonymously.)

5. **LICENSE present.** Shipped (`MIT`); both registries surface it.

6. *(Optional)* Add a protected **`release` Environment** with a required reviewer
   and reference it from the publish jobs for a manual gate on first publishes.

## Cutting a release

```bash
# 1. bump both manifests in lockstep
node scripts/bump-version.mjs 0.2.0

# 2. commit + tag + push
git commit -am "release: v0.2.0"
git tag v0.2.0
git push --follow-tags
```

That's it. The tag push runs `publish.yml`:

| Job | Does | Auth |
|---|---|---|
| `guard` | asserts `package.json` == `pyproject.toml` == tag; aborts on mismatch | — |
| `npm` | `npm publish --provenance --access public` | `NPM_TOKEN` + OIDC provenance |
| `pypi` | `python -m build` → `gh-action-pypi-publish` | OIDC Trusted Publishing |
| `docker` | build + push `ghcr.io/runagent-dev/runagent-superbrowser:{version,latest}` | `GITHUB_TOKEN` |
| `release` | GitHub Release with auto-generated notes | `GITHUB_TOKEN` |

## Dry runs (rehearse without uploading)

Run **Actions → Publish → Run workflow** (`workflow_dispatch`). It defaults to
`dry_run: true`, which builds and validates everything but uploads nothing
(`npm publish --dry-run`, PyPI/GHCR/Release skipped). Flip `dry_run` off to
publish from a manual run.

To rehearse PyPI end-to-end, push a pre-release tag (e.g. `v0.2.0-rc.1`) on a
branch and temporarily point the pypi job at TestPyPI
(`repository-url: https://test.pypi.org/legacy/`, with a matching TestPyPI
Trusted Publisher).

## Verify after publishing

```bash
npm view runagent-superbrowser version
pip index versions runagent-superbrowser
docker pull ghcr.io/runagent-dev/runagent-superbrowser:latest
```

## Rollback

- **npm:** you can't re-publish the same version. `npm deprecate runagent-superbrowser@0.2.0 "broken, use 0.2.1"` and ship a patch.
- **PyPI:** `pip`-installable releases can't be overwritten either. *Yank* the bad
  version on PyPI (hides it from new resolves without breaking pins) and ship a patch.
- **GHCR:** delete or re-tag the image version on the org Packages page.

Never reuse a version number — always roll forward with a new patch.
