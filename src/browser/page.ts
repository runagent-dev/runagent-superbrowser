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
import { humanClick } from './humanize.js';
import { typeText as cdpTypeText, pressKeyCombo, clearField, dispatchKey } from './input-keyboard.js';
import { getElementCenterBySelector } from './elements.js';
import { findCursorInteractiveElements, formatCursorElements } from './cursor-detect.js';
import { ConsoleCollector, type CollectedLog } from './console-collector.js';
import { DownloadMonitor } from './download-monitor.js';
import { validateUrl } from '../server/auth.js';
import type { FailureReason } from '../agent/types.js';
import { sanitizeImageBuffer } from './image-safety.js';
import { waitForPageReady, detectErrorPage, type ErrorPage } from './page-readiness.js';
import { feedbackBus } from '../agent/feedback-bus.js';
import { inputEventBus } from './input-events.js';

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

  /** Session ID set by HTTP server on session creation. Threaded through
   *  to humanize/input-mouse so input events can be broadcast to the
   *  live view via InputEventBus. */
  public sessionId?: string;

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
    // Attribute the failure to a specific WAF so the caller knows whether
    // 2captcha applies (Cloudflare Turnstile) or whether human handoff is
    // the only option (Akamai sensor_data).
    const cookies = await this.page.cookies().catch(() => [] as Array<{ name: string }>);
    const names = new Set(cookies.map((c) => c.name));
    let kind = 'unknown';
    if (names.has('cf_clearance') || names.has('__cf_bm') || finalTitle.includes('just a moment')) {
      kind = 'cloudflare';
    } else if (names.has('_abck') || names.has('ak_bmsc') || finalTitle.includes('access denied')) {
      kind = 'akamai';
    } else if (names.has('datadome')) {
      kind = 'datadome';
    } else if ([...names].some((n) => n.startsWith('incap_ses_') || n.startsWith('visid_incap_'))) {
      kind = 'imperva';
    } else if ([...names].some((n) => /^px-?/.test(n))) {
      kind = 'perimeterx';
    } else if (names.has('reese84')) {
      kind = 'kasada';
    }
    const remediation =
      kind === 'cloudflare' ? '2captcha-solvable (browser_solve_captcha auto)'
      : kind === 'akamai'   ? 'NOT 2captcha-solvable — needs humanized-mouse pass or human handoff'
      : kind === 'datadome' ? 'partially 2captcha-solvable; may need human handoff'
      : kind === 'kasada'   ? 'NOT 2captcha-solvable — needs human handoff'
      : 'unknown remediation — try browser_solve_captcha(auto), then human handoff';
    console.log(`[stealth] Bot-challenge did not resolve within timeout (kind=${kind}, ${remediation})`);
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
    // Phase 3.2: route through humanClick (Bezier-curve mouse motion +
    // pre-click hesitation + click-point jitter) by default. The raw
    // dispatchClick path skips humanization, which is fine for tests
    // but trips Cloudflare/Akamai's behavioral signals on real targets.
    // Opt out via SUPERBROWSER_HUMANIZE_ALL_CLICKS=0 (defaults to on).
    let coverReason: FailureReason | null = null;
    const humanizeAll = process.env.SUPERBROWSER_HUMANIZE_ALL_CLICKS !== '0';
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
          // Crosshair on the live view BEFORE the click so it lands in
          // the same screencast frame the user sees the page change in.
          this._emitClickTargetForElement(coords.x, coords.y, postScroll.rect, selector);
          const client = await this.getCDPSession();
          if (humanizeAll && (options?.clickCount ?? 1) === 1) {
            await humanClick(client, coords.x, coords.y, {
              button: options?.button,
              sessionId: this.sessionId,
            });
          } else {
            await dispatchClick(client, coords.x, coords.y, {
              button: options?.button,
              clickCount: options?.clickCount,
              sessionId: this.sessionId,
            });
          }
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
      // Re-probe so the crosshair sits on the post-wait rect.
      const p2 = await probeElement(this.page, selector);
      if (p2.rect) {
        const cx = Math.round(p2.rect.x + p2.rect.w / 2);
        const cy = Math.round(p2.rect.y + p2.rect.h / 2);
        this._emitClickTargetForElement(cx, cy, p2.rect, selector);
      }
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
        const p3 = await probeElement(this.page, selector);
        if (p3.rect) {
          const cx = Math.round(p3.rect.x + p3.rect.w / 2);
          const cy = Math.round(p3.rect.y + p3.rect.h / 2);
          this._emitClickTargetForElement(cx, cy, p3.rect, selector);
        }
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

  /**
   * Internal helper — emit a `cursor_target` event for a DOM-resolved
   * click. Live viewers render a green crosshair at (x, y) and (when a
   * rect is present) draw the element's bounding box outline alongside
   * it. No-op when sessionId isn't set.
   */
  private _emitClickTargetForElement(
    x: number,
    y: number,
    rect: { x: number; y: number; w: number; h: number } | null,
    target: string,
  ): void {
    if (!this.sessionId) return;
    const bbox = rect
      ? { x0: Math.round(rect.x), y0: Math.round(rect.y),
          x1: Math.round(rect.x + rect.w), y1: Math.round(rect.y + rect.h) }
      : undefined;
    inputEventBus.emitClickTarget(this.sessionId, x, y, true, bbox, target);
  }

  /** Click at specific page coordinates (from BrowserOS click_at). */
  async clickAt(x: number, y: number, options?: {
    button?: 'left' | 'right' | 'middle';
    clickCount?: number;
  }): Promise<void> {
    const client = await this.getCDPSession();
    // Crosshair on every coord-based click (not just bbox-routed). The
    // brain that uses raw (x, y) bypasses snap-to-element, so the
    // crosshair is the only visible signal for "where did this land".
    if (this.sessionId) {
      inputEventBus.emitClickTarget(this.sessionId, x, y, false);
    }
    await dispatchClick(client, x, y, { ...options, sessionId: this.sessionId });
    await this.waitForIdle(1000).catch(() => {});
  }

  /**
   * Click inside a vision-supplied bbox — pinpoint mode.
   *
   * Design: trust Gemini's bbox. The click lands on the geometric
   * centre of the bbox rectangle; we use `elementsFromPoint` only as a
   * visibility sanity check — if something interactive is at the
   * centre, we mark the click `snapped=true` (for the green UI
   * crosshair). We do NOT walk up to a different interactive ancestor
   * or re-aim at a neighbouring element; that would override Gemini's
   * bbox and defeat the point of the pinpoint contract.
   *
   * Fallback path runs ONLY when the centre pixel lies on an empty
   * area (no element at all — the bbox covers blank whitespace). In
   * that case we do a 5×5 grid scan to find the largest interactive
   * element whose rect intersects the bbox, same as before. This
   * covers the rare case of a grossly-loose Gemini bbox without
   * penalising the common case where Gemini is tight.
   */
  async clickInBbox(
    bbox: { x0: number; y0: number; x1: number; y1: number },
    options?: { button?: 'left' | 'right' | 'middle'; clickCount?: number },
  ): Promise<{ x: number; y: number; snapped: boolean; target?: string }> {
    const snap = await this.page.evaluate(
      (b: { x0: number; y0: number; x1: number; y1: number }) => {
        const SEL = 'a,button,input,select,textarea,'
          + '[role="button"],[role="link"],[role="checkbox"],'
          + '[role="tab"],[role="menuitem"],[onclick],[tabindex]';
        const cx = Math.round((b.x0 + b.x1) / 2);
        const cy = Math.round((b.y0 + b.y1) / 2);
        const describe = (el: Element): string => {
          const tag = el.tagName.toLowerCase();
          const id = (el as HTMLElement).id ? `#${(el as HTMLElement).id}` : '';
          const cls = (el as HTMLElement).className && typeof (el as HTMLElement).className === 'string'
            ? `.${(el as HTMLElement).className.split(/\s+/).filter(Boolean).slice(0, 2).join('.')}`
            : '';
          const txt = (el.textContent || '').trim().slice(0, 30);
          return `${tag}${id}${cls}${txt ? `[${txt}]` : ''}`;
        };
        // 1. Pinpoint: click the bbox centre. Sanity-check that SOMETHING
        //    is rendered there (not transparent padding, not off-page).
        //    If the centre hits any element at all — even a wrapping
        //    <div> — we trust Gemini's bbox and fire at (cx, cy).
        let centreStack: Element[] = [];
        try { centreStack = document.elementsFromPoint(cx, cy); } catch { centreStack = []; }
        const centreEl = centreStack.find(
          (el) => el !== document.documentElement && el !== document.body,
        );
        if (centreEl) {
          // Look for an interactive ancestor ONLY to label the target
          // nicely in the UI overlay. The click coordinates stay at the
          // bbox centre — we don't move them.
          const interactive = (centreEl as Element).closest(SEL) || centreEl;
          return {
            x: cx,
            y: cy,
            snapped: true,
            target: describe(interactive),
          };
        }
        // 2. Centre fell on empty space — bbox is probably loose or
        //    off-page. Grid-scan fallback: pick the interactive element
        //    whose rect overlaps the bbox most, click its own centre.
        let best: Element | null = null;
        let bestArea = 0;
        for (let i = 1; i < 5; i++) {
          for (let j = 1; j < 5; j++) {
            const px = b.x0 + ((b.x1 - b.x0) * i) / 5;
            const py = b.y0 + ((b.y1 - b.y0) * j) / 5;
            let stack: Element[] = [];
            try { stack = document.elementsFromPoint(px, py); } catch { stack = []; }
            for (const el of stack) {
              const hit = (el as Element).closest(SEL);
              if (!hit) continue;
              const r = hit.getBoundingClientRect();
              const ix = Math.max(0, Math.min(r.right, b.x1) - Math.max(r.left, b.x0));
              const iy = Math.max(0, Math.min(r.bottom, b.y1) - Math.max(r.top, b.y0));
              const area = ix * iy;
              if (area > bestArea) { bestArea = area; best = hit; }
            }
          }
        }
        if (best) {
          const r = best.getBoundingClientRect();
          return {
            x: Math.round(r.left + r.width / 2),
            y: Math.round(r.top + r.height / 2),
            snapped: true,
            target: describe(best),
          };
        }
        // 3. Hard fallback: click the raw centre anyway. snapped=false
        //    so the UI crosshair shows amber — operator can see we had
        //    no visual confirmation.
        return { x: cx, y: cy, snapped: false };
      },
      bbox,
    );

    // Broadcast resolved target to live viewers BEFORE the click so the
    // crosshair appears in the same frame the click lands.
    if (this.sessionId) {
      inputEventBus.emitClickTarget(
        this.sessionId,
        snap.x,
        snap.y,
        snap.snapped,
        bbox,
        snap.target,
      );
    }

    const client = await this.getCDPSession();
    await dispatchClick(client, snap.x, snap.y, { ...options, sessionId: this.sessionId });
    await this.waitForIdle(1000).catch(() => {});
    return snap;
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
    options?: { steps?: number; linear?: boolean; overshoot?: boolean },
  ): Promise<void> {
    const client = await this.getCDPSession();
    await dispatchDrag(client, startX, startY, endX, endY, { ...options, sessionId: this.sessionId });
  }

  // ─── Selector-based primitives (zero vision cost) ──────────────────────
  //
  // These complement `clickAt` / `clickInBbox` / `dragTo` for any target
  // whose position is derivable from a CSS selector. They call
  // `getBoundingClientRect()` in-page, then fire the same CDP dispatch
  // path the vision-driven variants use. No Gemini round-trip, no box_2d
  // quantisation, pixel-exact centre. Used by puzzle solvers (chess
  // squares, captcha handles, grid items) and by any worker that already
  // knows the selector it wants to hit.

  async getRects(
    selectors: string[],
    opts?: { ensureVisible?: boolean },
  ): Promise<Array<{
    x: number; y: number; w: number; h: number;
    cx: number; cy: number;
    visible: boolean; inViewport: boolean;
  } | null>> {
    return await this.page.evaluate(
      (sels: string[], ensure: boolean) => {
        return sels.map((sel) => {
          const el = document.querySelector(sel) as HTMLElement | null;
          if (!el) return null;
          if (ensure) {
            const pre = el.getBoundingClientRect();
            const vw = window.innerWidth, vh = window.innerHeight;
            const inView = pre.bottom > 0 && pre.top < vh
              && pre.right > 0 && pre.left < vw;
            if (!inView) {
              try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch { /* noop */ }
            }
          }
          const r = el.getBoundingClientRect();
          const vw = window.innerWidth, vh = window.innerHeight;
          return {
            x: r.x, y: r.y, w: r.width, h: r.height,
            cx: r.x + r.width / 2, cy: r.y + r.height / 2,
            visible: r.width > 0 && r.height > 0,
            inViewport: r.bottom > 0 && r.top < vh
              && r.right > 0 && r.left < vw,
          };
        });
      },
      selectors,
      !!opts?.ensureVisible,
    );
  }

  async clickSelector(
    selector: string,
    opts?: {
      button?: 'left' | 'right' | 'middle';
      clickCount?: number;
      linear?: boolean;
      ensureVisible?: boolean;
    },
  ): Promise<{ x: number; y: number; rect: { x: number; y: number; w: number; h: number } }> {
    const [rect] = await this.getRects([selector], { ensureVisible: opts?.ensureVisible ?? true });
    if (!rect || !rect.visible) {
      throw new Error(`clickSelector: selector not found or zero-size: ${selector}`);
    }
    const x = Math.round(rect.cx);
    const y = Math.round(rect.cy);
    if (this.sessionId) {
      inputEventBus.emitClickTarget(this.sessionId, x, y, true, undefined, selector);
    }
    const client = await this.getCDPSession();
    await dispatchClick(client, x, y, {
      button: opts?.button,
      clickCount: opts?.clickCount,
      linear: opts?.linear ?? true,  // selector-based default: deterministic
      sessionId: this.sessionId,
    });
    await this.waitForIdle(1000).catch(() => {});
    return { x, y, rect: { x: rect.x, y: rect.y, w: rect.w, h: rect.h } };
  }

  async dragSelectors(
    fromSelector: string,
    toSelector: string,
    opts?: {
      method?: 'drag' | 'click_click' | 'auto';
      holdMs?: number;
      linear?: boolean;
      steps?: number;
    },
  ): Promise<{
    from: { x: number; y: number };
    to: { x: number; y: number };
    methodUsed: 'drag' | 'click_click';
    mutated: boolean;
  }> {
    const [fromRect, toRect] = await this.getRects(
      [fromSelector, toSelector],
      { ensureVisible: true },
    );
    if (!fromRect || !fromRect.visible) {
      throw new Error(`dragSelectors: fromSelector not found: ${fromSelector}`);
    }
    if (!toRect || !toRect.visible) {
      throw new Error(`dragSelectors: toSelector not found: ${toSelector}`);
    }
    const fx = Math.round(fromRect.cx);
    const fy = Math.round(fromRect.cy);
    const tx = Math.round(toRect.cx);
    const ty = Math.round(toRect.cy);
    const method = opts?.method ?? 'auto';
    const linear = opts?.linear ?? true;
    const client = await this.getCDPSession();

    const fingerprint = async (sel: string): Promise<string> => {
      try {
        return await this.page.evaluate((s: string) => {
          const el = document.querySelector(s);
          if (!el) return '';
          // Nearest container that's likely to mutate on successful action.
          const host = el.closest('[data-board],[class*="board"],[role="grid"],[role="application"]')
            || el.parentElement || el;
          return (host.outerHTML || '').slice(0, 2048);
        }, sel);
      } catch { return ''; }
    };

    const tryClickClick = async (): Promise<void> => {
      await dispatchClick(client, fx, fy, { linear, sessionId: this.sessionId });
      await new Promise((r) => setTimeout(r, opts?.holdMs ?? 120));
      await dispatchClick(client, tx, ty, { linear, sessionId: this.sessionId });
      await this.waitForIdle(600).catch(() => {});
    };
    const tryDrag = async (): Promise<void> => {
      await dispatchDrag(client, fx, fy, tx, ty, {
        linear, steps: opts?.steps, sessionId: this.sessionId,
      });
      await this.waitForIdle(600).catch(() => {});
    };

    if (method === 'click_click') {
      await tryClickClick();
      return { from: { x: fx, y: fy }, to: { x: tx, y: ty }, methodUsed: 'click_click', mutated: true };
    }
    if (method === 'drag') {
      await tryDrag();
      return { from: { x: fx, y: fy }, to: { x: tx, y: ty }, methodUsed: 'drag', mutated: true };
    }

    // auto: snapshot container fingerprint, try click_click, fall back to drag
    const before = await fingerprint(fromSelector);
    await tryClickClick();
    const after = await fingerprint(fromSelector);
    if (before && before === after) {
      await tryDrag();
      const after2 = await fingerprint(fromSelector);
      return {
        from: { x: fx, y: fy }, to: { x: tx, y: ty },
        methodUsed: 'drag',
        mutated: !!after2 && after2 !== before,
      };
    }
    return {
      from: { x: fx, y: fy }, to: { x: tx, y: ty },
      methodUsed: 'click_click',
      mutated: before !== after,
    };
  }

  /**
   * Drag along an arbitrary polyline. For jigsaw traces, connect-the-dots
   * captchas, signature-drawing, and any free-form gesture. Reuses CDP
   * `Input.dispatchMouseEvent` directly since `dispatchDrag` hard-codes a
   * start→end Bezier curve.
   */
  async dragPath(
    points: Array<{ x: number; y: number }>,
    opts?: { holdMs?: number; stepMs?: number; button?: 'left' | 'right' | 'middle' },
  ): Promise<void> {
    if (points.length < 2) {
      throw new Error(`dragPath: need ≥2 points, got ${points.length}`);
    }
    const client = await this.getCDPSession();
    const button = opts?.button ?? 'left';
    const stepMs = opts?.stepMs ?? 16;
    const first = points[0];
    await client.send('Input.dispatchMouseEvent', {
      type: 'mouseMoved', x: first.x, y: first.y,
    });
    await new Promise((r) => setTimeout(r, opts?.holdMs ?? 50));
    await client.send('Input.dispatchMouseEvent', {
      type: 'mousePressed', x: first.x, y: first.y, button, clickCount: 1,
    });
    for (let i = 1; i < points.length; i++) {
      const p = points[i];
      await client.send('Input.dispatchMouseEvent', {
        type: 'mouseMoved', x: Math.round(p.x), y: Math.round(p.y), button,
      });
      if (stepMs > 0) await new Promise((r) => setTimeout(r, stepMs));
    }
    const last = points[points.length - 1];
    await client.send('Input.dispatchMouseEvent', {
      type: 'mouseReleased', x: last.x, y: last.y, button, clickCount: 1,
    });
    await this.waitForIdle(600).catch(() => {});
  }

  // ─── Slider primitive ──────────────────────────────────────────────────
  //
  // Sliders are their own family of control — not a click, not a captcha.
  // Three strategies in descending reliability:
  //
  //   A. Native <input type="range">: set .value, fire input+change events.
  //      React/Vue/Angular bindings all listen for this.
  //   B. ARIA slider (role="slider" / aria-valuenow): click to focus,
  //      drive with ArrowLeft/Right (or Home/End for extremes).
  //   C. Fully custom CSS widget: pixel drag from thumb centre to target
  //      x along the track, value_ratio * track.w.
  //
  // Frame-aware: probes every frame in `page.frames()` and snaps viewport
  // coordinates to the frame-offset so drag/click land on the correct
  // element even when the slider lives inside a (possibly same-origin)
  // embedded widget iframe.

  async setSlider(
    selector: string,
    value: number | [number, number],
    opts?: {
      as?: 'absolute' | 'ratio';
      method?: 'auto' | 'range-input' | 'keyboard' | 'drag';
    },
  ): Promise<{
    strategy: 'range-input' | 'keyboard' | 'drag' | 'unresolved';
    frameUrl: string;
    before: number | number[] | null;
    after: number | number[] | null;
    min: number | null;
    max: number | null;
    step: number | null;
    error?: string;
    framesSearched?: string[];
  }> {
    const as = opts?.as ?? 'absolute';
    const method = opts?.method ?? 'auto';

    // Find the frame that contains the selector (main frame first).
    const { resolved, framesSearched } = await this.resolveInFramesDetailed(selector);
    if (!resolved) {
      return {
        strategy: 'unresolved', frameUrl: '',
        before: null, after: null, min: null, max: null, step: null,
        error: `selector not found in any frame: ${selector}`,
        framesSearched,
      };
    }
    const { frame, kind, meta } = resolved;
    const frameUrl = frame.url();
    const min = meta.min;
    const max = meta.max;
    const step = meta.step ?? 1;

    // Coerce target value into absolute units (respecting min/max).
    const toAbs = (v: number): number => {
      if (as === 'ratio') {
        if (min == null || max == null) return v;
        return min + v * (max - min);
      }
      return v;
    };

    // Strategy A: native range input → value + input/change
    if (kind === 'range-input' && (method === 'auto' || method === 'range-input')) {
      const target = Array.isArray(value)
        ? [toAbs(value[0]), toAbs(value[1])] as [number, number]
        : toAbs(value as number);

      // String-based evaluate to avoid esbuild __name helper injection
      // when the TS source is loaded via tsx watch.
      const src = `(function(sel, target){
        var first = document.querySelector(sel);
        if (!first) return { ok: false, reason: 'not-found' };
        var els = [first];
        if (Array.isArray(target)) {
          var parent = first.parentElement;
          if (parent) {
            var sibs = Array.prototype.slice.call(parent.querySelectorAll('input[type="range"]'));
            if (sibs.length >= 2) els = sibs.slice(0, 2);
          }
        }
        var before = els.map(function(e){ return parseFloat(e.value); });
        var targets = Array.isArray(target) ? target : [target];
        var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
        setter = setter ? setter.set : null;
        for (var i = 0; i < els.length; i++) {
          var el = els[i];
          var tv = targets[Math.min(i, targets.length - 1)];
          var lo = parseFloat(el.min || String(tv));
          var hi = parseFloat(el.max || String(tv));
          var st = parseFloat(el.step || '1') || 1;
          var v = Math.max(lo, Math.min(hi, tv));
          v = Math.round((v - lo) / st) * st + lo;
          v = Math.round(v * 1e8) / 1e8;
          if (setter) setter.call(el, String(v)); else el.value = String(v);
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        }
        var after = els.map(function(e){ return parseFloat(e.value); });
        return { ok: true, before: before, after: after };
      })(${JSON.stringify(selector)}, ${JSON.stringify(target)})`;
      const result = (await frame.evaluate(src)) as
        { ok: true; before: number[]; after: number[] } | { ok: false; reason: string };
      if (result && result.ok === true) {
        await this.waitForIdle(400).catch(() => {});
        const beforeArr: number[] = result.before;
        const afterArr: number[] = result.after;
        const beforeOut: number | number[] = beforeArr.length === 1 ? beforeArr[0] : beforeArr;
        const afterOut: number | number[] = afterArr.length === 1 ? afterArr[0] : afterArr;
        return {
          strategy: 'range-input', frameUrl,
          before: beforeOut, after: afterOut,
          min, max, step,
        };
      }
      // Fall through on failure.
    }

    // Strategy B: ARIA slider → focus then arrow-key steps
    if ((kind === 'aria-slider' || kind === 'range-input')
        && (method === 'auto' || method === 'keyboard')) {
      const target = Array.isArray(value) ? toAbs(value[0]) : toAbs(value);
      // Need current value + viewport rect to focus.
      const stateSrc = `(function(sel){
        var el = document.querySelector(sel);
        if (!el) return null;
        var r = el.getBoundingClientRect();
        var aNow = el.getAttribute('aria-valuenow');
        var aMin = el.getAttribute('aria-valuemin');
        var aMax = el.getAttribute('aria-valuemax');
        return {
          x: r.x, y: r.y, w: r.width, h: r.height,
          cur: aNow != null ? parseFloat(aNow) : (el.value != null ? parseFloat(el.value) : NaN),
          lo: aMin != null ? parseFloat(aMin) : (el.min ? parseFloat(el.min) : NaN),
          hi: aMax != null ? parseFloat(aMax) : (el.max ? parseFloat(el.max) : NaN)
        };
      })(${JSON.stringify(selector)})`;
      const state = (await frame.evaluate(stateSrc)) as
        { x: number; y: number; w: number; h: number; cur: number; lo: number; hi: number } | null;
      if (state && isFinite(state.cur) && isFinite(state.lo) && isFinite(state.hi)) {
        const frameOffset = await this.getFrameOffset(frame);
        const cx = Math.round(state.x + state.w / 2 + frameOffset.x);
        const cy = Math.round(state.y + state.h / 2 + frameOffset.y);
        const client = await this.getCDPSession();
        await dispatchClick(client, cx, cy, { linear: true, sessionId: this.sessionId });
        // Snap to [lo, hi], walk in `step` increments. Cap at 500 steps.
        const clamped = Math.max(state.lo, Math.min(state.hi, target));
        const delta = clamped - state.cur;
        const stepN = step > 0 ? step : 1;
        const n = Math.min(500, Math.abs(Math.round(delta / stepN)));
        // Shortcut extremes.
        if (clamped === state.lo) {
          await dispatchKey(client, 'Home');
        } else if (clamped === state.hi) {
          await dispatchKey(client, 'End');
        } else {
          const keyName = delta >= 0 ? 'ArrowRight' : 'ArrowLeft';
          for (let i = 0; i < n; i++) {
            await dispatchKey(client, keyName);
            if (i % 25 === 24) await new Promise((r) => setTimeout(r, 8));
          }
        }
        const afterSrc = `(function(sel){
          var el = document.querySelector(sel);
          if (!el) return null;
          var aNow = el.getAttribute('aria-valuenow');
          if (aNow != null) return parseFloat(aNow);
          return el.value != null ? parseFloat(el.value) : null;
        })(${JSON.stringify(selector)})`;
        const after = (await frame.evaluate(afterSrc)) as number | null;
        if (after != null && isFinite(after)) {
          await this.waitForIdle(300).catch(() => {});
          return {
            strategy: 'keyboard', frameUrl,
            before: state.cur, after,
            min: state.lo, max: state.hi, step: stepN,
          };
        }
      }
      // Fall through on failure.
    }

    // Strategy C: pixel drag from thumb to target position along track
    if (method === 'auto' || method === 'drag') {
      const ratio = typeof value === 'number'
        ? (as === 'ratio'
            ? Math.max(0, Math.min(1, value))
            : (min != null && max != null
                ? (value - min) / Math.max(1e-9, (max - min))
                : 0.5))
        : (as === 'ratio' ? value[0] : 0.5);
      const rectSrc = `(function(sel){
        var el = document.querySelector(sel);
        if (!el) return null;
        var track = el.closest('[role="slider"],[class*="slider" i],[class*="track" i]') || el;
        var tr = track.getBoundingClientRect();
        var hr = el.getBoundingClientRect();
        return {
          track: { x: tr.x, y: tr.y, w: tr.width, h: tr.height },
          thumb: { x: hr.x, y: hr.y, w: hr.width, h: hr.height }
        };
      })(${JSON.stringify(selector)})`;
      const rect = (await frame.evaluate(rectSrc)) as
        { track: { x: number; y: number; w: number; h: number }; thumb: { x: number; y: number; w: number; h: number } } | null;
      if (!rect) {
        return {
          strategy: 'unresolved', frameUrl,
          before: null, after: null, min, max, step,
          error: 'could not read thumb/track rect',
        };
      }
      const off = await this.getFrameOffset(frame);
      const startX = Math.round(rect.thumb.x + rect.thumb.w / 2 + off.x);
      const startY = Math.round(rect.thumb.y + rect.thumb.h / 2 + off.y);
      const endX = Math.round(
        rect.track.x + rect.track.w * Math.max(0, Math.min(1, ratio)) + off.x,
      );
      const endY = startY;
      const client = await this.getCDPSession();
      await dispatchDrag(client, startX, startY, endX, endY, {
        linear: false,
        steps: 30,
        sessionId: this.sessionId,
      });
      await this.waitForIdle(400).catch(() => {});
      // Best-effort read-back (range input exposes .value; others may not).
      const afterSrc2 = `(function(sel){
        var el = document.querySelector(sel);
        if (!el) return null;
        var aNow = el.getAttribute('aria-valuenow');
        if (aNow != null) return parseFloat(aNow);
        return el.value != null ? parseFloat(el.value) : null;
      })(${JSON.stringify(selector)})`;
      const after = (await frame.evaluate(afterSrc2)) as number | null;
      return {
        strategy: 'drag', frameUrl,
        before: null,
        after: after != null && isFinite(after) ? after : null,
        min, max, step,
      };
    }

    return {
      strategy: 'unresolved', frameUrl,
      before: null, after: null, min, max, step,
      error: `no strategy applied (method=${method})`,
    };
  }

  /**
   * Locate a selector across all frames (main frame tried first). Returns
   * the frame plus a classification (`range-input` | `aria-slider` | `other`)
   * and any available min/max/step metadata.
   */
  private async resolveInFramesDetailed(selector: string): Promise<{
    resolved: {
      frame: import('puppeteer-core').Frame;
      kind: 'range-input' | 'aria-slider' | 'other';
      meta: { min: number | null; max: number | null; step: number | null };
    } | null;
    framesSearched: string[];
  }> {
    const frames: import('puppeteer-core').Frame[] = this.page.frames();
    // Reorder: main frame first, then same-origin, then others.
    const main = this.page.mainFrame();
    const sorted = [main, ...frames.filter((f) => f !== main)];
    const framesSearched: string[] = [];
    const probeSrc = `(function(sel){
      var el = document.querySelector(sel);
      if (!el) return null;
      var tag = el.tagName.toLowerCase();
      var type = (el.type || '').toLowerCase();
      var role = el.getAttribute('role');
      var kind = 'other';
      if (tag === 'input' && type === 'range') kind = 'range-input';
      else if (role === 'slider' || el.hasAttribute('aria-valuenow')) kind = 'aria-slider';
      function parseNum(s){ if (s == null || s === '') return null; var n = parseFloat(s); return isFinite(n) ? n : null; }
      var min = kind === 'range-input' ? parseNum(el.min) : parseNum(el.getAttribute('aria-valuemin'));
      var max = kind === 'range-input' ? parseNum(el.max) : parseNum(el.getAttribute('aria-valuemax'));
      var step = kind === 'range-input' ? parseNum(el.step) : null;
      return { kind: kind, meta: { min: min, max: max, step: step } };
    })(${JSON.stringify(selector)})`;
    for (const frame of sorted) {
      const url = frame.url();
      try {
        const probe = (await frame.evaluate(probeSrc)) as
          { kind: 'range-input' | 'aria-slider' | 'other'; meta: { min: number | null; max: number | null; step: number | null } } | null;
        framesSearched.push(`${url} (ok, found=${probe ? 'y' : 'n'})`);
        if (probe) return { resolved: { frame, kind: probe.kind, meta: probe.meta }, framesSearched };
      } catch (e) {
        framesSearched.push(`${url} (err: ${(e as Error).message})`);
      }
    }
    return { resolved: null, framesSearched };
  }

  /**
   * Vision-indexed slider drag. Caller has already resolved bboxes via
   * the cached vision response (`browser_list_sliders`-style flow) —
   * here we just do the arithmetic and fire the drag.
   *
   *   target_x = track.x + clamp(ratio, 0, 1) * track.w
   *
   * handleBbox center is the drag start; target is (target_x, handle_cy).
   * No frame resolution needed — CDP dispatches in document coords.
   */
  async setSliderAt(
    handleBbox: { x: number; y: number; w: number; h: number },
    trackBbox: { x: number; y: number; w: number; h: number },
    ratio: number,
  ): Promise<{
    strategy: 'vision-drag';
    handle_bbox: { x: number; y: number; w: number; h: number };
    track_bbox: { x: number; y: number; w: number; h: number };
    target_px: { x: number; y: number };
  }> {
    const clamped = Math.max(0, Math.min(1, ratio));
    const startX = Math.round(handleBbox.x + handleBbox.w / 2);
    const startY = Math.round(handleBbox.y + handleBbox.h / 2);
    const endX = Math.round(trackBbox.x + trackBbox.w * clamped);
    const endY = startY;
    const client = await this.getCDPSession();
    await dispatchDrag(client, startX, startY, endX, endY, {
      linear: false,
      steps: 30,
      sessionId: this.sessionId,
    });
    await this.waitForIdle(400).catch(() => {});
    return {
      strategy: 'vision-drag',
      handle_bbox: handleBbox,
      track_bbox: trackBbox,
      target_px: { x: endX, y: endY },
    };
  }

  /**
   * DOM-only slider enumeration. Walks every frame looking for
   * slider-shaped elements — native range inputs, ARIA sliders,
   * and custom widgets whose class names include "handle"/"thumb"/
   * "slider" inside a slider-looking container. Returns bboxes in
   * document coordinates + the nearest row-level label text, so the
   * caller can pick a slider without needing the vision pipeline.
   *
   * This is the escape hatch for pages where Gemini vision times out
   * (e.g. Chase retirement calculators).
   */
  async listSliderHandles(): Promise<Array<{
    index: number;
    frame_url: string;
    kind: 'range-input' | 'aria-slider' | 'custom';
    bbox: { x: number; y: number; w: number; h: number };
    label: string;
  }>> {
    const scanSrc = `(function(){
      try {
        var out = [];
        // Heuristic selector: native range, ARIA sliders, and elements
        // likely to be thumb/handle/slider-button. We'll dedupe later.
        var sel = [
          'input[type="range"]',
          '[role="slider"]',
          '[aria-valuenow]',
          '[class*="handle" i]',
          '[class*="thumb" i]',
          '[class*="slider-button" i]',
          '[class*="slider-handle" i]',
          '[data-handle]',
        ].join(',');
        var found = Array.prototype.slice.call(document.querySelectorAll(sel));
        var seen = new Set();
        for (var i = 0; i < found.length; i++) {
          var el = found[i];
          var r = el.getBoundingClientRect();
          if (!r || r.width < 3 || r.height < 3) continue;
          if (r.width > 200 || r.height > 200) continue;  // skip full tracks/wrappers
          var key = Math.round(r.left) + '_' + Math.round(r.top) + '_' + Math.round(r.width) + '_' + Math.round(r.height);
          if (seen.has(key)) continue;
          seen.add(key);

          // Classify.
          var tag = el.tagName.toLowerCase();
          var type = (el.type || '').toLowerCase();
          var role = el.getAttribute('role');
          var kind = 'custom';
          if (tag === 'input' && type === 'range') kind = 'range-input';
          else if (role === 'slider' || el.hasAttribute('aria-valuenow')) kind = 'aria-slider';

          // Find the nearest row-level label (same textContent scanner
          // we use in dragSliderUntil). Looks up to 120px above/below.
          var hcy = r.top + r.height / 2;
          var ytol = Math.max(r.height * 4, 80);
          var label = '';
          var bestDy = Infinity;
          var walker = document.createTreeWalker(
            document.body || document.documentElement,
            NodeFilter.SHOW_ELEMENT, null);
          var cand;
          while ((cand = walker.nextNode())) {
            if (cand === el || cand.contains(el)) continue;
            var cr = cand.getBoundingClientRect();
            if (!cr || cr.width === 0 || cr.height === 0) continue;
            if (cr.height > 80) continue;
            var text = (cand.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!text || text.length > 200 || text.length < 3) continue;
            var ccy = cr.top + cr.height / 2;
            var dy = Math.abs(ccy - hcy);
            if (dy > ytol) continue;
            // Prefer labels that contain a letter (skip pure "25" tick marks).
            if (!/[A-Za-z]/.test(text)) continue;
            if (dy < bestDy) {
              bestDy = dy;
              label = text;
            }
          }

          out.push({
            kind: kind,
            bbox: { x: r.left, y: r.top, w: r.width, h: r.height },
            label: label,
          });
        }
        return out;
      } catch (e) {
        return { error: String(e && e.message || e) };
      }
    })()`;

    const result: Array<{
      index: number; frame_url: string; kind: 'range-input' | 'aria-slider' | 'custom';
      bbox: { x: number; y: number; w: number; h: number }; label: string;
    }> = [];
    const frames = this.page.frames();
    const main = this.page.mainFrame();
    const ordered = [main, ...frames.filter((f) => f !== main)];
    for (const frame of ordered) {
      let offX = 0, offY = 0;
      if (frame !== main) {
        try {
          const fe = await frame.frameElement();
          if (fe) {
            const box = await fe.boundingBox();
            if (box) { offX = box.x; offY = box.y; }
          }
        } catch { /* skip */ }
      }
      try {
        const hits = (await frame.evaluate(scanSrc)) as
          Array<{ kind: 'range-input' | 'aria-slider' | 'custom';
                  bbox: { x: number; y: number; w: number; h: number };
                  label: string }>
          | { error: string } | null;
        if (!hits || !Array.isArray(hits)) continue;
        for (const h of hits) {
          result.push({
            index: result.length,
            frame_url: frame.url(),
            kind: h.kind,
            // Translate iframe-local bbox to document coords.
            bbox: {
              x: Math.round(h.bbox.x + offX),
              y: Math.round(h.bbox.y + offY),
              w: Math.round(h.bbox.w),
              h: Math.round(h.bbox.h),
            },
            label: h.label,
          });
        }
      } catch { /* frame detached / protected */ }
    }
    return result;
  }

  /**
   * Closed-loop slider drag. Holds the mouse down on the handle, steps
   * incrementally, polls every frame's DOM for a labelled value, and
   * stops when the target value is reached. This is the "watch the
   * number while you drag" pattern — works for custom slider widgets
   * where open-loop (target_x = ratio * track.w) fails because vision
   * didn't identify the track, or the widget uses non-linear scaling,
   * or has hidden snap points.
   *
   * Discovery: scan every frame's body for text nodes matching
   * `labelPattern` (RegExp string). First capture group is expected to
   * be the numeric value. Match text nodes whose parent element's bbox
   * is within ±verticalTolerance of the handle centre Y — this
   * associates the label with the correct slider when several sliders
   * stack vertically.
   */
  async dragSliderUntil(
    handleBbox: { x: number; y: number; w: number; h: number },
    targetValue: number,
    opts?: {
      labelPattern?: string;   // JS regex source; must have one capture group for the number
      tolerance?: number;      // |target - observed| within this → done
      maxIterations?: number;  // safety cap; default 25
      stepPx?: number;         // initial pixel step per iteration; default 8
      direction?: 'auto' | 'left' | 'right'; // 'auto' infers from value delta
    },
  ): Promise<{
    strategy: 'closed-loop';
    iterations: number;
    initial_value: number | null;
    final_value: number | null;
    target_value: number;
    tolerance: number;
    trace: Array<{ iter: number; cursor_x: number; value: number | null }>;
    label_text: string | null;
    label_selector_hint: string | null;
    completed: boolean;
  }> {
    const tolerance = opts?.tolerance ?? 0;
    const maxIter = opts?.maxIterations ?? 25;
    const initialStep = opts?.stepPx ?? 8;
    const userDir = opts?.direction ?? 'auto';
    const patternSrc = opts?.labelPattern
      ?? '(-?\\d+(?:\\.\\d+)?)';  // fallback: any number

    const handleCx = Math.round(handleBbox.x + handleBbox.w / 2);
    const handleCy = Math.round(handleBbox.y + handleBbox.h / 2);

    // Build the per-frame scan script. Walks ELEMENTS (not text nodes)
    // so the regex can match labels split across spans like
    // `<label>Age Range: <span>25</span> to <span>75</span></label>` —
    // textContent concatenates descendants into one string. Filters
    // (height <= 80, textContent <= 300) keep us on row-sized elements
    // and reject large ancestors (body, main) that would match anything.
    const scanSrc = (handleCyLocal: number, yTolerance: number) => `(function(pat, hcy, ytol){
      try {
        var re = new RegExp(pat);
        var best = null;
        var walker = document.createTreeWalker(
          document.body || document.documentElement,
          NodeFilter.SHOW_ELEMENT,
          null
        );
        var el;
        while ((el = walker.nextNode())) {
          var r = el.getBoundingClientRect();
          if (!r || r.width === 0 || r.height === 0) continue;
          if (r.height > 80) continue;
          var text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
          if (!text || text.length > 300) continue;
          var m = re.exec(text);
          if (!m) continue;
          var num = parseFloat(m[1]);
          if (!isFinite(num)) continue;
          var cy = r.top + r.height / 2;
          var dy = Math.abs(cy - hcy);
          if (dy > ytol) continue;
          var area = r.width * r.height;
          if (best === null || dy < best.dy || (dy === best.dy && area < best.area)) {
            best = { dy: dy, area: area, value: num, text: text,
                     x: r.left, y: r.top, w: r.width, h: r.height };
          }
        }
        return best ? { value: best.value, text: best.text,
                        x: best.x, y: best.y, w: best.w, h: best.h } : null;
      } catch (e) { return null; }
    })(${JSON.stringify(patternSrc)}, ${handleCyLocal}, ${yTolerance})`;

    // readValue: scan every frame; account for its viewport offset so
    // the handleCy (which is in document coords) is converted back to
    // iframe-local coords when querying. yTolerance widened to ~80px
    // because labels usually sit 30-50px above the slider track.
    const yTolerance = Math.max(handleBbox.h * 4, 80);
    const readValue = async (): Promise<{
      value: number | null; text: string | null;
    }> => {
      const frames = this.page.frames();
      for (const frame of frames) {
        let offX = 0, offY = 0;
        if (frame !== this.page.mainFrame()) {
          try {
            const fe = await frame.frameElement();
            if (fe) {
              const box = await fe.boundingBox();
              if (box) { offX = box.x; offY = box.y; }
            }
          } catch { /* skip */ }
        }
        const localCy = handleCy - offY;
        // Only scan frames whose viewport could contain the handle row.
        // (Handles the case of mini-iframes unrelated to the slider.)
        try {
          const result = await frame.evaluate(scanSrc(localCy, yTolerance));
          if (result) {
            const r = result as { value: number; text: string };
            return { value: r.value, text: r.text };
          }
        } catch { /* frame detached or cross-origin guarded */ }
      }
      return { value: null, text: null };
    };

    // Kick off: read initial value. If we can't read it at all, fail
    // BEFORE pressing the mouse — silent stepping without feedback is
    // how we get "hallucinated" slider drags.
    const initial = await readValue();
    const trace: Array<{ iter: number; cursor_x: number; value: number | null }> = [];
    trace.push({ iter: 0, cursor_x: handleCx, value: initial.value });

    if (initial.value === null) {
      // Grab a few nearby element labels for diagnostics. Uses the same
      // element-walk + textContent logic as the main scanner, so the
      // LLM sees what labels ARE on the row (e.g. "Age Range: 25 to 75")
      // and can adjust the regex accordingly.
      const sampleSrc = `(function(hcy, ytol){
        try {
          var out = [];
          var walker = document.createTreeWalker(
            document.body || document.documentElement,
            NodeFilter.SHOW_ELEMENT, null);
          var el;
          while ((el = walker.nextNode())) {
            var r = el.getBoundingClientRect();
            if (!r || r.width === 0 || r.height === 0) continue;
            if (r.height > 80) continue;
            var text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!text || text.length > 300) continue;
            var cy = r.top + r.height / 2;
            if (Math.abs(cy - hcy) > ytol) continue;
            out.push(text);
            if (out.length >= 10) break;
          }
          return out;
        } catch(e) { return []; }
      })(${handleCy}, ${yTolerance})`;
      const samples: string[] = [];
      for (const frame of this.page.frames()) {
        try {
          const res = (await frame.evaluate(sampleSrc)) as string[];
          if (res && res.length) samples.push(...res);
        } catch { /* skip */ }
        if (samples.length >= 10) break;
      }
      return {
        strategy: 'closed-loop',
        iterations: 0,
        initial_value: null,
        final_value: null,
        target_value: targetValue,
        tolerance,
        trace,
        label_text: `NO_MATCH — nearby text: ${JSON.stringify(samples.slice(0, 8))}`,
        label_selector_hint: null,
        completed: false,
      };
    }

    const client = await this.getCDPSession();
    // Position cursor then press.
    await client.send('Input.dispatchMouseEvent', {
      type: 'mouseMoved', x: handleCx, y: handleCy,
    });
    await new Promise((r) => setTimeout(r, 50));
    await client.send('Input.dispatchMouseEvent', {
      type: 'mousePressed', x: handleCx, y: handleCy,
      button: 'left', clickCount: 1,
    });

    let cursorX = handleCx;
    let lastValue: number | null = initial.value;
    let iters = 0;
    let completed = false;

    // Adaptive step size: start at initialStep; adjust by observed
    // value-per-pixel sensitivity after each move.
    let stepPx = initialStep;
    let consecutiveMisses = 0;

    try {
      for (iters = 1; iters <= maxIter; iters++) {
        // Direction logic.
        let dir: 1 | -1 = 1;
        if (userDir === 'left') dir = -1;
        else if (userDir === 'right') dir = 1;
        else if (lastValue != null) {
          if (Math.abs(lastValue - targetValue) <= tolerance) {
            completed = true;
            break;
          }
          dir = targetValue > lastValue ? 1 : -1;
        }

        const prevX = cursorX;
        const prevValue = lastValue;
        const nextX = Math.round(cursorX + dir * stepPx);
        // Smooth tween to the next X — small number of intermediate events
        // so the widget's pointerMove handlers fire the same way a human
        // drag would (React listeners throttle on mousemove rate).
        const subSteps = 4;
        for (let s = 1; s <= subSteps; s++) {
          const t = s / subSteps;
          const ix = Math.round(cursorX + (nextX - cursorX) * t);
          await client.send('Input.dispatchMouseEvent', {
            type: 'mouseMoved', x: ix, y: handleCy, button: 'left',
          });
          await new Promise((r) => setTimeout(r, 8));
        }
        cursorX = nextX;
        // Let the page process pointermove handlers.
        await new Promise((r) => setTimeout(r, 30));

        const reading = await readValue();
        trace.push({ iter: iters, cursor_x: cursorX, value: reading.value });

        if (reading.value != null) {
          consecutiveMisses = 0;
          // Update adaptive step: value-per-pixel from this delta.
          if (prevValue != null && cursorX !== prevX) {
            const vpp = (reading.value - prevValue) / (cursorX - prevX);
            if (isFinite(vpp) && Math.abs(vpp) > 1e-6) {
              const remaining = targetValue - reading.value;
              // Aim for remaining/2 on the next hop to avoid overshoot.
              const suggested = Math.abs(remaining / vpp) * 0.5;
              stepPx = Math.max(1, Math.min(80, Math.round(suggested)));
            }
          }
          lastValue = reading.value;
          if (Math.abs(lastValue - targetValue) <= tolerance) {
            completed = true;
            break;
          }
        } else {
          // Lost the value — shrink step. After 3 consecutive misses,
          // abort: the pattern probably doesn't match anything useful
          // and we'd otherwise drag blindly.
          consecutiveMisses++;
          if (consecutiveMisses >= 3) {
            break;
          }
          stepPx = Math.max(1, Math.floor(stepPx / 2));
        }
      }
    } finally {
      // Always release.
      await client.send('Input.dispatchMouseEvent', {
        type: 'mouseReleased', x: cursorX, y: handleCy,
        button: 'left', clickCount: 1,
      }).catch(() => {});
    }

    await this.waitForIdle(300).catch(() => {});

    return {
      strategy: 'closed-loop',
      iterations: iters,
      initial_value: initial.value,
      final_value: lastValue,
      target_value: targetValue,
      tolerance,
      trace,
      label_text: initial.text,
      label_selector_hint: null,
      completed,
    };
  }

  /**
   * Get the viewport offset of a frame's origin. Needed because CDP
   * mouse events are dispatched in top-level document coordinates, but
   * `getBoundingClientRect()` inside a frame is relative to that frame.
   * For the main frame this is always (0, 0).
   */
  private async getFrameOffset(
    frame: import('puppeteer-core').Frame,
  ): Promise<{ x: number; y: number }> {
    if (frame === this.page.mainFrame()) return { x: 0, y: 0 };
    try {
      const handle = await frame.frameElement();
      if (!handle) return { x: 0, y: 0 };
      const box = await handle.boundingBox();
      if (!box) return { x: 0, y: 0 };
      return { x: box.x, y: box.y };
    } catch {
      return { x: 0, y: 0 };
    }
  }

  /**
   * Screenshot a region of the viewport and return it as base64 JPEG.
   * Lets puzzle solvers ask focused visual questions (template match,
   * OCR, tiny vision crop) without paying for a full-page Gemini pass.
   */
  async getImageRegion(
    bbox: { x: number; y: number; w: number; h: number },
    opts?: { quality?: number },
  ): Promise<string> {
    const buf = await this.page.screenshot({
      type: 'jpeg',
      quality: opts?.quality ?? 80,
      clip: {
        x: Math.max(0, Math.round(bbox.x)),
        y: Math.max(0, Math.round(bbox.y)),
        width: Math.max(1, Math.round(bbox.w)),
        height: Math.max(1, Math.round(bbox.h)),
      },
    });
    return Buffer.from(buf).toString('base64');
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
        await dispatchClick(client, coords.x, coords.y, { sessionId: this.sessionId });
        await new Promise((r) => setTimeout(r, 100));

        if (clear) {
          await clearField(client, coords.x, coords.y);
          await new Promise((r) => setTimeout(r, 50));
        }

        await cdpTypeText(client, text, 30, { sessionId: this.sessionId });
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

  // Closed-loop scroll: walks the page in `direction` one viewport-fraction
  // at a time, polling DOM after each step for an element matching
  // `targetText` (case-insensitive substring or regex) and/or `targetRole`.
  // Stops on match, on a scrollY plateau (page end / page start signal),
  // or after `maxIterations`. The plateau detection is the load-bearing
  // bit: it tells the brain "no more content this way" so it doesn't
  // ask for more scrolling forever.
  async scrollUntil(opts: {
    targetText?: string;
    targetRole?: string;
    direction?: 'up' | 'down';
    maxIterations?: number;
    stepRatio?: number;
  }): Promise<{
    found: boolean;
    iterations: number;
    finalScrollY: number;
    scrolledPx: number;
    reason: 'matched' | 'page_end' | 'page_start' | 'max_iterations' | 'no_target';
    matchedSelector?: string;
    matchedText?: string;
  }> {
    const direction = opts.direction === 'up' ? 'up' : 'down';
    const maxIter = Math.max(1, Math.min(40, opts.maxIterations ?? 10));
    const stepRatio = Math.max(0.1, Math.min(1.0, opts.stepRatio ?? 0.8));
    const targetText = (opts.targetText ?? '').trim();
    const targetRole = (opts.targetRole ?? '').trim();

    if (!targetText && !targetRole) {
      const info = await this.getScrollInfo();
      return {
        found: false,
        iterations: 0,
        finalScrollY: info[0],
        scrolledPx: 0,
        reason: 'no_target',
      };
    }

    // Compile the regex once (caller-side) — regex literal first, fall
    // back to a substring search inside the page if it's not a valid
    // regex. Mirrors the BrowserDragSliderUntilTool label_pattern path.
    let regexSrc = '';
    let isRegex = false;
    if (targetText) {
      try {
        // Anchor-free regex: caller provides the pattern, we just compile it.
        new RegExp(targetText, 'i');
        regexSrc = targetText;
        isRegex = true;
      } catch {
        regexSrc = targetText;
        isRegex = false;
      }
    }

    const startInfo = await this.getScrollInfo();
    const startY = startInfo[0];
    const viewportH = startInfo[1];
    const stepDelta = Math.round(viewportH * stepRatio);

    let lastY = startY;
    let plateauHits = 0;
    let iterations = 0;
    let matchedSelector: string | undefined;
    let matchedText: string | undefined;

    // Polling helper — runs in the page context to find a visible element
    // matching the target. Returns { selector, text } or null.
    const findMatch = async (): Promise<{ selector: string; text: string } | null> => {
      return await this.page.evaluate(
        (args: { regexSrc: string; isRegex: boolean; role: string }) => {
          const { regexSrc: rs, isRegex: ir, role } = args;
          const matchText = (txt: string): boolean => {
            if (!rs) return true;
            if (ir) {
              try {
                return new RegExp(rs, 'i').test(txt);
              } catch {
                return txt.toLowerCase().includes(rs.toLowerCase());
              }
            }
            return txt.toLowerCase().includes(rs.toLowerCase());
          };
          const isVisible = (el: Element): boolean => {
            const r = (el as HTMLElement).getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return false;
            const vpH = window.innerHeight;
            const vpW = window.innerWidth;
            // Must be at least partially in viewport.
            if (r.bottom < 0 || r.top > vpH) return false;
            if (r.right < 0 || r.left > vpW) return false;
            const cs = window.getComputedStyle(el as HTMLElement);
            if (cs.visibility === 'hidden' || cs.display === 'none') return false;
            return true;
          };
          // Candidate set — bias toward labelled / interactive elements
          // since those are what users typically scroll *to*. Falls back
          // to all text-bearing elements when nothing matches.
          const interactive = Array.from(document.querySelectorAll(
            'a, button, input, select, textarea, label, summary, ' +
            '[role], [aria-label], [data-testid], h1, h2, h3, h4, h5, ' +
            'li, td, th, span, div'
          ));
          for (const el of interactive) {
            if (!isVisible(el)) continue;
            if (role) {
              const elRole = (el.getAttribute('role') || el.tagName.toLowerCase()).toLowerCase();
              if (elRole !== role.toLowerCase()) continue;
            }
            const txt = ((el as HTMLElement).innerText || (el as HTMLElement).textContent || '').trim();
            const ariaLbl = el.getAttribute('aria-label') || '';
            const placeholder = el.getAttribute('placeholder') || '';
            const composite = `${txt}\n${ariaLbl}\n${placeholder}`.trim();
            if (matchText(composite)) {
              const selectorBits: string[] = [el.tagName.toLowerCase()];
              const id = el.getAttribute('id');
              if (id) selectorBits.push(`#${id}`);
              const dt = el.getAttribute('data-testid');
              if (dt) selectorBits.push(`[data-testid="${dt}"]`);
              return {
                selector: selectorBits.join(''),
                text: composite.slice(0, 120),
              };
            }
          }
          return null;
        },
        { regexSrc, isRegex, role: targetRole },
      ) as { selector: string; text: string } | null;
    };

    // Check before any scroll — target may already be visible.
    const initialMatch = await findMatch();
    if (initialMatch) {
      const info = await this.getScrollInfo();
      return {
        found: true,
        iterations: 0,
        finalScrollY: info[0],
        scrolledPx: 0,
        reason: 'matched',
        matchedSelector: initialMatch.selector,
        matchedText: initialMatch.text,
      };
    }

    while (iterations < maxIter) {
      iterations += 1;
      // Step. We re-use scrollPage's behavior (window.scrollBy with a
      // ~viewport-100px chunk) but parametric on stepRatio so callers
      // can take smaller steps on dense pages.
      const delta = direction === 'down' ? stepDelta : -stepDelta;
      await this.page.evaluate((d: number) => {
        window.scrollBy(0, d);
      }, delta);
      // Brief settle for lazy-loaded content. 300ms balances against the
      // need to make ~10 iterations finish under 5 seconds.
      await new Promise((r) => setTimeout(r, 300));

      const info = await this.getScrollInfo();
      const y = info[0];

      // Plateau detection — same scrollY twice in a row means the page
      // can't scroll further this direction.
      if (Math.abs(y - lastY) < 2) {
        plateauHits += 1;
        if (plateauHits >= 2) {
          return {
            found: false,
            iterations,
            finalScrollY: y,
            scrolledPx: y - startY,
            reason: direction === 'down' ? 'page_end' : 'page_start',
          };
        }
      } else {
        plateauHits = 0;
      }
      lastY = y;

      const m = await findMatch();
      if (m) {
        matchedSelector = m.selector;
        matchedText = m.text;
        return {
          found: true,
          iterations,
          finalScrollY: y,
          scrolledPx: y - startY,
          reason: 'matched',
          matchedSelector,
          matchedText,
        };
      }
    }

    const finalInfo = await this.getScrollInfo();
    return {
      found: false,
      iterations,
      finalScrollY: finalInfo[0],
      scrolledPx: finalInfo[0] - startY,
      reason: 'max_iterations',
    };
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
