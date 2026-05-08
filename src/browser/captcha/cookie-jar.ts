/**
 * Per-(task, domain) cookie jar for bot-protection cookies.
 *
 * Problem: when a human clears a captcha via human-handoff, the site sets
 * a "you're verified" cookie (`cf_clearance`, `__cf_bm`, `datadome`, ...)
 * on the live page. That cookie dies with the in-memory session (30 min
 * idle / 2 hr max), so the next task spawned against the same domain
 * prompts the same human to re-solve. This jar persists JUST those
 * bot-protection cookies to disk so a follow-up session on the same
 * task+domain starts already cleared.
 *
 * Scope key: `${SUPERBROWSER_TASK_ID ?? 'global'}:${hostname}`. Mirrors how
 * `HandoffLedger` scopes — one human solve is valid for the whole task,
 * separate tasks do NOT share the jar by default (set task ID empty to
 * pool globally if that's what you actually want).
 *
 * Storage: `~/.superbrowser/cookie-jar/<hostname>.json`, one file per
 * domain so jar entries are trivially inspectable and deletable.
 *
 * Safety:
 *   - Only cookies with names matching the bot-protection whitelist are
 *     saved. Session/identity cookies never leave the browser.
 *   - UA is captured at save time. On load, if the current session UA
 *     differs, we skip restore — cf_clearance is UA+IP-pinned, feeding a
 *     mismatched UA back would immediately invalidate and make things
 *     worse than starting blank.
 *   - Hard TTL cap of 7 days regardless of cookie `expires`.
 *
 * Opt-in via SUPERBROWSER_COOKIE_JAR=1 — default off until validated.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';
import type { Cookie, CookieParam, Page } from 'puppeteer-core';
import { hostKey } from './domain-stats.js';

// Bot-protection cookie name patterns. Conservative whitelist — any cookie
// that doesn't match these stays in the live browser jar and nothing else.
const BOT_PROTECTION_COOKIE_PATTERNS: RegExp[] = [
  /^cf_clearance$/i,           // Cloudflare — IP+UA pinned, exact match required.
  /^__cf_bm$/i,                // Cloudflare bot management short-lived cookie.
  /^_hcaptcha/i,               // hCaptcha.
  /^h-captcha/i,
  /^recaptcha/i,               // reCAPTCHA.
  /^datadome$/i,               // DataDome.
  /^incap_ses_/i,              // Imperva Incapsula session.
  /^visid_incap_/i,            // Imperva visitor.
  /^AWSALB/i,                  // AWS Application Load Balancer sticky.
  /^AWSALBCORS/i,
  /^bm_sv$/i,                  // Akamai Bot Manager.
  /^bm_sz$/i,
  /^ak_bmsc$/i,
  /^_abck$/i,                  // Akamai.
  /^reese84$/i,                // Kasada.
  /^px-?/i,                    // PerimeterX (px3, px-captcha, etc.).
];

const HARD_TTL_MS = 7 * 24 * 60 * 60 * 1000;

function isEnabled(): boolean {
  return process.env.SUPERBROWSER_COOKIE_JAR === '1';
}

function defaultJarDir(): string {
  if (process.env.SUPERBROWSER_COOKIE_JAR_PATH) return process.env.SUPERBROWSER_COOKIE_JAR_PATH;
  return path.join(os.homedir(), '.superbrowser', 'cookie-jar');
}

function scopeKey(): string {
  return process.env.SUPERBROWSER_TASK_ID || 'global';
}

function jarFileFor(hostname: string): string {
  // Hostname is already colon/slash-free from hostKey, but sanitize defensively
  // so a crafted URL can't write outside the jar dir.
  const safe = hostname.replace(/[^a-z0-9._-]/gi, '_');
  return path.join(defaultJarDir(), `${safe}.json`);
}

interface PersistedEntry {
  userAgent: string;
  captureUrl: string;
  capturedAt: number;
  cookies: CookieParam[];
}

type JarFile = Record<string, PersistedEntry>; // scope → entry

function readJar(hostname: string): JarFile {
  const file = jarFileFor(hostname);
  try {
    if (!fs.existsSync(file)) return {};
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (parsed && typeof parsed === 'object') return parsed as JarFile;
  } catch { /* corrupt — start fresh */ }
  return {};
}

function writeJar(hostname: string, data: JarFile): void {
  const file = jarFileFor(hostname);
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, JSON.stringify(data, null, 2));
  } catch { /* best-effort */ }
}

function matchesProtectionPattern(name: string): boolean {
  return BOT_PROTECTION_COOKIE_PATTERNS.some((re) => re.test(name));
}

/**
 * Capture the current page's bot-protection cookies into the jar.
 * Returns the number of cookies saved (0 if disabled or nothing matched).
 */
export async function saveDomainCookies(page: Page, url: string): Promise<number> {
  if (!isEnabled()) return 0;
  const host = hostKey(url);
  if (host === '_unknown') return 0;

  let userAgent = '';
  try { userAgent = await page.browser().userAgent(); } catch { /* fall back to blank */ }

  let allCookies: Cookie[] = [];
  try { allCookies = await page.cookies(); } catch { return 0; }

  const kept: CookieParam[] = [];
  for (const c of allCookies) {
    if (!matchesProtectionPattern(c.name)) continue;
    kept.push({
      name: c.name,
      value: c.value,
      domain: c.domain,
      path: c.path,
      expires: c.expires,
      httpOnly: c.httpOnly,
      secure: c.secure,
      sameSite: c.sameSite,
    });
  }
  if (kept.length === 0) return 0;

  const jar = readJar(host);
  jar[scopeKey()] = {
    userAgent,
    captureUrl: url,
    capturedAt: Date.now(),
    cookies: kept,
  };
  writeJar(host, jar);
  return kept.length;
}

/**
 * Restore bot-protection cookies into the page's jar BEFORE navigation.
 * Returns number of cookies restored. No-ops when disabled, when the UA
 * differs from capture, or when the entry has expired.
 */
export async function loadDomainCookies(page: Page, url: string): Promise<number> {
  if (!isEnabled()) return 0;
  const host = hostKey(url);
  if (host === '_unknown') return 0;

  const jar = readJar(host);
  const entry = jar[scopeKey()];
  if (!entry) return 0;

  // Hard TTL cap — cookies older than 7 days are almost certainly stale,
  // and we'd rather the human re-solve than feed a dead cf_clearance to
  // Cloudflare and get the UA fingerprinted as a bot.
  if (Date.now() - entry.capturedAt > HARD_TTL_MS) return 0;

  // UA pinning: cf_clearance specifically, and most bot-management cookies
  // generally, bind to UA. A mismatched restore is worse than no restore.
  let currentUA = '';
  try { currentUA = await page.browser().userAgent(); } catch { /* blank */ }
  if (entry.userAgent && currentUA && entry.userAgent !== currentUA) return 0;

  // Filter cookies whose own `expires` has already passed.
  const nowSec = Date.now() / 1000;
  const live = entry.cookies.filter((c) => {
    if (!c.expires || c.expires < 0) return true; // session cookie — keep.
    return c.expires > nowSec;
  });
  if (live.length === 0) return 0;

  try {
    await page.setCookie(...live);
  } catch {
    return 0;
  }
  return live.length;
}
