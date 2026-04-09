/**
 * Captcha handling actions for the agent.
 */

import { z } from 'zod';
import { Action } from './registry.js';
import { detectCaptcha, screenshotCaptchaArea } from '../../browser/captcha.js';

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
