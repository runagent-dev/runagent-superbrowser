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

export class PageWrapper {
  private cdpClient: CDPSession | null = null;
  private pendingDialogs: DialogInfo[] = [];
  private dialogHandlerSetup = false;
  private consoleCollector = new ConsoleCollector();
  private downloadMonitor = new DownloadMonitor();
  private triedExternalSolver = false;

  constructor(
    private page: Page,
    private config: BrowserConfig,
  ) {}

  /** Get the underlying puppeteer Page. */
  getRawPage(): Page {
    return this.page;
  }

  // --- Navigation ---

  async navigate(url: string, timeout: number = 30000): Promise<void> {
    // SSRF protection
    const urlCheck = validateUrl(url);
    if (!urlCheck.valid) {
      throw new Error(`Blocked: ${urlCheck.error}`);
    }

    await this.page.goto(url, {
      waitUntil: 'domcontentloaded',
      timeout,
    });
    // Wait a bit for dynamic content
    await this.waitForIdle(2000).catch(() => {});

    // Auto-wait for bot protection challenges
    await this.waitForCloudflare();
    await this.waitForPerimeterX();

    // Auto-dismiss cookie consent, country selectors, and other overlays.
    // Run twice with a delay — many SPAs show overlays asynchronously after page load.
    await this.dismissConsentScreens().catch(() => {});
    await this.dismissOverlays().catch(() => {});
    // Second pass after a delay for late-appearing overlays
    await new Promise(r => setTimeout(r, 2000));
    await this.dismissConsentScreens().catch(() => {});
    await this.dismissOverlays().catch(() => {});
  }

  /**
   * Wait for Cloudflare challenge to resolve.
   *
   * Detection: page title ("Just a moment...") + Turnstile iframe.
   * Resolution: polls for cf-turnstile-response token in DOM (BrowserOS pattern)
   * AND checks title change. Timeout 30s (Turnstile can take 10-20s).
   */
  async waitForCloudflare(maxWait: number = 60000): Promise<boolean> {
    this.triedExternalSolver = false;
    const title = await this.page.title();
    const titleLower = title.toLowerCase();
    const isCfTitleChallenge = titleLower.includes('just a moment')
      || titleLower.includes('checking your browser')
      || titleLower.includes('attention required')
      || titleLower.includes('verify you are human')
      || titleLower.includes('security check');

    // Also check for Turnstile widget in DOM (title may be the site name while CF overlay is present)
    const hasTurnstileWidget = !isCfTitleChallenge ? await this.page.evaluate(() => {
      return !!document.querySelector(
        'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], '
        + '.cf-turnstile, #challenge-running, #challenge-stage, #challenge-form'
      );
    }).catch(() => false) : false;

    const isCfChallenge = isCfTitleChallenge || hasTurnstileWidget;

    if (!isCfChallenge) return true; // No challenge detected

    console.log('[stealth] Cloudflare challenge detected, waiting for resolution...');
    const start = Date.now();
    let clickedTurnstile = false;

    while (Date.now() - start < maxWait) {
      await new Promise(r => setTimeout(r, 1000));

      // Check 1: Turnstile token populated (BrowserOS captcha-waiter pattern)
      const hasTurnstileToken = await this.page.evaluate(() => {
        const input = document.querySelector(
          '[name="cf-turnstile-response"], input[name*="turnstile"]'
        ) as HTMLInputElement | null;
        return !!(input && input.value && input.value.length > 10);
      }).catch(() => false);

      if (hasTurnstileToken) {
        console.log(`[stealth] Turnstile token found in ${Date.now() - start}ms`);
        await this.waitForIdle(2000).catch(() => {});
        return true;
      }

      // Auto-click Turnstile checkbox if present (BrowserOS uses NopeCHA for this)
      if (!clickedTurnstile) {
        try {
          const clicked = await this.clickTurnstileCheckbox();
          if (clicked) {
            clickedTurnstile = true;
            console.log(`[stealth] Clicked Turnstile checkbox at ${Date.now() - start}ms`);
            // Give it extra time to process after click
            await new Promise(r => setTimeout(r, 5000));
            continue;
          }
        } catch { /* ignore */ }
      }

      // If click didn't work after 10s, try full captcha solver chain
      // (token injection -> AI vision -> 2captcha grid -> manual wait)
      if (clickedTurnstile && Date.now() - start > 10000 && !this.triedExternalSolver) {
        this.triedExternalSolver = true;
        try {
          const { detectCaptcha, solveCaptchaFull } = await import('./captcha.js');
          const captchaInfo = await detectCaptcha(this.page);
          if (captchaInfo) {
            const apiKey = process.env.CAPTCHA_API_KEY || process.env.TWOCAPTCHA_API_KEY || '';
            const provider = process.env.CAPTCHA_PROVIDER || '2captcha';

            // Create LLM provider for AI vision solving if API key available
            let llmProvider = null;
            const llmApiKey = process.env.OPENAI_API_KEY || process.env.ANTHROPIC_API_KEY;
            if (llmApiKey) {
              const { LLMProvider } = await import('../llm/provider.js');
              llmProvider = new LLMProvider({
                apiKey: llmApiKey,
                model: process.env.LLM_MODEL || 'gpt-4o',
              });
            }

            console.log(`[stealth] Attempting captcha solve (provider: ${provider || 'none'}, vision: ${llmProvider ? 'yes' : 'no'})...`);
            const result = await solveCaptchaFull(
              this.page,
              captchaInfo,
              { provider, apiKey, timeout: Math.max(maxWait - (Date.now() - start), 15000) },
              llmProvider,
              'auto',
            );
            if (result.solved) {
              console.log(`[stealth] Captcha solved via ${result.method} at ${Date.now() - start}ms`);
              await this.waitForIdle(2000).catch(() => {});
              return true;
            }
          }
        } catch { /* solver is best-effort */ }
      }

      // Check 2: Title changed (challenge page navigated away)
      const currentTitle = await this.page.title().catch(() => '');
      if (currentTitle
        && !currentTitle.toLowerCase().includes('just a moment')
        && !currentTitle.toLowerCase().includes('checking your browser')
        && !currentTitle.toLowerCase().includes('attention required')
        && !currentTitle.toLowerCase().includes('security verification')) {
        console.log(`[stealth] Cloudflare challenge resolved in ${Date.now() - start}ms`);
        await this.waitForIdle(1500).catch(() => {});
        return true;
      }

      // Check 3: URL changed (some CF challenges redirect without title change)
      const currentUrl = this.page.url();
      if (currentUrl && !currentUrl.includes('challenges.cloudflare.com')
        && !currentUrl.includes('/cdn-cgi/challenge-platform')) {
        const newTitle = await this.page.title().catch(() => '');
        if (newTitle && !newTitle.toLowerCase().includes('just a moment')) {
          console.log(`[stealth] Cloudflare challenge resolved via redirect in ${Date.now() - start}ms`);
          await this.waitForIdle(1500).catch(() => {});
          return true;
        }
      }
    }
    console.log('[stealth] Cloudflare challenge did not resolve within timeout');
    return false;
  }

  /**
   * Wait for PerimeterX/HUMAN Security "Press & Hold" challenge to resolve.
   *
   * PerimeterX shows a page with "Please confirm you are a human" and a
   * "Press & Hold" button (typically #px-captcha or an iframe). The button
   * requires a sustained mousedown for 4-8 seconds.
   *
   * Strategy: detect the challenge, locate the button, simulate a human
   * press-and-hold with realistic mouse movement and timing.
   */
  async waitForPerimeterX(maxWait: number = 30000): Promise<boolean> {
    // Detect PerimeterX challenge page
    const pxDetected = await this.page.evaluate(() => {
      const body = document.body?.innerText?.toLowerCase() || '';
      const hasPxText = body.includes('press & hold') || body.includes('press and hold');
      const hasPxElement = !!document.querySelector(
        '#px-captcha, [data-px-captcha], .px-captcha, '
        + 'iframe[src*="captcha.px-cdn.net"], iframe[src*="captcha.perimeterx"], '
        + '[id*="px-captcha"], [class*="px-captcha"]'
      );
      const hasHumanCheck = body.includes('confirm you are a human')
        || body.includes('are you a robot')
        || body.includes('denied');
      return (hasPxText || hasPxElement) && hasHumanCheck;
    }).catch(() => false);

    if (!pxDetected) return true; // No PX challenge

    console.log('[stealth] PerimeterX challenge detected, attempting Press & Hold...');
    const start = Date.now();
    let attempts = 0;
    const maxAttempts = 3;

    while (attempts < maxAttempts && Date.now() - start < maxWait) {
      attempts++;

      const solved = await this.pressAndHoldChallenge();
      if (solved) {
        // Wait for page to transition after successful solve
        await new Promise(r => setTimeout(r, 2000));

        // Check if challenge is gone
        const stillBlocked = await this.page.evaluate(() => {
          const body = document.body?.innerText?.toLowerCase() || '';
          return body.includes('press & hold') || body.includes('press and hold')
            || !!document.querySelector('#px-captcha');
        }).catch(() => false);

        if (!stillBlocked) {
          console.log(`[stealth] PerimeterX challenge solved in ${Date.now() - start}ms (attempt ${attempts})`);
          await this.waitForIdle(2000).catch(() => {});
          return true;
        }
      }

      // Wait before retry
      await new Promise(r => setTimeout(r, 2000 + Math.random() * 1000));
    }

    console.log('[stealth] PerimeterX challenge did not resolve within timeout');
    return false;
  }

  /**
   * Simulate a human "Press & Hold" action on the PerimeterX captcha button.
   * Returns true if the action was dispatched.
   */
  private async pressAndHoldChallenge(): Promise<boolean> {
    // Find the Press & Hold target — could be #px-captcha div or an iframe button
    const target = await this.page.evaluate(() => {
      // Direct element
      const pxCaptcha = document.querySelector(
        '#px-captcha, [data-px-captcha], .px-captcha, [id*="px-captcha"]'
      );
      if (pxCaptcha) {
        const rect = pxCaptcha.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
          return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, source: 'direct' };
        }
      }

      // Find by text content — look for "Press & Hold" button/div
      const allElements = document.querySelectorAll('button, div, span, a');
      for (const el of allElements) {
        const text = (el.textContent || '').trim();
        if (/press\s*[&+]\s*hold/i.test(text)) {
          const rect = el.getBoundingClientRect();
          if (rect.width > 20 && rect.height > 20) {
            return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, source: 'text' };
          }
        }
      }

      // Iframe fallback
      const iframe = document.querySelector(
        'iframe[src*="captcha.px-cdn.net"], iframe[src*="perimeterx"], iframe[src*="px-captcha"]'
      );
      if (iframe) {
        const rect = iframe.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
          return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, source: 'iframe' };
        }
      }

      return null;
    }).catch(() => null);

    if (!target) {
      console.log('[stealth] Could not locate Press & Hold button');
      return false;
    }

    console.log(`[stealth] Found Press & Hold target (${target.source}) at (${Math.round(target.x)}, ${Math.round(target.y)})`);

    // Human-like approach: move mouse from random position to button
    const startX = 100 + Math.random() * 300;
    const startY = target.y - 50 - Math.random() * 150;
    await this.page.mouse.move(startX, startY);
    await new Promise(r => setTimeout(r, 200 + Math.random() * 300));

    // Cubic Bezier movement to the button
    const targetX = target.x + (Math.random() - 0.5) * 6;
    const targetY = target.y + (Math.random() - 0.5) * 6;
    const steps = 12 + Math.floor(Math.random() * 8);
    const cp1x = startX + (targetX - startX) * 0.3 + (Math.random() - 0.5) * 25;
    const cp1y = startY + (targetY - startY) * 0.2 + (Math.random() - 0.5) * 15;
    const cp2x = startX + (targetX - startX) * 0.7 + (Math.random() - 0.5) * 15;
    const cp2y = startY + (targetY - startY) * 0.85 + (Math.random() - 0.5) * 10;

    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      const u = 1 - t;
      const cx = u*u*u*startX + 3*u*u*t*cp1x + 3*u*t*t*cp2x + t*t*t*targetX;
      const cy = u*u*u*startY + 3*u*u*t*cp1y + 3*u*t*t*cp2y + t*t*t*targetY;
      await this.page.mouse.move(cx, cy);
      const speed = Math.sin(t * Math.PI);
      await new Promise(r => setTimeout(r, 8 + (1 - speed) * 25 + Math.random() * 8));
    }

    // Brief hesitation before pressing (human-like)
    await new Promise(r => setTimeout(r, 100 + Math.random() * 200));

    // PRESS AND HOLD — PerimeterX requires 4-8 seconds of sustained mousedown
    const holdDuration = 5000 + Math.random() * 3000; // 5-8 seconds
    console.log(`[stealth] Pressing and holding for ${Math.round(holdDuration)}ms...`);

    await this.page.mouse.down();

    // During the hold, simulate slight micro-movements (humans aren't perfectly still)
    const holdStart = Date.now();
    while (Date.now() - holdStart < holdDuration) {
      // Tiny jitter (1-2px) every 200-500ms
      const jitterX = targetX + (Math.random() - 0.5) * 2;
      const jitterY = targetY + (Math.random() - 0.5) * 2;
      await this.page.mouse.move(jitterX, jitterY);
      await new Promise(r => setTimeout(r, 200 + Math.random() * 300));
    }

    await this.page.mouse.up();
    console.log(`[stealth] Released after ${Date.now() - holdStart}ms hold`);

    // Wait for the page to transition
    await new Promise(r => setTimeout(r, 3000));
    return true;
  }

  /**
   * Auto-dismiss cookie consent screens, GDPR popups, and privacy notices.
   *
   * Detects common consent frameworks (Google, OneTrust, CookieBot, generic GDPR)
   * and clicks the "Accept all" button. Runs at the page level so the LLM never
   * sees the consent overlay — zero iterations wasted.
   *
   * Returns true if a consent screen was detected and dismissed.
   */
  async dismissConsentScreens(timeout: number = 3000): Promise<boolean> {
    try {
      const dismissed = await this.page.evaluate(() => {
        // --- Strategy 1: Known consent button selectors ---
        const selectorCandidates = [
          // Google consent
          'button[jsname="b3VHJd"]',
          'form[action*="consent"] button',
          // OneTrust
          '#onetrust-accept-btn-handler',
          // CookieBot
          '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
          '#CybotCookiebotDialogBodyButtonAccept',
          // Quantcast
          '.qc-cmp2-summary-buttons button[mode="primary"]',
          'button[data-testid="GDPR-CTA-accept"]',
          'button[data-testid="consent-accept"]',
          // Didomi
          '#didomi-notice-agree-button',
          // TrustArc / TrustE
          '.trustarc-agree-btn', '#truste-consent-button', '#consent_prompt_submit',
          // Sourcepoint
          'button[title="Accept All"]', 'button[title="Accept all"]', '.sp_choice_type_11',
          // Usercentrics
          '#uc-btn-accept-banner', '[data-testid="uc-accept-all-button"]',
          // Osano
          '.osano-cm-accept-all', '.osano-cm-dialog__button--type_accept',
          // Termly
          '[data-tid="banner-accept"]', '.t-acceptAllButton',
          // Iubenda
          '.iubenda-cs-accept-btn',
          // Klaro
          '.cm-btn-accept-all', '.cm-btn-accept',
          // Complianz (WordPress)
          '.cmplz-accept', '#cmplz-cookiebanner-container .cmplz-btn.cmplz-accept',
          // Admiral / generic data-attribute patterns
          '[data-cy="cookie-banner-accept"]',
          '[data-action="accept-cookies"]', '[data-cookie-accept]',
          'button[data-consent="accept"]', '[data-qa="accept-cookies"]',
          '#accept-all-cookies', '#acceptAllCookies',
          '.js-accept-cookies', '.js-cookie-accept',
          // Generic class patterns
          '.cc-accept', '.cc-btn.cc-dismiss',
          '#cookie-accept', '#accept-cookies',
          'button[aria-label="Accept all"]', 'button[aria-label="Accept cookies"]',
          '.cookie-consent-accept', '.consent-accept',
        ];

        for (const selector of selectorCandidates) {
          const el = document.querySelector(selector) as HTMLElement | null;
          if (el && el.offsetParent !== null) {
            el.click();
            return true;
          }
        }

        // --- Strategy 2: Find buttons by text content ---
        const acceptPatterns = [
          /^accept\s*(all|cookies|&\s*close|and\s*continue|and\s*close)?$/i,
          /^agree\s*(and\s*(proceed|continue|close))?$/i,
          /^i\s*(agree|accept|understand)$/i,
          /^yes,?\s*i\s*(agree|accept|understand)$/i,
          /^got\s*it$/i,
          /^ok$/i,
          /^allow\s*(all)?$/i,
          /^continue$/i,
          /^confirm$/i,
          /^that'?s\s*ok$/i,
          // German
          /^alle\s*(cookies\s*)?akzeptieren$/i,
          /^einverstanden$/i,
          /^zustimmen$/i,
          // French
          /^tout\s*accepter$/i,
          /^accepter\s*(tout)?$/i,
          /^j'?accepte$/i,
          /^continuer$/i,
          // Spanish
          /^aceptar\s*(todo|todas)?$/i,
          // Portuguese
          /^aceitar\s*(tudo)?$/i,
          /^aceito$/i,
          // Italian
          /^accetta\s*tutti?$/i,
          /^accetto$/i,
          // Dutch
          /^accepteer\s*(alles|alle)?$/i,
          // Swedish
          /^godkänn\s*alla$/i,
          // Polish
          /^akceptuj\s*wszystkie$/i,
        ];

        const buttons = Array.from(document.querySelectorAll(
          'button, a[role="button"], [role="button"], input[type="button"], input[type="submit"]'
        )) as HTMLElement[];

        for (const btn of buttons) {
          const text = (btn.textContent || btn.getAttribute('value') || '').trim();
          if (text.length > 50) continue;
          for (const pattern of acceptPatterns) {
            if (pattern.test(text) && btn.offsetParent !== null) {
              btn.click();
              return true;
            }
          }
        }

        return false;
      });

      if (dismissed) {
        console.log('[consent] Auto-dismissed consent/cookie screen');
        await new Promise(r => setTimeout(r, 1500));
        return true;
      }
    } catch {
      // Best-effort
    }
    return false;
  }

  /**
   * Auto-dismiss non-consent overlays: country/locale selectors, age gates,
   * newsletter/promo popups, app install banners, notification prompts.
   *
   * Runs after dismissConsentScreens to catch overlays it doesn't handle.
   */
  async dismissOverlays(timeout: number = 3000): Promise<boolean> {
    try {
      const dismissed = await this.page.evaluate(() => {
        // --- Strategy 1: Known overlay selectors ---
        const overlayCandidates = [
          // Newsletter / promo close buttons
          '.newsletter-popup-close', '.modal-close', '.modal__close',
          '[data-dismiss="modal"]', '.popup-close', '.popup__close',
          // App install banners
          '.smartbanner-close', '.app-banner-close', '#branch-banner-close',
          // Notification prompts
          '.notification-dismiss', '#push-notification-decline',
        ];

        for (const selector of overlayCandidates) {
          const el = document.querySelector(selector) as HTMLElement | null;
          if (el && el.offsetParent !== null) {
            el.click();
            return 'selector';
          }
        }

        // --- Strategy 2: Country/locale selector + Age gate + Promo dismiss ---
        const overlayPatterns = [
          // Country/locale selectors
          /^yes,?\s*(stay|continue|shop)/i,
          /^stay\s*(on|in)\s/i,
          /^continue\s*(to|on|in)\s/i,
          /^shop\s*(in|on|from)\s/i,
          /^go\s*to\s*.*\s*(site|store|shop)/i,
          /^confirm\s*(country|location|region)/i,
          // Age gates
          /^yes,?\s*i\s*(am|'m)\s*(over|at\s*least|of\s*legal)/i,
          /^i\s*am\s*(over|at\s*least|of\s*legal)/i,
          /^enter\s*(site)?$/i,
          /^yes,?\s*enter$/i,
          /^verify\s*(my\s*)?age$/i,
          // Promo / newsletter dismissals
          /^no,?\s*thanks?$/i,
          /^not?\s*now$/i,
          /^maybe\s*later$/i,
          /^skip$/i,
          /^not?\s*interested$/i,
          /^remind\s*me\s*later$/i,
          /^close$/i,
          /^dismiss$/i,
        ];

        const buttons = Array.from(document.querySelectorAll(
          'button, a[role="button"], [role="button"], input[type="button"], input[type="submit"], a.btn, a.button'
        )) as HTMLElement[];

        for (const btn of buttons) {
          const text = (btn.textContent || btn.getAttribute('value') || '').trim();
          if (text.length > 80) continue;
          for (const pattern of overlayPatterns) {
            if (pattern.test(text) && btn.offsetParent !== null) {
              btn.click();
              return 'text';
            }
          }
        }

        // --- Strategy 3: Overlay container with close X button ---
        const overlayContainers = document.querySelectorAll(
          '[class*="overlay"], [class*="modal"], [class*="popup"], [class*="dialog"], '
          + '[id*="overlay"], [id*="modal"], [id*="popup"]'
        );
        for (const container of overlayContainers) {
          const el = container as HTMLElement;
          const style = window.getComputedStyle(el);
          if (style.display === 'none' || style.visibility === 'hidden') continue;
          const zIndex = parseInt(style.zIndex, 10);
          if (isNaN(zIndex) || zIndex < 10) continue;

          const closeBtn = el.querySelector(
            '[class*="close"], [aria-label="Close"], [aria-label="close"], '
            + 'button[class*="dismiss"], .icon-close, .close-icon'
          ) as HTMLElement | null;
          if (closeBtn && closeBtn.offsetParent !== null) {
            closeBtn.click();
            return 'close-btn';
          }
        }

        return null;
      });

      if (dismissed) {
        console.log(`[overlay] Auto-dismissed overlay via ${dismissed}`);
        await new Promise(r => setTimeout(r, 1500));
        return true;
      }
    } catch {
      // Best-effort
    }
    return false;
  }

  /**
   * Click the Cloudflare Turnstile checkbox inside its iframe.
   * Uses trusted CDP mouse events with human-like Bezier movement FIRST,
   * since Cloudflare checks Event.isTrusted and mouse trail presence.
   * Falls back to frame.evaluate only if the iframe bounding box can't be found.
   */
  private async clickTurnstileCheckbox(): Promise<boolean> {
    // PRIMARY: Trusted CDP mouse click with human-like Bezier movement.
    // Cloudflare analyzes mouse trail — a click with no movement = bot.
    // JS-dispatched clicks (frame.evaluate) lack isTrusted=true and have no mouse trail.
    const iframeBox = await this.page.evaluate(() => {
      const iframe = document.querySelector(
        'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
      );
      if (!iframe) return null;
      const rect = iframe.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return null;
      // Checkbox is in the left portion of the widget
      return {
        x: rect.left + 30,
        y: rect.top + rect.height / 2,
        width: rect.width,
        height: rect.height,
      };
    }).catch(() => null);

    if (iframeBox) {
      // Simulate human-like mouse movement: start from a random position, curve to checkbox
      const startX = 200 + Math.random() * 400;
      const startY = iframeBox.y - 100 - Math.random() * 200;
      await this.page.mouse.move(startX, startY);
      await new Promise(r => setTimeout(r, 100 + Math.random() * 200));

      // Move in steps toward the checkbox (human-like Bezier curve)
      const targetX = iframeBox.x + (Math.random() - 0.5) * 4; // slight jitter
      const targetY = iframeBox.y + (Math.random() - 0.5) * 4;
      const steps = 10 + Math.floor(Math.random() * 8);
      // Control points for cubic Bezier
      const cp1x = startX + (targetX - startX) * 0.3 + (Math.random() - 0.5) * 30;
      const cp1y = startY + (targetY - startY) * 0.1 + (Math.random() - 0.5) * 20;
      const cp2x = startX + (targetX - startX) * 0.7 + (Math.random() - 0.5) * 20;
      const cp2y = startY + (targetY - startY) * 0.9 + (Math.random() - 0.5) * 15;

      for (let i = 1; i <= steps; i++) {
        const t = i / steps;
        const u = 1 - t;
        // Cubic Bezier formula
        const cx = u*u*u*startX + 3*u*u*t*cp1x + 3*u*t*t*cp2x + t*t*t*targetX;
        const cy = u*u*u*startY + 3*u*u*t*cp1y + 3*u*t*t*cp2y + t*t*t*targetY;
        await this.page.mouse.move(cx, cy);
        // Variable speed: slower at start/end (sine easing)
        const speed = Math.sin(t * Math.PI);
        await new Promise(r => setTimeout(r, 8 + (1 - speed) * 30 + Math.random() * 10));
      }

      // Small pause before clicking (humans hesitate slightly)
      await new Promise(r => setTimeout(r, 50 + Math.random() * 150));
      // Human-like click: mousedown, hold, mouseup
      await this.page.mouse.down();
      await new Promise(r => setTimeout(r, 50 + Math.random() * 30));
      await this.page.mouse.up();
      return true;
    }

    // FALLBACK: Try frame.evaluate if iframe bounding box couldn't be determined
    // (e.g. iframe is hidden or positioned offscreen). Less reliable due to untrusted events.
    const frames = this.page.frames();
    for (const frame of frames) {
      const url = frame.url();
      if (!url.includes('challenges.cloudflare.com') && !url.includes('turnstile')) continue;

      const clicked = await frame.evaluate(() => {
        const checkbox = document.querySelector(
          'input[type="checkbox"], .ctp-checkbox-label, #challenge-stage, [role="checkbox"]'
        ) as HTMLElement | null;
        if (checkbox) {
          checkbox.click();
          return true;
        }
        return false;
      }).catch(() => false);

      if (clicked) return true;
    }

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
    return (await this.page.screenshot({
      type: 'jpeg',
      quality,
      fullPage: false,
    })) as Buffer;
  }

  async screenshotBase64(quality: number = 70): Promise<string> {
    const buffer = await this.screenshot(quality);
    return buffer.toString('base64');
  }

  // --- Element interaction ---

  /**
   * Click an element using 3-tier fallback (from BrowserOS):
   * 1. CDP Input.dispatchMouseEvent at element center coordinates
   * 2. Puppeteer page.click() with CSS selector
   * 3. JS click() via XPath evaluation
   */
  async clickElement(element: DOMElementNode, options?: {
    button?: 'left' | 'right' | 'middle';
    clickCount?: number;
  }): Promise<void> {
    const selector = element.enhancedCssSelectorForElement();

    // Tier 1: CDP mouse dispatch at computed coordinates
    try {
      const coords = await getElementCenterBySelector(this.page, selector);
      if (coords) {
        const client = await this.getCDPSession();
        await dispatchClick(client, coords.x, coords.y, {
          button: options?.button,
          clickCount: options?.clickCount,
        });
        await this.waitForIdle(1500).catch(() => {});
        return;
      }
    } catch {
      // Fallthrough
    }

    // Tier 2: Puppeteer click
    try {
      await this.page.waitForSelector(selector, { timeout: 5000 });
      await this.page.click(selector);
      await this.waitForIdle(1500).catch(() => {});
      return;
    } catch {
      // Fallthrough
    }

    // Tier 3: JS fallback via XPath
    if (element.xpath) {
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
    }
    await this.waitForIdle(1500).catch(() => {});
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
   */
  async typeText(element: DOMElementNode, text: string, clear: boolean = true): Promise<void> {
    const selector = element.enhancedCssSelectorForElement();

    try {
      // Focus the element by clicking it
      const coords = await getElementCenterBySelector(this.page, selector);
      const client = await this.getCDPSession();

      if (coords) {
        await dispatchClick(client, coords.x, coords.y);
        await new Promise((r) => setTimeout(r, 100));

        // Clear existing content (BrowserOS clearField pattern)
        if (clear) {
          await clearField(client, coords.x, coords.y);
          await new Promise((r) => setTimeout(r, 50));
        }

        // Type via CDP keyboard dispatch
        await cdpTypeText(client, text, 30);
      } else {
        // Fallback: puppeteer type
        await this.page.waitForSelector(selector, { timeout: 5000 });
        if (clear) {
          await this.page.click(selector, { clickCount: 3 });
          await this.page.keyboard.press('Backspace');
        }
        await this.page.type(selector, text, { delay: 30 });
      }
    } catch {
      // Last resort: JS value assignment
      if (element.xpath) {
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
      }
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

    const domResult = await buildDomTree(this.page);

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
