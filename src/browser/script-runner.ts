/**
 * Puppeteer script execution engine.
 *
 * Runs user-provided code with full access to the Puppeteer Page API
 * (page.goto, page.click, page.type, page.waitForSelector, etc.)
 * rather than just browser-context DOM JavaScript (document.querySelector).
 *
 * Uses the AsyncFunction constructor to create a function from code strings,
 * passing the Page object as a parameter.
 */

import type { Page } from 'puppeteer-core';

export interface ScriptResult {
  success: boolean;
  result?: unknown;
  error?: string;
  logs: string[];
  duration: number;
}

export interface ScriptContext {
  [key: string]: unknown;
}

export interface ScriptHelpers {
  sleep: (ms: number) => Promise<void>;
  log: (...args: unknown[]) => void;
  screenshot: (path?: string) => Promise<string>;
}

const DEFAULT_TIMEOUT = 60_000;
const MAX_TIMEOUT = 300_000;

// eslint-disable-next-line @typescript-eslint/no-empty-function
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor as new (
  ...args: string[]
) => (...args: unknown[]) => Promise<unknown>;

/**
 * Extract the function body from various code formats:
 * - Raw function body (most common from agents)
 * - `async ({ page, context }) => { ... }`
 * - `async function({ page, context }) { ... }`
 * - `export default async function({ page }) { ... }`
 */
function extractFunctionBody(code: string): string {
  const trimmed = code.trim();

  // export default async function ...
  const exportMatch = trimmed.match(
    /^export\s+default\s+async\s+function\s*\([^)]*\)\s*\{([\s\S]*)\}\s*;?\s*$/,
  );
  if (exportMatch) return exportMatch[1];

  // async ({ page, context, helpers }) => { ... }
  const arrowMatch = trimmed.match(
    /^async\s*\([^)]*\)\s*=>\s*\{([\s\S]*)\}\s*;?\s*$/,
  );
  if (arrowMatch) return arrowMatch[1];

  // async function({ page }) { ... }  or  async function run({ page }) { ... }
  const funcMatch = trimmed.match(
    /^async\s+function\s*\w*\s*\([^)]*\)\s*\{([\s\S]*)\}\s*;?\s*$/,
  );
  if (funcMatch) return funcMatch[1];

  // Raw function body — use as-is
  return trimmed;
}

/**
 * Run a Puppeteer script with full Page API access.
 *
 * The code receives three parameters:
 *   - `page`    — Puppeteer Page object (goto, click, type, waitForSelector, screenshot, etc.)
 *   - `context` — optional data object passed by the caller
 *   - `helpers` — convenience utilities (sleep, log, screenshot)
 *
 * Example code:
 * ```js
 * await page.goto('https://example.com');
 * await page.type('#search', 'hello');
 * await page.click('#submit');
 * await helpers.sleep(2000);
 * const title = await page.title();
 * helpers.log('Got title:', title);
 * return title;
 * ```
 */
export async function runPuppeteerScript(
  page: Page,
  code: string,
  context?: ScriptContext,
  timeout?: number,
): Promise<ScriptResult> {
  const effectiveTimeout = Math.min(timeout || DEFAULT_TIMEOUT, MAX_TIMEOUT);
  const logs: string[] = [];
  const startTime = Date.now();

  // Build helpers
  const helpers: ScriptHelpers = {
    sleep: (ms: number) => new Promise((resolve) => setTimeout(resolve, ms)),
    log: (...args: unknown[]) => {
      const line = args.map((a) => (typeof a === 'object' ? JSON.stringify(a) : String(a))).join(' ');
      logs.push(line);
    },
    screenshot: async (path?: string) => {
      const buffer = await page.screenshot({
        type: 'jpeg',
        quality: 75,
        ...(path ? { path } : {}),
      });
      return Buffer.from(buffer).toString('base64');
    },
  };

  try {
    const body = extractFunctionBody(code);
    const fn = new AsyncFunction('page', 'context', 'helpers', body);

    // Race between script execution and timeout
    const result = await Promise.race([
      fn(page, context || {}, helpers),
      new Promise<never>((_, reject) =>
        setTimeout(
          () => reject(new Error(`Script timed out after ${effectiveTimeout}ms`)),
          effectiveTimeout,
        ),
      ),
    ]);

    return {
      success: true,
      result: safeSerialize(result),
      logs,
      duration: Date.now() - startTime,
    };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return {
      success: false,
      error: message,
      logs,
      duration: Date.now() - startTime,
    };
  }
}

/**
 * Safely serialize a result value, handling circular references
 * and non-serializable types like Buffers.
 */
function safeSerialize(value: unknown): unknown {
  if (value === undefined || value === null) return value;
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;
  if (Buffer.isBuffer(value)) return `[Buffer: ${value.length} bytes]`;
  if (value instanceof Uint8Array) return `[Uint8Array: ${value.length} bytes]`;

  try {
    // Test for circular references
    JSON.stringify(value);
    return value;
  } catch {
    return String(value);
  }
}
