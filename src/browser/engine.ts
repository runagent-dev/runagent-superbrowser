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
  /**
   * Legacy boolean headless flag. When `true`, old-style headless is used
   * UNLESS `headlessMode` is also set — prefer `headlessMode` in new code.
   * Retained for backward compatibility with callers that pass `{headless: false}`.
   */
  headless: boolean;
  /**
   * Controls Chromium's headless mode:
   *   'new'   — Puppeteer's --headless=new. Runtime matches headful closely;
   *             fewer detectable signatures than old headless. Default.
   *   'old'   — Legacy --headless. More detectable; kept for environments
   *             where 'new' crashes (rare).
   *   false   — Headful. Requires an X display; use only when running under
   *             Xvfb or a real desktop session.
   */
  headlessMode?: 'new' | 'old' | false;
  viewport: { width: number; height: number };
  userAgent?: string;
  proxy?: string;
  downloadDir: string;
  blockAds: boolean;
  stealth: boolean;
  executablePath?: string;
  /**
   * Disable the GPU via --disable-gpu. Default false (GPU enabled). Setting
   * this to true forces SwiftShader, whose WebGL vendor string is a known
   * bot signature. Only enable for environments where GPU crashes.
   */
  disableGpu?: boolean;
}

const DEFAULT_CONFIG: BrowserConfig = {
  headless: true,
  headlessMode: 'new',
  viewport: { width: 1280, height: 1100 },
  downloadDir: '/tmp/superbrowser/downloads',
  blockAds: true,
  stealth: true,
  disableGpu: false,
};

/** Chrome build identity for stealth — must stay in sync with stealth.ts. */
const CHROME_VERSION = '130.0.6723.91';
const CHROME_MAJOR = CHROME_VERSION.split('.')[0];
const DEFAULT_UA =
  `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ` +
  `(KHTML, like Gecko) Chrome/${CHROME_VERSION} Safari/537.36`;
const DEFAULT_PLATFORM: 'macOS' | 'Windows' | 'Linux' = 'macOS';

/**
 * Chrome launch flags for headless automation with enhanced stealth.
 *
 * GPU: `--disable-gpu` is deliberately OMITTED. Disabling GPU forces
 * Chromium to render via SwiftShader, whose WebGL UNMASKED_VENDOR is
 * `"Google Inc. (Google)"` / renderer `"ANGLE (...) SwiftShader ..."` —
 * a canonical bot-signature string pattern. Real desktop users rarely
 * run with GPU disabled. For headless environments where GPU must be
 * off (rare), set `disableGpu: true` on BrowserConfig explicitly.
 *
 * `--disable-accelerated-2d-canvas` is likewise OMITTED — it's closely
 * coupled with `--disable-gpu` in fingerprint impact.
 */
const CHROME_FLAGS = [
  '--disable-blink-features=AutomationControlled',
  '--disable-features=IsolateOrigins,site-per-process,AutomationControlled',
  '--disable-setuid-sandbox',
  '--no-sandbox',
  '--disable-dev-shm-usage',
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

    // Opt-in GPU-disable for environments where Chromium can't use a GPU.
    // Defaults to enabled — disabling GPU forces SwiftShader, a known
    // bot-signature renderer string.
    if (this.config.disableGpu) {
      args.push('--disable-gpu');
      args.push('--disable-accelerated-2d-canvas');
    }

    // Resolve headless mode. `headlessMode` takes precedence when set; the
    // legacy `headless` boolean is honored for callers that still use it.
    // Puppeteer accepts `true | false | 'new'` (and historically 'chrome').
    // We map 'old' to `true` (legacy headless), 'new' to the string 'new',
    // and false to headful.
    const mode = this.config.headlessMode;
    let headlessOpt: boolean | 'new';
    if (mode === 'new') headlessOpt = 'new';
    else if (mode === 'old') headlessOpt = true;
    else if (mode === false) headlessOpt = false;
    else headlessOpt = this.config.headless ? 'new' : false;

    const launchOptions: Record<string, unknown> = {
      headless: headlessOpt,
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

    // Mutation counter: installs window.__nb_mutation_counter and a
    // MutationObserver that bumps it on any childList/attribute change.
    // Used by post-action effect verification — the HTTP mutation
    // handlers read this before + after an action and surface the
    // delta so the Python side can tell "Puppeteer dispatched" apart
    // from "the page actually changed." characterData is deliberately
    // excluded (fires on every text tick in chat apps, would tank CPU
    // on Slack/Notion); childList+subtree+attributes is sufficient for
    // React re-renders and DOM-level autocomplete state changes.
    // Shim `__name` so arrow functions compiled by tsx/esbuild (which
    // wraps every function in `__name(fn, 'name')` for name
    // preservation) don't hit `ReferenceError: __name is not defined`
    // when Puppeteer serializes them into the browser context. This
    // manifested as HTTP 500s on /session/:id/click — semantic_click
    // couldn't dismiss any popup. Keep it before the mutation observer
    // install so that instrumentation itself doesn't trip the shim
    // load order. `fn => fn` preserves behaviour — the `__name` helper
    // is a decorator-style identity wrapper in practice.
    await page.evaluateOnNewDocument(`
      (function () {
        try {
          if (typeof window === 'undefined') return;
          if (typeof window.__name !== 'function') {
            Object.defineProperty(window, '__name', {
              value: function (fn) { return fn; },
              configurable: true, writable: true,
            });
          }
        } catch (_) { /* silent */ }
      })();
    `);

    await page.evaluateOnNewDocument(`
      (function () {
        try {
          if (typeof window === 'undefined') return;
          if (window.__nb_mutation_counter_installed) return;
          window.__nb_mutation_counter_installed = true;
          window.__nb_mutation_counter = 0;
          var obs = new MutationObserver(function () {
            window.__nb_mutation_counter = (window.__nb_mutation_counter || 0) + 1;
          });
          var start = function () {
            if (document.documentElement) {
              obs.observe(document.documentElement, {
                childList: true, subtree: true, attributes: true,
              });
            }
          };
          if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', start, { once: true });
          } else {
            start();
          }
        } catch (e) { /* never let instrumentation break the page */ }
      })();
    `);

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
