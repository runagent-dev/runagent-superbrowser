/**
 * Product config loader — projects ~/.superbrowser/config.json into UNSET
 * environment variables. Imported for its side effect from src/index.ts,
 * immediately after `dotenv/config`, giving the precedence chain:
 *
 *   process env > .env > config.json explicit > profile preset > code defaults
 *
 * Fail-open by design: no file, an unparseable file, or any internal error is
 * a no-op — zero-config `npm run dev` behavior is unchanged. The Python SDK
 * runs the same algorithm (nanobot/superbrowser_config/loader.py); the shared
 * data tables live in envmap.ts / presets.ts (parity-checked against the
 * Python package's JSON files).
 */

import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

import { detectProfile, findChrome } from './detect.js';
import { ENV_RULES, type EnvRule } from './envmap.js';
import { PRESETS } from './presets.js';

export const GUARD_ENV = 'SUPERBROWSER_CONFIG_APPLIED';
export const CONFIG_PATH_ENV = 'SUPERBROWSER_CONFIG';
export const PROFILE_ENV = 'SUPERBROWSER_PROFILE';
export const CONFIG_VERSION = 1;

type Json = Record<string, unknown>;

export function configPath(env: NodeJS.ProcessEnv = process.env): string {
  return env[CONFIG_PATH_ENV] || join(homedir(), '.superbrowser', 'config.json');
}

function dig(data: unknown, dotted: string): unknown {
  let node: unknown = data;
  for (const part of dotted.split('.')) {
    if (node === null || typeof node !== 'object' || !(part in (node as Json))) return undefined;
    node = (node as Json)[part];
  }
  return node === null ? undefined : node;
}

function isPlainObject(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function deepMerge(base: Json, override: Json): Json {
  const out: Json = { ...base };
  for (const [key, value] of Object.entries(override)) {
    if (isPlainObject(value) && isPlainObject(out[key])) {
      out[key] = deepMerge(out[key] as Json, value);
    } else {
      out[key] = value;
    }
  }
  return out;
}

const TRUE_WORDS = ['1', 'true', 'yes', 'on'];
const FALSE_WORDS = ['0', 'false', 'no', 'off'];

function asBool(value: unknown): boolean | null {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number' && (value === 0 || value === 1)) return value === 1;
  if (typeof value === 'string') {
    const lowered = value.trim().toLowerCase();
    if (TRUE_WORDS.includes(lowered)) return true;
    if (FALSE_WORDS.includes(lowered)) return false;
  }
  return null;
}

function stringify(rule: EnvRule, value: unknown): string | null {
  if (value === undefined || value === null) return null;
  switch (rule.kind) {
    case 'str': {
      if (typeof value === 'boolean') return null;
      return String(value);
    }
    case 'int': {
      if (typeof value === 'boolean') return null;
      const parsed = typeof value === 'number' ? value : parseInt(String(value), 10);
      return Number.isFinite(parsed) ? String(Math.trunc(parsed)) : null;
    }
    case 'boolWord': {
      const parsed = asBool(value);
      return parsed === null ? null : parsed ? 'true' : 'false';
    }
    case 'boolFlag': {
      const parsed = asBool(value);
      return parsed === null ? null : parsed ? '1' : '0';
    }
    case 'listComma': {
      if (typeof value === 'string') return value || null;
      if (Array.isArray(value)) {
        const joined = value
          .map((item) => String(item).trim())
          .filter(Boolean)
          .join(',');
        return joined || null;
      }
      return null;
    }
    case 'chromeAuto': {
      if (typeof value !== 'string' || !value) return null;
      if (value === 'auto') return findChrome();
      return value;
    }
    default:
      return null;
  }
}

export function resolveProfile(raw: Json | null, env: NodeJS.ProcessEnv = process.env): string {
  return String(env[PROFILE_ENV] || (raw ? raw['profile'] : '') || detectProfile(env));
}

function apply(env: NodeJS.ProcessEnv): boolean {
  const path = configPath(env);
  if (!existsSync(path)) return false;

  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(path, 'utf-8'));
  } catch (err) {
    console.error(`[superbrowser-config] could not read ${path}: ${err}`);
    return false;
  }
  if (!isPlainObject(raw)) {
    console.error(`[superbrowser-config] ${path} is not a JSON object; ignoring`);
    return false;
  }
  const version = raw['version'];
  if (version !== undefined && version !== CONFIG_VERSION) {
    console.error(
      `[superbrowser-config] ${path} has version ${JSON.stringify(version)} ` +
        `(this build understands ${CONFIG_VERSION}); applying best-effort`
    );
  }

  const profile = resolveProfile(raw, env);
  const explicit: Json = {};
  for (const [key, value] of Object.entries(raw)) {
    if (key !== 'version' && key !== 'profile') explicit[key] = value;
  }
  const merged = deepMerge((PRESETS[profile] as Json) || {}, explicit);

  for (const rule of ENV_RULES) {
    const value = dig(merged, rule.path);
    if (value === undefined) continue;
    const text = stringify(rule, value);
    if (text === null) continue;
    // Group-skip: if the environment already owns ANY key of this rule,
    // filling the rest would desync pairs (TOKEN/SUPERBROWSER_TOKEN, DISPLAY).
    if (rule.env.some((key) => env[key] !== undefined)) continue;
    for (const key of rule.env) env[key] = text;
  }
  return true;
}

export function applyConfigToEnv(env: NodeJS.ProcessEnv = process.env): boolean {
  if (env[GUARD_ENV] === '1') return false;
  let applied = false;
  try {
    applied = apply(env);
  } catch (err) {
    console.error(`[superbrowser-config] ignored config error: ${err}`);
  }
  env[GUARD_ENV] = '1';
  return applied;
}

// Side effect on import — src/index.ts imports this module right after dotenv.
applyConfigToEnv();
