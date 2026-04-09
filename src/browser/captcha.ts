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

/**
 * Full captcha solver orchestrator.
 * Tries methods in order: token → AI vision → 2captcha grid.
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

  // Method: auto — try all strategies
  // 1. Token method (fast, works 95% of the time)
  if (config.provider && config.apiKey && captcha.siteKey) {
    console.log('  Trying token method...');
    const tokenSolved = await solveWithExternalApi(page, captcha, config);
    if (tokenSolved) {
      return { solved: true, method: 'token', attempts: 1 };
    }
    console.log('  Token method failed or rejected');
  }

  // 2. AI vision (if LLM available)
  if (llmProvider) {
    console.log('  Trying AI vision method...');
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

  // 4. Wait for manual solution as last resort
  if (!config.provider) {
    console.log('  Waiting for manual solution...');
    const solved = await waitForCaptchaSolution(page, captcha, config.timeout || 60000);
    return { solved, method: 'manual_wait', attempts: 1 };
  }

  return { solved: false, method: 'auto', attempts: 0, error: 'All methods exhausted' };
}
