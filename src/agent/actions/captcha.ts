/**
 * Captcha handling actions for the agent.
 */

import { z } from 'zod';
import { Action } from './registry.js';
import { detectCaptcha, screenshotCaptchaArea, solveCaptchaFull } from '../../browser/captcha.js';
import { LLMProvider } from '../../llm/provider.js';

/** Detect if there's a captcha on the current page. */
export const detectCaptchaAction = new Action({
  name: 'detect_captcha',
  description: 'Check if the current page has a captcha (reCAPTCHA, hCaptcha, Cloudflare Turnstile, or image/text captcha)',
  schema: z.object({}),
  handler: async (_input, page) => {
    const rawPage = page.getRawPage();
    const captcha = await detectCaptcha(rawPage);

    if (!captcha) {
      return {
        success: true,
        extractedContent: 'No captcha detected on this page',
      };
    }

    return {
      success: true,
      extractedContent: `Captcha detected: ${captcha.type}${captcha.siteKey ? ` (siteKey: ${captcha.siteKey})` : ''}`,
      includeInMemory: true,
    };
  },
});

/** Screenshot just the captcha area for analysis. */
export const screenshotCaptchaAction = new Action({
  name: 'screenshot_captcha',
  description: 'Take a close-up screenshot of the captcha area for solving. Returns the captcha image.',
  schema: z.object({}),
  handler: async (_input, page) => {
    const rawPage = page.getRawPage();
    const result = await screenshotCaptchaArea(rawPage);

    if (!result) {
      return {
        success: false,
        error: 'Could not find captcha area to screenshot',
      };
    }

    return {
      success: true,
      extractedContent: `Captcha screenshot captured (${result.screenshot.length} bytes). Analyze the image to solve.`,
      includeInMemory: true,
    };
  },
});

/** Solve captcha with all available methods (token, AI vision, 2captcha grid). */
export const solveCaptchaVisualAction = new Action({
  name: 'solve_captcha_visual',
  description:
    'Solve a captcha automatically using multiple strategies: token injection, AI vision (analyzes image grid tiles), or 2captcha grid API. Set method to "auto" to try all.',
  schema: z.object({
    method: z
      .enum(['auto', 'token', 'ai_vision', 'grid'])
      .default('auto')
      .describe('Solving method: auto (try all), token, ai_vision, grid'),
  }),
  handler: async (input, page) => {
    const { method } = input as { method: string };
    const rawPage = page.getRawPage();
    const captcha = await detectCaptcha(rawPage);

    if (!captcha) {
      return { success: true, extractedContent: 'No captcha detected on this page' };
    }

    const config = {
      provider: process.env.CAPTCHA_PROVIDER,
      apiKey: process.env.CAPTCHA_API_KEY,
      timeout: 60000,
    };

    // Create LLM provider for AI vision if keys are available
    let llm: LLMProvider | null = null;
    const apiKey = process.env.ANTHROPIC_API_KEY || process.env.OPENAI_API_KEY;
    if (apiKey) {
      llm = new LLMProvider({
        apiKey,
        model: process.env.LLM_MODEL || 'gpt-4o',
      });
    }

    const result = await solveCaptchaFull(rawPage, captcha, config, llm, method);

    if (result.solved) {
      return {
        success: true,
        extractedContent: `Captcha solved via ${result.method} method (${result.attempts} attempt(s))`,
        includeInMemory: true,
      };
    }

    return {
      success: false,
      error: `Captcha not solved: ${result.error || 'all methods failed'}. Use ask_human for manual solving.`,
    };
  },
});
