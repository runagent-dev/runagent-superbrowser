/**
 * Post-action effect snapshot + diff.
 *
 * Every mutation handler (click, type, keys, drag, select, drag-slider)
 * captures an {EffectSnapshot} BEFORE the action, runs the action,
 * waits for the page to settle, captures AFTER, then returns the
 * {EffectDelta} alongside the usual response fields. The Python side
 * uses the delta to tell "Puppeteer dispatched" apart from "the page
 * actually changed" — without this, a Puppeteer click that React
 * silently swallowed looks indistinguishable from a click that
 * committed a form, and the brain plans the next step against a state
 * that never materialized.
 *
 * The shape is intentionally flat so the Python JSON reader doesn't
 * need a schema upgrade. Optional / missing fields mean "I didn't
 * observe" — downstream treats that as "had effect" so a staged
 * rollout (TS ships first) doesn't regress existing behavior.
 */

import type { Page } from 'puppeteer-core';
import { waitForPageReady } from '../browser/page-readiness.js';

export interface EffectSnapshot {
  url: string;
  mutationCounter: number;
  focusedHash: string;
}

export interface EffectDelta {
  url_changed: boolean;
  mutation_delta: number;
  focused_changed: boolean;
}

/**
 * Read the three cheap signals we use to decide "did something
 * change?". Wrapped in a single evaluate to avoid three round trips.
 * Never throws — on any error returns a zero snapshot so the caller
 * sees a conservative "probably no effect" (the Python side's default
 * for a missing effect field is "had effect", so the worst case is a
 * false negative here that defaults safe).
 */
export async function captureEffect(page: Page): Promise<EffectSnapshot> {
  let url = '';
  try {
    url = page.url();
  } catch {
    /* page closed */
  }
  try {
    const probe = await page.evaluate(() => {
      const w = window as unknown as { __nb_mutation_counter?: number };
      const counter = typeof w.__nb_mutation_counter === 'number'
        ? w.__nb_mutation_counter
        : 0;
      let focusedHash = '';
      try {
        const el = document.activeElement as HTMLElement | null;
        if (el && el !== document.body && el !== document.documentElement) {
          const sig = (el.tagName || '') + '|'
            + (el.id || '') + '|'
            + ((el.getAttribute('name') || '').slice(0, 40)) + '|'
            + (el.outerHTML || '').slice(0, 200);
          // Tiny string-hash (djb2) — cheap, collision-resistant
          // enough for before/after comparison on the same page.
          let h = 5381;
          for (let i = 0; i < sig.length; i++) {
            h = ((h << 5) + h + sig.charCodeAt(i)) | 0;
          }
          focusedHash = (h >>> 0).toString(16);
        }
      } catch { /* ignore */ }
      return { counter, focusedHash };
    });
    return { url, mutationCounter: probe.counter, focusedHash: probe.focusedHash };
  } catch {
    return { url, mutationCounter: 0, focusedHash: '' };
  }
}

/**
 * Difference of two snapshots. `mutation_delta` can be negative if the
 * page navigated and the counter reset — treat that as "changed" via
 * `url_changed` rather than inventing an abs() here.
 */
export function diffEffect(before: EffectSnapshot, after: EffectSnapshot): EffectDelta {
  return {
    url_changed: before.url !== after.url,
    mutation_delta: Math.max(0, after.mutationCounter - before.mutationCounter),
    focused_changed: before.focusedHash !== after.focusedHash,
  };
}

/**
 * Default settle window between the action returning and the `after`
 * snapshot. Gives React (or any other framework) a beat to flush its
 * commit phase. Tunable via POST_ACTION_SETTLE_MS; 0 disables.
 */
export function postActionSettleMs(): number {
  const raw = process.env.POST_ACTION_SETTLE_MS;
  if (raw === undefined) return 150;
  const n = parseInt(raw, 10);
  return Number.isFinite(n) && n >= 0 ? n : 150;
}

/**
 * Sleep + wait for the page to be "ready" (readyState=complete,
 * aria-busy not set). Used between an action and its `after` effect
 * snapshot so the delta reflects a settled page, not a mid-transition
 * one. Never throws — timeouts are expected on pages that never reach
 * ready (e.g., infinite loaders) and must not fail the handler.
 */
export async function settleForEffect(page: Page): Promise<void> {
  const ms = postActionSettleMs();
  if (ms > 0) {
    await new Promise((r) => setTimeout(r, ms));
  }
  try {
    await waitForPageReady(page, 2000);
  } catch {
    /* best-effort */
  }
}
