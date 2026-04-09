/**
 * Captcha detection and solving.
 *
 * Detects reCAPTCHA, hCaptcha, and Cloudflare Turnstile on pages.
 * Solving strategies:
 *   1. Vision (LLM) — screenshot the captcha, ask AI to solve
 *   2. External API — 2captcha, anti-captcha, NopeCHA
 *   3. User delegation — ask user to solve manually
 *
 * Pattern from BrowserOS captcha-waiter.ts.
 */

import type { Page } from 'puppeteer-core';

export type CaptchaType = 'recaptcha' | 'hcaptcha' | 'turnstile' | 'image' | 'text' | 'unknown';

export interface CaptchaInfo {
  type: CaptchaType;
  siteKey?: string;
  iframeSrc?: string;
  solved: boolean;
}

export interface CaptchaSolverConfig {
  /** External solving service: '2captcha' | 'anticaptcha' | 'nopecha' */
  provider?: string;
  /** API key for the external solver. */
  apiKey?: string;
  /** Max time to wait for solution (ms). Default: 60000. */
  timeout?: number;
  /** Poll interval when waiting for solution (ms). Default: 2000. */
  pollInterval?: number;
}

/**
 * Detect captchas on the current page.
 * Checks for reCAPTCHA, hCaptcha, Cloudflare Turnstile, and generic image/text captchas.
 */
export async function detectCaptcha(page: Page): Promise<CaptchaInfo | null> {
  return page.evaluate(() => {
    // reCAPTCHA v2 (checkbox or invisible)
    const recaptchaIframe = document.querySelector(
      'iframe[src*="recaptcha"], iframe[src*="google.com/recaptcha"]',
    ) as HTMLIFrameElement | null;
    if (recaptchaIframe) {
      const src = recaptchaIframe.src;
      const keyMatch = src.match(/[?&]k=([^&]+)/);
      return {
        type: 'recaptcha' as const,
        siteKey: keyMatch?.[1],
        iframeSrc: src,
        solved: false,
      };
    }

    // reCAPTCHA v2 via div
    const recaptchaDiv = document.querySelector('.g-recaptcha, [data-sitekey]') as HTMLElement | null;
    if (recaptchaDiv) {
      return {
        type: 'recaptcha' as const,
        siteKey: recaptchaDiv.getAttribute('data-sitekey') || undefined,
        solved: false,
      };
    }

    // hCaptcha
    const hcaptchaIframe = document.querySelector(
      'iframe[src*="hcaptcha.com"], iframe[data-hcaptcha-widget-id]',
    ) as HTMLIFrameElement | null;
    if (hcaptchaIframe) {
      return {
        type: 'hcaptcha' as const,
        iframeSrc: hcaptchaIframe.src,
        solved: false,
      };
    }

    const hcaptchaDiv = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]') as HTMLElement | null;
    if (hcaptchaDiv) {
      return {
        type: 'hcaptcha' as const,
        siteKey: hcaptchaDiv.getAttribute('data-hcaptcha-sitekey') || hcaptchaDiv.getAttribute('data-sitekey') || undefined,
        solved: false,
      };
    }

    // Cloudflare Turnstile
    const turnstileIframe = document.querySelector(
      'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]',
    ) as HTMLIFrameElement | null;
    if (turnstileIframe) {
      return {
        type: 'turnstile' as const,
        iframeSrc: turnstileIframe.src,
        solved: false,
      };
    }

    const turnstileDiv = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]') as HTMLElement | null;
    if (turnstileDiv) {
      return {
        type: 'turnstile' as const,
        siteKey: turnstileDiv.getAttribute('data-turnstile-sitekey') || turnstileDiv.getAttribute('data-sitekey') || undefined,
        solved: false,
      };
    }

    // Generic captcha detection (image captcha, text captcha)
    const captchaKeywords = ['captcha', 'verify you are human', 'prove you are not a robot',
      'security check', 'are you a robot', 'bot verification'];
    const bodyText = (document.body.innerText || '').toLowerCase();
    for (const keyword of captchaKeywords) {
      if (bodyText.includes(keyword)) {
        const captchaImg = document.querySelector(
          'img[src*="captcha"], img[alt*="captcha"], img[id*="captcha"]',
        );
        return {
          type: captchaImg ? 'image' as const : 'text' as const,
          solved: false,
        };
      }
    }

    return null;
  });
}

/**
 * Wait for a captcha to be solved (e.g., by external service or user).
 * Polls the page for solution tokens.
 * Pattern from BrowserOS captcha-waiter.ts.
 */
export async function waitForCaptchaSolution(
  page: Page,
  captcha: CaptchaInfo,
  timeout: number = 60000,
  pollInterval: number = 2000,
): Promise<boolean> {
  const startTime = Date.now();

  while (Date.now() - startTime < timeout) {
    const solved = await page.evaluate((type: string) => {
      if (type === 'recaptcha') {
        // Check for g-recaptcha-response textarea with value
        const textarea = document.querySelector('#g-recaptcha-response, [name="g-recaptcha-response"]') as HTMLTextAreaElement | null;
        return !!(textarea && textarea.value && textarea.value.length > 10);
      }

      if (type === 'hcaptcha') {
        const textarea = document.querySelector('[name="h-captcha-response"], [name="g-recaptcha-response"]') as HTMLTextAreaElement | null;
        return !!(textarea && textarea.value && textarea.value.length > 10);
      }

      if (type === 'turnstile') {
        const input = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile"]') as HTMLInputElement | null;
        return !!(input && input.value && input.value.length > 10);
      }

      return false;
    }, captcha.type);

    if (solved) {
      captcha.solved = true;
      return true;
    }

    await new Promise((r) => setTimeout(r, pollInterval));
  }

  return false;
}

/**
 * Solve a captcha using an external API service.
 * Supports 2captcha and anti-captcha.
 */
export async function solveWithExternalApi(
  page: Page,
  captcha: CaptchaInfo,
  config: CaptchaSolverConfig,
): Promise<boolean> {
  if (!config.provider || !config.apiKey) return false;

  const pageUrl = page.url();
  const timeout = config.timeout || 60000;

  try {
    if (config.provider === '2captcha' && captcha.siteKey) {
      return await solve2Captcha(page, captcha, config.apiKey, pageUrl, timeout);
    }
    if (config.provider === 'anticaptcha' && captcha.siteKey) {
      return await solveAntiCaptcha(page, captcha, config.apiKey, pageUrl, timeout);
    }
  } catch (err) {
    console.error(`Captcha solver error (${config.provider}):`, err);
  }

  return false;
}

/**
 * Solve via 2captcha API.
 */
async function solve2Captcha(
  page: Page,
  captcha: CaptchaInfo,
  apiKey: string,
  pageUrl: string,
  timeout: number,
): Promise<boolean> {
  const methodMap: Record<string, string> = {
    recaptcha: 'userrecaptcha',
    hcaptcha: 'hcaptcha',
    turnstile: 'turnstile',
  };
  const method = methodMap[captcha.type];
  if (!method) return false;

  // Submit task
  const submitUrl = `https://2captcha.com/in.php?key=${apiKey}&method=${method}&googlekey=${captcha.siteKey}&pageurl=${encodeURIComponent(pageUrl)}&json=1`;
  const submitRes = await fetch(submitUrl);
  const submitData = (await submitRes.json()) as { status: number; request: string };

  if (submitData.status !== 1) return false;
  const taskId = submitData.request;

  // Poll for result
  const startTime = Date.now();
  const pollInterval = 5000;

  while (Date.now() - startTime < timeout) {
    await new Promise((r) => setTimeout(r, pollInterval));

    const resultUrl = `https://2captcha.com/res.php?key=${apiKey}&action=get&id=${taskId}&json=1`;
    const resultRes = await fetch(resultUrl);
    const resultData = (await resultRes.json()) as { status: number; request: string };

    if (resultData.status === 1) {
      // Inject solution token
      await injectCaptchaToken(page, captcha.type, resultData.request);
      captcha.solved = true;
      return true;
    }

    if (resultData.request !== 'CAPCHA_NOT_READY') {
      return false; // Error
    }
  }

  return false;
}

/**
 * Solve via anti-captcha API.
 */
async function solveAntiCaptcha(
  page: Page,
  captcha: CaptchaInfo,
  apiKey: string,
  pageUrl: string,
  timeout: number,
): Promise<boolean> {
  const typeMap: Record<string, string> = {
    recaptcha: 'RecaptchaV2TaskProxyless',
    hcaptcha: 'HCaptchaTaskProxyless',
    turnstile: 'TurnstileTaskProxyless',
  };
  const taskType = typeMap[captcha.type];
  if (!taskType) return false;

  // Create task
  const createRes = await fetch('https://api.anti-captcha.com/createTask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      clientKey: apiKey,
      task: {
        type: taskType,
        websiteURL: pageUrl,
        websiteKey: captcha.siteKey,
      },
    }),
  });
  const createData = (await createRes.json()) as { errorId: number; taskId: number };

  if (createData.errorId !== 0) return false;

  // Poll for result
  const startTime = Date.now();
  const pollInterval = 5000;

  while (Date.now() - startTime < timeout) {
    await new Promise((r) => setTimeout(r, pollInterval));

    const resultRes = await fetch('https://api.anti-captcha.com/getTaskResult', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clientKey: apiKey, taskId: createData.taskId }),
    });
    const resultData = (await resultRes.json()) as {
      status: string;
      solution?: { gRecaptchaResponse?: string; token?: string };
    };

    if (resultData.status === 'ready' && resultData.solution) {
      const token = resultData.solution.gRecaptchaResponse || resultData.solution.token || '';
      if (token) {
        await injectCaptchaToken(page, captcha.type, token);
        captcha.solved = true;
        return true;
      }
    }

    if (resultData.status !== 'processing') {
      return false; // Error
    }
  }

  return false;
}

/**
 * Inject a solved captcha token into the page.
 */
async function injectCaptchaToken(
  page: Page,
  type: string,
  token: string,
): Promise<void> {
  await page.evaluate(
    (captchaType: string, captchaToken: string) => {
      if (captchaType === 'recaptcha') {
        const textarea = document.querySelector('#g-recaptcha-response, [name="g-recaptcha-response"]') as HTMLTextAreaElement | null;
        if (textarea) {
          textarea.style.display = 'block';
          textarea.value = captchaToken;
        }
        // Call callback if available
        if (typeof (window as any).___grecaptcha_cfg !== 'undefined') {
          try {
            const clients = (window as any).___grecaptcha_cfg.clients;
            for (const key of Object.keys(clients || {})) {
              const client = clients[key];
              const callback = client?.rpcServer?.callbacks?.c?.callback;
              if (typeof callback === 'function') {
                callback(captchaToken);
              }
            }
          } catch { /* ignore */ }
        }
      }

      if (captchaType === 'hcaptcha') {
        const textarea = document.querySelector('[name="h-captcha-response"], [name="g-recaptcha-response"]') as HTMLTextAreaElement | null;
        if (textarea) {
          textarea.value = captchaToken;
        }
      }

      if (captchaType === 'turnstile') {
        const input = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile"]') as HTMLInputElement | null;
        if (input) {
          input.value = captchaToken;
        }
      }
    },
    type,
    token,
  );
}

/**
 * Screenshot just the captcha area for vision-based solving.
 */
export async function screenshotCaptchaArea(
  page: Page,
): Promise<{ screenshot: Buffer; description: string } | null> {
  const rect = await page.evaluate(() => {
    // Find captcha container
    const selectors = [
      'iframe[src*="recaptcha"]',
      'iframe[src*="hcaptcha"]',
      'iframe[src*="challenges.cloudflare.com"]',
      '.g-recaptcha',
      '.h-captcha',
      '.cf-turnstile',
      '[id*="captcha"]',
      '[class*="captcha"]',
    ];

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
          return {
            x: Math.max(0, Math.round(r.left - 20)),
            y: Math.max(0, Math.round(r.top - 20)),
            width: Math.round(r.width + 40),
            height: Math.round(r.height + 40),
          };
        }
      }
    }

    return null;
  });

  if (!rect) return null;

  const screenshot = await page.screenshot({
    type: 'jpeg',
    quality: 90,
    clip: rect,
  }) as Buffer;

  return { screenshot, description: 'Captcha area screenshot for solving' };
}
