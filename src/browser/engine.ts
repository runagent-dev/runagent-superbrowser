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

/** Chrome launch flags for headless automation. */
const CHROME_FLAGS = [
  '--disable-blink-features=AutomationControlled',
  '--disable-features=IsolateOrigins,site-per-process',
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

    // Inject stealth scripts before any page script
    if (this.config.stealth) {
      await page.evaluateOnNewDocument(getStealthScript());
      await page.evaluateOnNewDocument(getPlatformOverrideScript());
    }

    // Set viewport
    await page.setViewport(this.config.viewport);

    // Set user agent if configured
    if (this.config.userAgent) {
      await page.setUserAgent(this.config.userAgent);
    }

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
