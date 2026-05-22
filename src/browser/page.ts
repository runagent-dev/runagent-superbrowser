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
import { SHADOW_DOM_HELPERS_SRC } from './slider-helpers.js';
import { typeText as cdpTypeText, pressKeyCombo, clearField, dispatchKey } from './input-keyboard.js';
import { getElementCenterBySelector } from './elements.js';
import { findCursorInteractiveElements, formatCursorElements } from './cursor-detect.js';
import { findFirstInteractiveMatch } from './scroll-probe.js';
import { ConsoleCollector, type CollectedLog } from './console-collector.js';
import { DownloadMonitor } from './download-monitor.js';
import { validateUrl } from '../server/auth.js';
import type { FailureReason } from '../agent/types.js';
import { sanitizeImageBuffer } from './image-safety.js';
import {
  waitForPageReady,
  waitForVisualStable,
  detectErrorPage,
  type ErrorPage,
  type PageRef,
} from './page-readiness.js';
import { feedbackBus } from '../agent/feedback-bus.js';
import { inputEventBus } from './input-events.js';
import { BadRequest } from './errors.js';

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

  /**
   * Page reference frame (scrollY, scrollHeight, viewport dims) at the
   * moment the LAST vision-capable /state was served. The /click
   * handler reads this to detect "page has shifted since the brain saw
   * the screenshot" — when shifted, the V_n bbox the brain captured
   * resolves against a stale frame and the click would land on a
   * neighbour. See `compareViewportShift` in `page-readiness.ts`.
   *
   * Updated only when the brain actually receives a screenshot
   * (useVision=true); a no-vision /state probe doesn't reset this so
   * the brain's visual mental model stays the source of truth.
   */
  private lastVisionPageRef: PageRef | null = null;

  /** Read what the page reference frame was when the brain last got a
   *  screenshot. Returns null until the first vision capture. */
  public getVisionPageRef(): PageRef | null {
    return this.lastVisionPageRef;
  }

  /** Update the stored vision page reference. Called by the /state
   *  handler immediately after the screenshot is built. */
  public setVisionPageRef(ref: PageRef): void {
    this.lastVisionPageRef = ref;
  }

  constructor(
    private page: Page,
    private config: BrowserConfig,
  ) {
    // Programmatic navigation (location.href=, history.pushState that
    // triggers a load, server redirects, meta-refresh) doesn't go
    // through `navigate()` — but the vision page reference frame
    // captured against the old document is now stale, and the /click
    // handler's compareViewportShift gate will reject every legitimate
    // click until the next /state-with-vision overwrites it. Invalidate
    // here so the gate short-circuits via `stored==null` instead.
    this.page.on('framenavigated', (frame) => {
      if (frame === this.page.mainFrame()) {
        this.lastVisionPageRef = null;
      }
    });
  }

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

    // The vision page reference frame from the previous document no
    // longer applies — null it before goto() so the /click viewport-
    // shift gate doesn't reject early clicks on the new page. The
    // framenavigated hook also fires during goto(), but explicit
    // invalidation here covers cases where goto() throws partway.
    this.lastVisionPageRef = null;

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

    // v5 — VISUAL stability gate. DOM-ready isn't enough for screenshot-
    // driven vision: web fonts swap 200-1200ms after DCL (FOUT), shifting
    // text vertically; lazy hero images load late, pushing content down;
    // hydration commits frames after first paint. Without this wait,
    // bboxes computed against the pre-settled screenshot point at empty
    // space ABOVE where text actually lives by click time. Hard cap at
    // 1500ms means worst-case adds ~1500ms to a cold first navigation.
    // VISUAL_STABLE_DISABLE=1 to bypass.
    await waitForVisualStable(this.page).catch(() => {});

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
          // A1: pre-dispatch element-at-coords sanity check. After
          // probe + scroll-into-view, verify that the topmost element
          // at (coords.x, coords.y) is the expected element OR a
          // descendant of it. Catches cases where a transient overlay
          // (toast, tooltip, lazy-loaded modal) appeared between
          // probe and dispatch — without this, the click silently
          // lands on the overlay instead of the intended element.
          // Env-flag escape: SUPERBROWSER_PRECLICK_VALIDATE=0.
          const preclickEnabled = process.env.SUPERBROWSER_PRECLICK_VALIDATE !== '0';
          if (preclickEnabled) {
            // v6 H1: ambiguity flag from getElementCenterBySelector.
            // When the selector matches multiple elements, A1 must be
            // STRICTER — only accept exact-element match (no
            // descendant/ancestor leniency) to ensure we hit the
            // intended one of the matches.
            const ambiguous = (coords as unknown as { ambiguous?: boolean })
              .ambiguous === true;
            const validation = await this.page.evaluate(
              (args: { sel: string; cx: number; cy: number; ambiguous: boolean; driftPx: number }) => {
                const expected = document.querySelector(args.sel) as HTMLElement | null;
                if (!expected) return { ok: true, reason: 'no_expected' as const };
                let stack: Element[] = [];
                try {
                  stack = document.elementsFromPoint(args.cx, args.cy);
                } catch {
                  stack = [];
                }
                // v6 H4: empty stack is now a HARD fail. Off-viewport
                // coords or a transparent pixel mean the click won't
                // land on anything useful. Force the brain to scroll
                // the element into view and re-screenshot.
                if (stack.length === 0) {
                  return {
                    ok: false,
                    reason: 'empty_stack' as const,
                    overlay: 'transparent_or_off_viewport',
                    overlayText: '',
                  };
                }
                // Topmost element. v6 H3: dropped the
                // `top.contains(expected)` clause — it's too lenient
                // (accepts wrong sibling under shared ancestor when
                // page shifts). Keep:
                //   * exact match (top === expected)
                //   * descendant match (top is INSIDE expected — click
                //     bubbles up correctly to expected handler)
                // Reject ancestor case: when topmost is an ancestor of
                // expected, the click would dispatch on the ancestor
                // and may hit a different child entirely.
                const top = stack[0] as HTMLElement;
                if (top === expected) {
                  // v6 F3: live-rect drift check. Even when topmost
                  // matches, if the element's CURRENT bounds differ
                  // significantly from the click coords (>driftPx),
                  // the page is mid-render and we'd dispatch on a
                  // moving target. Refuse so the brain re-screenshots.
                  if (args.driftPx > 0) {
                    const r = expected.getBoundingClientRect();
                    const ncx = Math.round(r.left + r.width / 2);
                    const ncy = Math.round(r.top + r.height / 2);
                    const dx = ncx - args.cx;
                    const dy = ncy - args.cy;
                    if (Math.sqrt(dx * dx + dy * dy) > args.driftPx) {
                      return {
                        ok: false,
                        reason: 'rect_shifted' as const,
                        overlay: `shifted_by_${dx}_${dy}`,
                        overlayText: '',
                      };
                    }
                  }
                  return { ok: true, reason: 'match_exact' as const };
                }
                if (!args.ambiguous && expected.contains(top)) {
                  // top is INSIDE expected — clicking it bubbles up
                  // to expected's handler. Safe.
                  return { ok: true, reason: 'match_via_descendant' as const };
                }
                // Iframe heuristic: clicking would hit the iframe
                // host instead of inner content.
                if (top.tagName.toLowerCase() === 'iframe') {
                  return {
                    ok: false,
                    reason: 'target_in_iframe' as const,
                    overlay: 'iframe',
                    overlayText: '',
                  };
                }
                // Overlay covers the expected element OR ambiguous
                // selector matched multiple elements and the topmost
                // isn't the one we wanted.
                const overlayTag = top.tagName.toLowerCase();
                const overlayId = top.id ? `#${top.id}` : '';
                const overlayClass = top.className && typeof top.className === 'string'
                  ? `.${top.className.split(/\s+/).filter(Boolean).slice(0, 2).join('.')}`
                  : '';
                const overlayText = (top.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60);
                return {
                  ok: false,
                  reason: args.ambiguous
                    ? ('selector_ambiguous' as const)
                    : ('covered_by_overlay' as const),
                  overlay: `${overlayTag}${overlayId}${overlayClass}`,
                  overlayText,
                };
              },
              {
                sel: selector,
                cx: coords.x,
                cy: coords.y,
                ambiguous,
                driftPx: parseInt(
                  process.env.SUPERBROWSER_PRECLICK_DRIFT_PX || '5', 10,
                ) || 5,
              },
            );
            if (!validation.ok) {
              const reasonStr = validation.reason as string;
              return {
                success: false,
                reason: 'element_covered',
                tried,
                error:
                  reasonStr === 'target_in_iframe'
                    ? `Click target is inside an <iframe>; this index-based path lands on the iframe host. Use browser_click_at(vision_index=V_n) — it descends through same-origin iframes and falls back to a Frame walk for cross-origin OOPIFs. Or use browser_click_selector(selector='<inner>', in_iframe='<host_css>') to target by CSS inside the frame. If both keep missing, drop to browser_run_script(mutates=true) and reach in via page.frames(): const f = page.frames().find(fr => fr.url().includes('<host_substr>')); await f.evaluate(() => document.querySelector('<inner>').click()).`
                    : reasonStr === 'rect_shifted'
                    ? `Element shifted between probe and dispatch (${validation.overlay}px) — page is mid-render. Re-screenshot to re-acquire fresh coords.`
                    : reasonStr === 'empty_stack'
                    ? `Click coords land on empty space (off-viewport or transparent pixel). Scroll the element into view and re-screenshot before retrying.`
                    : reasonStr === 'selector_ambiguous'
                    ? `Selector matched multiple elements; topmost at coords is "${validation.overlay}${validation.overlayText ? ` (${validation.overlayText})` : ''}", not the intended one. Re-read elements and pick a more specific [index].`
                    : `Element is covered by ${validation.overlay}${validation.overlayText ? ` ("${validation.overlayText}")` : ''}. The click would land on the overlay instead of the intended element. Dismiss the overlay first or wait for it to dissolve.`,
                alternatives: [
                  'Re-screenshot to refresh element state',
                  'Dismiss any covering overlay (close button, Escape key)',
                  'Try browser_click_at(vision_index=V_n) — snaps to interactive inside the bbox',
                ],
              };
            }
          }
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
        // Cosmetic sweep — Puppeteer.click bypasses our humanize.ts
        // pipeline, so the overlay never sees mouse_move events.
        if (this.sessionId) {
          await inputEventBus.emitSweep(this.sessionId, cx, cy);
        }
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
            // Only scroll when actually off-screen. `block: 'center'`
            // would re-centre even a fully-visible element, shifting
            // the viewport and invalidating the brain's pre-click
            // V_n bboxes for the next call.
            const r = el.getBoundingClientRect();
            const inView = (
              r.top >= 0 && r.bottom <= window.innerHeight
              && r.left >= 0 && r.right <= window.innerWidth
            );
            if (!inView) {
              el.scrollIntoView({
                block: 'nearest',
                inline: 'nearest',
                behavior: 'instant',
              });
            }
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
   * Silent-click escalation: dispatch a JS-level `el.click()` on the
   * element under (x, y). Bypasses the bezier sweep (which can dismiss
   * autocomplete popups via mouseout on intermediate siblings) and any
   * site-side guard that gates on bezier-style mouse movement (some
   * frameworks short-circuit click handlers when the preceding mousemove
   * trail looks "too natural"). Used by the click ladder when the
   * primary CDP dispatch produced zero DOM mutations.
   *
   * The dispatched event is `isTrusted=false` — bot-aware sites may
   * silently reject it, but in practice many handlers don't bother
   * checking and a JS click DOES produce the expected mutation.
   */
  async dispatchJsClickAt(x: number, y: number): Promise<void> {
    if (this.sessionId) {
      inputEventBus.emitClickTarget(this.sessionId, x, y, false);
    }
    await this.page.evaluate(
      (args: { x: number; y: number }) => {
        const el = document.elementFromPoint(args.x, args.y);
        if (el && typeof (el as HTMLElement).click === 'function') {
          (el as HTMLElement).click();
        }
      },
      { x, y },
    );
    await this.waitForIdle(1000).catch(() => {});
  }

  /**
   * Silent-click escalation: focus the element under (x, y), then
   * press Enter via CDP. Last-resort recovery for buttons/links that
   * accept keyboard activation but gated their click handler in a way
   * that swallows synthetic mouse events. The keyboard event is
   * trusted (CDP-dispatched).
   */
  async dispatchKeyboardEnterAt(x: number, y: number): Promise<void> {
    if (this.sessionId) {
      inputEventBus.emitClickTarget(this.sessionId, x, y, false);
    }
    await this.page.evaluate(
      (args: { x: number; y: number }) => {
        const el = document.elementFromPoint(args.x, args.y) as HTMLElement | null;
        if (el && typeof el.focus === 'function') {
          try { el.focus(); } catch { /* tabindex -1 / non-focusable: ignore */ }
        }
      },
      { x, y },
    );
    await this.page.keyboard.press('Enter');
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
    options?: {
      button?: 'left' | 'right' | 'middle';
      clickCount?: number;
      /** v6 F2: vision's label for this bbox. When provided, Phase 1
       *  pinpoint validates the snapped element's text/aria-label
       *  aligns with the vision label. On mismatch (page-shift case),
       *  Phase 1 falls through to Phase 2 grid-scan instead of
       *  trusting the centre element. */
      expectedLabel?: string;
      /** Force a deterministic teleport click — no bezier sweep, no
       *  pre-click hover. Caller wins over the auto-detected
       *  isAutocompleteOption path. */
      linear?: boolean;
    },
  ): Promise<{
    x: number;
    y: number;
    snapped: boolean;
    target?: string;
    /** Advisory flag for the bridge. Variants:
     *  - 'target_in_iframe_resolved': same-origin iframe descent
     *    found the inner interactive element; click landed on the
     *    real target. Informational, not an error.
     *  - 'target_in_iframe_cross_origin': contentDocument was blocked
     *    by same-origin policy; the in-page snap could not descend.
     *    The /click handler falls back to Puppeteer Frame walk
     *    (clickInIframeFrame).
     *  - 'target_in_iframe': legacy — descent attempted but stopped
     *    without finding an inner SEL match. Click landed on iframe
     *    host; the bridge should escalate.
     *  - 'pointer_events_none_ancestor': an ancestor has
     *    pointer-events:none; click may have passed through to a
     *    layer behind. */
    warning?: string;
    /** Xpath of the snapped interactive element — the /click handler
     *  uses it to look up the corresponding selectorMap index, so the
     *  Python bridge can record `last_click_dom_index` and the cross-
     *  tool dead-click guard can recognize that V_n and [N] resolve
     *  to the same DOM element. */
    targetXpath?: string;
    /** Set when expectedLabel was provided AND Phase 2 grid-scan
     *  could not find any candidate matching that label. The caller
     *  must NOT dispatch a click — surface a structured mismatch so
     *  the brain re-screenshots. Closes the silent-misclick gap on
     *  filter-shift cases where a stale bbox now overlaps a
     *  same-shape neighbour with a different label. */
    labelMismatch?: boolean;
    /** When `labelMismatch` is true, describes the wrong-label
     *  element currently occupying the bbox. The /click handler
     *  packs this into the `element_mismatch` response shape that
     *  the Python bridge already knows how to surface. */
    found?: { tag: string; role: string; text: string };
    /** Set when the snapped element looks like an autocomplete /
     *  typeahead suggestion (role=option inside listbox/combobox/
     *  menu, or descendant of an aria-haspopup="listbox" ancestor).
     *  These dropdowns close on the bezier sweep's mouseout/blur
     *  events from intermediate neighbors, so the dispatcher uses
     *  a `linear:true` teleport click instead — mouse jumps to the
     *  exact target with no in-between hovers. */
    isAutocompleteOption?: boolean;
    /** Phase A: xpaths of the iframe host elements that the snap
     *  descended through to reach the inner target. Empty/undefined
     *  when the snap stayed in the top-level document. Bridge logs
     *  it for telemetry; tests can assert that descent occurred. */
    iframe_chain?: string[];
    /** Phase A: a stable CSS selector for the host iframe (best-guess
     *  from id / aria-label / name, falling back to `iframe`). Set
     *  whenever the snap touched an iframe — on success, miss, or
     *  cross-origin failure. The bridge surfaces it so the brain can
     *  pass it back via browser_click_selector(in_iframe=…) without
     *  running an inspection script first. */
    iframe_host_selector?: string;
    /** Phase G: the snapped element is a native HTML `<select>`. CDP
     *  click on a native select doesn't open the dropdown in headless
     *  Chromium — the bridge surfaces a hint pointing at
     *  browser_select_option(..., in_iframe=...) so the brain doesn't
     *  burn turns clicking the select repeatedly. The click STILL
     *  dispatches (hint-only policy) — value-set via dispatchClick
     *  transfers focus, which the brain may need for Tab navigation. */
    native_select?: boolean;
  }> {
    const expectedLabel = (options?.expectedLabel || '').trim();
    const snap = await this.page.evaluate(
      (args: {
        b: { x0: number; y0: number; x1: number; y1: number };
        expectedLabel: string;
      }) => {
        const b = args.b;
        const SEL = 'a,button,input,select,textarea,'
          + '[role="button"],[role="link"],[role="checkbox"],'
          + '[role="tab"],[role="menuitem"],[onclick],[tabindex]';
        // Autocomplete / typeahead dropdown detector. Real-world
        // autocompletes (Google search bar, Booking.com destination
        // search, Yelp service search ["nail trimming" → suggestions])
        // close the popup on mouseout/blur fired by neighbours. The
        // bezier mouse path sweeps across siblings on its way to the
        // target, dismissing the dropdown before the click lands.
        // When this returns true, clickInBbox switches to a
        // teleport-click (linear:true) so the cursor jumps straight
        // to the target with no intermediate hovers.
        const isAutocompleteOptionEl = (el: Element | null): boolean => {
          if (!el) return false;
          // Direct role-option signal.
          const role = (el.getAttribute && el.getAttribute('role') || '').toLowerCase();
          if (role === 'option' || role === 'menuitem') return true;
          // Walk up to 6 ancestors looking for popup signals. 6 is
          // enough to clear typical wrapping like
          //   .suggestion > .row > .label-wrap → host listbox.
          let walker: Element | null = el;
          for (let depth = 0; walker && depth < 6; depth += 1) {
            const r = (walker.getAttribute && walker.getAttribute('role') || '').toLowerCase();
            if (r === 'listbox' || r === 'combobox' || r === 'menu') return true;
            const haspopup = walker.getAttribute && walker.getAttribute('aria-haspopup');
            if (haspopup === 'listbox' || haspopup === 'menu' || haspopup === 'true') return true;
            const autocomplete = walker.getAttribute && walker.getAttribute('aria-autocomplete');
            if (autocomplete === 'list' || autocomplete === 'both') return true;
            // Headless UI / Radix popup state.
            const ds = walker.getAttribute && walker.getAttribute('data-state');
            if (ds === 'open' && (walker.getAttribute('data-radix-popper-content-wrapper') !== null
              || (walker.getAttribute('class') || '').toLowerCase().includes('popover'))) {
              return true;
            }
            walker = walker.parentElement;
          }
          return false;
        };
        // `let` so Phase A's iframe descent can update these to the
        // inner element's viewport coords. Phase 2 (grid scan) doesn't
        // reuse them — it operates directly off `b.x0..b.y1`.
        let cx = Math.round((b.x0 + b.x1) / 2);
        let cy = Math.round((b.y0 + b.y1) / 2);
        const describe = (el: Element): string => {
          const tag = el.tagName.toLowerCase();
          const id = (el as HTMLElement).id ? `#${(el as HTMLElement).id}` : '';
          const cls = (el as HTMLElement).className && typeof (el as HTMLElement).className === 'string'
            ? `.${(el as HTMLElement).className.split(/\s+/).filter(Boolean).slice(0, 2).join('.')}`
            : '';
          const txt = (el.textContent || '').trim().slice(0, 30);
          return `${tag}${id}${cls}${txt ? `[${txt}]` : ''}`;
        };
        // Compute the xpath of the snapped element so the TS-side /click
        // handler can resolve it to a selectorMap index. Mirrors the
        // shape buildDomTree generates: `/html[1]/body[1]/div[3]/...`.
        const xpathOf = (el: Element): string => {
          const parts: string[] = [];
          let cur: Element | null = el;
          while (cur && cur.nodeType === 1 && cur !== document.documentElement.parentElement) {
            const tag = cur.tagName.toLowerCase();
            let idx = 1;
            let sib: Element | null = cur.previousElementSibling;
            while (sib) {
              if (sib.tagName.toLowerCase() === tag) idx += 1;
              sib = sib.previousElementSibling;
            }
            parts.unshift(`${tag}[${idx}]`);
            cur = cur.parentElement;
          }
          return '/' + parts.join('/');
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
          // Find the most specific clickable element under the cursor.
          // First pass: walk the elementsFromPoint stack (front-to-back
          // z-order) looking for a SEL match — this finds an interactive
          // child element whose bounds the cursor is actually over (e.g.
          // <li><button>X</button></li> with the bezel sitting on the
          // button surface).
          let interactive: Element | null = null;
          for (const el of centreStack) {
            if (el === document.documentElement || el === document.body) break;
            try {
              if ((el as Element).matches(SEL)) { interactive = el as Element; break; }
            } catch { /* ignore */ }
          }
          // Second pass: walk UP from the front-most non-body element
          // looking for an interactive ancestor (the original Phase 1
          // behaviour — handles cases where the ancestor wraps the
          // interactive semantics).
          if (!interactive) {
            interactive = (centreEl as Element).closest(SEL) || centreEl;
          }
          // Third pass — DESCEND. Real-world dropdowns are
          // <li#suggestion-N><button>X</button></li> where the bbox
          // covers the li (visible row) but the click handler lives on
          // the inner button, and the bbox centre often falls on
          // padding outside the button's rect. If `interactive` is a
          // wrapper (li, div, [role=option]) and contains a smaller,
          // more specific clickable child whose rect overlaps the bbox,
          // click the child's centre instead.
          const CHILD_SEL = 'button,a,input,[role="button"],[role="option"],'
            + '[role="menuitem"],[role="link"],[role="tab"],[onclick]';
          let descendant: Element | null = null;
          let descendantArea = 0;
          try {
            const interRect = (interactive as HTMLElement).getBoundingClientRect();
            const interArea = Math.max(1, interRect.width * interRect.height);
            const kids = (interactive as Element).querySelectorAll(CHILD_SEL);
            for (const k of Array.from(kids)) {
              if (k === interactive) continue;
              const kr = (k as HTMLElement).getBoundingClientRect();
              if (kr.width <= 0 || kr.height <= 0) continue;
              // Skip children that are essentially the parent (>= 95%
              // of parent area) — wrappers, not clickable kernels.
              if (kr.width * kr.height >= interArea * 0.95) continue;
              // Overlap with the bbox.
              const ix = Math.max(0, Math.min(kr.right, b.x1) - Math.max(kr.left, b.x0));
              const iy = Math.max(0, Math.min(kr.bottom, b.y1) - Math.max(kr.top, b.y0));
              const overlap = ix * iy;
              if (overlap <= 0) continue;
              if (overlap > descendantArea) {
                descendantArea = overlap;
                descendant = k as Element;
              }
            }
          } catch { /* ignore */ }
          if (descendant) {
            interactive = descendant;
          }
          // === Phase A: same-origin iframe descent ===
          // When `interactive` IS an iframe, the centre coords land on
          // the iframe's BORDER from the outer document's perspective
          // — CDP click at (cx,cy) hits the host, not the inner
          // button. Descend into iframe.contentDocument and re-snap.
          //
          // Three failure modes get distinct signals:
          //   - contentDocument throws or is null  → cross-origin or
          //     unloaded; flag for Phase B Frame walk.
          //   - innerCentre missing at pinpoint    → run an in-iframe
          //     grid scan (vision bbox often loose for iframe targets;
          //     centre lands on padding while the link is offset).
          //   - inner grid scan also empty         → flag iframe_miss
          //     so Phase B fallback fires in http.ts.
          //
          // Caps descent at depth 3 to handle iframes-inside-iframes
          // without infinite loops.
          let iframeChain: string[] = [];
          let iframeDescentFailed = false;
          let iframeDescentMissed = false;   // contentDocument ok but no inner clickable
          let iframeHostSelector = '';        // best-guess CSS for the host (for bridge hint)
          let descentDepth = 0;
          while (interactive
                 && interactive.tagName.toLowerCase() === 'iframe'
                 && descentDepth < 3) {
            const iframeEl = interactive as HTMLIFrameElement;
            const ir = iframeEl.getBoundingClientRect();
            const localCx = cx - ir.left;
            const localCy = cy - ir.top;
            // Capture a host selector for the bridge advisory. Prefer
            // [id] / [aria-label] / [name] (stable hooks the brain
            // can pass back via in_iframe). Falls back to tag+nth.
            if (!iframeHostSelector) {
              const id = (iframeEl as HTMLElement).id;
              const ariaLabel = iframeEl.getAttribute('aria-label');
              const name = iframeEl.getAttribute('name');
              if (id) iframeHostSelector = `iframe#${id}`;
              else if (ariaLabel) iframeHostSelector = `iframe[aria-label="${ariaLabel}"]`;
              else if (name) iframeHostSelector = `iframe[name="${name}"]`;
              else iframeHostSelector = 'iframe';
            }
            let innerDoc: Document | null = null;
            try { innerDoc = iframeEl.contentDocument; } catch { /* x-origin */ }
            if (!innerDoc) { iframeDescentFailed = true; break; }

            // Pinpoint inside iframe (frame-local elementsFromPoint).
            let innerStack: Element[] = [];
            try {
              innerStack = innerDoc.elementsFromPoint(localCx, localCy);
            } catch { innerStack = []; }
            let innerCentre = innerStack.find(
              (el) => el !== innerDoc!.documentElement && el !== innerDoc!.body,
            );

            // If pinpoint missed (centre lands on iframe scrollbar /
            // padding / loose-bbox whitespace), run a 5×5 grid scan
            // INSIDE the iframe. Translates the outer bbox into
            // iframe-local coords first. Picks the largest interactive
            // whose iframe-local rect overlaps the bbox — same shape
            // as Phase 2 grid scan at the top level.
            let innerInteractive: Element | null = null;
            if (!innerCentre) {
              const localB = {
                x0: b.x0 - ir.left, y0: b.y0 - ir.top,
                x1: b.x1 - ir.left, y1: b.y1 - ir.top,
              };
              let best: Element | null = null;
              let bestArea = 0;
              for (let i = 1; i < 5; i++) {
                for (let j = 1; j < 5; j++) {
                  const px = localB.x0 + ((localB.x1 - localB.x0) * i) / 5;
                  const py = localB.y0 + ((localB.y1 - localB.y0) * j) / 5;
                  let stack: Element[] = [];
                  try {
                    stack = innerDoc.elementsFromPoint(px, py);
                  } catch { stack = []; }
                  for (const el of stack) {
                    const hit = (el as Element).closest(SEL);
                    if (!hit) continue;
                    const r = (hit as HTMLElement).getBoundingClientRect();
                    const ix = Math.max(0, Math.min(r.right, localB.x1) - Math.max(r.left, localB.x0));
                    const iy = Math.max(0, Math.min(r.bottom, localB.y1) - Math.max(r.top, localB.y0));
                    const area = ix * iy;
                    if (area > bestArea) {
                      bestArea = area;
                      best = hit;
                    }
                  }
                }
              }
              if (best) {
                innerInteractive = best;
                innerCentre = best;
              } else {
                // Inner grid scan also empty — bbox doesn't overlap any
                // clickable inside this iframe. Flag for Phase B.
                iframeDescentMissed = true;
                break;
              }
            }

            // Pinpoint succeeded. Walk the stack for the best SEL match
            // (front-to-back z-order), then fall back to closest(SEL).
            if (!innerInteractive) {
              for (const el of innerStack) {
                if (el === innerDoc.documentElement || el === innerDoc.body) break;
                try {
                  if ((el as Element).matches(SEL)) {
                    innerInteractive = el as Element;
                    break;
                  }
                } catch { /* ignore */ }
              }
              if (!innerInteractive) {
                innerInteractive = (innerCentre as Element).closest(SEL) || innerCentre;
              }
            }
            iframeChain.push(xpathOf(iframeEl));
            // Recompute viewport (cx,cy) from the inner element's
            // frame-local rect. ir.left/ir.top = host viewport offset;
            // innerRect is iframe-local coords, so we add them.
            const innerRect = (innerInteractive as HTMLElement).getBoundingClientRect();
            cx = Math.round(ir.left + innerRect.left + innerRect.width / 2);
            cy = Math.round(ir.top + innerRect.top + innerRect.height / 2);
            // Phase J: pre-focus iframe inputs. The upcoming CDP click
            // transfers focus on most pages, but cross-frame focus is
            // unreliable in headless Chromium — sometimes a click on
            // an iframe input leaves focus on the OUTER document.body,
            // and a follow-up `browser_keys` then types into the void.
            // Set focus explicitly here so the keystroke target is
            // committed before the click dispatches. Skip <select>
            // (CDP click can't drive the native dropdown anyway; Phase
            // G's hint redirects to browser_select_option).
            const _innerTag = innerInteractive.tagName.toLowerCase();
            if (_innerTag === 'input' || _innerTag === 'textarea'
                || (innerInteractive as HTMLElement).isContentEditable) {
              try { (innerInteractive as HTMLElement).focus(); } catch (_) { /* x-frame edge */ }
            }
            interactive = innerInteractive;
            descentDepth += 1;
          }
          const descended = iframeChain.length > 0;
          // === end Phase A ===
          // v6 F2: label-match check. When vision provided an
          // expectedLabel, verify the snapped element's text/aria-label
          // aligns. After a page shift, the bbox centre may now hit a
          // DIFFERENT interactive element — Phase 1 would happily
          // click it. On mismatch we fall through to Phase 2 grid-
          // scan which can find a different interactive whose rect
          // overlaps the bbox AND whose label aligns.
          let labelMatch = true;
          if (args.expectedLabel.length >= 3) {
            const snappedFull = (
              ((interactive as HTMLElement).textContent || '') + ' '
              + ((interactive as HTMLElement).getAttribute('aria-label') || '')
              + ' '
              + ((interactive as HTMLElement).getAttribute('title') || '')
            ).toLowerCase().replace(/\s+/g, ' ').trim();
            const expLc = args.expectedLabel.toLowerCase().trim();
            // Substring either direction handles two real cases:
            //   1) bbox label is a tight subset of element's full text
            //      (e.g. label='Sony', element text='Sony 12 reviews')
            //   2) element's text is truncated by browser-use's
            //      80-char cap, so element-text < bbox label
            labelMatch = (
              !!snappedFull
              && (
                snappedFull.includes(expLc)
                || expLc.includes(snappedFull.slice(0, 40))
              )
            );
          }
          if (labelMatch) {
          // A2: surface warning when the click would land on an
          // iframe host (inner content unreachable via outer-doc CDP)
          // or when an ancestor has pointer-events:none (click would
          // pass through to whatever sits behind). These are advisory
          // — the click still dispatches, but the bridge surfaces
          // them so the brain can react.
          let warning: string | undefined;
          if (interactive.tagName.toLowerCase() === 'iframe') {
            // Phase A descent could not produce an inner clickable.
            // Three signals so the bridge / http.ts can route correctly:
            //  - cross_origin: contentDocument blocked by SOP. Phase B
            //    Frame walk applies (it can still access cross-origin
            //    frames via Puppeteer's Target API).
            //  - miss: contentDocument accessible BUT neither pinpoint
            //    nor inner grid scan found a clickable overlapping the
            //    bbox. Either the bbox is loose (covers padding only)
            //    or the iframe has no clickable in this region.
            //    Phase B still worth trying — its broader scan may
            //    find a candidate.
            //  - legacy `target_in_iframe`: should not fire now (the
            //    descent loop always sets one of the above flags) —
            //    kept for backward-compat with any callers that pattern-
            //    match the legacy substring.
            warning = iframeDescentFailed
              ? 'target_in_iframe_cross_origin'
              : (iframeDescentMissed
                  ? 'target_in_iframe_miss'
                  : 'target_in_iframe');
          } else if (descended) {
            // Phase A: same-origin descent succeeded — click is about
            // to land on the real inner element. Informational only;
            // existing callers that branch on `warning` won't match
            // any of the legacy strings.
            warning = 'target_in_iframe_resolved';
          } else {
            // Walk up to body looking for pointer-events:none on
            // ancestors. A leaf with pointer-events:none doesn't
            // intercept clicks but its ancestor with pe:none means
            // the dispatch coords would pass through this layer and
            // land on whatever's behind.
            let walker: Element | null = interactive;
            let depth = 0;
            while (walker && walker !== document.body && depth < 8) {
              const pe = window.getComputedStyle(walker as HTMLElement).pointerEvents;
              if (pe === 'none') {
                warning = 'pointer_events_none_ancestor';
                break;
              }
              walker = walker.parentElement;
              depth += 1;
            }
          }
          // When we descended into a smaller clickable child, click
          // the child's centre — the bbox centre may have been on
          // padding outside the child's rect, which would dispatch
          // the click on a non-handler. For wrapper elements the
          // bbox centre is still right.
          //
          // Phase A: if iframe descent succeeded, cx/cy ALREADY point
          // at the inner element's viewport coords — don't let the
          // (outer-doc) descendant search override them.
          let dispatchX = cx;
          let dispatchY = cy;
          if (descendant && !descended) {
            const dr = (descendant as HTMLElement).getBoundingClientRect();
            dispatchX = Math.round(dr.left + dr.width / 2);
            dispatchY = Math.round(dr.top + dr.height / 2);
          }
          return {
            x: dispatchX,
            y: dispatchY,
            snapped: true,
            target: describe(interactive),
            warning,
            targetXpath: xpathOf(interactive),
            isAutocompleteOption: isAutocompleteOptionEl(interactive),
            iframe_chain: descended ? iframeChain : undefined,
            // Bridge surfaces this so brain can pass it back via
            // browser_click_selector(in_iframe=<host_selector>) without
            // having to run a separate inspection script.
            iframe_host_selector: (iframeDescentFailed || iframeDescentMissed
                                    || iframeChain.length > 0)
              ? iframeHostSelector || undefined
              : undefined,
            // Phase G: surface native <select> so the bridge can hint
            // the brain to use browser_select_option instead. Native
            // dropdowns don't open via CDP Input.dispatchMouseEvent in
            // headless Chromium — the click here only transfers focus.
            native_select: interactive.tagName.toLowerCase() === 'select'
              ? true : undefined,
          };
          } /* end if (labelMatch) */
          // labelMatch=false: fall through to Phase 2 grid-scan below.
          // Phase 1 trusted Gemini's centre but the snapped element's
          // label diverged from the bbox label (likely page-shift
          // case where layout moved a different element under the
          // bbox centre). Grid-scan can find an alternate interactive
          // whose rect overlaps the bbox AND whose label aligns.
        }
        // 2. Centre fell on empty space OR Phase 1 label-match
        //    failed — pick the interactive whose rect overlaps the
        //    bbox most. Chevron tiebreaker biases toward expand/
        //    collapse semantics on row-shaped bboxes.
        //
        //    Filter-shift fix: when expectedLabel is provided, weight
        //    candidates by labelMatchScore (1.0 match, 0.05 mismatch,
        //    0.1 unlabelled). Without this, a stale bbox that now
        //    overlaps a same-shape neighbour silently snaps to the
        //    neighbour. With it, only a label-matching candidate wins
        //    on overlap; if no candidate matches the label, we report
        //    `labelMismatch=true` and the caller skips dispatch so the
        //    brain re-screenshots instead of misclicking.
        const isRowBbox = (b.x1 - b.x0) >= 60 && (b.y1 - b.y0) >= 24;
        const CHEVRON_CHARS = '▼▶◀▲►◄⌃⌄⋮+−×⨯›';
        const chevronScoreOf = (el: Element): number => {
          const h = el as HTMLElement;
          if (h.getAttribute('aria-expanded') !== null) return 3;
          if (h.getAttribute('aria-haspopup')) return 2;
          const t = (h.textContent || '').trim();
          if (t.length === 1 && CHEVRON_CHARS.includes(t)) return 2;
          const al = (h.getAttribute('aria-label') || '').toLowerCase();
          if (/(expand|collapse|toggle|more)/.test(al)) return 1;
          return 0;
        };
        const expLc = args.expectedLabel.toLowerCase().trim();
        // Numeric short labels ("1".."31") are calendar day cells. Vision
        // emits the day number; the cell's visible textContent is that
        // same number. Without this branch, labelActive=false (length<3)
        // and snap falls back to pure area, which on a misaligned bbox
        // picks the row below by area dominance.
        const isNumericDay = /^\d{1,2}$/.test(expLc)
          && Number(expLc) >= 1 && Number(expLc) <= 31;
        const labelActive = args.expectedLabel.length >= 3 || isNumericDay;
        const labelScoreOf = (el: Element): number => {
          if (!labelActive) return 1;
          // Calendar-day path: match against visible textContent only,
          // word-exact (NOT substring). A `<button>24</button>` matches
          // day "24"; `aria-label="Saturday, May 24, 2026"` is skipped
          // because the digit "4" inside "2026" or "24" inside another
          // cell's `aria-label` would otherwise false-match.
          if (isNumericDay) {
            const text = ((el as HTMLElement).textContent || '')
              .toLowerCase().replace(/\s+/g, ' ').trim();
            if (!text) return 0.05;
            // textContent must be EXACTLY the day number (a calendar
            // cell renders just "24"). Buttons or rows that happen to
            // contain "24" in a longer phrase score 0.05.
            if (text === expLc) return 1;
            // Whole-word match in textContent — covers cells whose
            // visible text is "24 " with a trailing badge ("●" for
            // today, etc.). Splits on whitespace AND punctuation to
            // catch "24·" / "24*" / "24, today".
            const words = text.split(/[\s.,·*•\-]+/);
            if (words.includes(expLc)) return 0.9;
            return 0.05;
          }
          const full = (
            ((el as HTMLElement).textContent || '') + ' '
            + ((el as HTMLElement).getAttribute('aria-label') || '') + ' '
            + ((el as HTMLElement).getAttribute('title') || '')
          ).toLowerCase().replace(/\s+/g, ' ').trim();
          if (!full) return 0.1;
          if (full.includes(expLc) || expLc.includes(full.slice(0, 40))) {
            return 1;
          }
          // Lenient fallback for two element families where vision's
          // label systematically diverges from the DOM's text:
          //
          //   1. Dropdown items (role=option/menuitem/treeitem/listitem,
          //      <li>). Vision drifts on suggestion labels — abbreviates
          //      ("SF MOMA" vs "San Francisco Museum of Modern Art"),
          //      strips address context, paraphrases. Misclick risk is
          //      low because only one popup is open at a time and
          //      sibling options share the same drift pattern.
          //
          //   2. Value-bearing triggers (role=combobox, anything with
          //      aria-haspopup). Vision labels these by FUNCTION
          //      ("Start Time Picker"); the DOM exposes the current
          //      VALUE ("1:00 PM"). The two strings will never substring-
          //      match. Misclick risk is also low — these are singleton
          //      controls whose only sibling is another picker which
          //      would share the same divergence; we still pick the best
          //      candidate by area inside the bbox.
          //
          // Either family: accept ≥3-char word overlap (or no expected
          // words at all) as a partial match worth dispatching.
          const role = (
            ((el as HTMLElement).getAttribute('role') || '').toLowerCase()
          );
          const hasPopup = (
            (el as HTMLElement).getAttribute('aria-haspopup') || ''
          ).toLowerCase();
          const isDropdownItem = (
            role === 'option' || role === 'menuitem'
            || role === 'treeitem' || role === 'listitem'
          ) || (el as HTMLElement).tagName.toLowerCase() === 'li';
          const isValueBearingTrigger = (
            role === 'combobox'
            || (hasPopup !== '' && hasPopup !== 'false')
          );
          if (isDropdownItem || isValueBearingTrigger) {
            const expWords = new Set(
              expLc.split(/\s+/).filter((t) => t.length >= 3),
            );
            const fullWords = new Set(
              full.split(/\s+/).filter((t) => t.length >= 3),
            );
            let common = 0;
            for (const t of expWords) if (fullWords.has(t)) common += 1;
            // Value-bearing triggers often have NO overlap (semantic
            // role vs displayed value) — grant the leniency anyway so
            // the dispatch can land. The 0.7 score still loses to a 1.0
            // exact-match neighbour with comparable area, so a labelled
            // sibling wins if present.
            if (common >= 1 || (isValueBearingTrigger && expWords.size > 0)) {
              return 0.7;
            }
          }
          return 0.05;
        };
        let best: Element | null = null;
        let bestArea = 0;
        let bestComposite = 0;
        let bestLabelScore = 0;
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
              if (area <= 0) continue;
              const cs = isRowBbox ? chevronScoreOf(hit) : 0;
              const ls = labelScoreOf(hit);
              // Composite scoring: area dominates, chevron only nudges
              // when this candidate is within 30% of the current best
              // (preserves existing single-winner behaviour). Then the
              // label score multiplies the whole thing — when active,
              // a 1.0 match beats any 0.05 mismatch unless the
              // mismatch has 20× the area, which never happens for
              // sibling checkboxes.
              const within30 = bestArea > 0 && area > bestArea * 0.7;
              const baseScore = cs > 0 && within30
                ? area + bestArea * 0.5 * cs
                : area;
              const composite = baseScore * ls;
              if (composite > bestComposite) {
                bestComposite = composite;
                bestArea = area;
                bestLabelScore = ls;
                best = hit;
              }
            }
          }
        }
        if (best) {
          // Low-confidence snap: best candidate failed label-match
          // (ls < 0.5 means 0.05 or 0.1 — strict mismatch or
          // unlabelled when a label was expected). We still dispatch
          // the click at this candidate's centre (see the caller
          // notes on labelMismatch being advisory-only), but mark
          // snapped=false + labelMismatch=true so the bridge can log
          // the divergence and the brain can read `found.text` to see
          // what was actually clicked. The bridge's `[no_effect:...]`
          // tag only fires when this AND mutation_delta=0 — i.e. the
          // low-confidence click also failed to move the page.
          if (labelActive && bestLabelScore < 0.5) {
            const r = best.getBoundingClientRect();
            return {
              x: Math.round(r.left + r.width / 2),
              y: Math.round(r.top + r.height / 2),
              snapped: false,
              target: describe(best),
              targetXpath: xpathOf(best),
              labelMismatch: true,
              found: {
                tag: best.tagName.toLowerCase(),
                role: ((best as HTMLElement).getAttribute('role') || ''),
                text: ((best as HTMLElement).textContent || '')
                  .replace(/\s+/g, ' ').trim().slice(0, 120),
              },
            };
          }
          const r = best.getBoundingClientRect();
          return {
            x: Math.round(r.left + r.width / 2),
            y: Math.round(r.top + r.height / 2),
            snapped: true,
            target: describe(best),
            targetXpath: xpathOf(best),
            isAutocompleteOption: isAutocompleteOptionEl(best),
            // Phase G: surface native <select> on the grid-scan path too.
            native_select: best.tagName.toLowerCase() === 'select'
              ? true : undefined,
          };
        }
        // 3. Hard fallback: click the raw centre anyway. snapped=false
        //    so the UI crosshair shows amber — operator can see we had
        //    no visual confirmation.
        return { x: cx, y: cy, snapped: false };
      },
      { b: bbox, expectedLabel },
    );

    // labelMismatch is ADVISORY ONLY — we used to skip dispatch here
    // when Phase 2 grid-scan returned bestLabelScore < 0.5, but that
    // silently no-op'd legitimate clicks on value-bearing controls
    // (Chakra DateTimePicker, MUI Select, AntD picker triggers) whose
    // visible text is the displayed VALUE while vision labels them by
    // FUNCTION. The two strings systematically diverge and labelScore
    // never crosses the threshold.
    //
    // Page-shift attacks (the real reason this escape existed) are
    // already covered by the dedicated viewport_shifted guard at
    // src/server/http.ts:884-907 and src/browser/page-readiness.ts:550
    // (compareViewportShift), which fires BEFORE clickInBbox runs.
    // labelScoreOf's new value-bearing leniency keeps the strict-mismatch
    // path tight for genuine same-shape-neighbour cases while letting
    // combobox / aria-haspopup triggers through.
    //
    // snap.labelMismatch stays on the response so the bridge can log it
    // as a diagnostic in the operator stdout line; it no longer blocks.

    // Broadcast resolved target to live viewers BEFORE the click so the
    // crosshair appears in the same frame the click lands.
    if (this.sessionId) {
      // Cosmetic sweep for the linear branch — when dispatchClick
      // teleports (autocomplete or caller-supplied linear=true), no
      // CDP mouseMoved events fire and the live-view cursor jumps
      // without intermediate frames. emitSweep updates ONLY the WS
      // overlay (no CDP), so it's safe even for autocomplete (where
      // real intermediate hovers would dismiss the dropdown).
      const willBeLinear =
        options?.linear === true
        || (options?.linear == null && snap.isAutocompleteOption === true);
      if (willBeLinear) {
        await inputEventBus.emitSweep(this.sessionId, snap.x, snap.y);
      }
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
    // Autocomplete suggestions: switch to a teleport click so the
    // mouse jumps straight to the target with no in-between hovers.
    // The bezier sweep otherwise crosses neighbouring options whose
    // mouseout/blur handlers dismiss the dropdown before the click
    // lands — the user types "nail trimming", a suggestion appears,
    // the bezier pass over earlier suggestions closes the popup,
    // and the click then falls onto whatever's behind. Caller-
    // supplied `linear` always wins so this only kicks in when the
    // tool didn't already make a choice.
    const dispatchOpts = {
      ...options,
      sessionId: this.sessionId,
      linear: options?.linear ?? (snap.isAutocompleteOption === true),
    };
    await dispatchClick(client, snap.x, snap.y, dispatchOpts);
    // Microtask + double-RAF flushes the React/Vue commit phase and one
    // paint before the caller's effect snapshot. waitForIdle alone gates
    // only on network idle, which is already idle on most SPAs — so the
    // call returns in <1ms and async state updates fire AFTER the
    // captureEffect, producing mutation_delta=0 even though the click
    // landed. Adds ~32ms on idle pages; far cheaper than waitForVisualStable.
    try {
      await this.page.evaluate(() => new Promise<void>((r) => {
        requestAnimationFrame(() => requestAnimationFrame(() => r()));
      }));
    } catch { /* page closed mid-click */ }
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
  } | { __syntaxError: true; message: string } | null>> {
    return await this.page.evaluate(
      (sels: string[], ensure: boolean) => {
        return sels.map((sel) => {
          // Multi-selector resolution: walk querySelectorAll and pick the
          // first VISIBLE match. Plain querySelector returns the first
          // element in DOM order regardless of visibility — when a page
          // has a hidden listbox <li> earlier in the DOM (closed dropdown
          // still in tree), that's what gets picked, the rect is zero-
          // size, and clickSelector throws "selector not found or zero-
          // size" even though a visible match exists later.
          //
          // SyntaxError is reported separately so callers can
          // distinguish "invalid CSS" from "valid CSS, no match" —
          // the Python bridge rejects Playwright/jQuery extensions
          // (`:has-text`, `:contains`, etc.) upfront, but any other
          // malformed selector still surfaces a useful error here
          // instead of being mislabelled as a missing element.
          let nodeList: NodeListOf<Element>;
          try { nodeList = document.querySelectorAll(sel); }
          catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            if (err instanceof DOMException && err.name === 'SyntaxError') {
              return { __syntaxError: true as const, message: msg };
            }
            return null;
          }
          if (nodeList.length === 0) return null;
          let el: HTMLElement | null = null;
          for (const c of Array.from(nodeList)) {
            const he = c as HTMLElement;
            const r = he.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;
            const cs = window.getComputedStyle(he);
            if (cs.display === 'none' || cs.visibility === 'hidden'
                || cs.opacity === '0') continue;
            el = he;
            break;
          }
          // Fallback to first match — preserves the existing zero-size
          // error path for callers (clickSelector throws when nothing
          // visible was found).
          if (!el) el = nodeList[0] as HTMLElement;
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
    if (rect && '__syntaxError' in rect) {
      throw new BadRequest(
        `clickSelector: invalid CSS syntax in selector ${selector}: ${rect.message}`,
      );
    }
    if (!rect || !rect.visible) {
      throw new BadRequest(`clickSelector: selector not found or zero-size: ${selector}`);
    }
    const x = Math.round(rect.cx);
    const y = Math.round(rect.cy);

    // Coverage check — same shape as the A1 preclick validator in
    // clickElement, mirroring the labelMismatch escape in clickInBbox.
    // MUST run BEFORE the crosshair emit, otherwise the live viewer
    // flashes a green target while we abort dispatch — that's the
    // exact "burst but page doesn't change" bug clickInBbox already
    // fixed at line 1244 (return-before-emit on labelMismatch).
    //
    // We accept either an exact match (`top === expected`) or an
    // expected-contains-top relationship (the click bubbles up — this
    // covers the legitimate `<label for><input/></label>` case where
    // the input occludes its label centre). Any other top element is
    // an overlay or wrong frame; abort with a structured 400 so the
    // bridge can surface the reason.
    if (process.env.SUPERBROWSER_PRECLICK_VALIDATE !== '0') {
      const validation = await this.page.evaluate(
        (args: { sel: string; cx: number; cy: number }) => {
          const expected = document.querySelector(args.sel) as HTMLElement | null;
          if (!expected) return { ok: false as const, reason: 'no_expected' as const };
          let stack: Element[] = [];
          try { stack = document.elementsFromPoint(args.cx, args.cy); } catch { stack = []; }
          if (stack.length === 0) {
            return { ok: false as const, reason: 'empty_stack' as const };
          }
          const top = stack[0] as HTMLElement;
          if (top === expected || expected.contains(top)) {
            return { ok: true as const };
          }
          if (top.tagName.toLowerCase() === 'iframe') {
            return { ok: false as const, reason: 'target_in_iframe' as const };
          }
          const overlayTag = top.tagName.toLowerCase();
          const overlayId = top.id ? `#${top.id}` : '';
          const overlayCls = (top.className && typeof top.className === 'string')
            ? `.${top.className.split(/\s+/).filter(Boolean).slice(0, 2).join('.')}`
            : '';
          return {
            ok: false as const,
            reason: 'covered_by_overlay' as const,
            overlay: `${overlayTag}${overlayId}${overlayCls}`,
          };
        },
        { sel: selector, cx: x, cy: y },
      );
      if (!validation.ok) {
        const detail = ('overlay' in validation && validation.overlay)
          ? ` by ${validation.overlay}` : '';
        throw new BadRequest(
          `clickSelector: target validation failed (${validation.reason}${detail}) for selector: ${selector}`,
        );
      }
    }

    // Cosmetic cursor sweep — the live-view overlay only animates when
    // mouse_move bus events arrive, and the deterministic dispatch
    // below skips humanClick (no Bezier sweep). Emit a short
    // interpolated travel so the cursor doesn't appear to teleport.
    // No effect on CDP — purely overlay.
    //
    // GATE on `linear` to avoid double-sweep when the caller opts INTO
    // humanization (`linear: false`). `humanClick` starts its Bezier
    // path from a RANDOM offset of the target (humanize.ts:95-96), not
    // from lastCursor — so emitting our linear sweep first would land
    // the cursor at the target, then humanClick would jump it back to
    // a random nearby point and re-sweep. Visible jitter. When `linear`
    // is true (the default for selector clicks), humanClick is skipped
    // and our sweep is the only animation the overlay sees.
    const willBeLinear = opts?.linear ?? true;
    if (this.sessionId) {
      if (willBeLinear) {
        await inputEventBus.emitSweep(this.sessionId, x, y);
      }
      inputEventBus.emitClickTarget(this.sessionId, x, y, true, undefined, selector);
    }
    const client = await this.getCDPSession();
    await dispatchClick(client, x, y, {
      button: opts?.button,
      clickCount: opts?.clickCount,
      linear: willBeLinear,
      sessionId: this.sessionId,
    });
    await this.waitForIdle(1000).catch(() => {});
    return { x, y, rect: { x: rect.x, y: rect.y, w: rect.w, h: rect.h } };
  }

  /**
   * Phase D — selector click scoped to an <iframe>. The brain calls
   * `browser_click_selector(selector='button.start', in_iframe='#quiz')`
   * when the target lives inside an embedded frame. Resolution path:
   *
   *  1. Resolve the iframe host element in the main document via
   *     page.$(`hostSelector`). 400 if missing.
   *  2. Walk page.frames() to find the matching Puppeteer Frame for
   *     that host (URL-match / boundingBox containment).
   *  3. Run `frame.evaluate(querySelector + getBoundingClientRect)`
   *     to get the inner element's frame-local rect. 400 if missing.
   *  4. Translate frame-local centre → viewport coords using the host
   *     iframe's bounding box (works for same-origin AND cross-origin
   *     OOPIFs because the host element is always reachable from the
   *     parent document).
   *  5. Dispatch a CDP click at the viewport coords. Chromium's
   *     compositor routes hit-tests through the OOPIF.
   */
  async clickSelectorInIframe(
    hostSelector: string,
    selector: string,
    opts?: {
      button?: 'left' | 'right' | 'middle';
      clickCount?: number;
      linear?: boolean;
    },
  ): Promise<{
    x: number;
    y: number;
    rect: { x: number; y: number; w: number; h: number };
    iframe_host: string;
    frame_url: string;
    native_select?: boolean;
    focused_iframe_input?: boolean;
  }> {
    // Step 1+2: resolve host element + matching Frame.
    const hostHandle = await this.page.$(hostSelector);
    if (!hostHandle) {
      throw new BadRequest(
        `clickSelectorInIframe: iframe host not found: ${hostSelector}`,
      );
    }
    const hostBox = await hostHandle.boundingBox();
    if (!hostBox || hostBox.width <= 0 || hostBox.height <= 0) {
      await hostHandle.dispose();
      throw new BadRequest(
        `clickSelectorInIframe: iframe host zero-size or off-page: ${hostSelector}`,
      );
    }
    const frame = await hostHandle.contentFrame();
    await hostHandle.dispose();
    if (!frame) {
      throw new BadRequest(
        `clickSelectorInIframe: contentFrame() returned null for ${hostSelector}`
        + ' — the host may be a sandboxed iframe with srcdoc/about:blank.',
      );
    }

    // Step 3: frame-local selector resolution.
    const inner = await frame.evaluate((sel: string) => {
      // Visibility-aware querySelector: walk querySelectorAll and pick
      // the first non-zero-rect match. Mirrors the top-level getRects
      // resilience (page.ts:1485) so a closed-listbox sibling earlier
      // in DOM order doesn't shadow the visible target.
      let chosen: Element | null = null;
      try {
        const list = document.querySelectorAll(sel);
        for (const el of Array.from(list)) {
          const r = (el as HTMLElement).getBoundingClientRect();
          if (r.width > 0 && r.height > 0) { chosen = el; break; }
        }
      } catch (e) {
        return { __syntaxError: true as const, message: (e as Error).message };
      }
      if (!chosen) return null;
      const r = (chosen as HTMLElement).getBoundingClientRect();
      return {
        x: r.x, y: r.y, w: r.width, h: r.height,
        cx: r.x + r.width / 2, cy: r.y + r.height / 2,
        // Phase G: surface the inner element's tag so the caller can
        // flag native <select> clicks (which don't open dropdowns via
        // CDP — bridge directs the brain at browser_select_option).
        tag: chosen.tagName.toLowerCase(),
      };
    }, selector);
    if (inner && '__syntaxError' in inner) {
      throw new BadRequest(
        `clickSelectorInIframe: invalid CSS in inner selector ${selector}: ${inner.message}`,
      );
    }
    if (!inner) {
      throw new BadRequest(
        `clickSelectorInIframe: inner selector not found or zero-size: ${selector}`
        + ` (inside ${hostSelector})`,
      );
    }

    // Step 4: translate frame-local centre → viewport coords.
    const x = Math.round(hostBox.x + inner.cx);
    const y = Math.round(hostBox.y + inner.cy);
    const rect = {
      x: Math.round(hostBox.x + inner.x),
      y: Math.round(hostBox.y + inner.y),
      w: Math.round(inner.w),
      h: Math.round(inner.h),
    };

    const willBeLinear = opts?.linear ?? true;
    if (this.sessionId) {
      if (willBeLinear) {
        await inputEventBus.emitSweep(this.sessionId, x, y);
      }
      inputEventBus.emitClickTarget(
        this.sessionId, x, y, true, undefined,
        `${hostSelector} >> ${selector}`,
      );
    }

    // Step 5: CDP click at viewport coords.
    const client = await this.getCDPSession();
    await dispatchClick(client, x, y, {
      button: opts?.button,
      clickCount: opts?.clickCount,
      linear: willBeLinear,
      sessionId: this.sessionId,
    });
    await this.waitForIdle(1000).catch(() => {});

    // Phase J: explicit focus for iframe inputs. CDP
    // Input.dispatchMouseEvent at viewport coords reliably routes the
    // hit-test through the compositor to the iframe element, but the
    // FOCUS transfer for cross-frame inputs is not always carried
    // along — Chromium occasionally leaves focus on the outer
    // document.body. When the very next call is `browser_keys` (the
    // typical sequence for filling iframe inputs), keys silently fly
    // into document.body instead of the input.
    //
    // Force focus inside the iframe for text-bearing targets. Skip
    // <select> (focus is set by CDP click and explicit focus may
    // open the native dropdown which CDP can't drive anyway), skip
    // buttons / links (no keyboard input expected).
    const focusableTag =
      inner.tag === 'input' || inner.tag === 'textarea';
    if (focusableTag) {
      try {
        await frame.evaluate((sel: string) => {
          const list = document.querySelectorAll(sel);
          for (const el of Array.from(list)) {
            const r = (el as HTMLElement).getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {
              try { (el as HTMLElement).focus(); } catch (_) { /* ignore */ }
              break;
            }
          }
        }, selector);
      } catch {
        /* best-effort — never block on focus transfer */
      }
    }

    return {
      x, y, rect,
      iframe_host: hostSelector,
      frame_url: frame.url(),
      // Phase G: surface inner tag so the bridge can hint the brain
      // at browser_select_option when the target is a native <select>.
      native_select: inner.tag === 'select' ? true : undefined,
      // Phase J: indicate that we explicitly focused an iframe-internal
      // input. The bridge can surface this so the brain knows the
      // next `browser_keys` will reliably land in the right field.
      focused_iframe_input: focusableTag ? true : undefined,
    };
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
    if (fromRect && '__syntaxError' in fromRect) {
      throw new Error(
        `dragSelectors: invalid CSS syntax in fromSelector ${fromSelector}: ${fromRect.message}`,
      );
    }
    if (toRect && '__syntaxError' in toRect) {
      throw new Error(
        `dragSelectors: invalid CSS syntax in toSelector ${toSelector}: ${toRect.message}`,
      );
    }
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

    // Cosmetic cursor sweeps for the linear path — same rationale as
    // clickSelector: when `linear: true`, the dispatch teleports and
    // emits no `mouse_move` bus events, so the overlay shows no
    // intermediate frames. Gated on linear to avoid double-sweep when
    // humanClick/humanDrag is running its own Bezier animation.
    const tryClickClick = async (): Promise<void> => {
      if (linear && this.sessionId) {
        await inputEventBus.emitSweep(this.sessionId, fx, fy);
      }
      await dispatchClick(client, fx, fy, { linear, sessionId: this.sessionId });
      await new Promise((r) => setTimeout(r, opts?.holdMs ?? 120));
      if (linear && this.sessionId) {
        await inputEventBus.emitSweep(this.sessionId, tx, ty);
      }
      await dispatchClick(client, tx, ty, { linear, sessionId: this.sessionId });
      await this.waitForIdle(600).catch(() => {});
    };
    const tryDrag = async (): Promise<void> => {
      if (linear && this.sessionId) {
        await inputEventBus.emitSweep(this.sessionId, fx, fy);
      }
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
      // when the TS source is loaded via tsx watch. Uses shadow-DOM-
      // piercing helpers so custom elements (mds-slider on chase.com,
      // any Lit/React widget that hosts a native range input inside an
      // open shadow root) resolve. After setting .value we ALSO fire
      // input/change on the shadow host so widget-level listeners
      // recompute — the inner-input event doesn't always escape.
      const src = SHADOW_DOM_HELPERS_SRC + `;(function(sel, target){
        var first = __sb_queryDeep(document, sel);
        if (!first) return { ok: false, reason: 'not-found' };
        var els = [first];
        if (Array.isArray(target)) {
          // Look in the same root as 'first' for sibling range inputs.
          var rootScope = first.getRootNode ? first.getRootNode() : document;
          var parent = first.parentElement;
          if (parent) {
            var sibs = __sb_queryAllDeep(parent, 'input[type="range"]');
            if (sibs.length < 2 && rootScope && rootScope !== document) {
              sibs = __sb_queryAllDeep(rootScope, 'input[type="range"]');
            }
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
          __sb_dispatchHostSignal(el, ['input','change']);
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
      const stateSrc = SHADOW_DOM_HELPERS_SRC + `;(function(sel){
        var el = __sb_queryDeep(document, sel);
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
        const afterSrc = SHADOW_DOM_HELPERS_SRC + `;(function(sel){
          var el = __sb_queryDeep(document, sel);
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
      const rectSrc = SHADOW_DOM_HELPERS_SRC + `;(function(sel){
        var el = __sb_queryDeep(document, sel);
        if (!el) return null;
        // closest() walks light-DOM ancestors only; for shadow-rooted
        // sliders the track is usually a sibling/parent in the same root,
        // so closest still resolves it. If not, fall back to the host.
        var track = el.closest('[role="slider"],[class*="slider" i],[class*="track" i]') || el;
        if (track === el) {
          var rootScope = el.getRootNode ? el.getRootNode() : null;
          if (rootScope && rootScope.host) {
            var hostTrack = rootScope.host.closest && rootScope.host.closest('[role="slider"],[class*="slider" i],[class*="track" i]');
            if (hostTrack) track = hostTrack;
          }
        }
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
      const afterSrc2 = SHADOW_DOM_HELPERS_SRC + `;(function(sel){
        var el = __sb_queryDeep(document, sel);
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
    const probeSrc = SHADOW_DOM_HELPERS_SRC + `;(function(sel){
      var el = __sb_queryDeep(document, sel);
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
    const scanSrc = SHADOW_DOM_HELPERS_SRC + `;(function(){
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
        var found = __sb_queryAllDeep(document, sel);
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

          // Find the nearest row-level label across light DOM + shadow
          // roots. Looks up to 120px above/below the handle row.
          var hcy = r.top + r.height / 2;
          var ytol = Math.max(r.height * 4, 80);
          var label = '';
          var bestDy = Infinity;
          var elRef = el;
          __sb_walkDeepElements(document.body || document.documentElement, function(cand) {
            if (cand === elRef) return;
            try { if (cand.contains && cand.contains(elRef)) return; } catch(e){}
            var cr = cand.getBoundingClientRect ? cand.getBoundingClientRect() : null;
            if (!cr || cr.width === 0 || cr.height === 0) return;
            if (cr.height > 80) return;
            var text = (cand.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!text || text.length > 200 || text.length < 3) return;
            var ccy = cr.top + cr.height / 2;
            var dy = Math.abs(ccy - hcy);
            if (dy > ytol) return;
            // Prefer labels that contain a letter (skip pure "25" tick marks).
            if (!/[A-Za-z]/.test(text)) return;
            if (dy < bestDy) {
              bestDy = dy;
              label = text;
            }
          });

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
      const off = await this.getFrameOffset(frame);
      const offX = off.x;
      const offY = off.y;
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
    const scanSrc = (handleCyLocal: number, yTolerance: number) => SHADOW_DOM_HELPERS_SRC + `;(function(pat, hcy, ytol){
      try {
        var re = new RegExp(pat);
        var best = null;
        __sb_walkDeepElements(document.body || document.documentElement, function(el) {
          var r = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
          if (!r || r.width === 0 || r.height === 0) return;
          if (r.height > 80) return;
          var text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
          if (!text || text.length > 300) return;
          var m = re.exec(text);
          if (!m) return;
          var num = parseFloat(m[1]);
          if (!isFinite(num)) return;
          var cy = r.top + r.height / 2;
          var dy = Math.abs(cy - hcy);
          if (dy > ytol) return;
          var area = r.width * r.height;
          if (best === null || dy < best.dy || (dy === best.dy && area < best.area)) {
            best = { dy: dy, area: area, value: num, text: text,
                     x: r.left, y: r.top, w: r.width, h: r.height };
          }
        });
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
        const off = await this.getFrameOffset(frame);
        const localCy = handleCy - off.y;
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
      const sampleSrc = SHADOW_DOM_HELPERS_SRC + `;(function(hcy, ytol){
        try {
          var out = [];
          __sb_walkDeepElements(document.body || document.documentElement, function(el) {
            var r = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
            if (!r || r.width === 0 || r.height === 0) return;
            if (r.height > 80) return;
            var text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!text || text.length > 300) return;
            var cy = r.top + r.height / 2;
            if (Math.abs(cy - hcy) > ytol) return;
            out.push(text);
            if (out.length >= 10) return false;
          });
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
    // Primary: ask Puppeteer for the frame's host element. Works for
    // same-origin and most cross-origin frames.
    try {
      const handle = await frame.frameElement();
      if (handle) {
        const box = await handle.boundingBox();
        if (box) return { x: box.x, y: box.y };
      }
    } catch {
      /* fall through to URL-match fallback */
    }
    // Fallback: scan the parent frame for an <iframe> whose .src matches
    // this frame's url. Cross-origin iframes can throw on frameElement()
    // but the host iframe element is always reachable from the parent
    // frame's DOM. Returning {0,0} silently lands drag coords in the
    // wrong place — surface a warning when both paths fail.
    try {
      const parent = frame.parentFrame() ?? this.page.mainFrame();
      const url = frame.url();
      const offset = await parent.evaluate((targetUrl: string) => {
        const iframes = Array.prototype.slice.call(
          document.querySelectorAll('iframe'),
        ) as HTMLIFrameElement[];
        for (const f of iframes) {
          if (f.src === targetUrl || (f.contentWindow && f.contentWindow.location && f.contentWindow.location.href === targetUrl)) {
            const r = f.getBoundingClientRect();
            return { x: r.x, y: r.y };
          }
        }
        return null;
      }, url);
      if (offset) return offset;
    } catch {
      /* swallow */
    }
    console.warn(
      `[page.getFrameOffset] could not resolve offset for frame ${frame.url()}; using (0,0)`,
    );
    return { x: 0, y: 0 };
  }

  /**
   * Phase B helper — find the Puppeteer Frame whose host iframe
   * contains the given viewport coords, returning the frame plus the
   * coords translated into iframe-local space. Used by
   * `clickInIframeFrame` when in-page descent (Phase A) hit a cross-
   * origin iframe boundary. Walks `page.frames()` skipping the main
   * frame; for each non-main frame, resolves its host bounding box via
   * `frameElement().boundingBox()` (works for same-origin AND most
   * cross-origin iframes — see `getFrameOffset`'s URL-match fallback).
   * Picks the deepest match (innermost iframe wins on nested cases).
   */
  private async findFrameForIframeAt(
    vx: number,
    vy: number,
  ): Promise<{
    frame: import('puppeteer-core').Frame;
    localX: number;
    localY: number;
    hostBox: { x: number; y: number; w: number; h: number };
  } | null> {
    let best: {
      frame: import('puppeteer-core').Frame;
      localX: number;
      localY: number;
      hostBox: { x: number; y: number; w: number; h: number };
      area: number;
    } | null = null;
    for (const frame of this.page.frames()) {
      if (frame === this.page.mainFrame()) continue;
      let box: { x: number; y: number; width: number; height: number } | null = null;
      try {
        const handle = await frame.frameElement();
        if (handle) box = await handle.boundingBox();
      } catch {
        /* x-origin frameElement may throw; fall through */
      }
      if (!box) {
        // Use getFrameOffset's URL-match fallback for x-origin frames
        // whose frameElement() throws — gives us {x,y} but not w/h, so
        // we skip nested iframe disambiguation in that case.
        const off = await this.getFrameOffset(frame);
        if (off.x === 0 && off.y === 0) continue;
        box = { x: off.x, y: off.y, width: 1, height: 1 };
      }
      if (vx < box.x || vx > box.x + box.width
       || vy < box.y || vy > box.y + box.height) continue;
      const area = Math.max(1, box.width * box.height);
      if (!best || area < best.area) {
        // Smaller area = deeper / more specific match (iframe nested
        // inside another iframe). Prefer the innermost.
        best = {
          frame,
          localX: vx - box.x,
          localY: vy - box.y,
          hostBox: { x: box.x, y: box.y, w: box.width, h: box.height },
          area,
        };
      }
    }
    if (!best) return null;
    return {
      frame: best.frame,
      localX: best.localX,
      localY: best.localY,
      hostBox: best.hostBox,
    };
  }

  /**
   * Phase B — server-side cross-origin OOPIF click fallback. Invoked
   * by the /click handler when `clickInBbox` returns
   * `warning === 'target_in_iframe_cross_origin'` (Phase A's in-page
   * descent could not access `iframe.contentDocument` due to SOP).
   *
   * Strategy: walk `page.frames()` to find the iframe hosting the
   * bbox center. Run a frame-local snap inside `frame.evaluate()` to
   * find the inner interactive element. Dispatch the CDP click at
   * the recomputed viewport coords — Chromium's compositor routes it
   * to the OOPIF for most cases. On dispatch failure (zero DOM
   * mutation), the existing js/keyboard escalation ladder in the
   * /click handler picks up the recovery path.
   */
  async clickInIframeFrame(
    bbox: { x0: number; y0: number; x1: number; y1: number },
    options?: {
      button?: 'left' | 'right' | 'middle';
      clickCount?: number;
      expectedLabel?: string;
      linear?: boolean;
    },
  ): Promise<{
    x: number;
    y: number;
    snapped: boolean;
    target?: string;
    targetXpath?: string;
    warning?: string;
    iframe_chain?: string[];
    native_select?: boolean;
  }> {
    const cx = Math.round((bbox.x0 + bbox.x1) / 2);
    const cy = Math.round((bbox.y0 + bbox.y1) / 2);
    const found = await this.findFrameForIframeAt(cx, cy);
    if (!found) {
      return { x: cx, y: cy, snapped: false, warning: 'iframe_not_resolved' };
    }
    const { frame, localX, localY, hostBox } = found;

    // Frame-local snap. Two-stage: pinpoint at the bbox centre, then
    // 5×5 grid scan inside the bbox if pinpoint misses. The grid scan
    // is critical for iframe targets — vision bboxes for iframe
    // content are often loose (cover whitespace + the actual button),
    // so the centre lands on padding. Mirrors Phase 2 grid scan from
    // the top-level snap.
    const bboxLocal = {
      x0: bbox.x0 - hostBox.x, y0: bbox.y0 - hostBox.y,
      x1: bbox.x1 - hostBox.x, y1: bbox.y1 - hostBox.y,
    };
    const snap = await frame.evaluate(
      (args: {
        lx: number; ly: number;
        b: { x0: number; y0: number; x1: number; y1: number };
        expectedLabel: string;
      }) => {
        const SEL = 'a,button,input,select,textarea,'
          + '[role="button"],[role="link"],[role="checkbox"],'
          + '[role="tab"],[role="menuitem"],[onclick],[tabindex]';
        const xpathOf = (el: Element): string => {
          const parts: string[] = [];
          let cur: Element | null = el;
          while (cur && cur.nodeType === 1
                 && cur !== document.documentElement.parentElement) {
            const t = cur.tagName.toLowerCase();
            let idx = 1;
            let sib: Element | null = cur.previousElementSibling;
            while (sib) {
              if (sib.tagName.toLowerCase() === t) idx += 1;
              sib = sib.previousElementSibling;
            }
            parts.unshift(`${t}[${idx}]`);
            cur = cur.parentElement;
          }
          return '/' + parts.join('/');
        };
        const buildResult = (interactive: Element, method: string) => {
          const r = (interactive as HTMLElement).getBoundingClientRect();
          const tag = interactive.tagName.toLowerCase();
          const id = (interactive as HTMLElement).id ? `#${(interactive as HTMLElement).id}` : '';
          const txt = ((interactive as HTMLElement).textContent || '').trim().slice(0, 30);
          return {
            localCx: Math.round(r.left + r.width / 2),
            localCy: Math.round(r.top + r.height / 2),
            target: `${tag}${id}${txt ? `[${txt}]` : ''}`,
            targetXpath: xpathOf(interactive),
            tag,
            method,
          };
        };
        // Stage 1: pinpoint at bbox centre.
        let stack: Element[] = [];
        try { stack = document.elementsFromPoint(args.lx, args.ly); } catch { stack = []; }
        const centre = stack.find(
          (el) => el !== document.documentElement && el !== document.body,
        );
        if (centre) {
          let interactive: Element | null = null;
          for (const el of stack) {
            if (el === document.documentElement || el === document.body) break;
            try {
              if ((el as Element).matches(SEL)) { interactive = el as Element; break; }
            } catch { /* ignore */ }
          }
          if (!interactive) {
            interactive = (centre as Element).closest(SEL) || centre;
          }
          return buildResult(interactive, 'pinpoint');
        }
        // Stage 2: 5×5 grid scan inside the bbox (frame-local coords).
        // Picks the largest interactive whose rect overlaps the bbox.
        let best: Element | null = null;
        let bestArea = 0;
        for (let i = 1; i < 5; i++) {
          for (let j = 1; j < 5; j++) {
            const px = args.b.x0 + ((args.b.x1 - args.b.x0) * i) / 5;
            const py = args.b.y0 + ((args.b.y1 - args.b.y0) * j) / 5;
            let s: Element[] = [];
            try { s = document.elementsFromPoint(px, py); } catch { s = []; }
            for (const el of s) {
              const hit = (el as Element).closest(SEL);
              if (!hit) continue;
              const r = (hit as HTMLElement).getBoundingClientRect();
              const ix = Math.max(0, Math.min(r.right, args.b.x1) - Math.max(r.left, args.b.x0));
              const iy = Math.max(0, Math.min(r.bottom, args.b.y1) - Math.max(r.top, args.b.y0));
              const area = ix * iy;
              if (area > bestArea) { bestArea = area; best = hit; }
            }
          }
        }
        if (!best) return null;
        return buildResult(best, 'grid_scan');
      },
      {
        lx: localX,
        ly: localY,
        b: bboxLocal,
        expectedLabel: options?.expectedLabel ?? '',
      },
    );
    if (!snap) {
      return { x: cx, y: cy, snapped: false, warning: 'iframe_no_target' };
    }
    // Translate frame-local centre back to viewport coords. Chromium's
    // compositor uses viewport coords for hit-testing across the OOPIF
    // boundary, so this is the right input for CDP dispatch.
    const dispatchX = Math.round(hostBox.x + snap.localCx);
    const dispatchY = Math.round(hostBox.y + snap.localCy);
    const client = await this.getCDPSession();
    await dispatchClick(client, dispatchX, dispatchY, {
      ...options,
      sessionId: this.sessionId,
    });
    await this.waitForIdle(800).catch(() => {});
    // Best-effort xpath of the host iframe for telemetry. We don't
    // have a direct frame.frameElement xpath helper — log the URL and
    // a synthetic marker so the brain knows descent was the cross-
    // origin path.
    const frameUrl = frame.url();
    return {
      x: dispatchX,
      y: dispatchY,
      snapped: true,
      target: snap.target,
      targetXpath: snap.targetXpath,
      warning: 'target_in_iframe_resolved',
      iframe_chain: [`x-origin:${frameUrl.slice(0, 80)}`],
      // Phase G: surface native <select> via the snap.tag field we
      // captured in the frame.evaluate above.
      native_select: snap.tag === 'select' ? true : undefined,
    };
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

        const focusedOk = await this.page.evaluate((sel: string) => {
          const target = document.querySelector(sel) as HTMLElement | null;
          const active = document.activeElement as HTMLElement | null;
          if (!target || !active) return false;
          return target === active
            || target.contains(active)
            || active.contains(target);
        }, selector);
        if (!focusedOk) {
          try {
            await this.page.evaluate((sel: string) => {
              const el = document.querySelector(sel) as HTMLElement | null;
              if (el) (el as HTMLElement).focus();
            }, selector);
            await new Promise((r) => setTimeout(r, 80));
            const refocused = await this.page.evaluate((sel: string) => {
              const target = document.querySelector(sel) as HTMLElement | null;
              const active = document.activeElement as HTMLElement | null;
              if (!target || !active) return false;
              return target === active
                || target.contains(active)
                || active.contains(target);
            }, selector);
            if (!refocused) {
              return {
                success: false,
                reason: 'focus_lost',
                tried,
                error: 'Click landed but focus moved before type — likely a dropdown stole focus.',
                alternatives: [
                  'Take a fresh screenshot and pick the input by V_n',
                  'If an autocomplete is open, browser_click_at the suggestion you want',
                ],
              };
            }
          } catch {
            /* best-effort — fall through to type attempt */
          }
        }

        if (clear) {
          await clearField(client, this.page, coords.x, coords.y);
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

  // Robust window-scroll. `window.scrollBy` is a no-op on pages that lock
  // body overflow and use an inner div as the real scroll surface (a
  // pattern common to single-page wizards and full-bleed app shells).
  // Cascade: window → document.scrollingElement → largest scrollable
  // descendant. Returns {before, after, fallback} so the caller can
  // surface diagnostics when scroll silently fails.
  private async _windowScrollByWithFallback(
    delta: number,
  ): Promise<{ before: number; after: number; fallback: 'window' | 'scrollingElement' | 'largest_container' | 'none' }> {
    if (!Number.isFinite(delta) || delta === 0) {
      const y = (await this.getScrollInfo())[0];
      return { before: y, after: y, fallback: 'none' };
    }
    // Force instant scroll behavior so the synchronous `before`/`after`
    // check is reliable. Modern e-commerce frameworks routinely set
    // `html { scroll-behavior: smooth }` for back-to-top buttons /
    // anchor-link UX. With smooth behavior, `window.scrollBy()` is
    // ASYNC — `scrollY` updates over the animation, not synchronously.
    // The pre-fix code's `before === after` check fired prematurely,
    // returning `fallback: 'none'` even though the page was scrolling
    // smoothly behind the scenes. We override the CSS property at the
    // <html> AND <body> level for the duration of this call, then
    // restore it. Pages that observe style mutations see one transient
    // change; the visible behavior is correct synchronous scroll.
    const eager = (await this.page.evaluate((d: number) => {
      const root = document.documentElement;
      const body = document.body as HTMLElement | null;
      const savedRoot = root.style.scrollBehavior;
      const savedBody = body ? body.style.scrollBehavior : '';
      root.style.scrollBehavior = 'auto';
      if (body) body.style.scrollBehavior = 'auto';
      try {
        const before = Math.round(window.scrollY || (document.scrollingElement?.scrollTop ?? 0));
        window.scrollBy(0, d);
        let after = Math.round(window.scrollY || (document.scrollingElement?.scrollTop ?? 0));
        if (after === before) {
          const se = document.scrollingElement as HTMLElement | null;
          if (se) {
            se.scrollBy(0, d);
            after = Math.round(window.scrollY || se.scrollTop || 0);
          }
        }
        if (after === before) {
          // v6 G2 — collect top scroll-candidate descendants and try
          // EACH in turn until one actually moves. The previous code
          // tried only the largest, ignoring smaller containers like
          // filter sidebars (typically 150-180px tall). When the brain
          // is searching for a control inside the sidebar via
          // scrollUntil, the largest-only heuristic walked through main
          // content forever. Lowered the threshold from 200 → 100 and
          // try top 3.
          const candidates: Array<{ el: HTMLElement; score: number }> = [];
          const all = document.querySelectorAll<HTMLElement>('*');
          for (let i = 0; i < all.length; i++) {
            const el = all[i];
            if (el.clientHeight < 100) continue;
            if (el.scrollHeight <= el.clientHeight + 4) continue;
            const cs = window.getComputedStyle(el);
            if (cs.overflowY !== 'auto' && cs.overflowY !== 'scroll') continue;
            const score = el.clientHeight * (el.scrollHeight - el.clientHeight);
            candidates.push({ el, score });
          }
          candidates.sort((a, b) => b.score - a.score);
          for (const cand of candidates.slice(0, 3)) {
            const target = cand.el;
            const tBefore = target.scrollTop;
            target.scrollBy(0, d);
            if (target.scrollTop !== tBefore) {
              target.setAttribute('data-sb-doc-scroll-host', '1');
              return { before, after: target.scrollTop, fallback: 'largest_container' as const };
            }
          }
        } else {
          return { before, after, fallback: 'window' as const };
        }
        return { before, after, fallback: 'none' as const };
      } finally {
        root.style.scrollBehavior = savedRoot;
        if (body) body.style.scrollBehavior = savedBody;
      }
    }, delta)) as { before: number; after: number; fallback: 'window' | 'scrollingElement' | 'largest_container' | 'none' };
    if (eager.fallback !== 'none') {
      return eager;
    }
    // Retry-with-delay: in rare cases (frameworks that intercept scrollBy
    // and re-dispatch async, or pages where our instant override didn't
    // take), wait 300ms and re-read scrollY. If it moved during the
    // wait, treat as a window scroll. Catches stragglers without
    // disabling the fast path on simple pages.
    const isPageScrollable = await this.page.evaluate(() => {
      return document.documentElement.scrollHeight > window.innerHeight + 4;
    });
    if (!isPageScrollable) {
      return eager;
    }
    await new Promise((r) => setTimeout(r, 300));
    const after = (await this.getScrollInfo())[0];
    if (after !== eager.before) {
      return { before: eager.before, after, fallback: 'window' };
    }
    return eager;
  }

  async scrollPage(direction: 'up' | 'down'): Promise<void> {
    const viewportHeight = this.config.viewport.height;
    // Smaller step than viewport-100. Below-fold filter sections that
    // sit mid-page are routinely overshot by a near-viewport scroll —
    // the brain ends up at the bottom of a 5000px page and re-runs
    // vision with no filter in sight. Default ratio 0.4 keeps each
    // scroll inside ~440px on the standard 1100px viewport, so a
    // partial-fold filter section enters view in 1–2 steps. Override
    // via `SCROLL_STEP_RATIO` (clamped to [0.2, 0.95]); set to 0.91
    // to recover the legacy ~viewport step.
    const ratioRaw = process.env.SCROLL_STEP_RATIO;
    const parsed = ratioRaw ? parseFloat(ratioRaw) : NaN;
    const ratio = Number.isFinite(parsed)
      ? Math.max(0.2, Math.min(0.95, parsed))
      : 0.4;
    const step = Math.max(80, Math.round(viewportHeight * ratio));
    const distance = direction === 'down' ? step : -step;
    await this._windowScrollByWithFallback(distance);
    await new Promise((r) => setTimeout(r, 500));
  }

  async scrollToPercent(percent: number): Promise<void> {
    // Smooth-animate over N steps. Two reasons:
    //   1. The brain often misuses `percent` as "scroll a bit" when it
    //      really means "teleport to N% of total". Animating gives the
    //      page time to lazy-load AND lets vision capture intermediate
    //      states if the brain re-screenshots mid-scroll.
    //   2. The legacy single-step path had a math bug in the largest-
    //      container fallback (multiplied + divided by document range,
    //      which collapsed to nonsense on locked-body SPAs where
    //      document range and host range disagree). Per-step targeting
    //      against each surface's OWN range is stable on all layouts.
    const pct = Math.max(0, Math.min(100, percent));
    const STEPS = 4;
    for (let i = 1; i <= STEPS; i++) {
      const stepPct = (pct * i) / STEPS;
      await this.page.evaluate((p: number) => {
        // Try window first against the document's own range.
        const docMax = Math.max(
          0,
          document.documentElement.scrollHeight - window.innerHeight,
        );
        const docTarget = Math.round(docMax * p / 100);
        const before = window.scrollY;
        window.scrollTo(0, docTarget);
        if (Math.round(window.scrollY) !== before) return;
        // scrollingElement against its own range.
        const se = document.scrollingElement as HTMLElement | null;
        if (se) {
          const seMax = Math.max(0, se.scrollHeight - se.clientHeight);
          const seBefore = se.scrollTop;
          se.scrollTop = Math.round(seMax * p / 100);
          if (se.scrollTop !== seBefore) return;
        }
        // Largest-container fallback — compute target against THIS
        // host's own range, NOT the document's. Bug fix: previous code
        // multiplied a document-range target by host-range and divided
        // by document-range, which yields wrong scrollTop on locked-
        // body SPAs where the two ranges disagree (often jumping the
        // host to its end). Use percent directly against host range.
        let host: HTMLElement | null = null;
        let best = 0;
        const all = document.querySelectorAll<HTMLElement>('*');
        for (let k = 0; k < all.length; k++) {
          const el = all[k];
          if (el.clientHeight < 200) continue;
          if (el.scrollHeight <= el.clientHeight + 4) continue;
          const cs = window.getComputedStyle(el);
          if (cs.overflowY !== 'auto' && cs.overflowY !== 'scroll') continue;
          const score = el.clientHeight * (el.scrollHeight - el.clientHeight);
          if (score > best) { best = score; host = el; }
        }
        if (host) {
          const hostMax = Math.max(0, host.scrollHeight - host.clientHeight);
          host.scrollTop = Math.round(hostMax * p / 100);
          host.setAttribute('data-sb-doc-scroll-host', '1');
        }
      }, stepPct);
      // Per-step wait so vision/lazy-load has time to settle. Last
      // step gets a longer pause so the final landing is stable.
      await new Promise((r) => setTimeout(r, i === STEPS ? 500 : 100));
    }
  }

  // Incremental scroll by an explicit pixel distance — avoids the percent-vs-
  // viewport-vs-pixel confusion the LLM keeps tripping on. Returns the
  // actual delta moved so the caller can detect & report no-op scrolls.
  async scrollByPixels(direction: 'up' | 'down', pixels: number): Promise<{
    before: number;
    after: number;
    scrolledPx: number;
    fallback: 'window' | 'scrollingElement' | 'largest_container' | 'none';
  }> {
    const px = Math.max(1, Math.round(Math.abs(pixels)));
    const signed = direction === 'down' ? px : -px;
    const result = await this._windowScrollByWithFallback(signed);
    await new Promise((r) => setTimeout(r, 500));
    return {
      before: result.before,
      after: result.after,
      scrolledPx: result.after - result.before,
      fallback: result.fallback,
    };
  }

  async getScrollInfo(): Promise<[number, number, number]> {
    // Prefer the tagged document-scroll host (set by
    // `_windowScrollByWithFallback` when window.scrollBy turned out to
    // be a no-op). This keeps scrollUntil's plateau detection honest
    // on pages where the real scroll surface is an inner div.
    return this.page.evaluate(() => {
      const host = document.querySelector('[data-sb-doc-scroll-host]') as HTMLElement | null;
      if (host) {
        return [
          Math.round(host.scrollTop),
          Math.round(host.clientHeight),
          Math.round(host.scrollHeight),
        ];
      }
      return [
        Math.round(window.scrollY),
        Math.round(window.innerHeight),
        Math.round(document.documentElement.scrollHeight),
      ];
    }) as Promise<[number, number, number]>;
  }

  // Closed-loop scroll: walks the page (or a chosen container) toward a
  // target by small steps, narrating what just entered view via a trace.
  // The trace is the load-bearing bit for hallucination control: instead
  // of one big scroll + a stale screenshot, the brain gets every label
  // we passed. If the trace doesn't include the target text, it isn't
  // on this page — no need to invent coordinates and click.
  //
  // `cadence` resolves to a stepRatio when stepRatio is omitted: fine
  // (0.30) for "I'm looking for X" workflows, medium (0.55) for plain
  // navigation, coarse (0.85) preserves the legacy default.
  //
  // `autoReverse` (default true) closes the other failure mode: when a
  // down-scan hits page_end without a match, we turn around and walk
  // back up to page_start before returning. The brain no longer needs
  // to remember to scroll back.
  async scrollUntil(opts: {
    targetText?: string;
    targetRole?: string;
    direction?: 'up' | 'down';
    maxIterations?: number;
    stepRatio?: number;
    cadence?: 'fine' | 'medium' | 'coarse';
    autoReverse?: boolean;
    containerSelector?: string;
    emitTrace?: boolean;
  }): Promise<{
    found: boolean;
    iterations: number;
    finalScrollY: number;
    scrolledPx: number;
    reason: 'matched' | 'page_end' | 'page_start' | 'max_iterations' | 'no_target' | 'reversed_no_match' | 'no_scroll_surface' | 'target_in_no_scrollable_container' | 'no_forward_progress';
    matchedSelector?: string;
    matchedText?: string;
    trace: { i: number; scrollY: number; passed: string[] }[];
    reversed?: boolean;
    startScrollY: number;
    containerSelector?: string;
    /** Diagnostic: which surface we ended up driving. Helps the brain
     *  reason about silent failures ("we scrolled the body but Food
     *  Pairing is in a non-scrollable section"). */
    chosenContainer?: {
      selector?: string;
      tag?: string;
      role?: string;
      scrollHeight?: number;
      clientHeight?: number;
      containedTarget?: boolean;
    };
  }> {
    const targetText = (opts.targetText ?? '').trim();
    const targetRole = (opts.targetRole ?? '').trim();
    const containerSelector = (opts.containerSelector ?? '').trim() || undefined;
    /** Forward leg must move at least this many pixels before
     *  auto_reverse is allowed to rewind. If the page is non-scrollable
     *  (target is inside a static <header> / sticky banner / collapsed
     *  section), reversing 0 pixels of progress just hides the failure.
     *  100px is enough to clear typical anti-aliasing jitter. */
    const MIN_FORWARD_PROGRESS_PX = 100;

    if (!targetText && !targetRole) {
      const info = await this.getScrollInfo();
      return {
        found: false,
        iterations: 0,
        finalScrollY: info[0],
        scrolledPx: 0,
        reason: 'no_target',
        trace: [],
        startScrollY: info[0],
      };
    }

    // Resolve cadence → stepRatio. Explicit stepRatio wins. When neither
    // is given, default cadence depends on whether we have a target:
    // tighter cadence when the brain is actively hunting a label.
    const cadenceMap = { fine: 0.30, medium: 0.55, coarse: 0.85 } as const;
    const explicitStep = typeof opts.stepRatio === 'number';
    const resolvedRatio = explicitStep
      ? Math.max(0.1, Math.min(1.0, opts.stepRatio as number))
      : cadenceMap[opts.cadence ?? (targetText ? 'fine' : 'medium')];

    const maxIter = Math.max(1, Math.min(40, opts.maxIterations ?? 10));
    const autoReverse = opts.autoReverse !== false;
    const emitTrace = opts.emitTrace !== false;
    const startDirection = opts.direction === 'up' ? 'up' : 'down';

    // Regex compile flag — kept for containerProbe below. findMatch
    // does its own regex compile inside findFirstInteractiveMatch, so
    // we only need these locally for the container existence probe.
    let regexSrc = '';
    let isRegex = false;
    if (targetText) {
      regexSrc = targetText;
      try {
        new RegExp(targetText, 'i');
        isRegex = true;
      } catch {
        isRegex = false;
      }
    }

    // Reset the per-scan "already passed" tag so the trace reports
    // arrivals fresh for THIS scan (not bleed-over from a prior call).
    await this.page.evaluate((sel: string | undefined) => {
      const root = sel
        ? (document.querySelector(sel) as HTMLElement | null)
        : document.body;
      const scope = root || document.body;
      try {
        scope.querySelectorAll('[data-sb-passed]').forEach((el) => {
          el.removeAttribute('data-sb-passed');
        });
      } catch { /* ignore */ }
    }, containerSelector);

    const getGeometry = async (): Promise<{ y: number; vp: number; total: number }> => {
      if (containerSelector) {
        const r = await this.page.evaluate((sel: string) => {
          const el = document.querySelector(sel) as HTMLElement | null;
          if (!el) return null;
          return {
            y: Math.round(el.scrollTop),
            vp: Math.round(el.clientHeight),
            total: Math.round(el.scrollHeight),
          };
        }, containerSelector) as { y: number; vp: number; total: number } | null;
        return r ?? { y: 0, vp: 0, total: 0 };
      }
      const [y, vp, total] = await this.getScrollInfo();
      return { y, vp, total };
    };

    // v6 G1 — return whether the scroll actually moved any surface.
    // Previously this was a void function; the caller had no signal
    // when window/scrollingElement/largest-container all returned no-
    // op. Loop continued, plateau detection eventually fired, but the
    // brain saw `page_end` with scrolledPx=0 and was confused.
    const scrollByDelta = async (delta: number): Promise<{ moved: boolean }> => {
      if (containerSelector) {
        const before = (await getGeometry()).y;
        await this.page.evaluate(
          (args: { sel: string; d: number }) => {
            const el = document.querySelector(args.sel) as HTMLElement | null;
            if (el) el.scrollBy(0, args.d);
          },
          { sel: containerSelector, d: delta },
        );
        const after = (await getGeometry()).y;
        return { moved: after !== before };
      }
      // Use the robust window-scroll cascade so scroll_until works on
      // pages where body has overflow:hidden (wizard-style SPAs, full-
      // bleed apps).
      const result = await this._windowScrollByWithFallback(delta);
      return { moved: result.fallback !== 'none' && result.after !== result.before };
    };

    // Find a visible match within scope (page or container). Shared
    // matcher lives in scroll-probe.ts so browser_scroll's PROBE path
    // and browser_scroll_until use identical text/role/visibility logic.
    const findMatch = async (): Promise<{ selector: string; text: string } | null> => {
      return await findFirstInteractiveMatch(this.page, {
        targetText,
        targetRole,
        containerSelector,
      });
    };

    // Per-step "what entered view" diff. Tags newcomers with
    // data-sb-passed so we only narrate each label once. Returns up to
    // 5 short labels — enough for the brain to recognize the page
    // shape, capped to keep the trace prompt-budget under control.
    const collectArrivals = async (): Promise<string[]> => {
      if (!emitTrace) return [];
      return await this.page.evaluate((container?: string) => {
        const root: ParentNode = container
          ? (document.querySelector(container) as HTMLElement | null) ?? document
          : document;
        const containerEl = container ? (root as HTMLElement) : null;
        const inViewport = (el: Element): boolean => {
          const r = (el as HTMLElement).getBoundingClientRect();
          if (r.width <= 0 || r.height <= 0) return false;
          if (containerEl) {
            const cr = containerEl.getBoundingClientRect();
            if (r.bottom < cr.top || r.top > cr.bottom) return false;
          } else {
            if (r.bottom < 0 || r.top > window.innerHeight) return false;
          }
          const cs = window.getComputedStyle(el as HTMLElement);
          if (cs.visibility === 'hidden' || cs.display === 'none') return false;
          return true;
        };
        const shorten = (s: string): string => {
          const norm = s.replace(/\s+/g, ' ').trim();
          if (!norm) return '';
          return norm.length > 28 ? `${norm.slice(0, 26)}…` : norm;
        };
        const arrivals: string[] = [];
        const interactive = Array.from(root.querySelectorAll(
          'a, button, input, select, textarea, label, summary, ' +
          '[role], [aria-label], [data-testid], h1, h2, h3, h4',
        )) as HTMLElement[];
        for (const el of interactive) {
          if (arrivals.length >= 5) break;
          if (el.hasAttribute('data-sb-passed')) continue;
          if (!inViewport(el)) continue;
          el.setAttribute('data-sb-passed', '1');
          const aria = el.getAttribute('aria-label') || '';
          const txt = el.innerText || el.textContent || '';
          const placeholder = el.getAttribute('placeholder') || '';
          const label = shorten(aria || txt || placeholder);
          if (label) arrivals.push(label);
        }
        return arrivals;
      }, containerSelector) as string[];
    };

    // Initial geometry + freebie check (target may already be on screen).
    const startInfo = await getGeometry();
    const startY = startInfo.y;
    const stepDelta = Math.max(80, Math.round(startInfo.vp * resolvedRatio));
    const trace: { i: number; scrollY: number; passed: string[] }[] = [];

    // Seed trace with what's already in view at iter 0 — gives the brain
    // a complete narrative even if the match is found before scrolling.
    if (emitTrace) {
      const seed = await collectArrivals();
      if (seed.length) {
        trace.push({ i: 0, scrollY: startY, passed: seed });
      }
    }

    const initialMatch = await findMatch();
    if (initialMatch) {
      const info = await getGeometry();
      return {
        found: true,
        iterations: 0,
        finalScrollY: info.y,
        scrolledPx: 0,
        reason: 'matched',
        matchedSelector: initialMatch.selector,
        matchedText: initialMatch.text,
        trace,
        startScrollY: startY,
        containerSelector,
      };
    }

    // Pre-walk: does the target text exist in DOM at all, and if so,
    // is it inside a scrollable ancestor we can drive? When the answer
    // is "exists but not in any scroll surface" (e.g., sitting in a
    // collapsed <details>, or inside a non-overflowing static section),
    // bail with `target_in_no_scrollable_container` instead of
    // grinding through maxIter and reporting `page_end`. The brain
    // reads this and adjusts strategy (expand the section, or accept
    // the target is unreachable via scroll).
    const containerProbe = await this.page.evaluate(
      (args: { regexSrc: string; isRegex: boolean; container?: string }) => {
        const { regexSrc: rs, isRegex: ir, container } = args;
        if (!rs) return { existsInDom: false, hasScrollableAncestor: false };
        const matches = (txt: string): boolean => {
          if (ir) {
            try { return new RegExp(rs, 'i').test(txt); }
            catch { return txt.toLowerCase().includes(rs.toLowerCase()); }
          }
          return txt.toLowerCase().includes(rs.toLowerCase());
        };
        const root: ParentNode = container
          ? (document.querySelector(container) as HTMLElement | null) ?? document
          : document;
        const all = Array.from(root.querySelectorAll<HTMLElement>(
          'a, button, input, select, textarea, label, summary, '
          + '[role], [aria-label], [data-testid], h1, h2, h3, h4, h5, '
          + 'li, td, th, span, div',
        ));
        let candidate: HTMLElement | null = null;
        for (const el of all) {
          const txt = (el.innerText || el.textContent || '').trim();
          const aria = el.getAttribute('aria-label') || '';
          const placeholder = el.getAttribute('placeholder') || '';
          const composite = `${txt}\n${aria}\n${placeholder}`.trim();
          if (composite && matches(composite)) {
            candidate = el;
            break;
          }
        }
        if (!candidate) return { existsInDom: false, hasScrollableAncestor: false };
        // Walk parents looking for a scrollable ancestor. Document.body
        // / documentElement count when their respective scrollHeight
        // exceeds clientHeight.
        let walker: HTMLElement | null = candidate.parentElement;
        let depth = 0;
        let foundScroll: HTMLElement | null = null;
        while (walker && depth < 40) {
          const cs = window.getComputedStyle(walker);
          const overflowY = cs.overflowY;
          if (
            (overflowY === 'auto' || overflowY === 'scroll')
            && walker.scrollHeight > walker.clientHeight + 4
          ) {
            foundScroll = walker;
            break;
          }
          if (walker === document.body || walker === document.documentElement) break;
          walker = walker.parentElement;
          depth += 1;
        }
        // Document-level scroll counts too.
        const docScrollable =
          document.documentElement.scrollHeight > window.innerHeight + 4;
        return {
          existsInDom: true,
          hasScrollableAncestor: !!foundScroll || docScrollable,
        };
      },
      { regexSrc, isRegex, container: containerSelector },
    );

    if (
      containerProbe.existsInDom
      && !containerProbe.hasScrollableAncestor
      && !containerSelector
    ) {
      return {
        found: false,
        iterations: 0,
        finalScrollY: startY,
        scrolledPx: 0,
        reason: 'target_in_no_scrollable_container',
        trace,
        startScrollY: startY,
        containerSelector,
      };
    }

    // Run a single direction scan. Returns the outcome OR null if the
    // caller should auto-reverse and try the other way.
    const scanOnce = async (
      direction: 'up' | 'down',
      iterStart: number,
    ): Promise<{
      done: true;
      result: {
        found: boolean;
        iterations: number;
        finalScrollY: number;
        scrolledPx: number;
        reason: 'matched' | 'page_end' | 'page_start' | 'max_iterations' | 'no_scroll_surface';
        matchedSelector?: string;
        matchedText?: string;
      };
    } | { done: false; reason: 'page_end' | 'page_start'; iterations: number; finalScrollY: number }> => {
      let iterations = iterStart;
      let lastY = (await getGeometry()).y;
      let plateauHits = 0;
      // v6 G1 — track when scrollByDelta reports no scroll surface
      // moved at all. Two consecutive 0-move iterations = page has no
      // scrollable surface we can drive. Exit early with explicit
      // reason instead of grinding through maxIter and reporting
      // page_end (which misleads the brain into thinking it walked
      // the whole page).
      let noMoveStreak = 0;
      while (iterations < iterStart + maxIter) {
        iterations += 1;
        const delta = direction === 'down' ? stepDelta : -stepDelta;
        const stepResult = await scrollByDelta(delta);
        if (!stepResult.moved) {
          noMoveStreak += 1;
          if (noMoveStreak >= 2 && !containerSelector) {
            const info0 = await getGeometry();
            return {
              done: true,
              result: {
                found: false,
                iterations,
                finalScrollY: info0.y,
                scrolledPx: info0.y - startY,
                reason: 'no_scroll_surface',
              },
            };
          }
        } else {
          noMoveStreak = 0;
        }
        await new Promise((r) => setTimeout(r, 300));
        const info = await getGeometry();
        const y = info.y;

        if (emitTrace) {
          const passed = await collectArrivals();
          if (passed.length) {
            trace.push({ i: iterations, scrollY: y, passed });
          }
        }

        if (Math.abs(y - lastY) < 2) {
          plateauHits += 1;
          if (plateauHits >= 2) {
            return {
              done: false,
              reason: direction === 'down' ? 'page_end' : 'page_start',
              iterations,
              finalScrollY: y,
            };
          }
        } else {
          plateauHits = 0;
        }
        lastY = y;

        const m = await findMatch();
        if (m) {
          return {
            done: true,
            result: {
              found: true,
              iterations,
              finalScrollY: y,
              scrolledPx: y - startY,
              reason: 'matched',
              matchedSelector: m.selector,
              matchedText: m.text,
            },
          };
        }
      }
      const finalInfo = await getGeometry();
      return {
        done: true,
        result: {
          found: false,
          iterations,
          finalScrollY: finalInfo.y,
          scrolledPx: finalInfo.y - startY,
          reason: 'max_iterations',
        },
      };
    };

    // Phase 1.
    const phase1 = await scanOnce(startDirection, 0);
    if (phase1.done) {
      return { ...phase1.result, trace, startScrollY: startY, containerSelector };
    }

    // Phase 2 (auto-reverse). Only fires if the first leg hit a real
    // page boundary (not max_iterations) and reversal is enabled.
    if (!autoReverse) {
      return {
        found: false,
        iterations: phase1.iterations,
        finalScrollY: phase1.finalScrollY,
        scrolledPx: phase1.finalScrollY - startY,
        reason: phase1.reason,
        trace,
        startScrollY: startY,
        containerSelector,
      };
    }

    // If the forward leg made <100px of progress, reversing back is
    // pointless (and erases what little motion did happen). Bail with
    // a distinct `no_forward_progress` reason — brain reads it as "no
    // scroll surface accepted my scroll" rather than "I scanned the
    // whole page and didn't find it".
    const forwardProgress = Math.abs(phase1.finalScrollY - startY);
    if (forwardProgress < MIN_FORWARD_PROGRESS_PX) {
      return {
        found: false,
        iterations: phase1.iterations,
        finalScrollY: phase1.finalScrollY,
        scrolledPx: phase1.finalScrollY - startY,
        reason: 'no_forward_progress',
        trace,
        startScrollY: startY,
        containerSelector,
      };
    }

    const reverseDir: 'up' | 'down' = startDirection === 'down' ? 'up' : 'down';
    const phase2 = await scanOnce(reverseDir, phase1.iterations);
    if (phase2.done && phase2.result.found) {
      return {
        ...phase2.result,
        reversed: true,
        trace,
        startScrollY: startY,
        containerSelector,
      };
    }

    const finalY = phase2.done ? phase2.result.finalScrollY : phase2.finalScrollY;
    const finalIters = phase2.done ? phase2.result.iterations : phase2.iterations;
    return {
      found: false,
      iterations: finalIters,
      finalScrollY: finalY,
      scrolledPx: finalY - startY,
      reason: 'reversed_no_match',
      reversed: true,
      trace,
      startScrollY: startY,
      containerSelector,
    };
  }

  // In-container scroll for open dropdowns / listboxes / menus / modal
  // lists. When `containerSelector` isn't given, auto-detects the most
  // recently opened popup (role=listbox|menu, headlessui-state=open,
  // data-state=open) and walks UP its ancestor chain to find the
  // smallest scrollable host. Falls back to the focused element's
  // scrollable ancestor.
  async scrollWithin(opts: {
    containerSelector?: string;
    direction?: 'up' | 'down';
    amount?: 'page' | 'half' | number;
    targetText?: string;
    maxIterations?: number;
  }): Promise<{
    found: boolean;
    iterations: number;
    finalScrollY: number;
    scrolledPx: number;
    reason: 'matched' | 'page_end' | 'page_start' | 'max_iterations' | 'no_target' | 'reversed_no_match' | 'no_container' | 'no_scroll_surface' | 'target_in_no_scrollable_container' | 'no_forward_progress';
    matchedSelector?: string;
    matchedText?: string;
    trace: { i: number; scrollY: number; passed: string[] }[];
    reversed?: boolean;
    startScrollY: number;
    containerSelector?: string;
    resolvedContainer: string;
  }> {
    // 1. Resolve the container selector. If the caller supplied one, use it.
    //    Otherwise auto-detect — multi-signal: aria-controls of an open
    //    trigger (strongest), known popup roles, then descendants that
    //    look like an open dropdown by their children's roles
    //    (menuitemcheckbox/menuitemradio/option/menuitem). For each
    //    popup candidate, search BOTH up the ancestor chain AND down
    //    the descendant tree for a scrollable element — many sites
    //    (Material UI, Radix Select, Best Buy) put the overflow:auto
    //    on a wrapper INSIDE the popup, not on the popup itself.
    let resolved = (opts.containerSelector ?? '').trim();
    if (!resolved) {
      resolved = await this.page.evaluate(() => {
        const isVisible = (el: HTMLElement): boolean => {
          const r = el.getBoundingClientRect();
          if (r.width <= 0 || r.height <= 0) return false;
          const cs = window.getComputedStyle(el);
          if (cs.visibility === 'hidden' || cs.display === 'none') return false;
          return true;
        };
        const isScrollable = (el: HTMLElement): boolean => {
          const cs = window.getComputedStyle(el);
          const oy = cs.overflowY;
          return (oy === 'auto' || oy === 'scroll')
              && el.scrollHeight > el.clientHeight + 4;
        };
        // Walk UP the ancestor chain looking for the first scrollable
        // element. Skips document.body which is page-level scroll, not
        // popup-inner.
        const findScrollHostUp = (start: HTMLElement | null): HTMLElement | null => {
          let cur: HTMLElement | null = start;
          while (cur && cur !== document.body) {
            if (isScrollable(cur)) return cur;
            cur = cur.parentElement;
          }
          return null;
        };
        // Walk DOWN inside `root`, BFS, looking for the first scrollable
        // descendant. Capped at depth/breadth to avoid pathological cost
        // on large popups. Returns the FIRST match in BFS order so the
        // innermost wrapping scroller wins for nested layouts.
        const findScrollHostDown = (root: HTMLElement): HTMLElement | null => {
          const queue: HTMLElement[] = [];
          for (const c of Array.from(root.children) as HTMLElement[]) {
            queue.push(c);
          }
          let inspected = 0;
          while (queue.length && inspected < 200) {
            const cur = queue.shift()!;
            inspected += 1;
            if (isScrollable(cur)) return cur;
            for (const c of Array.from(cur.children) as HTMLElement[]) {
              queue.push(c);
            }
          }
          return null;
        };
        // Resolve a popup candidate to its scroll host: try the element
        // itself, then walk DOWN inside it (most sites — Material UI,
        // Radix Select, Best Buy — put overflow on an inner wrapper),
        // then walk UP for the rare case where the scrollable parent
        // contains the popup.
        const resolveHost = (popup: HTMLElement): HTMLElement | null => {
          if (isScrollable(popup)) return popup;
          const down = findScrollHostDown(popup);
          if (down) return down;
          return findScrollHostUp(popup);
        };
        type Candidate = { host: HTMLElement; popup: HTMLElement; z: number };
        const candidates: Candidate[] = [];
        const seen = new Set<HTMLElement>();
        const tryAdd = (popup: HTMLElement) => {
          if (!isVisible(popup)) return;
          if (seen.has(popup)) return;
          seen.add(popup);
          const host = resolveHost(popup);
          if (!host) return;
          const z = parseInt(window.getComputedStyle(host).zIndex || '0', 10);
          candidates.push({ host, popup, z: isNaN(z) ? 0 : z });
        };
        // Signal 1 (strongest): aria-controls target of any element with
        // aria-expanded="true". This is the canonical ARIA pattern —
        // the open trigger names its popup by id. When the brain just
        // clicked a dropdown trigger, this nails the right popup.
        const expanded = Array.from(
          document.querySelectorAll<HTMLElement>('[aria-expanded="true"][aria-controls]'),
        );
        for (const trig of expanded) {
          const ctrlId = (trig.getAttribute('aria-controls') || '').trim();
          if (!ctrlId) continue;
          // aria-controls may be a space-separated list of IDs.
          for (const id of ctrlId.split(/\s+/)) {
            const target = document.getElementById(id);
            if (target instanceof HTMLElement) tryAdd(target);
          }
        }
        // Signal 2: known popup roles + framework open markers.
        const popupSelectors = [
          '[role="listbox"]:not([aria-hidden="true"])',
          '[role="menu"]:not([aria-hidden="true"])',
          '[role="dialog"]:not([aria-hidden="true"])',
          '[data-headlessui-state="open"]',
          '[data-state="open"]',
          '[data-radix-popper-content-wrapper]',
        ];
        for (const sel of popupSelectors) {
          for (const el of Array.from(document.querySelectorAll<HTMLElement>(sel))) {
            tryAdd(el);
          }
        }
        // Signal 3: any visible element whose children include 2+
        // visible option/menuitem-style entries. Catches custom
        // dropdowns that omit ARIA on the wrapper. We promote the
        // CLOSEST common ancestor (the option's parent) as the popup
        // candidate — that's usually the right scroll surface or its
        // wrapper.
        const optionLike = Array.from(document.querySelectorAll<HTMLElement>(
          '[role="option"], [role="menuitem"],'
          + ' [role="menuitemcheckbox"], [role="menuitemradio"]',
        )).filter(isVisible);
        const byParent = new Map<HTMLElement, number>();
        for (const opt of optionLike) {
          const p = opt.parentElement;
          if (!p) continue;
          byParent.set(p, (byParent.get(p) || 0) + 1);
        }
        for (const [parent, n] of byParent) {
          if (n >= 2) tryAdd(parent);
        }
        // Fallback: scrollable ancestor of focused element.
        if (candidates.length === 0 && document.activeElement
            && document.activeElement !== document.body) {
          const host = findScrollHostUp(document.activeElement as HTMLElement);
          if (host) {
            candidates.push({ host, popup: host, z: 0 });
          }
        }
        if (candidates.length === 0) return '';
        // Prefer hosts that contain option-like children — those are the
        // actual menu containers, not page-level scroll wrappers.
        const withOptions = candidates.filter(
          (c) => c.host.querySelector(
            '[role="option"], [role="menuitem"],'
            + ' [role="menuitemcheckbox"], [role="menuitemradio"]',
          ),
        );
        const pool = withOptions.length ? withOptions : candidates;
        pool.sort((a, b) => {
          if (b.z !== a.z) return b.z - a.z;
          return a.host.scrollHeight - b.host.scrollHeight;
        });
        const winner = pool[0].host;
        const id = `sb-scroll-host-${Math.random().toString(36).slice(2, 10)}`;
        winner.setAttribute('data-sb-scroll-host', id);
        return `[data-sb-scroll-host="${id}"]`;
      }) as string;
    }

    if (!resolved) {
      const startInfo = await this.getScrollInfo();
      return {
        found: false,
        iterations: 0,
        finalScrollY: startInfo[0],
        scrolledPx: 0,
        reason: 'no_container',
        trace: [],
        startScrollY: startInfo[0],
        resolvedContainer: '',
      };
    }

    // 2. With target_text → delegate to scrollUntil scoped to container.
    if ((opts.targetText ?? '').trim()) {
      const result = await this.scrollUntil({
        targetText: opts.targetText,
        direction: opts.direction,
        maxIterations: opts.maxIterations ?? 12,
        containerSelector: resolved,
        cadence: 'fine',
        autoReverse: true,
        emitTrace: true,
      });
      return { ...result, resolvedContainer: resolved };
    }

    // 3. Without target → one-shot scroll by amount.
    const direction = opts.direction === 'up' ? 'up' : 'down';
    const geo = await this.page.evaluate((sel: string) => {
      const el = document.querySelector(sel) as HTMLElement | null;
      if (!el) return null;
      return {
        y: Math.round(el.scrollTop),
        vp: Math.round(el.clientHeight),
        total: Math.round(el.scrollHeight),
      };
    }, resolved) as { y: number; vp: number; total: number } | null;

    if (!geo) {
      return {
        found: false,
        iterations: 0,
        finalScrollY: 0,
        scrolledPx: 0,
        reason: 'no_container',
        trace: [],
        startScrollY: 0,
        resolvedContainer: resolved,
      };
    }
    const startY = geo.y;
    const amount = opts.amount;
    let pxDelta: number;
    if (typeof amount === 'number') pxDelta = Math.max(1, Math.round(Math.abs(amount)));
    else if (amount === 'half') pxDelta = Math.max(40, Math.round(geo.vp * 0.5));
    else pxDelta = Math.max(60, Math.round(geo.vp * 0.85));
    const signed = direction === 'down' ? pxDelta : -pxDelta;
    await this.page.evaluate(
      (args: { sel: string; d: number }) => {
        const el = document.querySelector(args.sel) as HTMLElement | null;
        if (el) el.scrollBy(0, args.d);
      },
      { sel: resolved, d: signed },
    );
    await new Promise((r) => setTimeout(r, 300));
    const after = await this.page.evaluate((sel: string) => {
      const el = document.querySelector(sel) as HTMLElement | null;
      return el ? Math.round(el.scrollTop) : 0;
    }, resolved) as number;
    const moved = after - startY;
    const reachedEnd = direction === 'down' ? Math.abs(moved) < 2 : after <= 4;
    return {
      found: false,
      iterations: 1,
      finalScrollY: after,
      scrolledPx: moved,
      reason: reachedEnd ? (direction === 'down' ? 'page_end' : 'page_start') : 'max_iterations',
      trace: [],
      startScrollY: startY,
      resolvedContainer: resolved,
      containerSelector: resolved,
    };
  }

  // --- Scroll a bbox into view (page or inner popup) ---
  //
  // Given a CSS-pixel bbox, pick the right scroll surface (the nearest
  // scrollable popup ancestor of the element at the bbox center, or
  // the page if no inner popup), and scroll so the bbox is fully on
  // screen. Used by both `browser_scroll_to_bbox` and the auto-scroll
  // step inside `bbox_click` so the brain never has to call
  // `browser_scroll_within` to position a dropdown option before
  // clicking it.
  async scrollBboxIntoView(bbox: {
    x0: number;
    y0: number;
    x1: number;
    y1: number;
  }): Promise<{
    scrolled: boolean;
    containerKind: 'page' | 'popup' | 'already_visible';
    deltaY: number;
    newBbox: { x0: number; y0: number; x1: number; y1: number };
  }> {
    const result = await this.page.evaluate((b: {
      x0: number; y0: number; x1: number; y1: number;
    }) => {
      const cx = (b.x0 + b.x1) / 2;
      const cy = (b.y0 + b.y1) / 2;
      const findScrollHost = (start: HTMLElement | null): HTMLElement | null => {
        let cur: HTMLElement | null = start;
        while (cur && cur !== document.body) {
          const cs = window.getComputedStyle(cur);
          const oy = cs.overflowY;
          if ((oy === 'auto' || oy === 'scroll')
              && cur.scrollHeight > cur.clientHeight + 4) {
            return cur;
          }
          cur = cur.parentElement;
        }
        return null;
      };
      // Probe the element at the bbox center. If outside the viewport,
      // we won't get a hit — fall back to using the bbox center as a
      // direction signal for window scroll.
      const vw = window.innerWidth || 0;
      const vh = window.innerHeight || 0;
      const padding = 24;
      const fullyInViewport = (
        b.x0 >= 0 && b.x1 <= vw
        && b.y0 >= 0 && b.y1 <= vh
      );
      if (fullyInViewport) {
        return {
          scrolled: false,
          containerKind: 'already_visible' as const,
          deltaY: 0,
          newBbox: { x0: b.x0, y0: b.y0, x1: b.x1, y1: b.y1 },
        };
      }
      // Try inner popup first: look for an open listbox/menu/dialog
      // whose rect contains the bbox center.
      const popupSelectors = [
        '[role="listbox"]:not([aria-hidden="true"])',
        '[role="menu"]:not([aria-hidden="true"])',
        '[role="dialog"]:not([aria-hidden="true"])',
        '[data-headlessui-state="open"]',
        '[data-state="open"]',
      ];
      let innerHost: HTMLElement | null = null;
      for (const sel of popupSelectors) {
        for (const el of Array.from(document.querySelectorAll<HTMLElement>(sel))) {
          const r = el.getBoundingClientRect();
          if (r.width <= 0 || r.height <= 0) continue;
          if (cx < r.left || cx > r.right) continue;
          // The bbox center is horizontally inside this popup — its
          // scroll host is the right surface. (Vertical containment
          // is intentionally skipped: the bbox may be vertically OUTSIDE
          // the popup's current viewport, which is exactly the case we
          // need to scroll for.)
          const cs = window.getComputedStyle(el);
          if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
              && el.scrollHeight > el.clientHeight + 4) {
            innerHost = el;
          } else {
            innerHost = findScrollHost(el);
          }
          if (innerHost) break;
        }
        if (innerHost) break;
      }
      if (innerHost) {
        const hostRect = innerHost.getBoundingClientRect();
        // We want bbox center to be at host's vertical center.
        const desiredCenterY = hostRect.top + hostRect.height / 2;
        const deltaInner = Math.round(cy - desiredCenterY);
        const beforeY = innerHost.scrollTop;
        innerHost.scrollBy(0, deltaInner);
        const afterY = innerHost.scrollTop;
        const actualDelta = afterY - beforeY;
        // Re-read bbox after inner scroll: rect shifts by -actualDelta
        // relative to viewport since the inner container moved up by
        // actualDelta px.
        const newY0 = b.y0 - actualDelta;
        const newY1 = b.y1 - actualDelta;
        return {
          scrolled: actualDelta !== 0,
          containerKind: 'popup' as const,
          deltaY: actualDelta,
          newBbox: { x0: b.x0, y0: newY0, x1: b.x1, y1: newY1 },
        };
      }
      // Fall through: page-level scroll.
      const desiredCenterY = vh / 2;
      const deltaPage = Math.round(cy - desiredCenterY);
      const beforeScrollY = window.scrollY || window.pageYOffset || 0;
      window.scrollBy(0, deltaPage);
      const afterScrollY = window.scrollY || window.pageYOffset || 0;
      const actualPageDelta = afterScrollY - beforeScrollY;
      return {
        scrolled: actualPageDelta !== 0,
        containerKind: 'page' as const,
        deltaY: actualPageDelta,
        newBbox: {
          x0: b.x0,
          y0: b.y0 - actualPageDelta,
          x1: b.x1,
          y1: b.y1 - actualPageDelta,
        },
      };
    }, bbox);
    // Let layout settle before caller re-snaps for click.
    await new Promise((r) => setTimeout(r, 120));
    return result as {
      scrolled: boolean;
      containerKind: 'page' | 'popup' | 'already_visible';
      deltaY: number;
      newBbox: { x0: number; y0: number; x1: number; y1: number };
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

  async getMarkdownContent(opts?: { includeAnchors?: boolean }): Promise<string> {
    const includeAnchors = !!(opts && opts.includeAnchors);
    return this.page.evaluate((withAnchors: boolean) => {
      // Capture LIVE heading positions BEFORE cloning. The clone is
      // detached from layout so getBoundingClientRect on cloned nodes
      // returns zeros. Pair by (level, text, occurrence index) when
      // emitting in the clone.
      type LiveHead = { level: number; text: string; y: number; consumed: boolean };
      const liveHeads: LiveHead[] = [];
      if (withAnchors) {
        for (let lvl = 1; lvl <= 6; lvl++) {
          document.querySelectorAll(`h${lvl}`).forEach((h) => {
            const text = (h.textContent || '').trim();
            if (!text) return;
            const rect = (h as HTMLElement).getBoundingClientRect();
            // Skip headings whose ancestor is display:none / hidden;
            // a zero-rect heading isn't useful as a scroll anchor.
            if (rect.width <= 0 && rect.height <= 0) return;
            const y = Math.round(rect.top + window.scrollY);
            liveHeads.push({ level: lvl, text, y, consumed: false });
          });
        }
      }

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

      // Convert headings (with optional inline @y anchor)
      for (let i = 1; i <= 6; i++) {
        clone.querySelectorAll(`h${i}`).forEach((h) => {
          const text = h.textContent?.trim() || '';
          if (!text) return;
          let suffix = '';
          if (withAnchors) {
            // First unconsumed live heading at the same level + text.
            const match = liveHeads.find(
              (lh) => !lh.consumed && lh.level === i && lh.text === text,
            );
            if (match) {
              match.consumed = true;
              suffix = ` [@y=${match.y}]`;
            }
          }
          h.textContent = '\n' + '#'.repeat(i) + ' ' + text + suffix + '\n';
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
      let out = text.trim().substring(0, 50000);

      if (withAnchors) {
        // Trailing OUTLINE line — gives the brain the page envelope so
        // it can compute pixels = (anchor_y - current_scrollY) for
        // browser_scroll(direction='down', pixels=…) without a second
        // tool call.
        const sy = Math.round(window.scrollY);
        const sh = Math.round(document.documentElement.scrollHeight);
        const vp = Math.round(window.innerHeight);
        out += `\n\n[OUTLINE scrollY=${sy} scrollHeight=${sh} vp=${vp}]`;

        // v6 G4 — list scroll containers so the brain knows where to
        // pass `container_selector=` when browser_scroll_until returns
        // `no_scroll_surface` or `reversed_no_match`. Top 5 by score
        // (clientHeight × overflow). Selectors are short tag+id+class
        // hints, not stable CSS — but enough to disambiguate.
        const containers: Array<{ sel: string; cw: number; ch: number; sh: number; sy: number; score: number }> = [];
        try {
          const all = document.querySelectorAll<HTMLElement>('*');
          for (let i = 0; i < all.length; i++) {
            const el = all[i];
            if (el.clientHeight < 100) continue;
            if (el.scrollHeight <= el.clientHeight + 4) continue;
            const cs = window.getComputedStyle(el);
            if (cs.overflowY !== 'auto' && cs.overflowY !== 'scroll') continue;
            const tag = el.tagName.toLowerCase();
            const id = el.id ? `#${el.id}` : '';
            const cls = (el.className && typeof el.className === 'string')
              ? `.${el.className.split(/\s+/).filter(Boolean).slice(0, 1).join('.')}`
              : '';
            const sel = `${tag}${id}${cls}`;
            const score = el.clientHeight * (el.scrollHeight - el.clientHeight);
            containers.push({
              sel,
              cw: Math.round(el.clientWidth),
              ch: Math.round(el.clientHeight),
              sh: Math.round(el.scrollHeight),
              sy: Math.round(el.scrollTop),
              score,
            });
          }
        } catch { /* best-effort */ }
        containers.sort((a, b) => b.score - a.score);
        const top = containers.slice(0, 5);
        if (top.length > 0) {
          const lines = top.map(
            (c) => `  - ${c.sel} (${c.cw}×${c.ch}, scrollH=${c.sh}, scrollY=${c.sy})`,
          );
          out += `\n[SCROLL_CONTAINERS\n${lines.join('\n')}\n]`;
        }
      }
      return out;
    }, includeAnchors);
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
