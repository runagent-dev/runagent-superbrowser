/**
 * reCAPTCHA v2 solvers — checkbox auto-pass + 2captcha grid API.
 *
 * The old vision-LLM grid strategy (`recaptchaAIGridStrategy`) has been
 * removed. Vision-based solves now happen in the Python vision agent
 * (`nanobot/vision_agent/`), invoked via
 * `browser_solve_captcha(method='vision')` — one unified prompt, cheaper
 * model, shared prompt with the general page-understanding middleman.
 */

import type { CaptchaInfo } from '../../captcha.js';
import {
  clickRecaptchaCheckbox,
  detectCaptcha,
  waitForCaptchaSolution,
} from '../../captcha.js';
import type { CaptchaStrategy, RichSolveResult, StrategyContext } from '../types.js';

/** Step 1 — click the "I'm not a robot" checkbox. May auto-pass via stealth. */
export const recaptchaCheckboxStrategy: CaptchaStrategy = {
  name: 'recaptcha_checkbox',
  supportedTypes: ['recaptcha'],
  // Highest priority for reCAPTCHA — checkbox is essentially free if
  // stealth works, and it routinely auto-passes a real-looking session.
  priority: 90,
  estimatedCostCents: 0,
  requiresLLM: false,
  requiresApiKey: false,

  canHandle(info: CaptchaInfo): boolean {
    return info.type === 'recaptcha';
  },

  async run(info: CaptchaInfo, ctx: StrategyContext): Promise<RichSolveResult> {
    const start = Date.now();
    try {
      const clicked = await clickRecaptchaCheckbox(ctx.page);
      if (!clicked) {
        return {
          solved: false, method: 'recaptcha_checkbox', attempts: 1,
          error: 'checkbox not found or not clickable',
        };
      }

      // Check for auto-resolve after a brief settle window.
      const recheck = await detectCaptcha(ctx.page);
      if (!recheck) {
        return {
          solved: true,
          method: 'recaptcha_checkbox',
          subMethod: 'checkbox_autopass',
          vendorDetected: 'google',
          attempts: 1,
          durationMs: Date.now() - start,
        };
      }

      // Check if the token was populated without a challenge (low-risk user).
      const tokenFilled = await waitForCaptchaSolution(ctx.page, info, 3000, 500);
      if (tokenFilled) {
        return {
          solved: true,
          method: 'recaptcha_checkbox',
          subMethod: 'checkbox_token_fill',
          vendorDetected: 'google',
          attempts: 1,
          durationMs: Date.now() - start,
        };
      }

      return {
        solved: false, method: 'recaptcha_checkbox', attempts: 1,
        error: 'checkbox clicked but challenge still present — grid challenge likely',
      };
    } catch (e) {
      return { solved: false, method: 'recaptcha_checkbox', attempts: 1, error: (e as Error).message };
    }
  },
};

/** Grid solver via 2captcha API (works without LLM). */
export const recaptchaGridApiStrategy: CaptchaStrategy = {
  name: 'recaptcha_grid_api',
  supportedTypes: ['recaptcha', 'hcaptcha', 'image'],
  priority: 40,
  estimatedCostCents: 5,
  requiresLLM: false,
  requiresApiKey: true,

  canHandle(info: CaptchaInfo, ctx: StrategyContext): boolean {
    return ctx.config.provider === '2captcha' && Boolean(ctx.config.apiKey);
  },

  async run(info: CaptchaInfo, ctx: StrategyContext): Promise<RichSolveResult> {
    const { solveWithGridApi } = await import('../../captcha.js');
    const start = Date.now();
    try {
      const result = await solveWithGridApi(ctx.page, info, ctx.config);
      return {
        ...result,
        method: 'recaptcha_grid_api',
        subMethod: '2captcha_grid',
        durationMs: Date.now() - start,
      };
    } catch (e) {
      return { solved: false, method: 'recaptcha_grid_api', attempts: 1, error: (e as Error).message };
    }
  },
};
