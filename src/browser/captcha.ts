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

import type { Page, Frame } from 'puppeteer-core';
import type { LLMProvider } from '../llm/provider.js';

export type CaptchaType = 'recaptcha' | 'hcaptcha' | 'turnstile' | 'image' | 'text' | 'slider' | 'visual_puzzle' | 'unknown';

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
    // --- Cloudflare detection FIRST (Cloudflare pages often embed reCAPTCHA as fallback) ---

    // Cloudflare managed challenge — check page title/body first
    const pageTitle = document.title.toLowerCase();
    const isCfPage = pageTitle.includes('just a moment')
      || pageTitle.includes('checking your browser')
      || pageTitle.includes('attention required');
    if (isCfPage) {
      return { type: 'turnstile' as const, solved: false };
    }

    // Cloudflare Turnstile widget
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

    // --- Standard CAPTCHA detection ---

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

    // Slider CAPTCHA detection (Temu, GeeTest, custom slider puzzles)
    const sliderSelectors = [
      '[class*="slider" i][class*="captcha" i]',
      '[class*="slider" i][class*="verify" i]',
      '[class*="slide-verify" i]',
      '[class*="slide_verify" i]',
      '[class*="puzzle" i][class*="slider" i]',
      '[class*="captcha-slider" i]',
      '[class*="geetest" i]',
      '[id*="slider" i][id*="captcha" i]',
      '[class*="drag" i][class*="verify" i]',
    ];
    for (const sel of sliderSelectors) {
      const el = document.querySelector(sel);
      if (el) {
        return { type: 'slider' as const, solved: false };
      }
    }

    // Visual puzzle detection (image selection, rotation, jigsaw)
    const puzzleSelectors = [
      '[class*="verify-wrap" i]',
      '[class*="verification-wrap" i]',
      '[class*="puzzle-image" i]',
      '[class*="captcha-image" i]',
      '[class*="verify-img" i]',
      '[class*="image-captcha" i]',
    ];
    for (const sel of puzzleSelectors) {
      const el = document.querySelector(sel);
      if (el) {
        return { type: 'visual_puzzle' as const, solved: false };
      }
    }

    // Generic captcha detection (image captcha, text captcha).
    // Requires BOTH a keyword hit AND a DOM anchor for an actual captcha widget —
    // keyword-only matches produce false positives on pages with legitimate copy
    // like "verify your email" or security policy footers.
    const captchaKeywords = ['captcha', 'verify you are human', 'prove you are not a robot',
      'security check', 'are you a robot', 'bot verification'];
    const bodyText = (document.body.innerText || '').toLowerCase();
    for (const keyword of captchaKeywords) {
      if (bodyText.includes(keyword)) {
        const captchaImg = document.querySelector(
          'img[src*="captcha"], img[alt*="captcha"], img[id*="captcha"]',
        );
        if (captchaImg) {
          return { type: 'image' as const, solved: false };
        }

        const imgGrid = document.querySelectorAll(
          '[class*="verify" i] img, [class*="captcha" i] img',
        );
        if (imgGrid.length >= 4) {
          return { type: 'visual_puzzle' as const, solved: false };
        }

        return null;
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

// ==========================================================================
// VISUAL CAPTCHA SOLVING — AI Vision + 2captcha Grid
// ==========================================================================

export interface ChallengeGrid {
  screenshot: Buffer;
  instruction: string;
  rows: number;
  cols: number;
  frame: Frame;
}

export interface CaptchaSolveResult {
  solved: boolean;
  method: string;
  attempts: number;
  error?: string;
}

/**
 * Click the reCAPTCHA "I'm not a robot" checkbox.
 * This must be done before the challenge grid appears.
 * Returns true if the checkbox was found and clicked, or if reCAPTCHA auto-resolved.
 */
export async function clickRecaptchaCheckbox(page: Page): Promise<boolean> {
  // Find the reCAPTCHA anchor iframe (the one with the checkbox)
  const frames = page.frames();
  for (const frame of frames) {
    const url = frame.url();
    if (url.includes('recaptcha') && (url.includes('anchor') || url.includes('api2/anchor'))) {
      try {
        // Click the checkbox inside the anchor frame
        const checkbox = await frame.$('.recaptcha-checkbox-border, #recaptcha-anchor');
        if (checkbox) {
          await checkbox.click();
          console.log('[captcha] Clicked reCAPTCHA checkbox');
          // Wait to see if it auto-resolves (good stealth = auto-pass)
          await new Promise((r) => setTimeout(r, 3000));

          // Check if the checkbox got a checkmark (solved without challenge)
          const checked = await frame.evaluate(() => {
            const anchor = document.querySelector('#recaptcha-anchor');
            return anchor?.getAttribute('aria-checked') === 'true';
          }).catch(() => false);

          if (checked) {
            console.log('[captcha] reCAPTCHA auto-resolved after checkbox click');
            return true;
          }
          // Not auto-resolved — challenge grid should now be visible
          return true;
        }
      } catch (err) {
        console.error('[captcha] Error clicking checkbox:', err);
      }
    }
  }

  // Fallback: try clicking by coordinates on the main page
  // The reCAPTCHA iframe is usually around 300x78 pixels, checkbox near left
  const recaptchaIframe = await page.$('iframe[src*="recaptcha"][src*="anchor"]');
  if (recaptchaIframe) {
    const box = await recaptchaIframe.boundingBox();
    if (box) {
      // Checkbox is at roughly (28, 28) inside the iframe
      await page.mouse.click(box.x + 28, box.y + 28);
      console.log('[captcha] Clicked reCAPTCHA checkbox via coordinates');
      await new Promise((r) => setTimeout(r, 3000));
      return true;
    }
  }

  return false;
}

/**
 * Find the reCAPTCHA challenge iframe (the popup with image grid).
 * This appears after clicking the reCAPTCHA checkbox.
 */
export async function findChallengeFrame(page: Page): Promise<Frame | null> {
  // Wait briefly for challenge to appear
  await new Promise((r) => setTimeout(r, 2000));

  const frames = page.frames();
  for (const frame of frames) {
    const url = frame.url();
    // reCAPTCHA bframe contains the challenge
    if (url.includes('recaptcha') && (url.includes('bframe') || url.includes('api2/bframe'))) {
      return frame;
    }
  }

  // Fallback: look for challenge iframe by title
  for (const frame of frames) {
    try {
      const title = await frame.title();
      if (title.toLowerCase().includes('recaptcha challenge') || title.toLowerCase().includes('recaptcha verification')) {
        return frame;
      }
    } catch {
      // Frame may be detached
    }
  }

  // hCaptcha challenge frame
  for (const frame of frames) {
    const url = frame.url();
    if (url.includes('hcaptcha.com') && url.includes('challenge')) {
      return frame;
    }
  }

  return null;
}

/**
 * Screenshot the image grid from the challenge iframe and extract metadata.
 */
export async function screenshotChallengeGrid(page: Page): Promise<ChallengeGrid | null> {
  const frame = await findChallengeFrame(page);
  if (!frame) return null;

  try {
    // Extract instruction text and grid dimensions
    const gridInfo = await frame.evaluate(() => {
      // reCAPTCHA instruction
      const instructionEl =
        document.querySelector('.rc-imageselect-desc-wrapper') ||
        document.querySelector('.rc-imageselect-instructions') ||
        document.querySelector('.rc-imageselect-desc');
      const instruction = instructionEl?.textContent?.trim() || '';

      // Determine grid size from table structure
      const table = document.querySelector('table.rc-imageselect-table, table.rc-imageselect-table-33, table.rc-imageselect-table-44');
      let rows = 3;
      let cols = 3;

      if (table) {
        const tableRows = table.querySelectorAll('tr');
        rows = tableRows.length || 3;
        if (tableRows.length > 0) {
          cols = tableRows[0].querySelectorAll('td').length || 3;
        }
      }

      // Check for 4x4 grid class
      if (document.querySelector('.rc-imageselect-table-44')) {
        rows = 4;
        cols = 4;
      }

      // Get the image grid container bounds
      const gridEl = document.querySelector('.rc-imageselect-challenge') || document.querySelector('table.rc-imageselect-table');
      let bounds = null;
      if (gridEl) {
        const r = gridEl.getBoundingClientRect();
        bounds = { x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) };
      }

      return { instruction, rows, cols, bounds };
    });

    if (!gridInfo.bounds) return null;

    // Screenshot the grid area from the challenge iframe
    // We need to screenshot from the main page since frame screenshots can be unreliable
    // Find the challenge iframe element on the main page
    const iframeRect = await page.evaluate(() => {
      const iframes = document.querySelectorAll('iframe');
      for (const iframe of iframes) {
        const src = iframe.src || '';
        if (src.includes('recaptcha') && (src.includes('bframe') || src.includes('api2/bframe'))) {
          const r = iframe.getBoundingClientRect();
          return { x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) };
        }
        // hCaptcha
        if (src.includes('hcaptcha.com') && src.includes('challenge')) {
          const r = iframe.getBoundingClientRect();
          return { x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height) };
        }
      }
      return null;
    });

    if (!iframeRect) return null;

    // Calculate the absolute position of the grid within the page
    const clipRect = {
      x: Math.max(0, iframeRect.x + gridInfo.bounds.x),
      y: Math.max(0, iframeRect.y + gridInfo.bounds.y),
      width: gridInfo.bounds.width,
      height: gridInfo.bounds.height,
    };

    const screenshot = await page.screenshot({
      type: 'jpeg',
      quality: 90,
      clip: clipRect,
    }) as Buffer;

    return {
      screenshot,
      instruction: gridInfo.instruction,
      rows: gridInfo.rows,
      cols: gridInfo.cols,
      frame,
    };
  } catch (err) {
    console.error('Failed to screenshot challenge grid:', err);
    return null;
  }
}

/**
 * Click specific tiles in the challenge grid.
 * Tile indices are 1-based (1 = top-left, rows*cols = bottom-right).
 */
export async function clickChallengeTiles(
  page: Page,
  tileIndices: number[],
  grid: ChallengeGrid,
): Promise<void> {
  const { rows, cols, frame } = grid;

  for (const tileIdx of tileIndices) {
    const zeroIdx = tileIdx - 1; // Convert to 0-based
    const row = Math.floor(zeroIdx / cols);
    const col = zeroIdx % cols;

    try {
      // Click the tile inside the challenge frame
      await frame.evaluate(
        (r: number, c: number) => {
          const table = document.querySelector('table.rc-imageselect-table, table.rc-imageselect-table-33, table.rc-imageselect-table-44');
          if (!table) return;
          const trs = table.querySelectorAll('tr');
          if (trs[r]) {
            const tds = trs[r].querySelectorAll('td');
            if (tds[c]) {
              (tds[c] as HTMLElement).click();
            }
          }
        },
        row,
        col,
      );

      // Brief pause between clicks
      await new Promise((r) => setTimeout(r, 300 + Math.random() * 200));
    } catch (err) {
      console.error(`Failed to click tile ${tileIdx}:`, err);
    }
  }

  // Wait for any dynamic tile replacements
  await new Promise((r) => setTimeout(r, 2000));
}

/**
 * Click the Verify / Next / Skip button in the challenge frame.
 */
export async function clickVerifyButton(page: Page): Promise<void> {
  const frame = await findChallengeFrame(page);
  if (!frame) return;

  try {
    await frame.evaluate(() => {
      // reCAPTCHA verify button
      const verifyBtn =
        document.querySelector('#recaptcha-verify-button') ||
        document.querySelector('.rc-button-default') ||
        document.querySelector('button[id*="verify"]');
      if (verifyBtn) {
        (verifyBtn as HTMLElement).click();
        return;
      }

      // hCaptcha verify button
      const hcaptchaBtn = document.querySelector('.button-submit');
      if (hcaptchaBtn) {
        (hcaptchaBtn as HTMLElement).click();
      }
    });
  } catch {
    // Frame may have been replaced after solving
  }

  // Wait for verification
  await new Promise((r) => setTimeout(r, 3000));
}

/**
 * Check if new images appeared after clicking tiles (reCAPTCHA dynamic mode).
 */
async function hasNewImages(frame: Frame): Promise<boolean> {
  try {
    return await frame.evaluate(() => {
      // reCAPTCHA adds 'rc-imageselect-dynamic-selected' class to tiles being replaced
      const dynamic = document.querySelectorAll('.rc-imageselect-dynamic-selected, .rc-image-tile-wrapper img[src*="payload"]');
      return dynamic.length > 0;
    });
  } catch {
    return false;
  }
}

/**
 * Solve captcha using AI vision (multimodal LLM).
 */
export async function solveWithAIVision(
  page: Page,
  captcha: CaptchaInfo,
  llmProvider: LLMProvider,
  maxRounds: number = 3,
): Promise<CaptchaSolveResult> {
  let totalAttempts = 0;

  for (let round = 0; round < maxRounds; round++) {
    totalAttempts++;
    const grid = await screenshotChallengeGrid(page);
    if (!grid) {
      return { solved: false, method: 'ai_vision', attempts: totalAttempts, error: 'Could not screenshot challenge grid' };
    }

    const b64Image = grid.screenshot.toString('base64');
    const totalTiles = grid.rows * grid.cols;

    try {
      const response = await llmProvider.chat([
        {
          role: 'user',
          content: [
            {
              type: 'image_url',
              image_url: { url: `data:image/jpeg;base64,${b64Image}` },
            },
            {
              type: 'text',
              text: `This is a ${grid.rows}x${grid.cols} CAPTCHA image grid. The instruction says: "${grid.instruction}"

Tiles are numbered 1 to ${totalTiles}, left-to-right, top-to-bottom:
${Array.from({ length: grid.rows }, (_, r) =>
  Array.from({ length: grid.cols }, (_, c) => r * grid.cols + c + 1).join(' | ')
).join('\n')}

Which tiles contain the target described in the instruction? Return ONLY a JSON array of tile numbers. Example: [1, 4, 7]
If no tiles match, return an empty array: []`,
            },
          ],
        },
      ], { temperature: 0.1 });

      // Parse the LLM response to extract tile indices
      const tiles = parseTileIndices(response.content, totalTiles);
      if (tiles.length === 0) {
        // No matching tiles — might be wrong, try clicking verify anyway
        await clickVerifyButton(page);
      } else {
        await clickChallengeTiles(page, tiles, grid);

        // Check if new images appeared (dynamic reCAPTCHA)
        if (await hasNewImages(grid.frame)) {
          // Continue to next round to handle new images
          continue;
        }

        await clickVerifyButton(page);
      }

      // Check if solved
      const solved = await waitForCaptchaSolution(page, captcha, 5000, 1000);
      if (solved) {
        return { solved: true, method: 'ai_vision', attempts: totalAttempts };
      }

      // Not solved yet — might need another round
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error(`AI vision captcha attempt ${totalAttempts} failed:`, msg);
      return { solved: false, method: 'ai_vision', attempts: totalAttempts, error: msg };
    }
  }

  return { solved: false, method: 'ai_vision', attempts: totalAttempts, error: 'Max rounds exceeded' };
}

/**
 * Parse tile indices from LLM response text.
 * Handles various formats: [1,4,7], "1, 4, 7", "tiles 1, 4, and 7", etc.
 */
function parseTileIndices(text: string, maxTile: number): number[] {
  // Try JSON array first
  const jsonMatch = text.match(/\[[\d\s,]*\]/);
  if (jsonMatch) {
    try {
      const arr = JSON.parse(jsonMatch[0]) as number[];
      return arr.filter((n) => typeof n === 'number' && n >= 1 && n <= maxTile);
    } catch {
      // Fall through to regex
    }
  }

  // Extract all numbers from the text
  const numbers = text.match(/\d+/g);
  if (numbers) {
    return numbers
      .map(Number)
      .filter((n) => n >= 1 && n <= maxTile);
  }

  return [];
}

/**
 * Solve captcha using 2captcha grid method (send screenshot, get tile indices).
 */
export async function solveWithGridApi(
  page: Page,
  captcha: CaptchaInfo,
  config: CaptchaSolverConfig,
  maxRounds: number = 3,
): Promise<CaptchaSolveResult> {
  if (!config.provider || !config.apiKey || config.provider !== '2captcha') {
    return { solved: false, method: 'grid_api', attempts: 0, error: 'Grid method requires 2captcha provider and API key' };
  }

  let totalAttempts = 0;
  const timeout = config.timeout || 60000;

  for (let round = 0; round < maxRounds; round++) {
    totalAttempts++;
    const grid = await screenshotChallengeGrid(page);
    if (!grid) {
      return { solved: false, method: 'grid_api', attempts: totalAttempts, error: 'Could not screenshot challenge grid' };
    }

    try {
      const b64Image = grid.screenshot.toString('base64');

      // Submit to 2captcha grid method
      const submitParams = new URLSearchParams({
        key: config.apiKey,
        method: 'base64',
        body: b64Image,
        textinstructions: grid.instruction,
        recaptchagrid: '1',
        recaptcharows: String(grid.rows),
        recaptchacols: String(grid.cols),
        json: '1',
      });

      const submitRes = await fetch('https://2captcha.com/in.php', {
        method: 'POST',
        body: submitParams,
      });
      const submitData = (await submitRes.json()) as { status: number; request: string };

      if (submitData.status !== 1) {
        return { solved: false, method: 'grid_api', attempts: totalAttempts, error: `2captcha submit failed: ${submitData.request}` };
      }

      const taskId = submitData.request;

      // Poll for result
      const startTime = Date.now();
      const pollInterval = config.pollInterval || 5000;

      while (Date.now() - startTime < timeout) {
        await new Promise((r) => setTimeout(r, pollInterval));

        const resultUrl = `https://2captcha.com/res.php?key=${config.apiKey}&action=get&id=${taskId}&json=1`;
        const resultRes = await fetch(resultUrl);
        const resultData = (await resultRes.json()) as { status: number; request: string };

        if (resultData.status === 1) {
          // Parse result — format: "click:3/6/8" (1-based tile indices)
          const tiles = parseGridApiResult(resultData.request, grid.rows * grid.cols);
          if (tiles.length > 0) {
            await clickChallengeTiles(page, tiles, grid);

            // Check for new images
            if (await hasNewImages(grid.frame)) {
              continue; // Next round
            }

            await clickVerifyButton(page);

            const solved = await waitForCaptchaSolution(page, captcha, 5000, 1000);
            if (solved) {
              return { solved: true, method: 'grid_api', attempts: totalAttempts };
            }
          }
          break; // Got a result but didn't solve — break out
        }

        if (resultData.request !== 'CAPCHA_NOT_READY') {
          return { solved: false, method: 'grid_api', attempts: totalAttempts, error: `2captcha error: ${resultData.request}` };
        }
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error(`Grid API captcha attempt ${totalAttempts} failed:`, msg);
      return { solved: false, method: 'grid_api', attempts: totalAttempts, error: msg };
    }
  }

  return { solved: false, method: 'grid_api', attempts: totalAttempts, error: 'Max rounds exceeded' };
}

/**
 * Parse 2captcha grid API result.
 * Formats: "click:3/6/8" or "click:3,6,8" or just "3/6/8"
 */
function parseGridApiResult(result: string, maxTile: number): number[] {
  const cleaned = result.replace(/^click:/, '');
  const parts = cleaned.split(/[\/,]/);
  return parts
    .map((p) => parseInt(p.trim(), 10))
    .filter((n) => !isNaN(n) && n >= 1 && n <= maxTile);
}

// ==========================================================================
// GENERIC VISION SOLVER — for sliders, Temu puzzles, arbitrary challenges
// ==========================================================================

interface VisionSolveStep {
  action: 'click' | 'drag' | 'type' | 'type_at' | 'key' | 'scroll' | 'wait' | 'done';
  x?: number;
  y?: number;
  startX?: number;
  startY?: number;
  endX?: number;
  endY?: number;
  text?: string;
  keys?: string[];
  duration?: number;
  direction?: string;
}

/** System prompt for the vision CAPTCHA solver, adapted from ReCAP-Agent. */
function getVisionSolverSystemPrompt(width: number, height: number): string {
  return `You are an autonomous GUI agent solving a CAPTCHA challenge. You interact with the page by outputting actions.

# Available Actions (JSON format)
- click: {"action":"click","x":N,"y":N} — Click at (x,y) coordinates
- drag: {"action":"drag","startX":N,"startY":N,"endX":N,"endY":N} — Drag from start to end
- type_at: {"action":"type_at","x":N,"y":N,"text":"..."} — Click at (x,y) then type text
- type: {"action":"type","text":"..."} — Type text into the focused element
- key: {"action":"key","keys":["Enter"]} — Press keyboard key(s)
- scroll: {"action":"scroll","direction":"down"} — Scroll the page
- wait: {"action":"wait","duration":2} — Wait N seconds
- done: {"action":"done"} — Signal that the CAPTCHA appears solved

# Rules
- The viewport is ${width}x${height} pixels. All coordinates are absolute pixels.
- Click the CENTER of elements, not edges.
- For slider puzzles: drag the handle (small element on a track) to the target position.
- For image grids (select matching images): click each correct image tile.
- For text CAPTCHAs: use type_at to click the input field and type the text you read.
- For checkboxes ("I'm not a robot"): click the checkbox.
- For Cloudflare Turnstile: click the checkbox/widget area.
- Output ONE action per step. Be precise with coordinates.

# Response Format
First reason briefly about what you see, then output exactly one action.
\`\`\`
<think>I see a slider puzzle with a handle at x=100 and a target notch at x=350.</think>
{"action":"drag","startX":100,"startY":300,"endX":350,"endY":300}
\`\`\`

Output ONLY the think block and JSON. No other text.`;
}

/**
 * Solve arbitrary visual challenges using a screenshot-to-LLM-to-action loop.
 * Inspired by ReCAP-Agent: maintains conversation history, structured prompts,
 * one action per LLM call, up to maxRounds calls.
 */
export async function solveWithVisionGeneric(
  page: Page,
  llmProvider: LLMProvider,
  maxRounds: number = 5,
): Promise<CaptchaSolveResult> {
  let totalAttempts = 0;

  // Get viewport dimensions once
  const viewport = await page.evaluate(() => ({
    width: window.innerWidth,
    height: window.innerHeight,
  }));

  // Maintain conversation history across rounds (ReCAP-Agent pattern)
  const history: { role: 'system' | 'user' | 'assistant'; content: string | Array<{type: string; text?: string; image_url?: {url: string}}>}[] = [
    { role: 'system', content: getVisionSolverSystemPrompt(viewport.width, viewport.height) },
  ];

  for (let round = 0; round < maxRounds; round++) {
    totalAttempts++;

    // Take screenshot
    const screenshot = await page.screenshot({ type: 'jpeg', quality: 85 }) as Buffer;
    const b64Image = screenshot.toString('base64');

    // Build user message with screenshot
    const userPrompt = round === 0
      ? 'Solve the CAPTCHA shown in this screenshot. Observe carefully and take the next action.'
      : 'Continue solving. Here is the current state after your last action. Take the next action.';

    history.push({
      role: 'user',
      content: [
        { type: 'image_url', image_url: { url: `data:image/jpeg;base64,${b64Image}` } },
        { type: 'text', text: userPrompt },
      ],
    });

    try {
      const response = await llmProvider.chat(
        history as any,
        { temperature: 0.1 },
      );

      // Add assistant response to history for context in next round
      history.push({ role: 'assistant', content: response.content });

      // Parse the action from the LLM response
      const jsonMatch = response.content.match(/\{[^{}]*"action"\s*:\s*"[^"]+?"[^{}]*\}/);
      if (!jsonMatch) {
        console.log(`Vision generic round ${totalAttempts}: No action JSON in response`);
        continue;
      }

      let step: VisionSolveStep;
      try {
        step = JSON.parse(jsonMatch[0]);
      } catch {
        console.log(`Vision generic round ${totalAttempts}: Failed to parse action JSON`);
        continue;
      }

      console.log(`Vision generic round ${totalAttempts}: action=${step.action}`);

      // Check if LLM signals done
      if (step.action === 'done') {
        // Verify by checking if captcha is actually gone
        await new Promise((r) => setTimeout(r, 2000));
        const stillHasCaptcha = await detectCaptcha(page);
        if (!stillHasCaptcha) {
          console.log(`Vision generic: Solved after ${totalAttempts} round(s)`);
          return { solved: true, method: 'vision_generic', attempts: totalAttempts };
        }
        console.log(`Vision generic: LLM said done but captcha still present`);
        continue;
      }

      // Execute the single action
      try {
        await executeVisionStep(page, step);
      } catch (stepErr) {
        console.error(`Vision generic: Action failed:`, stepErr);
      }

      // Brief pause after action
      await new Promise((r) => setTimeout(r, 1000 + Math.random() * 500));

      // Check if captcha cleared after this action
      const stillHasCaptcha = await detectCaptcha(page);
      if (!stillHasCaptcha) {
        const title = await page.title();
        const titleBlocked = title.toLowerCase().includes('just a moment')
          || title.toLowerCase().includes('checking your browser')
          || title.toLowerCase().includes('attention required');
        if (!titleBlocked) {
          console.log(`Vision generic: Challenge cleared after ${totalAttempts} round(s)`);
          return { solved: true, method: 'vision_generic', attempts: totalAttempts };
        }
      }
      console.log(`Vision generic: Challenge still present after round ${totalAttempts} (type: ${stillHasCaptcha?.type || 'title-based'})`);

    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error(`Vision generic round ${totalAttempts} failed:`, msg);
      // Add error context to history so LLM knows what happened
      history.push({ role: 'user', content: `Error executing your action: ${msg}. Try a different approach.` });
      if (totalAttempts >= maxRounds) {
        return { solved: false, method: 'vision_generic', attempts: totalAttempts, error: msg };
      }
    }
  }

  return { solved: false, method: 'vision_generic', attempts: totalAttempts, error: 'Max rounds exceeded' };
}

/**
 * Execute a single vision solver action on the page.
 * Supports: click, drag, type, type_at, key, scroll, wait.
 */
async function executeVisionStep(page: Page, step: VisionSolveStep): Promise<void> {
  const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

  switch (step.action) {
    case 'click':
      if (step.x !== undefined && step.y !== undefined) {
        await page.mouse.click(step.x, step.y);
        await delay(200 + Math.random() * 200);
      }
      break;

    case 'drag':
      if (step.startX !== undefined && step.startY !== undefined
          && step.endX !== undefined && step.endY !== undefined) {
        // Human-like drag with easing and jitter
        const dragSteps = 25 + Math.floor(Math.random() * 10);
        await page.mouse.move(step.startX, step.startY);
        await page.mouse.down();
        await delay(80 + Math.random() * 80);

        const dx = step.endX - step.startX;
        const dy = step.endY - step.startY;
        for (let i = 1; i <= dragSteps; i++) {
          const p = i / dragSteps;
          const eased = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
          await page.mouse.move(
            step.startX + dx * eased + (Math.random() - 0.5) * 2,
            step.startY + dy * eased + (Math.random() - 0.5) * 2,
          );
          await delay(12 + Math.random() * 12);
        }
        await page.mouse.move(step.endX, step.endY);
        await delay(50 + Math.random() * 80);
        await page.mouse.up();
        await delay(400);
      }
      break;

    case 'type_at':
      // Click at coordinates then type — for text CAPTCHAs
      if (step.x !== undefined && step.y !== undefined && step.text) {
        await page.mouse.click(step.x, step.y);
        await delay(200);
        // Clear any existing text
        await page.keyboard.down('Control');
        await page.keyboard.press('a');
        await page.keyboard.up('Control');
        await delay(50);
        await page.keyboard.type(step.text, { delay: 40 + Math.random() * 40 });
        await delay(200);
      }
      break;

    case 'type':
      if (step.text) {
        // Try to find and fill an input field first (ReCAP-Agent pattern)
        const filled = await page.evaluate((text: string) => {
          const selectors = [
            'input[type="text"]', 'input:not([type])', 'textarea',
            'input[id*="captcha"]', 'input[name*="captcha"]',
            'input[id*="answer"]', 'input[name*="answer"]',
          ];
          for (const sel of selectors) {
            const el = document.querySelector(sel) as HTMLInputElement | null;
            if (el && el.offsetParent !== null) {
              el.focus();
              el.value = text;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              return true;
            }
          }
          return false;
        }, step.text);
        if (!filled) {
          // Fallback to keyboard typing
          await page.keyboard.type(step.text, { delay: 40 + Math.random() * 40 });
        }
        await delay(200);
      }
      break;

    case 'key':
      if (step.keys && step.keys.length > 0) {
        for (const key of step.keys) {
          await page.keyboard.press(key as any);
          await delay(100);
        }
      }
      break;

    case 'scroll': {
      const deltaY = step.direction === 'up' ? -300 : 300;
      await page.evaluate((dy: number) => window.scrollBy(0, dy), deltaY);
      await delay(300);
      break;
    }

    case 'wait':
      await delay((step.duration || 2) * 1000);
      break;
  }
}

/**
 * Full captcha solver orchestrator.
 * Tries methods in order: token → AI vision → vision generic → 2captcha grid.
 */
export async function solveCaptchaFull(
  page: Page,
  captcha: CaptchaInfo,
  config: CaptchaSolverConfig,
  llmProvider?: LLMProvider | null,
  method: string = 'auto',
): Promise<CaptchaSolveResult> {
  console.log(`Solving captcha (type: ${captcha.type}, method: ${method})`);

  // Method: token only
  if (method === 'token') {
    if (config.provider && config.apiKey) {
      const solved = await solveWithExternalApi(page, captcha, config);
      return { solved, method: 'token', attempts: 1 };
    }
    return { solved: false, method: 'token', attempts: 0, error: 'No captcha provider configured' };
  }

  // Method: AI vision only
  if (method === 'ai_vision') {
    if (!llmProvider) {
      return { solved: false, method: 'ai_vision', attempts: 0, error: 'No LLM provider configured for AI vision' };
    }
    return solveWithAIVision(page, captcha, llmProvider);
  }

  // Method: grid API only
  if (method === 'grid') {
    return solveWithGridApi(page, captcha, config);
  }

  // Method: generic vision (screenshot → LLM → actions)
  if (method === 'vision_generic') {
    if (!llmProvider) {
      return { solved: false, method: 'vision_generic', attempts: 0, error: 'No LLM provider configured' };
    }
    return solveWithVisionGeneric(page, llmProvider);
  }

  // Method: auto — delegate to the strategy registry (Phase 3.1).
  // The registry picks per-type specialized solvers (turnstile_explicit,
  // slider_drag_feedback, recaptcha_checkbox, etc.) in priority+cost order
  // and returns a RichSolveResult with method/subMethod/trace/timing so
  // callers can learn which approach worked per-domain.
  if (method === 'auto') {
    const { solveCaptchaViaRegistry } = await import('./captcha/orchestrator.js');
    const rich = await solveCaptchaViaRegistry(page, captcha, config, llmProvider);
    if (rich.solved || rich.method !== 'all_strategies_failed') {
      return rich;
    }
    // Only fall through if the registry produced no candidate at all —
    // preserve the legacy waterfall for unknown captcha types.
  }

  // Legacy auto waterfall (fallback when the registry has no candidate).
  // 1. Token method (fast, works 95% of the time)
  if (config.provider && config.apiKey && captcha.siteKey) {
    console.log('  Trying token method...');
    const tokenSolved = await solveWithExternalApi(page, captcha, config);
    if (tokenSolved) {
      return { solved: true, method: 'token', attempts: 1 };
    }
    console.log('  Token method failed or rejected');
  }

  // 1.5. For reCAPTCHA: click the checkbox first (required before grid appears)
  if (captcha.type === 'recaptcha') {
    console.log('  Clicking reCAPTCHA checkbox...');
    const clicked = await clickRecaptchaCheckbox(page);
    if (clicked) {
      // Re-check if captcha is still present (might have auto-resolved)
      const recheckCaptcha = await detectCaptcha(page);
      if (!recheckCaptcha) {
        console.log('  reCAPTCHA auto-resolved after checkbox click');
        return { solved: true, method: 'checkbox_autopass', attempts: 1 };
      }
      // Check if the token was populated (solved via checkbox)
      const tokenFilled = await waitForCaptchaSolution(page, captcha, 3000, 500);
      if (tokenFilled) {
        console.log('  reCAPTCHA solved via checkbox click');
        return { solved: true, method: 'checkbox', attempts: 1 };
      }
    }
  }

  // 2. AI vision for grid-based CAPTCHAs (reCAPTCHA, hCaptcha image grids)
  if (llmProvider && (captcha.type === 'recaptcha' || captcha.type === 'hcaptcha' || captcha.type === 'image')) {
    console.log('  Trying AI vision method (grid)...');
    const aiResult = await solveWithAIVision(page, captcha, llmProvider);
    if (aiResult.solved) return aiResult;
    console.log(`  AI vision failed: ${aiResult.error}`);
  }

  // 3. 2captcha grid method
  if (config.provider === '2captcha' && config.apiKey) {
    console.log('  Trying 2captcha grid method...');
    const gridResult = await solveWithGridApi(page, captcha, config);
    if (gridResult.solved) return gridResult;
    console.log(`  Grid method failed: ${gridResult.error}`);
  }

  // 4. Generic vision solver for sliders, visual puzzles, and unknown types
  if (llmProvider) {
    console.log('  Trying generic vision solver...');
    const genericResult = await solveWithVisionGeneric(page, llmProvider);
    if (genericResult.solved) {
      console.log(`  Generic vision solved in ${genericResult.attempts} attempt(s)`);
      return genericResult;
    }
    console.log(`  Generic vision failed: ${genericResult.error}`);
  }

  // 5. Wait for manual solution as last resort
  if (!config.provider) {
    console.log('  Waiting for manual solution...');
    const solved = await waitForCaptchaSolution(page, captcha, config.timeout || 60000);
    return { solved, method: 'manual_wait', attempts: 1 };
  }

  return { solved: false, method: 'auto', attempts: 0, error: 'All methods exhausted' };
}
