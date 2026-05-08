/**
 * Page-readiness + error-page detection.
 *
 * Two separate failure modes solved here:
 *   1. Post-navigation readiness: `domcontentloaded + 2s idle` (old
 *      behavior in page.ts:navigate) is not enough for modern SPAs — they
 *      often swap content seconds after DCL. Polling readyState + an
 *      aria-busy check gives a real "rendered" signal.
 *   2. Error-page drift: Chrome hands back a successful goto even when
 *      the destination is a chrome-error:// page, a 404 body, or an ISP
 *      block page. Without detecting these, the navigator LLM happily
 *      tries to click "ERR_CONNECTION_REFUSED" like it was a button.
 *
 * Both helpers are pure reads — they don't mutate page state — so they
 * can be called from navigate(), post-click, or getState() equivalently.
 */

import type { Page } from 'puppeteer-core';

export type ErrorPageKind =
  | 'chrome-error'
  | 'http-4xx'
  | 'http-5xx'
  | 'dns'
  | 'tls'
  | 'text-error';

export interface ErrorPage {
  kind: ErrorPageKind;
  detail: string;
}

/**
 * Poll `document.readyState === 'complete'` AND top-of-tree not
 * `[aria-busy="true"]`. Returns 'ready' as soon as both hold, 'timeout'
 * if still not ready at the deadline.
 *
 * Intentionally loose: doesn't wait for network idle (too flaky on
 * pages with long-polling or analytics beacons) and doesn't wait for
 * visual stability (would defeat the purpose of letting the agent
 * observe a loading spinner). The goal is "DOM is no longer mid-
 * construction", not "everything has settled".
 */
export async function waitForPageReady(
  page: Page,
  timeoutMs: number = 8000,
  pollIntervalMs: number = 100,
): Promise<'ready' | 'timeout'> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const ready = await page.evaluate(() => {
        if (document.readyState !== 'complete') return false;
        const root = document.documentElement;
        if (root?.getAttribute('aria-busy') === 'true') return false;
        if (document.body?.getAttribute('aria-busy') === 'true') return false;
        return true;
      });
      if (ready) return 'ready';
    } catch {
      // page closed / navigated — treat as still-loading
    }
    await new Promise((r) => setTimeout(r, pollIntervalMs));
  }
  return 'timeout';
}

/**
 * Wait for VISUAL stability — fonts loaded, above-fold images decoded,
 * and no recent layout shifts.
 *
 * Companion to `waitForPageReady` which only checks DOM-readiness
 * (`readyState=='complete'` + `aria-busy`). DOM-ready is not enough
 * for screenshot-driven vision: fonts can swap 200-1200ms after DCL
 * (FOUT), shifting text vertically; lazy hero images load late,
 * pushing content down; React/Vue hydration can commit components
 * after first paint. The bboxes computed against a pre-settled
 * screenshot point at empty space above where text actually lives
 * by the time the brain clicks.
 *
 * Three checks, all best-effort with a hard cap at `maxMs`:
 *   1. `document.fonts.ready` — resolves when @font-face files have
 *      finished loading. Resolves immediately when nothing's pending.
 *   2. Above-fold image decode — `Promise.allSettled` over images
 *      that intersect the viewport, calling `img.decode()` which
 *      fulfills only when pixels are ready to paint.
 *   3. Layout-shift idle — `PerformanceObserver({ type: 'layout-shift' })`
 *      collects shifts (filtering `hadRecentInput`); resolves when no
 *      shifts have occurred for `quietMs`.
 *
 * Returns 'stable' when all three completed before `maxMs`, 'timeout'
 * when the hard cap fired. Either return value is safe — the caller
 * proceeds in both cases. Wrapped in try/catch so a broken page
 * (e.g., closed mid-call) never throws.
 *
 * Configurable via env:
 *   VISUAL_STABLE_MAX_MS  (default 1500)  — hard cap
 *   VISUAL_STABLE_QUIET_MS (default 200)  — layout-shift idle window
 *   VISUAL_STABLE_DISABLE=1                — bypass entirely (legacy)
 */
export async function waitForVisualStable(
  page: Page,
  maxMsArg?: number,
  quietMsArg?: number,
): Promise<'stable' | 'timeout'> {
  if (process.env.VISUAL_STABLE_DISABLE === '1') return 'stable';
  const envMax = parseInt(process.env.VISUAL_STABLE_MAX_MS || '', 10);
  const envQuiet = parseInt(process.env.VISUAL_STABLE_QUIET_MS || '', 10);
  const maxMs = Math.max(
    100,
    Math.min(5000, maxMsArg ?? (Number.isFinite(envMax) ? envMax : 1500)),
  );
  const quietMs = Math.max(
    50,
    Math.min(1000, quietMsArg ?? (Number.isFinite(envQuiet) ? envQuiet : 200)),
  );
  try {
    return (await page.evaluate(async (args: { maxMs: number; quietMs: number }) => {
      const start = performance.now();
      const remaining = () => Math.max(0, args.maxMs - (performance.now() - start));

      // (1) Fonts. Resolves immediately if nothing is pending. Race
      // against the budget so a stuck font-load can't hang us.
      try {
        const fontsReady = (document as unknown as {
          fonts?: { ready?: Promise<unknown> };
        }).fonts?.ready;
        if (fontsReady) {
          await Promise.race([
            fontsReady,
            new Promise((r) => setTimeout(r, Math.min(remaining(), args.maxMs - 100))),
          ]);
        }
      } catch { /* best-effort */ }

      // (2) Above-fold image decode. Filter to images visible in
      // viewport with non-zero size. `decode()` resolves when pixels
      // are paint-ready; reject path is harmless via allSettled.
      try {
        if (remaining() > 100) {
          const vh = window.innerHeight;
          const imgs = Array.from(document.querySelectorAll('img'))
            .filter((img) => {
              try {
                const r = (img as HTMLImageElement).getBoundingClientRect();
                return r.top < vh && r.bottom > 0 && r.width > 0 && r.height > 0;
              } catch {
                return false;
              }
            }) as HTMLImageElement[];
          const decodes = imgs.map((img) => {
            if (img.complete && img.naturalWidth > 0) return Promise.resolve();
            if (typeof img.decode === 'function') {
              return img.decode().catch(() => undefined);
            }
            return new Promise<void>((resolve) => {
              img.addEventListener('load', () => resolve(), { once: true });
              img.addEventListener('error', () => resolve(), { once: true });
            });
          });
          await Promise.race([
            Promise.allSettled(decodes),
            new Promise((r) => setTimeout(r, Math.max(50, remaining() - 100))),
          ]);
        }
      } catch { /* best-effort */ }

      // (3) Layout-shift idle. Resolve when no non-input shift has
      // occurred for `quietMs`. Hard cap at `maxMs` total.
      const tail = remaining();
      if (tail < args.quietMs) return 'stable';
      try {
        const PO = (window as unknown as {
          PerformanceObserver?: typeof PerformanceObserver;
        }).PerformanceObserver;
        if (!PO) return 'stable';
        await new Promise<void>((resolve) => {
          let lastShift = performance.now();
          let observer: PerformanceObserver | null = null;
          try {
            observer = new PO((list) => {
              const entries = list.getEntries() as Array<PerformanceEntry & { hadRecentInput?: boolean }>;
              for (const e of entries) {
                if (!e.hadRecentInput) lastShift = performance.now();
              }
            });
            observer.observe({ type: 'layout-shift', buffered: true });
          } catch {
            resolve();
            return;
          }
          const tick = setInterval(() => {
            const now = performance.now();
            if (now - lastShift >= args.quietMs) {
              try { observer?.disconnect(); } catch { /* ignore */ }
              clearInterval(tick);
              resolve();
              return;
            }
            if (now - start >= args.maxMs) {
              try { observer?.disconnect(); } catch { /* ignore */ }
              clearInterval(tick);
              resolve();
              return;
            }
          }, 50);
        });
      } catch { /* best-effort */ }
      return (performance.now() - start) >= args.maxMs ? 'timeout' : 'stable';
    }, { maxMs, quietMs })) as 'stable' | 'timeout';
  } catch {
    // Page closed / navigated mid-evaluate — treat as not-stable but
    // don't fail the caller; the next screenshot path can handle it.
    return 'timeout';
  }
}

/** Text patterns that reliably indicate an error page body (not a real one). */
const ERROR_TEXT_PATTERNS: ReadonlyArray<[RegExp, ErrorPageKind]> = [
  [/This site can(?:'|&#39;|&apos;)t be reached/i, 'chrome-error'],
  [/\bERR_CONNECTION_REFUSED\b/i, 'chrome-error'],
  [/\bERR_CONNECTION_RESET\b/i, 'chrome-error'],
  [/\bERR_CONNECTION_TIMED_OUT\b/i, 'chrome-error'],
  [/\bERR_NAME_NOT_RESOLVED\b/i, 'dns'],
  [/\bDNS_PROBE_FINISHED\b/i, 'dns'],
  [/\bERR_CERT_\w+/i, 'tls'],
  [/\bERR_SSL_\w+/i, 'tls'],
  [/\bNET::ERR_/i, 'chrome-error'],
  [/502 Bad Gateway/i, 'http-5xx'],
  [/503 Service (?:Unavailable|Temporarily)/i, 'http-5xx'],
  [/504 Gateway Timeout/i, 'http-5xx'],
  [/Aw,? Snap!/i, 'chrome-error'],
  // SPA soft-404s: status is usually 200 but the UI has clearly told
  // the user the page doesn't exist or the search has no hits. These
  // catch the "agent keeps retrying the same dead URL" loop.
  [/\bPage not found\b/i, 'text-error'],
  [/\b404 not found\b/i, 'text-error'],
  [/we can(?:'|&#39;)t find (?:the|that) page/i, 'text-error'],
  [/we couldn(?:'|&#39;)t find/i, 'text-error'],
  [/\bno (?:results|matches) found\b/i, 'text-error'],
  [/\bsomething went wrong\b/i, 'text-error'],
  [/\boops[,!.]? (?:something|page)/i, 'text-error'],
];

/**
 * Classify the page as an error page if any of these hold:
 *   - URL is chrome-error://...
 *   - statusCode is provided and >= 400
 *   - body text matches a known error pattern
 *
 * Returns `null` on success (the happy path) to make the caller check
 * ergonomic: `const err = await detectErrorPage(...); if (err) { ... }`.
 *
 * Small heuristic: only text-scans the first 8KB of body. Real error
 * pages always put the signal in the first screen; a full read is
 * expensive (large pages) and the patterns are distinctive enough that
 * false positives deeper in a legitimate page are a non-issue.
 */
export async function detectErrorPage(
  page: Page,
  statusCode: number | null,
): Promise<ErrorPage | null> {
  // 1. chrome-error:// URLs are unambiguous.
  let url = '';
  try {
    url = page.url();
  } catch {
    // page closed — treat as not-an-error; caller has bigger problems.
    return null;
  }
  if (url.startsWith('chrome-error://')) {
    return { kind: 'chrome-error', detail: url };
  }

  // 2. HTTP status classification. We only classify 4xx/5xx if we got
  // a status from the navigation response — missing status means XHR
  // or intermediate nav where we have no authoritative signal.
  if (statusCode && statusCode >= 500) {
    return { kind: 'http-5xx', detail: `HTTP ${statusCode}` };
  }
  if (statusCode && statusCode >= 400) {
    // Don't short-circuit on 4xx alone — some auth pages serve the real
    // login UI with a 401. Only flag 4xx if the body also looks like an
    // error page. Fall through to text scan.
  }

  // 3. Body-text scan, capped at ~8KB.
  let bodyHead = '';
  try {
    bodyHead = await page.evaluate(() => {
      const b = document.body?.innerText || '';
      return b.slice(0, 8192);
    });
  } catch {
    return null;
  }
  for (const [re, kind] of ERROR_TEXT_PATTERNS) {
    const m = bodyHead.match(re);
    if (m) {
      // If we saw a 4xx status and the body text confirms, mark as 4xx;
      // otherwise use the pattern's own kind (chrome-error / dns / tls).
      const effectiveKind = statusCode && statusCode >= 400 && statusCode < 500 ? 'http-4xx' : kind;
      return { kind: effectiveKind, detail: m[0].slice(0, 200) };
    }
  }

  // Bare 4xx with no body-text match: trust the browser rendered
  // something useful (e.g., a login form) and don't block.
  return null;
}
