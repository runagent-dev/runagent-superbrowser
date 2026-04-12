/**
 * Browser lifecycle manager.
 *
 * Launches Chromium via puppeteer-extra with stealth plugin,
 * manages browser contexts, and handles cleanup.
 * Patterns from browserless (browsers.cdp.ts).
 */

import { EventEmitter } from 'events';
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
  viewport: { width: 1280, height: 1100 },
  downloadDir: '/tmp/superbrowser/downloads',
  blockAds: true,
  stealth: true,
};

/** Chrome build identity for stealth — must stay in sync with stealth.ts. */
const CHROME_VERSION = '130.0.6723.91';
const CHROME_MAJOR = CHROME_VERSION.split('.')[0];
const DEFAULT_UA =
  `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ` +
  `(KHTML, like Gecko) Chrome/${CHROME_VERSION} Safari/537.36`;
const DEFAULT_PLATFORM: 'macOS' | 'Windows' | 'Linux' = 'macOS';

/** Chrome launch flags for headless automation with enhanced stealth. */
const CHROME_FLAGS = [
  '--disable-blink-features=AutomationControlled',
  '--disable-features=IsolateOrigins,site-per-process,AutomationControlled',
  '--disable-setuid-sandbox',
  '--no-sandbox',
  '--disable-dev-shm-usage',
  '--disable-accelerated-2d-canvas',
  '--disable-gpu',
  '--no-first-run',
  '--no-zygote',
  '--disable-extensions',
  '--disable-background-networking',
  '--disable-default-apps',
  '--disable-sync',
  '--disable-translate',
  '--metrics-recording-only',
  '--mute-audio',
  '--no-default-browser-check',
  '--enable-features=NetworkService,NetworkServiceInProcess',
  // Additional anti-detection flags (from browser-use patterns)
  '--disable-infobars',
  '--disable-background-timer-throttling',
  '--disable-backgrounding-occluded-windows',
  '--disable-renderer-backgrounding',
  '--disable-hang-monitor',
  '--disable-ipc-flooding-protection',
  '--disable-component-update',
  '--disable-domain-reliability',
  // Locale consistency — matches navigator.languages in stealth.ts.
  '--lang=en-US',
  '--accept-lang=en-US,en;q=0.9',
];

/** Auto-detect Chrome/Chromium binary path. */
function findChromePath(): string | undefined {
  const fs = require('fs');
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
      `--window-size=${this.config.viewport.width},${this.config.viewport.height}`,
    ];

    if (this.config.proxy) {
      args.push(`--proxy-server=${this.config.proxy}`);
    }

    const launchOptions: Record<string, unknown> = {
      headless: this.config.headless,
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

    // Per-session seed so canvas/audio fingerprint noise is stable within
    // a session but differs across sessions.
    const sessionSeed = Math.floor(Math.random() * 2147483647);

    // Inject stealth scripts before any page script
    if (this.config.stealth) {
      await page.evaluateOnNewDocument(
        getStealthScript({
          sessionSeed,
          chromeVersion: CHROME_VERSION,
          platform: DEFAULT_PLATFORM,
        }),
      );
      await page.evaluateOnNewDocument(getPlatformOverrideScript());
    }

    // Set viewport
    await page.setViewport(this.config.viewport);

    // Set user agent — derived from CHROME_VERSION so stealth UA hints stay in sync.
    const ua = this.config.userAgent || DEFAULT_UA;
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
