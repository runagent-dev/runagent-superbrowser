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
  /** When the sandbox rejects a mutation, surfaces the rule that fired. */
  blocked_op?: string;
  logs: string[];
  duration: number;
}

export interface ScriptContext {
  [key: string]: unknown;
}

export interface ScriptRunOptions {
  /**
   * Whether the script is allowed to mutate the page (click, type,
   * submit, set input values). When false (default), Puppeteer Page
   * mutation methods are blocked at the proxy layer and an in-page
   * freeze prelude disables `HTMLElement.prototype.click`, the input
   * value setter, and `dispatchEvent` for click/input/change/keydown-
   * family events.
   *
   * `mutates=true` is vision-gated at the Python bridge layer — the
   * script-runner itself just forwards the flag so the freeze is skipped.
   */
  mutates?: boolean;
}

export interface ScriptHelpers {
  sleep: (ms: number) => Promise<void>;
  log: (...args: unknown[]) => void;
  screenshot: (path?: string) => Promise<string>;
  /**
   * Write `value` into an `<input>`/`<textarea>` the way React
   * expects — via the native `value` setter on the prototype, then
   * dispatch synthetic `input`+`change` events. Plain
   * `el.value = x` is swallowed by React's synthetic event system
   * because the controlled-input value tracker sees "no change";
   * this helper bypasses that and produces the same observable
   * state as a real keystroke.
   *
   * Resolves `selector` in-page via `document.querySelector`.
   * Returns the value that was written. Throws if the element
   * isn't found or isn't an input/textarea.
   */
  reactSetValue: (selector: string, value: string) => Promise<string>;
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
 * Puppeteer Page methods that perform a page mutation (click, type,
 * submit, navigate). Blocked when the script is invoked with
 * `mutates=false` so the agent can't bypass the humanized-cursor path
 * by reaching into raw Puppeteer. Mutations that aren't in this list
 * (scroll, focus, hover, goto — which is a deliberate escape valve
 * for arbitrary scripts) stay available.
 */
const BLOCKED_PAGE_METHODS = new Set<string>([
  'click', 'type', 'keyboard', 'mouse', 'touchscreen',
  'select', 'tap', 'check', 'uncheck', 'press',
  // Some Puppeteer builds expose these as top-level shortcuts:
  '$eval', // safe read-only — whitelist explicitly by NOT listing it
].filter((m) => m !== '$eval'));

/**
 * In-page JS snippet executed before every `page.evaluate`-style call
 * when `mutates=false`. Freezes interactive APIs so user scripts that
 * run via `page.evaluate(() => el.click())` fail loudly instead of
 * silently bypassing the cursor path. Uses non-configurable property
 * descriptors so evasion via `Object.getOwnPropertyDescriptor`,
 * `Reflect.apply`, or bracket access all hit the same frozen slot.
 */
const FREEZE_PRELUDE = `
  (function __nb_freeze__() {
    if (window.__nb_mutation_freeze_installed) return;
    var blocked = function (op) {
      return function () {
        throw new Error(
          '[blocked_op:' + op + '] run_script(mutates=false) cannot ' +
          'synthesize user interactions. Use browser_click_at(' +
          'vision_index=V_n) or browser_semantic_click instead.'
        );
      };
    };
    try {
      Object.defineProperty(HTMLElement.prototype, 'click', {
        value: blocked('HTMLElement.click'),
        configurable: false, writable: false,
      });
    } catch (e) { /* already frozen or cross-origin; fall through */ }
    var protos = [
      HTMLInputElement && HTMLInputElement.prototype,
      HTMLTextAreaElement && HTMLTextAreaElement.prototype,
      HTMLSelectElement && HTMLSelectElement.prototype,
    ];
    for (var i = 0; i < protos.length; i++) {
      var p = protos[i];
      if (!p) continue;
      try {
        Object.defineProperty(p, 'value', {
          set: blocked('.value='),
          get: Object.getOwnPropertyDescriptor(p, 'value').get,
          configurable: false,
        });
      } catch (e) { /* silent */ }
    }
    try {
      var origDispatch = EventTarget.prototype.dispatchEvent;
      var BLOCKED_EV = {
        click: 1, mousedown: 1, mouseup: 1,
        input: 1, change: 1,
        keydown: 1, keyup: 1, keypress: 1,
        pointerdown: 1, pointerup: 1,
      };
      Object.defineProperty(EventTarget.prototype, 'dispatchEvent', {
        value: function (ev) {
          var t = ev && ev.type;
          if (t && BLOCKED_EV[t]) {
            throw new Error(
              '[blocked_op:dispatchEvent(' + t + ')] run_script(' +
              'mutates=false) cannot dispatch interactive events. ' +
              'Use a cursor tool.'
            );
          }
          return origDispatch.apply(this, arguments);
        },
        configurable: false, writable: false,
      });
    } catch (e) { /* silent */ }
    window.__nb_mutation_freeze_installed = true;
  })();
`;

/**
 * Wrap a Puppeteer Page in a read-only Proxy that blocks mutation
 * methods. Used when `mutates=false`.
 *
 * Design: blacklist the mutation methods rather than whitelist the
 * full read API — Puppeteer's surface is large and evolving, and
 * accidentally blocking a legitimate read method is worse than a
 * missed mutation in this sandbox (in-page freeze is the real gate).
 */
function makeReadOnlyPageProxy(page: Page): Page {
  const handler: ProxyHandler<Page> = {
    get(target, prop, receiver) {
      if (typeof prop === 'string' && BLOCKED_PAGE_METHODS.has(prop)) {
        const op = `page.${prop}`;
        return () => {
          throw new Error(
            `[blocked_op:${op}] run_script(mutates=false) cannot call `
            + `Puppeteer mutation methods. Use browser_click_at(`
            + `vision_index=V_n), browser_semantic_click, or `
            + `browser_type_at instead — these dispatch isTrusted=true `
            + `events via the humanized cursor path, which WAFs don't `
            + `flag as bot activity.`,
          );
        };
      }
      if (typeof prop === 'string' && prop === 'evaluate') {
        // Wrap evaluate so the freeze prelude runs before every
        // user-supplied evaluator. This stops in-page JS from
        // synthesizing clicks / value-sets via `el.click()`,
        // `dispatchEvent`, or direct `.value=` writes.
        const origEvaluate = Reflect.get(target, prop, receiver) as Function;
        return async (...args: unknown[]) => {
          try {
            await origEvaluate.call(target, FREEZE_PRELUDE);
          } catch { /* best-effort */ }
          return origEvaluate.apply(target, args);
        };
      }
      const v = Reflect.get(target, prop, receiver);
      return typeof v === 'function' ? v.bind(target) : v;
    },
  };
  return new Proxy(page, handler);
}

/**
 * Run a Puppeteer script with full Page API access.
 *
 * The code receives three parameters:
 *   - `page`    — Puppeteer Page object (goto, click, type, waitForSelector, screenshot, etc.)
 *   - `context` — optional data object passed by the caller
 *   - `helpers` — convenience utilities (sleep, log, screenshot)
 *
 * When `options.mutates === false` (the default), `page` is wrapped in
 * a proxy that rejects mutation methods (click, type, keyboard/mouse
 * access, select, tap, press, check/uncheck) and a freeze prelude is
 * injected into every `page.evaluate` call to disable in-page JS
 * click/value/dispatchEvent. Mutating scripts must opt in with
 * `mutates=true`, which the Python bridge gates behind the validator
 * and fresh-vision check.
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
  options?: ScriptRunOptions,
): Promise<ScriptResult> {
  const effectiveTimeout = Math.min(timeout || DEFAULT_TIMEOUT, MAX_TIMEOUT);
  const logs: string[] = [];
  const startTime = Date.now();
  const mutates = Boolean(options?.mutates);
  const pageForScript = mutates ? page : makeReadOnlyPageProxy(page);

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
    reactSetValue: async (selector: string, value: string) => {
      if (!mutates) {
        throw new Error(
          '[blocked_op:reactSetValue] helpers.reactSetValue mutates the '
          + 'page. Pass mutates=true, or — preferably — use '
          + 'browser_type_at(vision_index=V_n) / browser_semantic_type '
          + 'which route through the humanized keyboard path.',
        );
      }
      return page.evaluate((sel: string, val: string) => {
        const el = document.querySelector(sel) as
          | HTMLInputElement
          | HTMLTextAreaElement
          | null;
        if (!el) throw new Error(`reactSetValue: selector not found: ${sel}`);
        const proto = el.tagName === 'TEXTAREA'
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
        if (!setter) throw new Error('reactSetValue: value setter unavailable');
        setter.call(el, val);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return val;
      }, selector, value);
    },
  };

  try {
    const body = extractFunctionBody(code);
    const fn = new AsyncFunction('page', 'context', 'helpers', body);

    // Race between script execution and timeout
    const result = await Promise.race([
      fn(pageForScript, context || {}, helpers),
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
    // Surface `[blocked_op:…]` as a structured field so the Python
    // bridge can rewrite the error into a uniform "use a cursor tool"
    // message without string-matching inside the error payload.
    const blockedMatch = message.match(/\[blocked_op:([^\]]+)\]/);
    return {
      success: false,
      error: message,
      ...(blockedMatch ? { blocked_op: blockedMatch[1] } : {}),
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
