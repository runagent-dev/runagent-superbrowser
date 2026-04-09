/**
 * Unified navigation utility from browserless.
 *
 * Handles both URL and HTML content navigation with full
 * page setup options (cookies, auth, headers, scripts, etc.).
 */

import type { Page, HTTPResponse, CookieParam } from 'puppeteer-core';

export interface GotoOptions {
  waitUntil?: 'load' | 'domcontentloaded' | 'networkidle0' | 'networkidle2';
  timeout?: number;
  referer?: string;
}

export interface NavigationOptions {
  /** URL to navigate to (mutually exclusive with html). */
  url?: string;

  /** HTML content to set (mutually exclusive with url). */
  html?: string;

  /** Navigation options. */
  gotoOptions?: GotoOptions;

  // --- Page setup (from browserless screenshot/pdf/scrape APIs) ---

  /** Cookies to set before navigation. */
  cookies?: CookieParam[];

  /** HTTP basic authentication credentials. */
  authenticate?: { username: string; password: string };

  /** Extra HTTP headers to set. */
  setExtraHTTPHeaders?: Record<string, string>;

  /** Custom user agent string. */
  userAgent?: string;

  /** Custom viewport dimensions. */
  viewport?: { width: number; height: number; deviceScaleFactor?: number };

  /** Emulate media type (print or screen). */
  emulateMediaType?: 'print' | 'screen' | null;

  /** Whether JavaScript is enabled (default: true). */
  setJavaScriptEnabled?: boolean;

  /** Script tags to inject after page load. */
  addScriptTag?: Array<{ url?: string; path?: string; content?: string }>;

  /** Style tags to inject after page load. */
  addStyleTag?: Array<{ url?: string; path?: string; content?: string }>;

  // --- Request interception (from browserless) ---

  /** Regex patterns to block (matched against request URLs). */
  rejectRequestPattern?: string[];

  /** Resource types to block (e.g., 'image', 'stylesheet', 'font'). */
  rejectResourceTypes?: string[];

  /** Custom request interceptors. */
  requestInterceptors?: Array<{
    pattern: string;
    response: {
      status?: number;
      contentType?: string;
      body: string;
    };
  }>;

  // --- Wait conditions (from browserless) ---

  /** Wait N ms after navigation. */
  waitForTimeout?: number;

  /** Wait for a CSS selector to appear. */
  waitForSelector?: { selector: string; timeout?: number; visible?: boolean; hidden?: boolean };

  /** Wait for a JS function to return true. */
  waitForFunction?: { fn: string; timeout?: number; polling?: number };

  /** Wait for a browser event. */
  waitForEvent?: { event: string; timeout?: number };

  /** If true, ignore wait condition failures. */
  bestAttempt?: boolean;

  /** Scroll the full page before capturing. */
  scrollPage?: boolean;
}

export interface NavigationResult {
  /** The HTTP response from navigation. */
  response: HTTPResponse | null;

  /** Response status code. */
  statusCode: number;

  /** Final URL after redirects. */
  url: string;

  /** Response headers. */
  headers: Record<string, string>;
}

/**
 * Navigate to a URL or set HTML content with full setup options.
 * This is the unified navigation function used by all browserless routes.
 */
export async function goto(
  page: Page,
  options: NavigationOptions,
): Promise<NavigationResult> {
  const gotoOpts = options.gotoOptions || {};
  const waitUntil = gotoOpts.waitUntil || 'domcontentloaded';
  const timeout = gotoOpts.timeout || 30000;

  // --- Pre-navigation setup ---

  // Viewport
  if (options.viewport) {
    await page.setViewport(options.viewport);
  }

  // User agent
  if (options.userAgent) {
    await page.setUserAgent(options.userAgent);
  }

  // Cookies
  if (options.cookies && options.cookies.length > 0) {
    await page.setCookie(...options.cookies);
  }

  // Authentication
  if (options.authenticate) {
    await page.authenticate(options.authenticate);
  }

  // Extra HTTP headers
  if (options.setExtraHTTPHeaders) {
    await page.setExtraHTTPHeaders(options.setExtraHTTPHeaders);
  }

  // JavaScript enable/disable
  if (options.setJavaScriptEnabled !== undefined) {
    await page.setJavaScriptEnabled(options.setJavaScriptEnabled);
  }

  // Media type emulation
  if (options.emulateMediaType !== undefined) {
    await page.emulateMediaType(options.emulateMediaType || undefined);
  }

  // --- Request interception ---
  const hasInterception =
    (options.rejectRequestPattern && options.rejectRequestPattern.length > 0) ||
    (options.rejectResourceTypes && options.rejectResourceTypes.length > 0) ||
    (options.requestInterceptors && options.requestInterceptors.length > 0);

  if (hasInterception) {
    await page.setRequestInterception(true);

    const rejectPatterns = (options.rejectRequestPattern || []).map((p) => new RegExp(p));
    const rejectTypes = new Set(options.rejectResourceTypes || []);
    const interceptors = options.requestInterceptors || [];

    page.on('request', (req) => {
      const url = req.url();
      const resourceType = req.resourceType();

      // Check reject patterns
      if (rejectPatterns.some((p) => p.test(url))) {
        req.abort().catch(() => {});
        return;
      }

      // Check reject resource types
      if (rejectTypes.has(resourceType)) {
        req.abort().catch(() => {});
        return;
      }

      // Check custom interceptors
      for (const interceptor of interceptors) {
        if (new RegExp(interceptor.pattern).test(url)) {
          req.respond({
            status: interceptor.response.status || 200,
            contentType: interceptor.response.contentType || 'text/plain',
            body: interceptor.response.body,
          }).catch(() => {});
          return;
        }
      }

      req.continue().catch(() => {});
    });
  }

  // --- Navigate ---
  let response: HTTPResponse | null = null;

  if (options.url) {
    response = await page.goto(options.url, {
      waitUntil,
      timeout,
      referer: gotoOpts.referer,
    });
  } else if (options.html) {
    await page.setContent(options.html, { waitUntil, timeout });
  }

  // --- Post-navigation setup ---

  // Inject script tags
  if (options.addScriptTag) {
    for (const tag of options.addScriptTag) {
      await page.addScriptTag(tag);
    }
  }

  // Inject style tags
  if (options.addStyleTag) {
    for (const tag of options.addStyleTag) {
      await page.addStyleTag(tag);
    }
  }

  // --- Wait conditions ---
  const bestAttemptCatch = options.bestAttempt
    ? (err: unknown) => { /* ignore */ }
    : undefined;

  if (options.waitForTimeout) {
    await new Promise((r) => setTimeout(r, options.waitForTimeout));
  }

  if (options.waitForSelector) {
    const { selector, timeout: selectorTimeout, visible, hidden } = options.waitForSelector;
    try {
      await page.waitForSelector(selector, {
        timeout: selectorTimeout || 10000,
        visible,
        hidden,
      });
    } catch (err) {
      if (bestAttemptCatch) bestAttemptCatch(err);
      else throw err;
    }
  }

  if (options.waitForFunction) {
    const { fn, timeout: fnTimeout, polling } = options.waitForFunction;
    try {
      await page.waitForFunction(fn, {
        timeout: fnTimeout || 10000,
        polling: polling || 'raf',
      });
    } catch (err) {
      if (bestAttemptCatch) bestAttemptCatch(err);
      else throw err;
    }
  }

  if (options.waitForEvent) {
    const { event, timeout: eventTimeout } = options.waitForEvent;
    try {
      await Promise.race([
        page.evaluate(
          (evt: string) =>
            new Promise<void>((resolve) => {
              document.addEventListener(evt, () => resolve(), { once: true });
            }),
          event,
        ),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error(`Timed out waiting for event: ${event}`)), eventTimeout || 10000),
        ),
      ]);
    } catch (err) {
      if (bestAttemptCatch) bestAttemptCatch(err);
      else throw err;
    }
  }

  // --- Full page scroll ---
  if (options.scrollPage) {
    await autoScroll(page);
  }

  // --- Build result ---
  const statusCode = response?.status() || 0;
  const finalUrl = response?.url() || page.url();
  const responseHeaders: Record<string, string> = {};
  if (response) {
    const headers = response.headers();
    for (const [key, value] of Object.entries(headers)) {
      responseHeaders[key] = value;
    }
  }

  return {
    response,
    statusCode,
    url: finalUrl,
    headers: responseHeaders,
  };
}

/**
 * Scroll the full page to trigger lazy-loaded content.
 */
async function autoScroll(page: Page): Promise<void> {
  await page.evaluate(async () => {
    await new Promise<void>((resolve) => {
      let totalHeight = 0;
      const distance = 100;
      const timer = setInterval(() => {
        const scrollHeight = document.body.scrollHeight;
        window.scrollBy(0, distance);
        totalHeight += distance;
        if (totalHeight >= scrollHeight) {
          clearInterval(timer);
          window.scrollTo(0, 0);
          resolve();
        }
      }, 100);
    });
  });
}
