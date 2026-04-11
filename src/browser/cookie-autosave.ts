/**
 * Auto-save cookies when cf_clearance is detected via CDP network events.
 *
 * Monitors Set-Cookie headers for cf_clearance and triggers an immediate
 * cookie save to COOKIE_DIR. Also runs a periodic 30s save to catch
 * other important cookies (session tokens, auth tokens).
 *
 * Pattern inspired by browser-use's StorageStateWatchdog.
 */

import fs from 'fs';
import path from 'path';
import type { Page, CDPSession } from 'puppeteer-core';

const COOKIE_DIR = process.env.COOKIE_DIR || '/tmp/superbrowser/cookies';
const SAVE_INTERVAL_MS = 30_000; // 30 seconds

interface CookieAutoSaveOptions {
  /** Only auto-save cookies for this domain (optional filter). */
  domain?: string;
}

export class CookieAutoSave {
  private page: Page;
  private cdpSession: CDPSession | null = null;
  private interval: ReturnType<typeof setInterval> | null = null;
  private domain: string | undefined;
  private stopped = false;

  constructor(page: Page, options?: CookieAutoSaveOptions) {
    this.page = page;
    this.domain = options?.domain;
  }

  /** Start monitoring for cf_clearance cookies and periodic save. */
  async start(): Promise<void> {
    fs.mkdirSync(COOKIE_DIR, { recursive: true });

    try {
      this.cdpSession = await this.page.createCDPSession();
      await this.cdpSession.send('Network.enable');

      // Listen for Set-Cookie headers containing bot protection cookies
      const PROTECTION_PATTERNS = ['cf_clearance', '__cf_bm', '_px', '_pxhd', '_pxvid', '_pxde'];
      this.cdpSession.on('Network.responseReceivedExtraInfo', (params: any) => {
        if (this.stopped) return;
        const setCookie = params.headers?.['set-cookie'] || params.headers?.['Set-Cookie'] || '';
        const matched = PROTECTION_PATTERNS.find(p => setCookie.includes(p));
        if (matched) {
          console.log(`[cookie-autosave] Protection cookie "${matched}" detected — saving immediately`);
          this.saveCookies().catch(() => {});
        }
      });
    } catch (err) {
      // CDP session creation may fail for some pages — fall back to periodic only
      console.log(`[cookie-autosave] CDP monitoring unavailable: ${err}`);
    }

    // Periodic save every 30 seconds
    this.interval = setInterval(() => {
      if (!this.stopped) {
        this.saveCookies().catch(() => {});
      }
    }, SAVE_INTERVAL_MS);
  }

  /** Save all cookies grouped by domain. */
  async saveCookies(): Promise<void> {
    try {
      const cookies = await this.page.cookies();
      if (!cookies.length) return;

      const byDomain: Record<string, any[]> = {};
      for (const c of cookies) {
        const domain = (c.domain || '').replace(/^\./, '');
        if (!domain) continue;
        // If a domain filter is set, only save cookies for that domain and its parent
        if (this.domain && !domain.includes(this.domain) && !this.domain.includes(domain)) continue;
        if (!byDomain[domain]) byDomain[domain] = [];
        byDomain[domain].push(c);
      }

      for (const [domain, domainCookies] of Object.entries(byDomain)) {
        const safeName = domain.replace(/[^a-zA-Z0-9.-]/g, '_');
        const filePath = path.join(COOKIE_DIR, `${safeName}.json`);

        // Merge with existing cookies (don't overwrite cookies from other sessions)
        let existing: any[] = [];
        try {
          if (fs.existsSync(filePath)) {
            existing = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
          }
        } catch {}

        // Merge: new cookies override existing ones with same name+path
        const merged = new Map<string, any>();
        for (const c of existing) {
          merged.set(`${c.name}:${c.path || '/'}`, c);
        }
        for (const c of domainCookies) {
          merged.set(`${c.name}:${c.path || '/'}`, c);
        }

        fs.writeFileSync(filePath, JSON.stringify(Array.from(merged.values()), null, 2));
      }
    } catch (err) {
      // Best-effort — don't crash the session
    }
  }

  /** Stop monitoring and clean up. */
  stop(): void {
    this.stopped = true;
    if (this.interval) {
      clearInterval(this.interval);
      this.interval = null;
    }
    // Final save on stop
    this.saveCookies().catch(() => {});
  }
}
