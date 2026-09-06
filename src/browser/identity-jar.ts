/**
 * Per-domain IDENTITY jar — the "log in once, stay logged in" store.
 *
 * The bot-protection cookie-jar (cookie-jar.ts) deliberately saves ONLY
 * antibot cookies. Real login sessions live in ordinary cookies it filters
 * out, so a human who logs in via the live-view handoff would have to log in
 * again next session. This jar captures the FULL cookie set for a domain
 * after a login and restores it before navigation, so the session persists.
 *
 * Storage: ~/.superbrowser/identities/<hostname>.json (0600, dir 0700), one
 * file per domain — inspectable and individually deletable ("forget site").
 *
 * Opt-in: SUPERBROWSER_IDENTITY_JAR=1 (default off).
 *
 * Security: this is logged-in session state — treat the file like a password.
 * It is the same exposure class as the T3 Chrome profiles already stored in
 * this directory. Optional at-rest encryption via SUPERBROWSER_IDENTITY_KEY
 * (32-byte hex → AES-256-GCM over the cookies field only). See docs/identity.md.
 */

import crypto from 'crypto';
import fs from 'fs';
import os from 'os';
import path from 'path';
import type { CookieParam, Page } from 'puppeteer-core';
import { hostKey } from './captcha/domain-stats.js';

const DEFAULT_TTL_DAYS = 30;

function isEnabled(): boolean {
  return process.env.SUPERBROWSER_IDENTITY_JAR === '1';
}

function ttlMs(): number {
  const days = parseInt(process.env.SUPERBROWSER_IDENTITY_TTL_DAYS || String(DEFAULT_TTL_DAYS), 10);
  return (Number.isFinite(days) && days > 0 ? days : DEFAULT_TTL_DAYS) * 24 * 60 * 60 * 1000;
}

function jarDir(): string {
  return path.join(os.homedir(), '.superbrowser', 'identities');
}

/**
 * One identity file per FULL hostname (www.example.com.json) — same keying as
 * cookie-jar.ts. Keying by a naive "registrable domain" (last two labels) is
 * unsafe: on multi-label TLDs (amazon.co.uk, tesco.co.uk) it collapses to
 * `co.uk`, so saving one site would clobber another's login. Full-hostname
 * keying keeps every site independent, at the cost of per-subdomain
 * persistence (matching cookie-jar's long-standing behavior).
 */
function jarFileFor(hostname: string): string {
  const safe = hostname.replace(/[^a-z0-9._-]/gi, '_');
  return path.join(jarDir(), `${safe}.json`);
}

/**
 * A cookie belongs to this hostname's identity if the hostname would send it:
 * the cookie's own domain equals the hostname or is a parent of it (so a
 * `.example.com` login cookie is captured for `www.example.com`).
 */
function cookieBelongs(cookieDomain: string, hostname: string): boolean {
  const d = cookieDomain.replace(/^\./, '').toLowerCase();
  return d === hostname || hostname.endsWith(`.${d}`);
}

interface IdentityFile {
  version: 1;
  domain: string;
  capturedAt: number;
  lastUsedAt: number;
  userAgent: string;
  cookies?: CookieParam[];
  enc?: 'aes-256-gcm';
  encData?: string; // base64(iv|tag|ciphertext) when encrypted
  label?: string;
}

function encryptionKey(): Buffer | null {
  const hex = process.env.SUPERBROWSER_IDENTITY_KEY;
  if (!hex) return null;
  try {
    const key = Buffer.from(hex, 'hex');
    return key.length === 32 ? key : null;
  } catch {
    return null;
  }
}

function sealCookies(file: IdentityFile, cookies: CookieParam[]): void {
  const key = encryptionKey();
  if (!key) {
    file.cookies = cookies;
    return;
  }
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
  const body = Buffer.concat([cipher.update(JSON.stringify(cookies), 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  file.enc = 'aes-256-gcm';
  file.encData = Buffer.concat([iv, tag, body]).toString('base64');
}

function openCookies(file: IdentityFile): CookieParam[] {
  if (!file.enc) return file.cookies || [];
  const key = encryptionKey();
  if (!key || !file.encData) return [];
  try {
    const raw = Buffer.from(file.encData, 'base64');
    const iv = raw.subarray(0, 12);
    const tag = raw.subarray(12, 28);
    const body = raw.subarray(28);
    const decipher = crypto.createDecipheriv('aes-256-gcm', key, iv);
    decipher.setAuthTag(tag);
    const out = Buffer.concat([decipher.update(body), decipher.final()]).toString('utf8');
    return JSON.parse(out) as CookieParam[];
  } catch {
    return []; // wrong/absent key — behaves like no saved identity
  }
}

function readIdentity(hostname: string): IdentityFile | null {
  try {
    const file = jarFileFor(hostname);
    if (!fs.existsSync(file)) return null;
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    return parsed && typeof parsed === 'object' ? (parsed as IdentityFile) : null;
  } catch {
    return null;
  }
}

function writeIdentity(hostname: string, file: IdentityFile): void {
  const dir = jarDir();
  fs.mkdirSync(dir, { recursive: true });
  try {
    fs.chmodSync(dir, 0o700);
  } catch {
    /* best-effort (e.g. non-POSIX) */
  }
  const target = jarFileFor(hostname);
  const tmp = `${target}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(file, null, 2), { mode: 0o600 });
  fs.renameSync(tmp, target);
}

/**
 * Save the current page's full cookie set, keyed by its hostname.
 * Returns the number of cookies saved (0 when disabled / nothing matched).
 */
export async function saveIdentity(page: Page, url: string): Promise<number> {
  if (!isEnabled()) return 0;
  const host = hostKey(url);
  if (host === '_unknown') return 0;

  let cookies: CookieParam[] = [];
  try {
    const all = await page.cookies();
    cookies = all
      .filter((c) => cookieBelongs(c.domain || '', host))
      .map((c) => ({
        name: c.name,
        value: c.value,
        domain: c.domain,
        path: c.path,
        expires: c.expires,
        httpOnly: c.httpOnly,
        secure: c.secure,
        sameSite: c.sameSite,
      })) as CookieParam[];
  } catch {
    return 0;
  }
  if (cookies.length === 0) return 0;

  let userAgent = '';
  try {
    userAgent = await page.browser().userAgent();
  } catch {
    /* advisory only */
  }

  const now = Date.now();
  const file: IdentityFile = {
    version: 1,
    domain: host,
    capturedAt: now,
    lastUsedAt: now,
    userAgent,
  };
  sealCookies(file, cookies);
  try {
    writeIdentity(host, file);
  } catch {
    return 0;
  }
  return cookies.length;
}

/**
 * Restore a saved identity before navigation. Skips expired files (TTL) and
 * individually filters out cookies whose own `expires` has passed. Returns
 * the number of cookies restored.
 */
export async function loadIdentity(page: Page, url: string): Promise<number> {
  if (!isEnabled()) return 0;
  const host = hostKey(url);
  if (host === '_unknown') return 0;
  const file = readIdentity(host);
  if (!file) return 0;
  if (Date.now() - file.capturedAt > ttlMs()) return 0;

  const nowSec = Date.now() / 1000;
  const cookies = openCookies(file).filter((c) => !c.expires || c.expires <= 0 || c.expires > nowSec);
  if (cookies.length === 0) return 0;
  try {
    await page.setCookie(...cookies);
  } catch {
    return 0;
  }
  file.lastUsedAt = Date.now();
  try {
    writeIdentity(host, file);
  } catch {
    /* best-effort refresh of lastUsedAt */
  }
  return cookies.length;
}

export function hasIdentity(url: string): boolean {
  return readIdentity(hostKey(url)) !== null;
}

/**
 * Refresh an EXISTING identity in place (never creates one). The autosave
 * seams call this so a saved login stays fresh as the site rotates cookies,
 * without silently persisting identities for every site the agent visits.
 */
export async function maybeRefreshIdentity(page: Page, url: string): Promise<number> {
  if (!isEnabled()) return 0;
  if (readIdentity(hostKey(url)) === null) return 0;
  return saveIdentity(page, url);
}

/**
 * Forget a site's saved logins. Because identities are hostname-keyed, a bare
 * domain ("example.com") removes that host AND all its subdomains
 * (www.example.com, api.example.com) — the intuitive "forget this site".
 * Returns true if anything was removed.
 */
export function deleteIdentity(domain: string): boolean {
  const target = hostKey(domain.includes('://') ? domain : `https://${domain}`);
  const key = (target === '_unknown' ? domain : target).toLowerCase();
  let removed = false;
  let files: string[] = [];
  try {
    files = fs.readdirSync(jarDir());
  } catch {
    return false;
  }
  for (const name of files) {
    if (!name.endsWith('.json')) continue;
    const host = name.slice(0, -'.json'.length).toLowerCase();
    if (host === key || host.endsWith(`.${key}`)) {
      try {
        fs.unlinkSync(path.join(jarDir(), name));
        removed = true;
      } catch {
        /* ignore */
      }
    }
  }
  return removed;
}

export function listIdentities(): Array<{
  domain: string;
  capturedAt: number;
  lastUsedAt: number;
  cookieCount: number;
  encrypted: boolean;
}> {
  const out: Array<{ domain: string; capturedAt: number; lastUsedAt: number; cookieCount: number; encrypted: boolean }> =
    [];
  let files: string[] = [];
  try {
    files = fs.readdirSync(jarDir());
  } catch {
    return out;
  }
  for (const name of files) {
    if (!name.endsWith('.json')) continue;
    try {
      const parsed = JSON.parse(fs.readFileSync(path.join(jarDir(), name), 'utf8')) as IdentityFile;
      out.push({
        domain: parsed.domain,
        capturedAt: parsed.capturedAt,
        lastUsedAt: parsed.lastUsedAt,
        cookieCount: parsed.enc ? -1 : (parsed.cookies || []).length,
        encrypted: !!parsed.enc,
      });
    } catch {
      /* skip corrupt */
    }
  }
  return out;
}
