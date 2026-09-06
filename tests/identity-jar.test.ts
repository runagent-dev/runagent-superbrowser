/**
 * identity-jar.ts tests. A fake puppeteer Page carries an in-memory cookie
 * store so save/load/TTL/encryption round-trip without a real browser. Each
 * test isolates HOME so it writes into a temp ~/.superbrowser/identities.
 */

import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  deleteIdentity,
  hasIdentity,
  listIdentities,
  loadIdentity,
  maybeRefreshIdentity,
  saveIdentity,
} from '../src/browser/identity-jar.js';

type Cookie = {
  name: string;
  value: string;
  domain: string;
  path?: string;
  expires?: number;
  httpOnly?: boolean;
  secure?: boolean;
};

class FakePage {
  private store: Cookie[];
  private ua: string;
  constructor(cookies: Cookie[], ua = 'Mozilla/5.0 test') {
    this.store = cookies;
    this.ua = ua;
  }
  async cookies(): Promise<Cookie[]> {
    return this.store;
  }
  async setCookie(...cookies: Cookie[]): Promise<void> {
    for (const c of cookies) this.store.push(c);
  }
  browser() {
    return { userAgent: async () => this.ua };
  }
  getStore(): Cookie[] {
    return this.store;
  }
}

let tmpHome: string;
let origHome: string | undefined;
let origEnabled: string | undefined;

beforeEach(() => {
  tmpHome = mkdtempSync(join(os.tmpdir(), 'sb-identity-'));
  origHome = process.env.HOME;
  origEnabled = process.env.SUPERBROWSER_IDENTITY_JAR;
  process.env.HOME = tmpHome;
  process.env.SUPERBROWSER_IDENTITY_JAR = '1';
  delete process.env.SUPERBROWSER_IDENTITY_KEY;
  delete process.env.SUPERBROWSER_IDENTITY_TTL_DAYS;
});

afterEach(() => {
  process.env.HOME = origHome;
  if (origEnabled === undefined) delete process.env.SUPERBROWSER_IDENTITY_JAR;
  else process.env.SUPERBROWSER_IDENTITY_JAR = origEnabled;
  rmSync(tmpHome, { recursive: true, force: true });
});

const loginCookies = (): Cookie[] => [
  { name: 'session', value: 'abc123', domain: '.example.com' },
  { name: 'csrf', value: 'tok', domain: 'www.example.com' },
  { name: 'other', value: 'x', domain: 'unrelated.org' }, // filtered out
];

describe('identity-jar', () => {
  it('is a no-op when disabled', async () => {
    process.env.SUPERBROWSER_IDENTITY_JAR = '0';
    const page = new FakePage(loginCookies());
    expect(await saveIdentity(page as never, 'https://www.example.com/account')).toBe(0);
    expect(hasIdentity('https://www.example.com')).toBe(false);
  });

  it('saves hostname-scoped cookies (incl. parent-domain) and restores them', async () => {
    const saver = new FakePage(loginCookies());
    const saved = await saveIdentity(saver as never, 'https://www.example.com/account');
    expect(saved).toBe(2); // .example.com + www.example.com; 'other' on unrelated.org excluded
    expect(hasIdentity('https://www.example.com')).toBe(true);

    const fresh = new FakePage([]);
    const restored = await loadIdentity(fresh as never, 'https://www.example.com/');
    expect(restored).toBe(2);
    expect(fresh.getStore().map((c) => c.name).sort()).toEqual(['csrf', 'session']);
  });

  it('keys by full hostname — different subdomains do not clobber', async () => {
    await saveIdentity(new FakePage([{ name: 'a', value: '1', domain: 'www.example.com' }]) as never, 'https://www.example.com/');
    await saveIdentity(new FakePage([{ name: 'b', value: '2', domain: 'shop.example.com' }]) as never, 'https://shop.example.com/');
    const listed = listIdentities().map((i) => i.domain).sort();
    expect(listed).toEqual(['shop.example.com', 'www.example.com']);
    // forget the whole site — removes both subdomains
    expect(deleteIdentity('example.com')).toBe(true);
    expect(listIdentities()).toHaveLength(0);
  });

  it('multi-label TLD sites stay independent (no co.uk collapse)', async () => {
    await saveIdentity(new FakePage([{ name: 'amz', value: '1', domain: 'www.amazon.co.uk' }]) as never, 'https://www.amazon.co.uk/');
    await saveIdentity(new FakePage([{ name: 'tsc', value: '2', domain: 'www.tesco.co.uk' }]) as never, 'https://www.tesco.co.uk/');
    // both survive — a naive registrable-domain (co.uk) would have clobbered one
    expect(listIdentities()).toHaveLength(2);
    const amz = new FakePage([]);
    expect(await loadIdentity(amz as never, 'https://www.amazon.co.uk/')).toBe(1);
    expect(amz.getStore()[0].name).toBe('amz');
  });

  it('lists and deletes identities', async () => {
    await saveIdentity(new FakePage(loginCookies()) as never, 'https://www.example.com/');
    const listed = listIdentities();
    expect(listed).toHaveLength(1);
    expect(listed[0].domain).toBe('www.example.com');
    expect(listed[0].cookieCount).toBe(2);
    expect(deleteIdentity('www.example.com')).toBe(true);
    expect(hasIdentity('https://www.example.com')).toBe(false);
  });

  it('skips restore past the TTL', async () => {
    process.env.SUPERBROWSER_IDENTITY_TTL_DAYS = '0'; // clamps to default 30, so force via file age instead
    await saveIdentity(new FakePage(loginCookies()) as never, 'https://www.example.com/');
    // simulate a very old capture by reaching into the file
    const fs = await import('node:fs');
    const file = join(tmpHome, '.superbrowser', 'identities', 'www.example.com.json');
    const data = JSON.parse(fs.readFileSync(file, 'utf8'));
    data.capturedAt = Date.now() - 40 * 24 * 60 * 60 * 1000; // 40 days ago
    fs.writeFileSync(file, JSON.stringify(data));
    const restored = await loadIdentity(new FakePage([]) as never, 'https://www.example.com/');
    expect(restored).toBe(0);
  });

  it('filters individually-expired cookies on restore', async () => {
    const past = Date.now() / 1000 - 100;
    const future = Date.now() / 1000 + 100000;
    const page = new FakePage([
      { name: 'live', value: '1', domain: '.example.com', expires: future },
      { name: 'dead', value: '2', domain: '.example.com', expires: past },
    ]);
    await saveIdentity(page as never, 'https://www.example.com/');
    const fresh = new FakePage([]);
    const restored = await loadIdentity(fresh as never, 'https://www.example.com/');
    expect(restored).toBe(1);
    expect(fresh.getStore().map((c) => c.name)).toEqual(['live']);
  });

  it('round-trips through AES-256-GCM when a key is set', async () => {
    process.env.SUPERBROWSER_IDENTITY_KEY = 'a'.repeat(64); // 32 bytes hex
    await saveIdentity(new FakePage(loginCookies()) as never, 'https://www.example.com/');
    const listed = listIdentities();
    expect(listed[0].encrypted).toBe(true);

    const fresh = new FakePage([]);
    expect(await loadIdentity(fresh as never, 'https://www.example.com/')).toBe(2);

    // wrong key -> behaves like no identity, never throws
    process.env.SUPERBROWSER_IDENTITY_KEY = 'b'.repeat(64);
    expect(await loadIdentity(new FakePage([]) as never, 'https://www.example.com/')).toBe(0);
  });

  it('maybeRefreshIdentity only refreshes existing identities', async () => {
    // no identity yet -> no-op
    expect(await maybeRefreshIdentity(new FakePage(loginCookies()) as never, 'https://www.example.com/')).toBe(0);
    expect(hasIdentity('https://www.example.com')).toBe(false);
    // after a save, refresh works
    await saveIdentity(new FakePage(loginCookies()) as never, 'https://www.example.com/');
    expect(await maybeRefreshIdentity(new FakePage(loginCookies()) as never, 'https://www.example.com/')).toBe(2);
  });
});
