/**
 * reCAPTCHA v2 solver: checkbox click → auto-resolve detection → AI vision grid.
 *
 * Splits the old monolithic recaptcha flow into two steps so the orchestrator
 * can measure which sub-method worked (checkbox auto-pass is free; AI grid
 * costs ~0.3c per round).
 */

import type { CaptchaInfo } from '../../captcha.js';
import {
  clickRecaptchaCheckbox,
  detectCaptcha,
  solveWithAIVision,
  waitForCaptchaSolution,
} from '../../captcha.js';
import type { CaptchaStrategy, RichSolveResult, StrategyContext } from '../types.js';

/** Step 1 — click the "I'm not a robot" checkbox. May auto-pass via stealth. */
export const recaptchaCheckboxStrategy: CaptchaStrategy = {
  name: 'recaptcha_checkbox',
  supportedTypes: ['recaptcha'],
  // Higher priority than vision — checkbox is essentially free if stealth works.
  priority: 85,
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

/** Step 2 — solve the image-tile grid challenge with vision LLM. */
export const recaptchaAIGridStrategy: CaptchaStrategy = {
  name: 'recaptcha_ai_grid',
  supportedTypes: ['recaptcha', 'hcaptcha', 'image'],
  priority: 60,
  estimatedCostCents: 3, // ~3c/round for vision LLM on a grid
  requiresLLM: true,
  requiresApiKey: false,

  canHandle(info: CaptchaInfo): boolean {
    return info.type === 'recaptcha' || info.type === 'hcaptcha' || info.type === 'image';
  },

  async run(info: CaptchaInfo, ctx: StrategyContext): Promise<RichSolveResult> {
    if (!ctx.llm) {
      return { solved: false, method: 'recaptcha_ai_grid', attempts: 0, error: 'no LLM' };
    }
    const start = Date.now();
    try {
      const result = await solveWithAIVision(ctx.page, info, ctx.llm);
      return {
        ...result,
        method: 'recaptcha_ai_grid',
        subMethod: `grid_rounds=${result.attempts || 1}`,
        vendorDetected: info.type === 'hcaptcha' ? 'hcaptcha' : 'google',
        durationMs: Date.now() - start,
      };
    } catch (e) {
      return { solved: false, method: 'recaptcha_ai_grid', attempts: 1, error: (e as Error).message };
    }
  },
};

/** Grid solver via 2captcha API (works without LLM). */
export const recaptchaGridApiStrategy: CaptchaStrategy = {
  name: 'recaptcha_grid_api',
  supportedTypes: ['recaptcha', 'hcaptcha', 'image'],
  priority: 50,
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
