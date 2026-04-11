/**
 * Browser lifecycle manager.
 *
 * Launches Chromium via puppeteer-extra with stealth plugin,
 * manages browser contexts, and handles cleanup.
 * Patterns from browserless (browsers.cdp.ts).
 */

import { EventEmitter } from 'events';
import fs from 'fs';
import os from 'os';
import puppeteerExtra from 'puppeteer-extra';
import StealthPlugin from 'puppeteer-extra-plugin-stealth';
import type { Browser, Page } from 'puppeteer-core';

const puppeteer = puppeteerExtra as any;
import { getStealthScript, getPlatformOverrideScript } from './stealth.js';
import { PageWrapper } from './page.js';

puppeteer.use(StealthPlugin() as any);

export interface BrowserConfig {
  headless: boolean;
  viewport: { width: number; height: number };
  userAgent?: string;
  proxy?: string;
  downloadDir: string;
  blockAds: boolean;
  stealth: boolean;
  executablePath?: string;
}

const DEFAULT_CONFIG: BrowserConfig = {
  headless: true,
  viewport: { width: 1440, height: 900 },
  downloadDir: '/tmp/superbrowser/downloads',
  blockAds: true,
  stealth: true,
};

/**
 * Chrome launch flags — minimal set following BrowserOS pattern.
 *
 * Cloudflare and similar bot detectors fingerprint Chrome flags.
 * Flags like --disable-blink-features, --excludeSwitches, --disable-infobars
 * are specifically flagged as automation indicators. We keep only what's
 * strictly necessary for headless operation in containers.
 *
 * When AGGRESSIVE_STEALTH is not 'false' (default: enabled), additional
 * flags from browser-use are added. The most critical is
 * --disable-blink-features=AutomationControlled which prevents the
 * webdriver flag from being set at the browser level (JS-level masking
 * in stealth.ts is detectable by timing checks).
 */
const CHROME_FLAGS = [
  // Essential for container/headless operation
  '--no-sandbox',
  '--disable-setuid-sandbox',
  '--disable-dev-shm-usage',
  // Realistic browser behavior (BrowserOS uses these)
  '--no-first-run',
  '--no-default-browser-check',
  '--use-mock-keychain',
  // Reduce noise without looking suspicious
  '--mute-audio',
  '--disable-default-apps',
];

/** Additional stealth flags ported from browser-use (browser/profile.py). */
const AGGRESSIVE_STEALTH_FLAGS = [
  '--disable-blink-features=AutomationControlled',
  '--disable-features=AutomationControlled,BackForwardCache',
  '--disable-infobars',
  '--disable-popup-blocking',
  '--disable-sync',
  '--disable-hang-monitor',
  '--disable-component-update',
  '--disable-domain-reliability',
  '--disable-background-timer-throttling',
  '--disable-backgrounding-occluded-windows',
  '--disable-ipc-flooding-protection',
];

/** Persistent browser profile directory. Set BROWSER_PROFILE env var to persist login sessions. */
const BROWSER_PROFILE_DIR = process.env.BROWSER_PROFILE || '';

/** Auto-detect Chrome/Chromium binary path. */
function findChromePath(): string | undefined {
  const candidates = [
    '/usr/bin/google-chrome-stable',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium-browser',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/local/bin/chromium',
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  return undefined;
}

export class BrowserEngine extends EventEmitter {
  private browser: Browser | null = null;
  private config: BrowserConfig;
  private running = false;

  constructor(config: Partial<BrowserConfig> = {}) {
    super();
    this.config = { ...DEFAULT_CONFIG, ...config };
  }

  /** Launch Chromium with stealth and configured options. */
  async launch(): Promise<void> {
    if (this.running) return;

    const args = [
      ...CHROME_FLAGS,
      ...(process.env.AGGRESSIVE_STEALTH !== 'false' ? AGGRESSIVE_STEALTH_FLAGS : []),
      `--window-size=${this.config.viewport.width},${this.config.viewport.height}`,
    ];

    if (this.config.proxy) {
      args.push(`--proxy-server=${this.config.proxy}`);
    }

    // Persistent browser profile — retains cookies/sessions across restarts
    // Set BROWSER_PROFILE=/path/to/profile to enable
    if (BROWSER_PROFILE_DIR) {
      fs.mkdirSync(BROWSER_PROFILE_DIR, { recursive: true });
      args.push(`--user-data-dir=${BROWSER_PROFILE_DIR}`);
      console.log(`[engine] Using persistent profile: ${BROWSER_PROFILE_DIR}`);
    }

    const launchOptions: Record<string, unknown> = {
      // Use 'new' headless mode — much harder to detect than old headless
      headless: this.config.headless ? 'new' : false,
      args,
      defaultViewport: this.config.viewport,
      ignoreHTTPSErrors: true,
    };

    // Set executable path — explicit config, env var, or auto-detect
    const execPath = this.config.executablePath || findChromePath();
    if (execPath) {
      launchOptions.executablePath = execPath;
    }

    this.browser = await puppeteer.launch(launchOptions) as Browser;

    this.browser.on('disconnected', () => {
      this.running = false;
      this.emit('disconnected');
    });

    this.running = true;
    this.emit('launched');
  }

  /** Create a new page with stealth scripts applied. Auto-relaunches if crashed. */
  async newPage(): Promise<PageWrapper> {
    // Auto-recovery: relaunch if browser crashed
    if (!this.browser || !this.running) {
      console.log('Browser not running — auto-relaunching...');
      this.browser = null;
      this.running = false;
      await this.launch();
    }
    if (!this.browser) throw new Error('Failed to launch browser');

    const page = await this.browser.newPage();

    // Detect actual OS for consistent fingerprint (mismatched platform is a detection signal)
    const isLinux = os.platform() === 'linux';
    const isMac = os.platform() === 'darwin';

    // Inject stealth scripts before any page script
    if (this.config.stealth) {
      const platform = isMac ? 'MacIntel' : (isLinux ? 'Linux x86_64' : 'Win32');
      await page.evaluateOnNewDocument(getStealthScript(isLinux));
      await page.evaluateOnNewDocument(getPlatformOverrideScript(platform));
    }

    // Set viewport
    await page.setViewport(this.config.viewport);

    // Set user agent — match actual OS for consistent fingerprint
    const ua = this.config.userAgent
      || (isLinux
        ? 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36'
        : 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36');
    await page.setUserAgent(ua);

    // Setup CDP session for download behavior
    const client = await page.createCDPSession();
    await client.send('Page.setDownloadBehavior', {
      behavior: 'allow',
      downloadPath: this.config.downloadDir,
    });

    // Block heavy resources if ad-blocking enabled
    if (this.config.blockAds) {
      await page.setRequestInterception(true);
      page.on('request', (req) => {
        const resourceType = req.resourceType();
        const url = req.url();

        // NEVER block Cloudflare challenge resources — they are required for bypass
        const cfWhitelist = [
          /challenges\.cloudflare\.com/,
          /cloudflare\.com\/cdn-cgi/,
          /turnstile/,
          /cf-beacon/,
        ];
        if (cfWhitelist.some((p) => p.test(url))) {
          req.continue().catch(() => {});
          return;
        }

        // Block common ad/tracker domains and heavy resources
        const blockedTypes = ['media', 'font'];
        const blockedPatterns = [
          /doubleclick\.net/,
          /google-analytics\.com/,
          /googletagmanager\.com/,
          /facebook\.net.*\/tr/,
          /analytics/,
          /adservice/,
        ];

        if (blockedTypes.includes(resourceType)) {
          req.abort().catch(() => {});
          return;
        }

        if (blockedPatterns.some((p) => p.test(url))) {
          req.abort().catch(() => {});
          return;
        }

        req.continue().catch(() => {});
      });
    }

    const wrapper = new PageWrapper(page, this.config);
    this.emit('newPage', wrapper);
    return wrapper;
  }

  /** Get the raw browser instance. */
  getBrowser(): Browser | null {
    return this.browser;
  }

  /** Check if browser is running. */
  isRunning(): boolean {
    return this.running;
  }

  /** Close browser and cleanup. */
  async close(): Promise<void> {
    if (this.browser) {
      try {
        await this.browser.close();
      } catch {
        // Browser may already be closed
      }
      this.browser = null;
      this.running = false;
      this.emit('closed');
    }
  }
}
