/**
 * Build-staleness check + /health/version endpoint.
 *
 * Phase K of the wineaccess revamp. Production runs `node build/index.js`
 * but TS edits land in `src/`. If `npm run build` isn't re-run, the
 * Python side (which is hot-reloaded via venv) sees new behaviour
 * while the TS side is silently stuck on old code. The wineaccess
 * trace exposed this: drift detection, find-target endpoint, and
 * section-aware select_option were all merged but the running TS
 * server didn't have any of it.
 *
 * Defaults: warn loudly on stale build, don't block startup. Set
 * STRICT_BUILD=1 (CI / prod) to upgrade the warning to a hard exit.
 */

import { readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import type { Express, Request, Response } from 'express';

interface BuildStaleness {
  staleSources: Array<{ path: string; mtimeMs: number }>;
  buildMtimeMs: number | null;
  newestSrcMtimeMs: number;
  newestSrcPath: string | null;
}

const SRC_DIR = 'src';
const BUILD_INDEX = 'build/index.js';

function walkTsFiles(root: string, out: Array<{ path: string; mtimeMs: number }>): void {
  let entries: Array<{ name: string; type: string }>;
  try {
    entries = readdirSync(root, { withFileTypes: true })
      .map((d) => ({ name: d.name, type: d.isDirectory() ? 'dir' : (d.isFile() ? 'file' : 'other') }));
  } catch {
    return;
  }
  for (const e of entries) {
    if (e.name.startsWith('.')) continue;
    const full = join(root, e.name);
    if (e.type === 'dir') {
      // Skip generated / vendored dirs.
      if (e.name === 'node_modules' || e.name === 'build' || e.name === 'dist' || e.name === '__pycache__') continue;
      walkTsFiles(full, out);
    } else if (e.type === 'file' && (e.name.endsWith('.ts') || e.name.endsWith('.tsx'))) {
      try {
        out.push({ path: full, mtimeMs: statSync(full).mtimeMs });
      } catch {
        // ignore unreadable
      }
    }
  }
}

export function checkBuildStaleness(rootDir: string = process.cwd()): BuildStaleness {
  const buildPath = join(rootDir, BUILD_INDEX);
  let buildMtimeMs: number | null = null;
  try {
    buildMtimeMs = statSync(buildPath).mtimeMs;
  } catch {
    // Build doesn't exist — fresh checkout? `npm start` will fail
    // anyway when it tries to load build/index.js, so we don't need
    // to flag here.
  }

  const tsFiles: Array<{ path: string; mtimeMs: number }> = [];
  walkTsFiles(join(rootDir, SRC_DIR), tsFiles);

  let newestSrcMtimeMs = 0;
  let newestSrcPath: string | null = null;
  for (const f of tsFiles) {
    if (f.mtimeMs > newestSrcMtimeMs) {
      newestSrcMtimeMs = f.mtimeMs;
      newestSrcPath = relative(rootDir, f.path);
    }
  }

  const staleSources: Array<{ path: string; mtimeMs: number }> = [];
  if (buildMtimeMs !== null) {
    for (const f of tsFiles) {
      // Treat anything newer than build/index.js by >2s as stale.
      // The 2s slack absorbs filesystem clock drift on container
      // mounts and avoids flagging files touched milliseconds after
      // the build finished.
      if (f.mtimeMs > buildMtimeMs + 2000) {
        staleSources.push({ path: relative(rootDir, f.path), mtimeMs: f.mtimeMs });
      }
    }
    staleSources.sort((a, b) => b.mtimeMs - a.mtimeMs);
  }

  return { staleSources, buildMtimeMs, newestSrcMtimeMs, newestSrcPath };
}

function fmtAge(mtimeMs: number): string {
  const ageMin = Math.round((Date.now() - mtimeMs) / 60000);
  if (ageMin < 60) return `${ageMin}m ago`;
  if (ageMin < 60 * 24) return `${Math.round(ageMin / 60)}h ago`;
  return `${Math.round(ageMin / (60 * 24))}d ago`;
}

/**
 * Print a startup banner if build/ is older than any src/*.ts file.
 * Returns true if stale (caller can hard-exit if STRICT_BUILD=1).
 */
export function warnIfBuildStale(rootDir: string = process.cwd()): boolean {
  const result = checkBuildStaleness(rootDir);
  if (result.buildMtimeMs === null) {
    process.stderr.write(
      '\n[BUILD_STALE] build/index.js does not exist. Run `npm run build` before `npm start`.\n\n',
    );
    return true;
  }
  if (result.staleSources.length === 0) {
    return false;
  }
  const banner = [
    '',
    '╔══════════════════════════════════════════════════════════════════╗',
    '║  ⚠ BUILD STALE — TS sources newer than build/index.js            ║',
    '║  The running server does NOT include your latest TS edits.       ║',
    '║  Run `npm run build` and restart, OR use `npm run dev`.          ║',
    '╚══════════════════════════════════════════════════════════════════╝',
    `  build/index.js     mtime: ${new Date(result.buildMtimeMs).toISOString()}  (${fmtAge(result.buildMtimeMs)})`,
    `  newest src/*.ts    mtime: ${new Date(result.newestSrcMtimeMs).toISOString()}  (${fmtAge(result.newestSrcMtimeMs)})`,
    `  stale source count: ${result.staleSources.length}`,
    '  most recently modified sources:',
    ...result.staleSources.slice(0, 8).map(
      (s) => `    · ${s.path}  (${fmtAge(s.mtimeMs)})`,
    ),
    '',
  ];
  for (const line of banner) {
    process.stderr.write(line + '\n');
  }
  return true;
}

/**
 * Mount /health/version. Returns build mtime, newest src mtime,
 * staleness flag, optional commit hash. Operators / CI / probes
 * can hit this to detect Python/TS deploy drift.
 */
export function mountHealthVersion(app: Express, rootDir: string = process.cwd()): void {
  app.get('/health/version', (_req: Request, res: Response) => {
    const result = checkBuildStaleness(rootDir);
    res.json({
      ok: true,
      build: {
        mtime: result.buildMtimeMs ? new Date(result.buildMtimeMs).toISOString() : null,
        mtime_ms: result.buildMtimeMs,
      },
      src: {
        newest_path: result.newestSrcPath,
        newest_mtime: result.newestSrcMtimeMs ? new Date(result.newestSrcMtimeMs).toISOString() : null,
        newest_mtime_ms: result.newestSrcMtimeMs,
      },
      stale: result.staleSources.length > 0,
      stale_source_count: result.staleSources.length,
      stale_sources_preview: result.staleSources.slice(0, 5).map((s) => s.path),
      git_commit: process.env.GIT_COMMIT || null,
      uptime_s: Math.round(process.uptime()),
      now: new Date().toISOString(),
    });
  });
}
