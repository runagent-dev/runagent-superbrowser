/**
 * Token-based solver: 2captcha / anti-captcha.
 *
 * Cheap and reliable for reCAPTCHA / hCaptcha / Turnstile when a site key
 * is available. Posts the site key to the solver service, polls for a
 * token, and injects it into the page.
 */

import type { CaptchaInfo } from '../../captcha.js';
import { solveWithExternalApi } from '../../captcha.js';
import type { CaptchaStrategy, RichSolveResult, StrategyContext } from '../types.js';

export const tokenExternalStrategy: CaptchaStrategy = {
  name: 'token_external',
  supportedTypes: ['recaptcha', 'hcaptcha', 'turnstile'],
  // Sits below recaptcha_checkbox so the cheap auto-pass path runs first,
  // but above slider/vision/handoff: when the user has paid for 2captcha,
  // we'd rather burn 3¢ on a high-success solver than thrash vision LLMs.
  priority: 85,
  estimatedCostCents: 3,
  requiresLLM: false,
  requiresApiKey: true,

  canHandle(info: CaptchaInfo, ctx: StrategyContext): boolean {
    return Boolean(ctx.config.provider && ctx.config.apiKey && info.siteKey);
  },

  async run(info: CaptchaInfo, ctx: StrategyContext): Promise<RichSolveResult> {
    const start = Date.now();
    try {
      const solved = await solveWithExternalApi(ctx.page, info, ctx.config);
      return {
        solved,
        method: 'token_external',
        subMethod: ctx.config.provider,
        vendorDetected: info.type === 'turnstile' ? 'cloudflare'
          : info.type === 'recaptcha' ? 'google'
          : info.type === 'hcaptcha' ? 'hcaptcha'
          : undefined,
        attempts: 1,
        totalRounds: 1,
        durationMs: Date.now() - start,
        siteKey: info.siteKey,
        iframeUrl: info.iframeSrc,
      };
    } catch (e) {
      return {
        solved: false,
        method: 'token_external',
        attempts: 1,
        error: (e as Error).message,
      };
    }
  },
};
