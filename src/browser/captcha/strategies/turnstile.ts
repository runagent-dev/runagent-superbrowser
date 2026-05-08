/**
 * Cloudflare Turnstile solver with a 4-phase priority ladder.
 *
 * Most Turnstile widgets auto-pass when the browser's fingerprint looks
 * human. So we burn zero LLM/2captcha budget on the probe phase — if that
 * fails, we escalate to interactive click, then token injection via
 * 2captcha, then form auto-submit.
 *
 * Phases:
 *   1. PROBE (0-8s):  poll for `[name="cf-turnstile-response"]` to populate.
 *                     Success = fingerprint is good enough.
 *   2. CLICK (8-15s): locate and humanClick the Turnstile checkbox if visible.
 *   3. TOKEN (15s+):  POST siteKey to 2captcha Turnstile endpoint, inject.
 *   4. SUBMIT:        watch for nav/URL change; if none in 5s, requestSubmit()
 *                     the closest enclosing form.
 */

import type { Page } from 'puppeteer-core';
import type { CaptchaInfo } from '../../captcha.js';
import { solveWithExternalApi } from '../../captcha.js';
import { humanClick } from '../../humanize.js';
import type { CaptchaStrategy, RichSolveResult, StrategyContext } from '../types.js';

const PROBE_TIMEOUT_MS = 8000;
const INTERACTIVE_TIMEOUT_MS = 7000;
const SUBMIT_WATCH_MS = 5000;

async function probeToken(page: Page, timeoutMs: number): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const token = await page.evaluate(() => {
      const el = document.querySelector<HTMLInputElement>('[name="cf-turnstile-response"]');
      return el?.value || null;
    });
    if (token && token.length > 20) return true;
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

/** Find the Turnstile checkbox position across iframes. Returns viewport coords or null. */
async function findCheckbox(page: Page): Promise<{ x: number; y: number } | null> {
  // First try: main-frame element (some Turnstile builds expose the iframe container directly)
  const mainBox = await page.evaluate(() => {
    const selectors = [
      '.cf-turnstile',
      '[data-sitekey]',
      'iframe[src*="challenges.cloudflare.com"]',
      'iframe[src*="turnstile"]',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel) as HTMLElement | null;
      if (el) {
        const r = el.getBoundingClientRect();
        if (r.width > 20 && r.height > 20) {
          // Turnstile checkbox lives in the left portion of the widget (~28px in).
          return { x: r.left + 28, y: r.top + r.height / 2 };
        }
      }
    }
    return null;
  });
  if (mainBox) return mainBox;

  // Fallback: walk child frames to locate the checkbox inside the Turnstile iframe.
  for (const frame of page.frames()) {
    const url = frame.url();
    if (!url.includes('challenges.cloudflare.com') && !url.includes('turnstile')) continue;
    try {
      const inner = await frame.evaluate(() => {
        const el = document.querySelector('input[type="checkbox"]') as HTMLElement | null;
        return el ? el.getBoundingClientRect() : null;
      });
      if (inner) {
        // We have frame-local coords; need viewport offset. Use frame element.
        const frameEl = await frame.frameElement?.();
        if (!frameEl) continue;
        const frameBox = await frameEl.evaluate((el: Element) => {
          const r = (el as HTMLElement).getBoundingClientRect();
          return { x: r.left, y: r.top };
        });
        return {
          x: Math.round(frameBox.x + inner.left + inner.width / 2),
          y: Math.round(frameBox.y + inner.top + inner.height / 2),
        };
      }
    } catch {
      // Cross-origin frames block evaluate — skip.
    }
  }
  return null;
}

/** Watch for navigation or token population within watchMs. */
async function watchForSuccess(page: Page, watchMs: number): Promise<'nav' | 'token' | 'none'> {
  const start = Date.now();
  while (Date.now() - start < watchMs) {
    const tokenFilled = await page.evaluate(() => {
      const el = document.querySelector<HTMLInputElement>('[name="cf-turnstile-response"]');
      return Boolean(el?.value && el.value.length > 20);
    }).catch(() => false);
    if (tokenFilled) return 'token';
    await new Promise((r) => setTimeout(r, 300));
  }
  return 'none';
}

/** After token injection, submit the form holding the Turnstile widget. */
async function autoSubmitForm(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const field = document.querySelector('[name="cf-turnstile-response"]') as HTMLElement | null;
    if (!field) return false;
    const form = field.closest('form') as HTMLFormElement | null;
    if (!form) return false;
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.submit();
    return true;
  }).catch(() => false);
}

export const turnstileStrategy: CaptchaStrategy = {
  name: 'turnstile_explicit',
  supportedTypes: ['turnstile'],
  // Runs first for Turnstile — probe phase is free and often sufficient.
  priority: 95,
  estimatedCostCents: 0,
  requiresLLM: false,
  requiresApiKey: false, // uses external API only in phase 3

  canHandle(info: CaptchaInfo): boolean {
    return info.type === 'turnstile';
  },

  async run(info: CaptchaInfo, ctx: StrategyContext): Promise<RichSolveResult> {
    const start = Date.now();
    const trace: NonNullable<RichSolveResult['visionTrace']> = [];

    // Phase 1: probe — does the token populate on its own?
    if (await probeToken(ctx.page, PROBE_TIMEOUT_MS)) {
      return {
        solved: true,
        method: 'turnstile_explicit',
        subMethod: 'fingerprint_autopass',
        vendorDetected: 'cloudflare',
        attempts: 1,
        durationMs: Date.now() - start,
        siteKey: info.siteKey,
        visionTrace: trace,
      };
    }
    trace.push({ round: 1, action: { phase: 'probe', result: 'no_token' } });

    // Phase 2: interactive click on the checkbox
    const box = await findCheckbox(ctx.page);
    if (box) {
      try {
        const client = await ctx.page.createCDPSession();
        await humanClick(client, box.x, box.y);
        trace.push({ round: 2, action: { phase: 'click', at: box } });
        if (await probeToken(ctx.page, INTERACTIVE_TIMEOUT_MS)) {
          return {
            solved: true,
            method: 'turnstile_explicit',
            subMethod: 'interactive_click',
            vendorDetected: 'cloudflare',
            attempts: 2,
            durationMs: Date.now() - start,
            siteKey: info.siteKey,
            visionTrace: trace,
          };
        }
      } catch (e) {
        trace.push({ round: 2, action: { phase: 'click', error: (e as Error).message } });
      }
    }

    // Phase 3: token solver via 2captcha (only if configured)
    if (ctx.config.provider && ctx.config.apiKey && info.siteKey) {
      try {
        const solved = await solveWithExternalApi(ctx.page, info, ctx.config);
        trace.push({ round: 3, action: { phase: 'token_external', solved } });
        if (solved) {
          // Phase 4: auto-submit if no navigation within SUBMIT_WATCH_MS
          const outcome = await watchForSuccess(ctx.page, SUBMIT_WATCH_MS);
          if (outcome === 'none') {
            await autoSubmitForm(ctx.page);
            trace.push({ round: 4, action: { phase: 'auto_submit' } });
          }
          return {
            solved: true,
            method: 'turnstile_explicit',
            subMethod: 'token_external',
            vendorDetected: 'cloudflare',
            attempts: 3,
            durationMs: Date.now() - start,
            siteKey: info.siteKey,
            visionTrace: trace,
          };
        }
      } catch (e) {
        trace.push({ round: 3, action: { phase: 'token_external', error: (e as Error).message } });
      }
    }

    return {
      solved: false,
      method: 'turnstile_explicit',
      subMethod: 'all_phases_exhausted',
      attempts: 3,
      durationMs: Date.now() - start,
      error: 'turnstile: probe, click, and token phases all failed',
      visionTrace: trace,
    };
  },
};
