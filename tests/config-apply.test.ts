/**
 * Tests for the TS config loader (src/config/apply.ts) — the twin of
 * nanobot/superbrowser_config/loader.py. Uses injected env objects so the
 * suite never touches the developer's real process.env or config file.
 */

import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import { applyConfigToEnv, deepMerge, GUARD_ENV, resolveProfile } from '../src/config/apply.js';

function envWithConfig(data: unknown, extra: Record<string, string> = {}): NodeJS.ProcessEnv {
  const dir = mkdtempSync(join(tmpdir(), 'sb-config-'));
  const path = join(dir, 'config.json');
  writeFileSync(path, typeof data === 'string' ? data : JSON.stringify(data));
  return { SUPERBROWSER_CONFIG: path, ...extra };
}

describe('applyConfigToEnv', () => {
  it('is a no-op without a config file', () => {
    const env: NodeJS.ProcessEnv = { SUPERBROWSER_CONFIG: join(tmpdir(), 'nope', 'config.json') };
    expect(applyConfigToEnv(env)).toBe(false);
    expect(env[GUARD_ENV]).toBe('1');
    expect(Object.keys(env).sort()).toEqual(['SUPERBROWSER_CONFIG', GUARD_ENV].sort());
  });

  it('is a no-op on broken JSON', () => {
    const env = envWithConfig('{not json');
    expect(applyConfigToEnv(env)).toBe(false);
    expect(Object.keys(env).sort()).toEqual(['SUPERBROWSER_CONFIG', GUARD_ENV].sort());
  });

  it('projects explicit values with per-reader boolean forms', () => {
    const env = envWithConfig({
      version: 1,
      profile: 'docker',
      engine: { port: 3200, headless: false },
      vision: { enabled: true, somOverlay: false },
      brain: { provider: 'anthropic', apiKey: 'sk-x' },
    });
    expect(applyConfigToEnv(env)).toBe(true);
    expect(env.PORT).toBe('3200');
    expect(env.HEADLESS).toBe('false'); // boolWord
    expect(env.VISION_ENABLED).toBe('1'); // boolFlag
    expect(env.VISION_SOM_OVERLAY).toBe('0');
    expect(env.LLM_PROVIDER).toBe('anthropic');
    expect(env.LLM_API_KEY).toBe('sk-x');
  });

  it('never overrides already-set env (env > config)', () => {
    const env = envWithConfig({ version: 1, profile: 'docker', engine: { port: 3200 } }, { PORT: '9999' });
    applyConfigToEnv(env);
    expect(env.PORT).toBe('9999');
  });

  it('applies the profile preset, explicit values winning', () => {
    const env = envWithConfig({
      version: 1,
      profile: 'local',
      engine: { concurrency: { maxConcurrent: 7 } },
    });
    applyConfigToEnv(env);
    expect(env.CONCURRENT).toBe('7'); // explicit beats preset 3
    expect(env.QUEUED).toBe('5'); // local preset
    expect(env.SUPERBROWSER_COOKIE_JAR).toBe('1');
    expect(env.T3_XVFB_DISPLAY).toBeUndefined(); // local preset sets no display
  });

  it('vm preset fills the display pair; group-skip protects a real DISPLAY', () => {
    const filled = envWithConfig({ version: 1, profile: 'vm' });
    applyConfigToEnv(filled);
    expect(filled.T3_XVFB_DISPLAY).toBe(':99');
    expect(filled.DISPLAY).toBe(':99');

    const desktop = envWithConfig({ version: 1, profile: 'vm' }, { DISPLAY: ':0' });
    applyConfigToEnv(desktop);
    expect(desktop.DISPLAY).toBe(':0');
    expect(desktop.T3_XVFB_DISPLAY).toBeUndefined();
  });

  it('fills token pair together and group-skips when half set', () => {
    const both = envWithConfig({ version: 1, profile: 'docker', engine: { token: 't1' } });
    applyConfigToEnv(both);
    expect(both.TOKEN).toBe('t1');
    expect(both.SUPERBROWSER_TOKEN).toBe('t1');

    const half = envWithConfig({ version: 1, profile: 'docker', engine: { token: 't1' } }, { TOKEN: 'shell' });
    applyConfigToEnv(half);
    expect(half.TOKEN).toBe('shell');
    expect(half.SUPERBROWSER_TOKEN).toBeUndefined();
  });

  it('joins firewall lists and skips the unprojected gateway section', () => {
    const env = envWithConfig({
      version: 1,
      profile: 'docker',
      engine: { firewall: { allow: ['a.com', 'b.com'] } },
      gateway: { enabled: true, port: 8460 },
    });
    applyConfigToEnv(env);
    expect(env.FIREWALL_ALLOW_LIST).toBe('a.com,b.com');
    expect(Object.keys(env).some((key) => key.startsWith('GATEWAY'))).toBe(false);
  });

  it('is idempotent via the guard', () => {
    const env = envWithConfig({ version: 1, profile: 'docker', engine: { port: 1 } });
    expect(applyConfigToEnv(env)).toBe(true);
    env.PORT = 'changed';
    expect(applyConfigToEnv(env)).toBe(false);
    expect(env.PORT).toBe('changed');
  });

  it('SUPERBROWSER_PROFILE overrides the file profile', () => {
    const env = envWithConfig({ version: 1, profile: 'local' }, { SUPERBROWSER_PROFILE: 'vm' });
    applyConfigToEnv(env);
    expect(env.CONCURRENT).toBe('10'); // vm preset
  });
});

describe('helpers', () => {
  it('deepMerge merges nested objects', () => {
    expect(deepMerge({ a: { x: 1, y: 2 } }, { a: { y: 3 }, b: 4 })).toEqual({ a: { x: 1, y: 3 }, b: 4 });
  });

  it('resolveProfile prefers env, then file', () => {
    expect(resolveProfile({ profile: 'local' }, { SUPERBROWSER_PROFILE: 'vm' })).toBe('vm');
    expect(resolveProfile({ profile: 'local' }, {})).toBe('local');
  });
});
