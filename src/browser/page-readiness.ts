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

/**
 * Wait for a click target to stop moving before we dispatch.
 *
 * Sister helper to `waitForVisualStable`. The shape is similar (single
 * `page.evaluate` with an in-page poll loop, env-tunable, best-effort
 * try/catch) but the question is element-level, not page-level: has THIS
 * target's bounding rect held still long enough that dispatching a click
 * at coords sampled NOW will land where we think it lands?
 *
 * Why this exists: on heavy-rendering sites (dropdowns that fetch fresh
 * data, animated reflows), `clickInBbox` reads `getBoundingClientRect`
 * one tick before `Input.dispatchMouseEvent` — by the time the event
 * reaches the page, the target has translated tens of pixels. The
 * post-click `verify_action` flags this as a silent miss but only after
 * the user has already lost a turn. Catching it pre-dispatch turns a
 * misclick into a 150-600ms wait.
 *
 * Two target shapes:
 *   { kind: 'xpath', xpath } — DOM-index click branch. Re-resolves the
 *     element once if it gets unmounted mid-poll (React remount), so
 *     a single hydration commit doesn't fail the whole gate.
 *   { kind: 'point', x, y } — vision-bbox branch. Captures the
 *     interactive at the bbox centre ONCE and tracks it; re-querying
 *     elementFromPoint each tick would defeat the gate (we want THE
 *     element to stop, not whichever element happens to sit under the
 *     pixel after a layout shift).
 *
 * Couples bounds-quiet with mutation-counter quiet. A target whose rect
 * is frozen for 150ms inside a tree that's bumping `__nb_mutation_counter`
 * by hundreds per tick is one frame from moving — the gate resets the
 * quiet window in that case and reports `mutating_environment` if it
 * times out.
 *
 * Returns shape lets the caller decide:
 *   - `stable: true`  → use lastBounds, dispatch.
 *   - `stable: false, lastBounds: <something>` → still dispatch with
 *     latest bounds; clickInBbox Phase 1/2 fallbacks + post-click
 *     verify catch the misses, never block forever.
 *   - `stable: false, lastBounds: null, reason: 'no_target'` → caller
 *     falls through to legacy path (e.g. clickElement for DOM-index).
 *
 * Configurable via env:
 *   CLICK_STABILITY_DISABLE=1               — bypass entirely
 *   CLICK_STABILITY_MAX_MS  (default 600)   — hard cap
 *   CLICK_STABILITY_QUIET_MS (default 150)  — bounds-quiet window
 *   CLICK_STABILITY_PIXEL_EPSILON (default 2) — pixel tolerance
 */
export type TargetIdent =
  | { kind: 'xpath'; xpath: string }
  | { kind: 'point'; x: number; y: number };

export interface TargetStableResult {
  stable: boolean;
  lastBounds: { x: number; y: number; w: number; h: number } | null;
  samples: number;
  reason:
    | 'stable'
    | 'timeout'
    | 'no_target'
    | 'mutating_environment'
    | 'disabled';
}

export async function waitForTargetStable(
  page: Page,
  target: TargetIdent,
  opts?: { maxMs?: number; quietMs?: number; pixelEpsilon?: number },
): Promise<TargetStableResult> {
  if (process.env.CLICK_STABILITY_DISABLE === '1') {
    return { stable: true, lastBounds: null, samples: 0, reason: 'disabled' };
  }
  const envMax = parseInt(process.env.CLICK_STABILITY_MAX_MS || '', 10);
  const envQuiet = parseInt(process.env.CLICK_STABILITY_QUIET_MS || '', 10);
  const envEps = parseFloat(process.env.CLICK_STABILITY_PIXEL_EPSILON || '');
  const maxMs = Math.max(
    60,
    Math.min(3000, opts?.maxMs ?? (Number.isFinite(envMax) ? envMax : 600)),
  );
  const quietMs = Math.max(
    50,
    Math.min(
      2000,
      opts?.quietMs ?? (Number.isFinite(envQuiet) ? envQuiet : 150),
    ),
  );
  const pixelEps = Math.max(
    0.5,
    Math.min(20, opts?.pixelEpsilon ?? (Number.isFinite(envEps) ? envEps : 2)),
  );
  try {
    return (await page.evaluate(
      async (args: {
        target: TargetIdent;
        maxMs: number;
        quietMs: number;
        pixelEps: number;
      }) => {
        const SEL = 'a,button,input,select,textarea,'
          + '[role="button"],[role="link"],[role="checkbox"],'
          + '[role="tab"],[role="menuitem"],[onclick],[tabindex]';

        const resolveXpath = (xp: string): Element | null => {
          try {
            const r = document.evaluate(
              xp, document, null,
              XPathResult.FIRST_ORDERED_NODE_TYPE, null,
            ).singleNodeValue;
            return r instanceof HTMLElement ? r : null;
          } catch {
            return null;
          }
        };

        let el: Element | null = null;
        if (args.target.kind === 'xpath') {
          el = resolveXpath(args.target.xpath);
        } else {
          try {
            const stack = document.elementsFromPoint(
              args.target.x, args.target.y,
            );
            const found = stack.find(
              (s) => s !== document.documentElement && s !== document.body,
            );
            if (found) el = (found.closest(SEL) as Element) || found;
          } catch { /* no_target below */ }
        }
        if (!el) {
          return {
            stable: false,
            lastBounds: null,
            samples: 0,
            reason: 'no_target' as const,
          };
        }

        const w = window as unknown as { __nb_mutation_counter?: number };
        const start = performance.now();
        let lastRect: DOMRect | null = null;
        let stableSince = -1;
        let mutAtStable = -1;
        let samples = 0;
        let lastBounds: {
          x: number; y: number; w: number; h: number;
        } | null = null;

        const same = (a: DOMRect, b: DOMRect): boolean => (
          Math.abs(a.left - b.left) <= args.pixelEps
          && Math.abs(a.top - b.top) <= args.pixelEps
          && Math.abs(a.width - b.width) <= args.pixelEps
          && Math.abs(a.height - b.height) <= args.pixelEps
        );

        return await new Promise<{
          stable: boolean;
          lastBounds: { x: number; y: number; w: number; h: number } | null;
          samples: number;
          reason:
            | 'stable' | 'timeout' | 'no_target' | 'mutating_environment'
            | 'disabled';
        }>((resolve) => {
          const tick = (): void => {
            samples += 1;
            const now = performance.now();
            // Re-resolve a remounted element on the xpath path. The
            // point path can't safely re-resolve (we'd start tracking a
            // different element), so we exit on disconnect.
            if (el && !(el as HTMLElement).isConnected) {
              if (args.target.kind === 'xpath') {
                el = resolveXpath(args.target.xpath);
              } else {
                el = null;
              }
            }
            if (!el) {
              resolve({
                stable: false,
                lastBounds,
                samples,
                reason: 'no_target',
              });
              return;
            }
            const r = (el as HTMLElement).getBoundingClientRect();
            lastBounds = { x: r.left, y: r.top, w: r.width, h: r.height };
            const mutNow = w.__nb_mutation_counter ?? 0;
            if (lastRect && same(r, lastRect)) {
              if (stableSince < 0) {
                stableSince = now;
                mutAtStable = mutNow;
              }
              const heldQuiet = (now - stableSince) >= args.quietMs;
              const noMutBurst = (mutNow - mutAtStable) <= 4;
              if (heldQuiet && noMutBurst) {
                resolve({
                  stable: true, lastBounds, samples, reason: 'stable',
                });
                return;
              }
              if (heldQuiet && !noMutBurst) {
                // Bounds froze but the surrounding tree is churning —
                // the cascade is one frame away. Reset the quiet window
                // and let it re-establish only once mutations also
                // calm down.
                stableSince = now;
                mutAtStable = mutNow;
              }
            } else {
              stableSince = -1;
            }
            lastRect = r;
            if ((now - start) >= args.maxMs) {
              const reason
                = (mutNow - mutAtStable) > 4 || mutAtStable < 0
                  ? 'mutating_environment'
                  : 'timeout';
              resolve({
                stable: false,
                lastBounds,
                samples,
                reason: reason as 'mutating_environment' | 'timeout',
              });
              return;
            }
            setTimeout(tick, 50);
          };
          setTimeout(tick, 0);
        });
      },
      { target, maxMs, quietMs, pixelEps },
    )) as TargetStableResult;
  } catch {
    // Page navigated mid-evaluate — treat as no_target so the caller
    // falls through to its legacy path. Never throw from a stability
    // gate; clickInBbox's own Phase 1/2/3 cascade handles the rest.
    return {
      stable: false,
      lastBounds: null,
      samples: 0,
      reason: 'no_target',
    };
  }
}

/**
 * Page-level reference frame for detecting layout shifts BETWEEN a
 * vision capture and a subsequent click. Carries the four signals that
 * change when the page reflows globally:
 *   - scrollY: caught by lazy-load auto-scroll, hash-anchor jumps,
 *     focus-on-load, autofocus, programmatic scroll.
 *   - scrollHeight: catches content injected (banner ad, modal,
 *     "subscribe" toast) or removed above/below the viewport — the
 *     V_n bboxes the brain captured are now in the wrong reference
 *     frame even if scrollY itself didn't move.
 *   - viewportHeight / viewportWidth: catches window resize / device
 *     rotation — rare in headless but possible during human takeover.
 *
 * Captured atomically with the screenshot in the /state vision path
 * and stored on PageWrapper. Compared at /click dispatch time. When
 * shifted beyond threshold, the click handler returns a structured
 * `viewport_shifted` error so the brain re-screenshots instead of
 * dispatching a click whose bbox is in a stale reference frame.
 */
export interface PageRef {
  scrollY: number;
  scrollHeight: number;
  viewportHeight: number;
  viewportWidth: number;
}

export interface ViewportShiftDelta {
  scrollY: number;
  scrollHeight: number;
  viewportHeight: number;
  viewportWidth: number;
}

export interface ViewportShiftResult {
  shifted: boolean;
  reason:
    | 'no_baseline'
    | 'no_shift'
    | 'scroll'
    | 'height'
    | 'viewport'
    | 'disabled';
  delta: ViewportShiftDelta;
  stored: PageRef | null;
  current: PageRef | null;
}

/**
 * Capture the current page reference frame in a single page.evaluate
 * (one CDP roundtrip). Never throws — on any error returns a zero
 * snapshot so the caller can decide whether to skip the comparison.
 */
export async function capturePageRef(page: Page): Promise<PageRef> {
  try {
    const ref = await page.evaluate(() => ({
      scrollY: Math.round((window as Window).scrollY || 0),
      scrollHeight: Math.max(
        document.body?.scrollHeight ?? 0,
        document.documentElement?.scrollHeight ?? 0,
      ),
      viewportHeight: (window as Window).innerHeight || 0,
      viewportWidth: (window as Window).innerWidth || 0,
    }));
    return ref;
  } catch {
    return { scrollY: 0, scrollHeight: 0, viewportHeight: 0, viewportWidth: 0 };
  }
}

/**
 * Compare two page reference frames. Returns `shifted: true` when any
 * dimension exceeds its threshold. Per-axis thresholds are tunable via
 * env so production can tighten or loosen without redeploys.
 *
 * Threshold rationale:
 *   - `scrollPx` default 12 → tighter than the user-reported "a bit
 *     upwards or downwards" misclick window. Below this, anti-aliasing
 *     and rounding can falsely fire.
 *   - `heightPx` default 100 → tolerates fonts settling, image decode
 *     reflow, sidebar collapse animations. Catches banner/modal
 *     injection (typically 60-200 px) and infinite-scroll batches.
 *   - `viewportPx` default 24 → window-resize catch; routine on
 *     responsive layouts but never fires in stable headless runs.
 *
 * Kill switch: `VIEWPORT_SHIFT_DISABLE=1` returns `shifted:false,
 * reason:'disabled'` immediately. Use during incident triage if the
 * gate over-fires on a specific site.
 */
export function compareViewportShift(
  stored: PageRef | null,
  current: PageRef,
  opts?: { scrollPx?: number; heightPx?: number; viewportPx?: number },
): ViewportShiftResult {
  const zero: ViewportShiftDelta = {
    scrollY: 0, scrollHeight: 0, viewportHeight: 0, viewportWidth: 0,
  };
  if (process.env.VIEWPORT_SHIFT_DISABLE === '1') {
    return {
      shifted: false, reason: 'disabled', delta: zero, stored, current,
    };
  }
  if (!stored) {
    return {
      shifted: false, reason: 'no_baseline', delta: zero, stored, current,
    };
  }
  const envScroll = parseInt(process.env.VIEWPORT_SHIFT_PX || '', 10);
  const envHeight = parseInt(process.env.VIEWPORT_SHIFT_HEIGHT_PX || '', 10);
  const envVp = parseInt(process.env.VIEWPORT_SHIFT_VIEWPORT_PX || '', 10);
  const scrollPx = Math.max(
    1,
    opts?.scrollPx ?? (Number.isFinite(envScroll) ? envScroll : 12),
  );
  const heightPx = Math.max(
    1,
    opts?.heightPx ?? (Number.isFinite(envHeight) ? envHeight : 100),
  );
  const viewportPx = Math.max(
    1,
    opts?.viewportPx ?? (Number.isFinite(envVp) ? envVp : 24),
  );
  const delta: ViewportShiftDelta = {
    scrollY: current.scrollY - stored.scrollY,
    scrollHeight: current.scrollHeight - stored.scrollHeight,
    viewportHeight: current.viewportHeight - stored.viewportHeight,
    viewportWidth: current.viewportWidth - stored.viewportWidth,
  };
  if (Math.abs(delta.scrollY) > scrollPx) {
    return { shifted: true, reason: 'scroll', delta, stored, current };
  }
  if (Math.abs(delta.scrollHeight) > heightPx) {
    return { shifted: true, reason: 'height', delta, stored, current };
  }
  if (
    Math.abs(delta.viewportHeight) > viewportPx
    || Math.abs(delta.viewportWidth) > viewportPx
  ) {
    return { shifted: true, reason: 'viewport', delta, stored, current };
  }
  return { shifted: false, reason: 'no_shift', delta, stored, current };
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
