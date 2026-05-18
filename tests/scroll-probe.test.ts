/**
 * Tests for scroll-probe.ts — the pixel-scroll anti-hallucination
 * probe + newly-visible label trace.
 *
 * Pattern lifted from viewport-shift.test.ts: pure-unit fake-page
 * tests, no Puppeteer launch. The complex matching logic lives inside
 * a single page.evaluate body which we can't easily unit-test in
 * isolation; the tests here verify the wrapper contract — args
 * passthrough, early returns, response shape — and rely on the
 * Python end-to-end smoke suite for the in-browser semantics.
 */

import { describe, it, expect } from 'vitest';
import type { Page } from 'puppeteer-core';
import {
  runScrollProbe,
  capturePreScrollBboxes,
  findFirstInteractiveMatch,
  type ProbeResult,
} from '../src/browser/scroll-probe.js';

interface FakePage {
  evaluate: (fn: unknown, args: unknown) => Promise<unknown>;
  lastArgs: unknown;
  next: unknown;
  calls: number;
}

function makeFakePage(next: unknown): FakePage {
  const fake: FakePage = {
    calls: 0,
    lastArgs: undefined,
    next,
    evaluate: async (_fn: unknown, args: unknown): Promise<unknown> => {
      fake.calls += 1;
      fake.lastArgs = args;
      return fake.next;
    },
  };
  return fake;
}

describe('runScrollProbe', () => {
  it('returns the evaluate result unchanged when targetText is empty', async () => {
    const result = { probe: null, newly_visible: ['Foo', 'Bar'] };
    const page = makeFakePage(result);
    const got = await runScrollProbe(page as unknown as Page, {
      collectNewlyVisible: true,
    });
    expect(got).toEqual(result);
    expect(page.calls).toBe(1);
  });

  it('passes regexSrc + isRegex correctly for a literal substring', async () => {
    const page = makeFakePage({ probe: null, newly_visible: [] });
    await runScrollProbe(page as unknown as Page, {
      targetText: 'Price',
      collectNewlyVisible: true,
    });
    const args = page.lastArgs as { regexSrc: string; isRegex: boolean };
    expect(args.regexSrc).toBe('Price');
    // 'Price' compiles as a regex (it's a valid regex source) so isRegex=true.
    expect(args.isRegex).toBe(true);
  });

  it('passes isRegex=false for an invalid regex source', async () => {
    const page = makeFakePage({ probe: null, newly_visible: [] });
    await runScrollProbe(page as unknown as Page, {
      // Unbalanced parenthesis — not a valid regex.
      targetText: 'Price (USD',
      collectNewlyVisible: true,
    });
    const args = page.lastArgs as { regexSrc: string; isRegex: boolean };
    expect(args.regexSrc).toBe('Price (USD');
    expect(args.isRegex).toBe(false);
  });

  it('forwards collectNewlyVisible=false (percent-mode skip)', async () => {
    const page = makeFakePage({ probe: null, newly_visible: [] });
    await runScrollProbe(page as unknown as Page, {
      collectNewlyVisible: false,
    });
    const args = page.lastArgs as { collectNewlyVisible: boolean };
    expect(args.collectNewlyVisible).toBe(false);
  });

  it('forwards preBboxes array to the evaluate call', async () => {
    const preBboxes = [
      { selector: 'button#price', rect: { top: 100, left: 0, w: 80, h: 30 } },
      { selector: 'a#brand', rect: { top: 200, left: 0, w: 80, h: 30 } },
    ];
    const page = makeFakePage({ probe: null, newly_visible: [] });
    await runScrollProbe(page as unknown as Page, {
      targetText: 'Price',
      preBboxes,
      collectNewlyVisible: true,
    });
    const args = page.lastArgs as { preBboxes: typeof preBboxes };
    expect(args.preBboxes).toEqual(preBboxes);
  });

  it('returns probe object with sticky_candidate when set by evaluate', async () => {
    const probe: ProbeResult = {
      in_viewport: true,
      fully_in_viewport: true,
      anywhere_in_dom: true,
      above_fold: false,
      below_fold: false,
      sticky_candidate: true,
      matched_text: 'Price',
      matched_selector: 'button#price',
    };
    const page = makeFakePage({ probe, newly_visible: [] });
    const got = await runScrollProbe(page as unknown as Page, {
      targetText: 'Price',
      collectNewlyVisible: true,
    });
    expect(got.probe).toEqual(probe);
    expect(got.probe?.sticky_candidate).toBe(true);
  });

  it('returns newly_visible array as-is from evaluate', async () => {
    const labels = ['Brand', 'Sort by', 'Refine'];
    const page = makeFakePage({ probe: null, newly_visible: labels });
    const got = await runScrollProbe(page as unknown as Page, {
      collectNewlyVisible: true,
    });
    expect(got.newly_visible).toEqual(labels);
  });
});

describe('capturePreScrollBboxes', () => {
  it('returns empty array when targetText is empty', async () => {
    const page = makeFakePage([]);
    const got = await capturePreScrollBboxes(page as unknown as Page, '');
    expect(got).toEqual([]);
    // No evaluate call when no target.
    expect(page.calls).toBe(0);
  });

  it('returns empty array when targetText is whitespace only', async () => {
    const page = makeFakePage([]);
    const got = await capturePreScrollBboxes(page as unknown as Page, '   ');
    expect(got).toEqual([]);
    expect(page.calls).toBe(0);
  });

  it('forwards trimmed targetText + regex flag to evaluate', async () => {
    const result = [
      { selector: 'button#price', rect: { top: 1200, left: 0, w: 80, h: 30 } },
    ];
    const page = makeFakePage(result);
    const got = await capturePreScrollBboxes(page as unknown as Page, '  Price  ');
    expect(got).toEqual(result);
    const args = page.lastArgs as { regexSrc: string; isRegex: boolean };
    expect(args.regexSrc).toBe('Price');
    expect(args.isRegex).toBe(true);
  });
});

describe('findFirstInteractiveMatch', () => {
  it('returns null without an evaluate call when both targetText and targetRole are empty', async () => {
    const page = makeFakePage(null);
    const got = await findFirstInteractiveMatch(page as unknown as Page, {});
    expect(got).toBeNull();
    expect(page.calls).toBe(0);
  });

  it('passes regex flag correctly for a valid pattern', async () => {
    const match = { selector: 'button#go', text: 'Apply' };
    const page = makeFakePage(match);
    const got = await findFirstInteractiveMatch(page as unknown as Page, {
      targetText: 'Apply',
    });
    expect(got).toEqual(match);
    const args = page.lastArgs as { regexSrc: string; isRegex: boolean };
    expect(args.regexSrc).toBe('Apply');
    expect(args.isRegex).toBe(true);
  });

  it('forwards containerSelector when provided', async () => {
    const page = makeFakePage(null);
    await findFirstInteractiveMatch(page as unknown as Page, {
      targetText: 'Pick',
      containerSelector: '#popup',
    });
    const args = page.lastArgs as { container?: string };
    expect(args.container).toBe('#popup');
  });

  it('treats whitespace-only opts as empty (early null return)', async () => {
    const page = makeFakePage(null);
    const got = await findFirstInteractiveMatch(page as unknown as Page, {
      targetText: '   ',
      targetRole: '\t',
    });
    expect(got).toBeNull();
    expect(page.calls).toBe(0);
  });
});
