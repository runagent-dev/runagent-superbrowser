/**
 * Page wrapper with high-level browser operations.
 *
 * Combines patterns from browserless (screenshot/navigation),
 * nanobrowser (getState, clickElementNode, inputTextElementNode),
 * and BrowserOS (dialog handling, console capture, file upload, PDF).
 */

import type { Page, CDPSession } from 'puppeteer-core';
import type { BrowserConfig } from './engine.js';
import { buildDomTree, type PageState, type DialogInfo, type DOMElementNode } from './dom.js';
import { getAccessibilitySnapshot } from './accessibility.js';
import { dispatchClick, dispatchHover, dispatchDrag, dispatchScroll } from './input-mouse.js';
import { typeText as cdpTypeText, pressKeyCombo, clearField } from './input-keyboard.js';
import { getElementCenterBySelector } from './elements.js';
import { findCursorInteractiveElements, formatCursorElements } from './cursor-detect.js';
import { ConsoleCollector, type CollectedLog } from './console-collector.js';
import { DownloadMonitor } from './download-monitor.js';
import { validateUrl } from '../server/auth.js';
import type { FailureReason } from '../agent/types.js';
import { sanitizeImageBuffer } from './image-safety.js';
import { waitForPageReady, detectErrorPage, type ErrorPage } from './page-readiness.js';
import { feedbackBus } from '../agent/feedback-bus.js';

/**
 * Structured result of a tool invocation at the browser layer. Designed so
 * the LLM can pick a different tactic on failure without re-screenshotting
 * to re-diagnose. `tried` records which fallback tiers ran; `alternatives`
 * names concrete next moves.
 */
export interface ToolResult {
  success: boolean;
  reason?: FailureReason;
  tried: string[];
  alternatives?: string[];
  error?: string;
}

/**
 * Probe an element's interaction-readiness before firing an action.
 * Runs in-page so the answer is fresh (post-layout, post-reconcile).
 */
async function probeElement(
  page: Page,
  selector: string,
): Promise<{
  found: boolean;
  visible: boolean;
  inViewport: boolean;
  disabled: boolean;
  covered: boolean;
  rect: { x: number; y: number; w: number; h: number } | null;
}> {
  try {
    return await page.evaluate((sel: string) => {
      const el = document.querySelector(sel) as HTMLElement | null;
      if (!el) {
        return { found: false, visible: false, inViewport: false, disabled: false, covered: false, rect: null };
      }
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      const visible = style.display !== 'none'
        && style.visibility !== 'hidden'
        && parseFloat(style.opacity || '1') > 0.01
        && rect.width > 0 && rect.height > 0;
      const vw = window.innerWidth, vh = window.innerHeight;
      const inViewport = rect.bottom > 0 && rect.top < vh && rect.right > 0 && rect.left < vw;
      const disabled = (el as HTMLButtonElement).disabled === true
        || el.getAttribute('aria-disabled') === 'true';
      let covered = false;
      if (visible && inViewport) {
        const cx = Math.round(rect.left + rect.width / 2);
        const cy = Math.round(rect.top + rect.height / 2);
        let hit: Element | null = null;
        try { hit = document.elementFromPoint(cx, cy); } catch { hit = null; }
        if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) {
          covered = true;
        }
      }
      return {
        found: true,
        visible,
        inViewport,
        disabled,
        covered,
        rect: { x: rect.left, y: rect.top, w: rect.width, h: rect.height },
      };
    }, selector);
  } catch {
    return { found: false, visible: false, inViewport: false, disabled: false, covered: false, rect: null };
  }
}

export class PageWrapper {
  private cdpClient: CDPSession | null = null;
  private pendingDialogs: DialogInfo[] = [];
  private dialogHandlerSetup = false;
  private consoleCollector = new ConsoleCollector();
  private downloadMonitor = new DownloadMonitor();
  /**
   * Last rendered selectorMap. Used to mark newly-appeared elements in the
   * next getState() call with `*[n]` — browser-use's signal for "this
   * element appeared after your previous action".
   */
  private priorSelectorMap: Map<number, DOMElementNode> | null = null;
  /**
   * Last detected error-page state. Set by navigate(); cleared as soon as
   * the next navigate/click survives a readiness check. getState() reads
   * this so the navigator can short-circuit without re-running detection
   * on every step.
   */
  private lastErrorPage: ErrorPage | null = null;
  /**
   * Per-session set of URLs we've already seen produce an error page.
   * When the LLM is confused about a site's URL structure, it tends to
   * retry the same dead URL several times. Short-circuit on re-nav to
   * the same URL so the LLM sees the error synthetically without
   * spending another goto + waitForPageReady + error detection round.
   */
  private badUrls: Map<string, ErrorPage> = new Map();

  constructor(
    private page: Page,
    private config: BrowserConfig,
  ) {}

  /** Get the underlying puppeteer Page. */
  getRawPage(): Page {
    return this.page;
  }

  // --- Navigation ---

  async navigate(url: string, timeout: number = 30000): Promise<{ statusCode: number | null; finalUrl: string; errorPage?: ErrorPage }> {
    // SSRF protection
    const urlCheck = validateUrl(url);
    if (!urlCheck.valid) {
      throw new Error(`Blocked: ${urlCheck.error}`);
    }

    // URL blocklist short-circuit: if this URL previously produced an
    // error page, surface that cached result instead of navigating to
    // the same dead URL. Saves a ~2-8s round trip on LLM retry loops.
    const cached = this.badUrls.get(url);
    if (cached) {
      this.lastErrorPage = cached;
      feedbackBus.publish({
        kind: 'error_page',
        detail: { url, kind: cached.kind, detail: cached.detail },
      });
      return { statusCode: null, finalUrl: url, errorPage: cached };
    }

    const response = await this.page.goto(url, {
      waitUntil: 'domcontentloaded',
      timeout,
    });

    // Wait for the DOM to stop churning. waitForPageReady replaces the
    // flat 2s idle — modern SPAs often swap content seconds after DCL,
    // and the old wait sometimes captured a half-rendered page.
    await waitForPageReady(this.page, 5000);
    // Keep the original tiny idle too — some pages commit DOM changes
    // right at 'complete' as part of their hydration tail.
    await this.waitForIdle(800).catch(() => {});

    // Auto-wait for Cloudflare challenge if detected
    await this.waitForCloudflare();

    const statusCode = response ? response.status() : null;

    // Error-page classification. Populates lastErrorPage so getState()
    // can surface it; navigator reads it to short-circuit the LLM call.
    const errorPage = await detectErrorPage(this.page, statusCode);
    const finalUrl = this.page.url();
    this.lastErrorPage = errorPage;
    if (errorPage) {
      // Remember both the requested URL and the landed URL so either
      // form short-circuits on retry. Cap the cache — the LLM might
      // grind out dozens of URL variations and we don't need to hold
      // them all.
      if (this.badUrls.size < 128) {
        this.badUrls.set(url, errorPage);
        if (finalUrl && finalUrl !== url) this.badUrls.set(finalUrl, errorPage);
      }
      feedbackBus.publish({
        kind: 'error_page',
        detail: { url: finalUrl, kind: errorPage.kind, detail: errorPage.detail },
      });
    } else {
      // Success — drop any stale entries for this URL (content may have
      // changed since the last 404).
      this.badUrls.delete(url);
      if (finalUrl) this.badUrls.delete(finalUrl);
      feedbackBus.publish({ kind: 'error_cleared' });
    }

    return {
      statusCode,
      finalUrl,
      ...(errorPage ? { errorPage } : {}),
    };
  }

  /** Current error-page state, if any. Cleared on next successful navigate. */
  getLastErrorPage(): ErrorPage | null {
    return this.lastErrorPage;
  }

  /** Clear the error-page flag — caller decided to continue past it. */
  clearErrorPage(): void {
    this.lastErrorPage = null;
    feedbackBus.publish({ kind: 'error_cleared' });
  }

  /**
   * Wait for a bot-challenge page to resolve.
   *
   * Detects Cloudflare / Akamai / Imperva / DataDome / PerimeterX interstitials
   * and waits for BOTH of:
   *   (a) the challenge title to clear, AND
   *   (b) a corresponding challenge cookie to be set (cf_clearance, _abck,
   *       datadome, incap_ses_*, __cf_bm, reese84).
   *
   * Why cookies too: on Akamai (_abck) the page title often flips to the
   * target content a fraction of a second before the sensor posts and the
   * clearance cookie lands. Returning on title alone races — the very next
   * navigation then 403s because the cookie hasn't been recorded. Waiting
   * for the cookie closes that race.
   */
  async waitForCloudflare(maxWait: number = 25000): Promise<boolean> {
    const title = await this.page.title();
    const lower = title.toLowerCase();
    const challengeTitleFragments = [
      'just a moment',
      'checking your browser',
      'attention required',
      'one more step',           // Cloudflare variant
      'access denied',           // Akamai / some CF
      'pardon our interruption', // Imperva Incapsula
      'please wait',             // DataDome / generic
    ];
    const isChallenge = challengeTitleFragments.some((f) => lower.includes(f));

    if (!isChallenge) return true;

    console.log('[stealth] Bot-challenge page detected — waiting for resolution + clearance cookie');
    const start = Date.now();

    // Patterns for the cookies posted by the major edge-protection products.
    // Matching any one of these after the title clears means the challenge
    // actually minted a clearance token (not just a title swap).
    const clearanceCookiePatterns = [
      /^cf_clearance$/i,     // Cloudflare Managed Challenge
      /^__cf_bm$/i,          // Cloudflare Bot Management
      /^_abck$/i,            // Akamai Bot Manager (sensor)
      /^ak_bmsc$/i,          // Akamai Bot Manager (session)
      /^datadome$/i,         // DataDome
      /^incap_ses_/i,        // Imperva Incapsula session
      /^visid_incap_/i,      // Imperva Incapsula visitor
      /^reese84$/i,          // Kasada
      /^px-?.+$/i,           // PerimeterX (px3, px-captcha, etc.)
    ];

    const hasClearanceCookie = async (): Promise<boolean> => {
      try {
        const cookies = await this.page.cookies();
        return cookies.some((c) =>
          clearanceCookiePatterns.some((re) => re.test(c.name)),
        );
      } catch {
        return false;
      }
    };

    while (Date.now() - start < maxWait) {
      await new Promise((r) => setTimeout(r, 1500));
      const currentTitle = (await this.page.title()).toLowerCase();
      const titleCleared = !challengeTitleFragments.some((f) =>
        currentTitle.includes(f),
      );

      if (titleCleared) {
        // Title cleared. Give the sensor a brief window to post and the
        // clearance cookie to land before we trust the resolution.
        if (await hasClearanceCookie()) {
          console.log(
            `[stealth] Challenge resolved + clearance cookie present after ${Date.now() - start}ms`,
          );
          await this.waitForIdle(1500).catch(() => {});
          return true;
        }
        // Title cleared but no cookie yet — poll a little longer; some
        // stacks set the cookie up to ~1s after the content paints.
      }
    }

    // Timed out. If the title cleared but no cookie ever landed, we're
    // probably on a stealth-bypassed SPA that doesn't use a clearance cookie
    // — treat as success to avoid false-negative blocking of normal sites.
    const finalTitle = (await this.page.title()).toLowerCase();
    const stillChallenged = challengeTitleFragments.some((f) =>
      finalTitle.includes(f),
    );
    if (!stillChallenged) {
      console.log('[stealth] Title cleared but no clearance cookie seen — treating as resolved');
      return true;
    }
    console.log('[stealth] Bot-challenge did not resolve within timeout');
    return false;
  }

  async goBack(): Promise<void> {
    await this.page.goBack({ waitUntil: 'domcontentloaded', timeout: 10000 });
    await this.waitForIdle(1500).catch(() => {});
  }

  async getUrl(): Promise<string> {
    return this.page.url();
  }

  async getTitle(): Promise<string> {
    return this.page.title();
  }

  // --- Screenshots ---

  async screenshot(quality: number = 70): Promise<Buffer> {
    const raw = (await this.page.screenshot({
      type: 'jpeg',
      quality,
      fullPage: false,
    })) as Buffer;
    // Sanitize: caps at MAX_BYTES, strips EXIF/ICC, resizes if >1568px/side.
    // Cheap no-op when already compact; necessary guard when a site renders
    // a very tall viewport or when quality=100 is passed.
    const san = await sanitizeImageBuffer(raw);
    return san.buffer;
  }

  async screenshotBase64(quality: number = 70): Promise<string> {
    const buffer = await this.screenshot(quality);
    return buffer.toString('base64');
  }

  // --- Element interaction ---

  /**
   * Click an element using a 3-tier fallback cascade (from BrowserOS).
   * Only tiers 1 and 2 produce `event.isTrusted === true`. Tier 3 uses
   * `el.click()` which sets `isTrusted === false` — trivially detectable
   * by bot-protection scripts, so it's gated behind `allowUntrustedClick`.
   *
   * 1. CDP Input.dispatchMouseEvent at element center coords (isTrusted=true)
   * 2. Puppeteer page.click() with CSS selector (CDP-backed, isTrusted=true)
   * 3. JS el.click() via XPath (isTrusted=false — DETECTABLE, opt-in only)
   *
   * Returns a structured ToolResult so callers (and ultimately the LLM) can
   * choose a different tactic on failure without re-screenshotting just to
   * re-diagnose. A probe runs before the first click; on covered/off_viewport,
   * we scroll into view and retry the CDP click once.
   */
  async clickElement(element: DOMElementNode, options?: {
    button?: 'left' | 'right' | 'middle';
    clickCount?: number;
    /** Allow the JS fallback (isTrusted=false). Default false. */
    allowUntrustedClick?: boolean;
  }): Promise<ToolResult> {
    const selector = element.enhancedCssSelectorForElement();
    const tried: string[] = [];

    const probe = await probeElement(this.page, selector);
    if (!probe.found) {
      return {
        success: false,
        reason: 'stale_selector',
        tried,
        error: `Selector did not match any element: ${selector}`,
        alternatives: [
          'Re-read the current interactive elements list — the index may be stale',
          'Try a different selector (aria-label, text content)',
        ],
      };
    }
    if (probe.disabled) {
      return {
        success: false,
        reason: 'disabled',
        tried,
        error: 'Element is disabled',
        alternatives: [
          'Fill required fields before clicking',
          'Wait for the element to become enabled (browser_wait_for text)',
        ],
      };
    }
    if (!probe.visible) {
      return {
        success: false,
        reason: 'not_visible',
        tried,
        error: 'Element is hidden (display:none, visibility:hidden, or zero-size)',
        alternatives: [
          'Trigger the flow that reveals this element (open a menu, expand a section)',
          'Use a different selector for the visible twin',
        ],
      };
    }

    // Auto-remediate: scroll into view if off-viewport, before any click attempt.
    if (!probe.inViewport) {
      try {
        await this.page.evaluate((sel: string) => {
          const el = document.querySelector(sel) as HTMLElement | null;
          if (el) el.scrollIntoView({ block: 'center', behavior: 'instant' as ScrollBehavior });
        }, selector);
        await new Promise((r) => setTimeout(r, 150));
      } catch { /* scroll is best-effort */ }
    }

    // Tier 1: CDP mouse dispatch at computed coordinates (isTrusted=true)
    let coverReason: FailureReason | null = null;
    try {
      const coords = await getElementCenterBySelector(this.page, selector);
      if (coords) {
        // Re-check covered after potential scroll.
        const postScroll = await probeElement(this.page, selector);
        if (postScroll.covered) {
          coverReason = 'element_covered';
          // Fall through to tier 2; sometimes the overlay is intentional and the
          // real target dispatches on the wrapper.
        } else {
          tried.push('cdp');
          const client = await this.getCDPSession();
          await dispatchClick(client, coords.x, coords.y, {
            button: options?.button,
            clickCount: options?.clickCount,
          });
          await this.waitForIdle(1500).catch(() => {});
          return { success: true, tried };
        }
      }
    } catch {
      // Fallthrough
    }

    // Tier 2: Puppeteer click (isTrusted=true — Puppeteer dispatches via CDP)
    tried.push('puppeteer');
    try {
      await this.page.waitForSelector(selector, { timeout: 5000 });
      await this.page.click(selector);
      await this.waitForIdle(1500).catch(() => {});
      return { success: true, tried };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      // Tier 3 (JS) gated by allowUntrustedClick — if not allowed, return
      // a structured failure instead of throwing.
      if (!options?.allowUntrustedClick) {
        return {
          success: false,
          reason: coverReason ?? 'unknown',
          tried,
          error: msg,
          alternatives: coverReason === 'element_covered'
            ? [
              'Dismiss the overlay first (cookie banner, modal close button)',
              'Try clicking the overlay itself if it IS the target',
              'Use clickAt(x,y) with coordinates below the overlay',
            ]
            : [
              'Re-read the elements list — the DOM may have shifted',
              'Wait for network/animation to settle (browser_wait_for text)',
              'Try a nearby selector (parent button, sibling link)',
            ],
        };
      }
    }

    // Tier 3: JS fallback via XPath (isTrusted=false). Opt-in only.
    if (element.xpath && options?.allowUntrustedClick) {
      tried.push('js');
      try {
        await this.page.evaluate((xpath: string) => {
          const result = document.evaluate(
            xpath, document, null,
            XPathResult.FIRST_ORDERED_NODE_TYPE, null,
          );
          const el = result.singleNodeValue as HTMLElement;
          if (el) {
            el.scrollIntoView({ block: 'center' });
            el.click();
          }
        }, element.xpath);
        await this.waitForIdle(1500).catch(() => {});
        return { success: true, tried };
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return {
          success: false,
          reason: coverReason ?? 'unknown',
          tried,
          error: msg,
        };
      }
    }

    return {
      success: false,
      reason: coverReason ?? 'unknown',
      tried,
      error: `All click tiers exhausted for ${selector}`,
    };
  }

  /** Click at specific page coordinates (from BrowserOS click_at). */
  async clickAt(x: number, y: number, options?: {
    button?: 'left' | 'right' | 'middle';
    clickCount?: number;
  }): Promise<void> {
    const client = await this.getCDPSession();
    await dispatchClick(client, x, y, options);
    await this.waitForIdle(1000).catch(() => {});
  }

  /** Hover over an element (from BrowserOS hover). */
  async hoverElement(element: DOMElementNode): Promise<void> {
    const selector = element.enhancedCssSelectorForElement();
    const coords = await getElementCenterBySelector(this.page, selector);
    if (coords) {
      const client = await this.getCDPSession();
      await dispatchHover(client, coords.x, coords.y);
    } else {
      await this.page.hover(selector);
    }
  }

  /** Hover at specific coordinates (from BrowserOS hover_at). */
  async hoverAt(x: number, y: number): Promise<void> {
    const client = await this.getCDPSession();
    await dispatchHover(client, x, y);
  }

  /** Drag from one element to another or to coordinates (from BrowserOS drag). */
  async dragTo(
    startX: number, startY: number,
    endX: number, endY: number,
    options?: { steps?: number },
  ): Promise<void> {
    const client = await this.getCDPSession();
    await dispatchDrag(client, startX, startY, endX, endY, options);
  }

  /** Scroll within a specific element or the page (from BrowserOS scroll). */
  async scrollElement(
    element: DOMElementNode | null,
    direction: 'up' | 'down' | 'left' | 'right',
    amount: number = 300,
  ): Promise<void> {
    const client = await this.getCDPSession();

    let x = this.config.viewport.width / 2;
    let y = this.config.viewport.height / 2;

    if (element) {
      const selector = element.enhancedCssSelectorForElement();
      const coords = await getElementCenterBySelector(this.page, selector);
      if (coords) {
        x = coords.x;
        y = coords.y;
      }
    }

    const deltaX = direction === 'left' ? -amount : direction === 'right' ? amount : 0;
    const deltaY = direction === 'up' ? -amount : direction === 'down' ? amount : 0;

    await dispatchScroll(client, x, y, deltaX, deltaY);
    await new Promise((r) => setTimeout(r, 300));
  }

  /**
   * Type text into an element using CDP keyboard dispatch.
   * Smart field clearing: click → Ctrl+A → Backspace (from BrowserOS fill pattern).
   *
   * Returns a structured ToolResult. Pre-probes the element; on off-viewport,
   * scrolls into view before attempting CDP typing.
   */
  async typeText(element: DOMElementNode, text: string, clear: boolean = true): Promise<ToolResult> {
    const selector = element.enhancedCssSelectorForElement();
    const tried: string[] = [];

    const probe = await probeElement(this.page, selector);
    if (!probe.found) {
      return {
        success: false, reason: 'stale_selector', tried,
        error: `Selector did not match any element: ${selector}`,
        alternatives: ['Re-read interactive elements — index may be stale'],
      };
    }
    if (probe.disabled) {
      return {
        success: false, reason: 'disabled', tried,
        error: 'Input is disabled',
        alternatives: ['Fill prerequisite fields', 'Wait for form unlock'],
      };
    }
    if (!probe.visible) {
      return {
        success: false, reason: 'not_visible', tried,
        error: 'Input is hidden',
        alternatives: ['Expand the section containing this field first'],
      };
    }

    if (!probe.inViewport) {
      try {
        await this.page.evaluate((sel: string) => {
          const el = document.querySelector(sel) as HTMLElement | null;
          if (el) el.scrollIntoView({ block: 'center', behavior: 'instant' as ScrollBehavior });
        }, selector);
        await new Promise((r) => setTimeout(r, 150));
      } catch { /* best-effort */ }
    }

    try {
      const coords = await getElementCenterBySelector(this.page, selector);
      const client = await this.getCDPSession();

      if (coords) {
        tried.push('cdp');
        await dispatchClick(client, coords.x, coords.y);
        await new Promise((r) => setTimeout(r, 100));

        if (clear) {
          await clearField(client, coords.x, coords.y);
          await new Promise((r) => setTimeout(r, 50));
        }

        await cdpTypeText(client, text, 30);
        return { success: true, tried };
      }

      tried.push('puppeteer');
      await this.page.waitForSelector(selector, { timeout: 5000 });
      if (clear) {
        await this.page.click(selector, { clickCount: 3 });
        await this.page.keyboard.press('Backspace');
      }
      await this.page.type(selector, text, { delay: 30 });
      return { success: true, tried };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      // JS fallback only when xpath is known; this path can be bot-detectable
      // (events are synthesized with isTrusted=false) but is the last resort.
      if (element.xpath) {
        tried.push('js');
        try {
          await this.page.evaluate((xpath: string, inputText: string) => {
            const result = document.evaluate(
              xpath, document, null,
              XPathResult.FIRST_ORDERED_NODE_TYPE, null,
            );
            const el = result.singleNodeValue as HTMLInputElement;
            if (el) {
              el.scrollIntoView({ block: 'center' });
              el.focus();
              el.value = '';
              el.value = inputText;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            }
          }, element.xpath, text);
          return { success: true, tried };
        } catch (e) {
          const m2 = e instanceof Error ? e.message : String(e);
          return {
            success: false, reason: 'unknown', tried,
            error: `${msg}; JS fallback failed: ${m2}`,
          };
        }
      }
      return {
        success: false, reason: 'unknown', tried, error: msg,
        alternatives: ['Try a different selector', 'Verify field accepts keyboard input'],
      };
    }
  }

  async selectOption(element: DOMElementNode, value: string): Promise<void> {
    const selector = element.enhancedCssSelectorForElement();
    await this.page.select(selector, value);
  }

  /**
   * Send keyboard keys via CDP dispatch.
   * Supports combos like "Control+A", "Meta+Shift+P", special keys, etc.
   * Pattern from BrowserOS keyboard.ts pressKeyCombo.
   */
  async sendKeys(keys: string): Promise<void> {
    const client = await this.getCDPSession();
    await pressKeyCombo(client, keys);
  }

  // --- Scrolling ---

  async scrollPage(direction: 'up' | 'down'): Promise<void> {
    const viewportHeight = this.config.viewport.height;
    const distance = direction === 'down' ? viewportHeight - 100 : -(viewportHeight - 100);
    await this.page.evaluate((d: number) => {
      window.scrollBy(0, d);
    }, distance);
    await new Promise((r) => setTimeout(r, 500));
  }

  async scrollToPercent(percent: number): Promise<void> {
    await this.page.evaluate((pct: number) => {
      const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
      window.scrollTo(0, Math.round(maxScroll * pct / 100));
    }, percent);
    await new Promise((r) => setTimeout(r, 500));
  }

  async getScrollInfo(): Promise<[number, number, number]> {
    return this.page.evaluate(() => [
      Math.round(window.scrollY),
      Math.round(window.innerHeight),
      Math.round(document.documentElement.scrollHeight),
    ]) as Promise<[number, number, number]>;
  }

  // --- State ---

  async getState(options: {
    useVision?: boolean;
    includeAccessibility?: boolean;
    includeConsole?: boolean;
    includeCursorElements?: boolean;
  } = {}): Promise<PageState> {
    const {
      useVision = true,
      includeAccessibility = false,
      includeConsole = true,
      includeCursorElements = false,
    } = options;

    const domResult = await buildDomTree(this.page, 0, this.priorSelectorMap);
    // Persist the new map so the NEXT getState() can diff against it.
    this.priorSelectorMap = domResult.selectorMap;

    let screenshot: string | undefined;
    if (useVision) {
      screenshot = await this.screenshotBase64();
    }

    let accessibilityTree: string | undefined;
    if (includeAccessibility) {
      try {
        accessibilityTree = await getAccessibilitySnapshot(this.page);
      } catch {
        // AX tree not available
      }
    }

    // FROM BROWSEROS: Detect cursor-interactive elements the DOM tree misses
    if (includeCursorElements) {
      try {
        const cursorElements = await findCursorInteractiveElements(this.page);
        if (cursorElements.length > 0) {
          const formatted = formatCursorElements(cursorElements);
          accessibilityTree = (accessibilityTree || '') + formatted;
        }
      } catch {
        // Cursor detection is best-effort
      }
    }

    const pendingDialogs = this.pendingDialogs.length > 0
      ? [...this.pendingDialogs]
      : undefined;

    let consoleErrors: string[] | undefined;
    if (includeConsole) {
      const errors = this.consoleCollector.getErrors(5);
      if (errors.length > 0) {
        consoleErrors = errors.map((e) => e.text);
      }
    }

    return {
      ...domResult,
      screenshot,
      accessibilityTree,
      pendingDialogs,
      consoleErrors,
      errorPage: this.lastErrorPage ?? undefined,
    };
  }

  /** Enable download monitoring via CDP events (from BrowserOS). */
  async enableDownloadMonitor(): Promise<void> {
    const client = await this.getCDPSession();
    await this.downloadMonitor.enable(client, this.config.downloadDir);
  }

  /** Wait for a download to complete. */
  async waitForDownload(timeout?: number): Promise<{ filename: string; url: string } | null> {
    const result = await this.downloadMonitor.waitForDownload(timeout);
    if (!result) return null;
    return { filename: result.suggestedFilename, url: result.url };
  }

  /** Get link URLs from the page (from BrowserOS snapshot.ts link extraction). */
  async extractLinks(): Promise<Array<{ text: string; href: string }>> {
    return this.page.evaluate(() => {
      const links: Array<{ text: string; href: string }> = [];
      const seen = new Set<string>();
      document.querySelectorAll('a[href]').forEach((a) => {
        const href = (a as HTMLAnchorElement).href;
        if (!href || href.startsWith('javascript:') || seen.has(href)) return;
        seen.add(href);
        const text = (a.textContent || '').trim().substring(0, 100);
        links.push({ text, href });
      });
      return links;
    });
  }

  // --- FROM BROWSEROS: Dialog handling ---

  async setupDialogHandler(): Promise<void> {
    if (this.dialogHandlerSetup) return;
    this.page.on('dialog', async (dialog) => {
      this.pendingDialogs.push({
        type: dialog.type(),
        message: dialog.message(),
        defaultValue: dialog.defaultValue(),
      });
    });
    this.dialogHandlerSetup = true;
  }

  async handleDialog(accept: boolean, text?: string): Promise<void> {
    this.pendingDialogs.shift();

    // Handle via promise race with timeout to avoid unhandled rejection
    try {
      await Promise.race([
        new Promise<void>((resolve, reject) => {
          this.page.once('dialog', async (d) => {
            try {
              if (accept) {
                await d.accept(text);
              } else {
                await d.dismiss();
              }
              resolve();
            } catch (err) {
              reject(err);
            }
          });
        }),
        new Promise<void>((_, reject) =>
          setTimeout(() => reject(new Error('Dialog handling timed out')), 5000),
        ),
      ]);
    } catch {
      // Dialog may have already been dismissed or timed out
    }
  }

  getPendingDialogs(): DialogInfo[] {
    return [...this.pendingDialogs];
  }

  // --- Console capture (enhanced from BrowserOS console-collector) ---

  async enableConsoleCapture(): Promise<void> {
    const client = await this.getCDPSession();
    await this.consoleCollector.enable(client);
  }

  getConsoleMessages(filter?: 'error' | 'warning'): CollectedLog[] {
    return this.consoleCollector.getLogs({ level: filter });
  }

  /** Search console logs by text (from BrowserOS). */
  searchConsoleLogs(search: string, limit?: number): CollectedLog[] {
    return this.consoleCollector.getLogs({ search, limit });
  }

  // --- FROM BROWSEROS: File upload ---

  async uploadFile(element: DOMElementNode, filePaths: string[]): Promise<void> {
    const selector = element.enhancedCssSelectorForElement();
    const fileInput = await this.page.$(selector);
    if (!fileInput) throw new Error('File input element not found');
    await (fileInput as any).uploadFile(...filePaths);
  }

  // --- FROM BROWSEROS: PDF export ---

  async exportPdf(options?: {
    format?: 'A4' | 'Letter';
    printBackground?: boolean;
  }): Promise<Buffer> {
    return (await this.page.pdf({
      format: options?.format || 'A4',
      printBackground: options?.printBackground ?? true,
      margin: { top: '10mm', bottom: '10mm', left: '10mm', right: '10mm' },
    })) as Buffer;
  }

  // --- FROM BROWSEROS: Content extraction to markdown ---

  async getMarkdownContent(): Promise<string> {
    return this.page.evaluate(() => {
      // Simple markdown extraction from the page
      const clone = document.body.cloneNode(true) as HTMLElement;

      // Remove scripts, styles, nav, footer, etc.
      const removeSelectors = 'script, style, noscript, nav, footer, header, aside, [role="navigation"], [role="banner"], [aria-hidden="true"]';
      clone.querySelectorAll(removeSelectors).forEach((el) => el.remove());

      // Convert links
      clone.querySelectorAll('a').forEach((a) => {
        const text = a.textContent?.trim() || '';
        const href = a.getAttribute('href') || '';
        if (text && href) {
          a.textContent = `[${text}](${href})`;
        }
      });

      // Convert headings
      for (let i = 1; i <= 6; i++) {
        clone.querySelectorAll(`h${i}`).forEach((h) => {
          const text = h.textContent?.trim() || '';
          if (text) {
            h.textContent = '\n' + '#'.repeat(i) + ' ' + text + '\n';
          }
        });
      }

      // Convert lists
      clone.querySelectorAll('li').forEach((li) => {
        const text = li.textContent?.trim() || '';
        if (text) {
          li.textContent = '- ' + text;
        }
      });

      // Get text and normalize whitespace
      let text = clone.innerText || clone.textContent || '';
      text = text.replace(/[ \t]+/g, ' ');
      text = text.replace(/\n{3,}/g, '\n\n');
      return text.trim().substring(0, 50000);
    });
  }

  // --- FROM BROWSEROS: DOM search ---

  async domSearch(selector: string): Promise<string[]> {
    return this.page.evaluate((sel: string) => {
      const elements = document.querySelectorAll(sel);
      return Array.from(elements).map((el) => {
        const text = (el as HTMLElement).innerText || el.textContent || '';
        return text.trim().substring(0, 200);
      }).filter(Boolean);
    }, selector);
  }

  // --- FROM BROWSEROS: Custom wait conditions ---

  async waitForCondition(jsExpression: string, timeout: number = 10000): Promise<boolean> {
    try {
      await this.page.waitForFunction(jsExpression, { timeout });
      return true;
    } catch {
      return false;
    }
  }

  // --- FROM BROWSEROS: Evaluate arbitrary script ---

  async evaluateScript(script: string): Promise<unknown> {
    return this.page.evaluate(script);
  }

  // --- Waiting ---

  async waitForIdle(timeout: number = 3000): Promise<void> {
    try {
      await this.page.waitForNetworkIdle({ idleTime: 500, timeout });
    } catch {
      // Timeout is acceptable
    }
  }

  // --- Lifecycle ---

  async close(): Promise<void> {
    // Cleanup CDP session to prevent resource leak
    if (this.cdpClient) {
      try {
        await this.cdpClient.detach();
      } catch {
        // Already detached
      }
      this.cdpClient = null;
    }
    try {
      await this.page.close();
    } catch {
      // Page may already be closed
    }
  }

  /** Get or create CDP session for low-level operations. */
  async getCDPSession(): Promise<CDPSession> {
    if (!this.cdpClient) {
      this.cdpClient = await this.page.createCDPSession();
    }
    return this.cdpClient;
  }
}
