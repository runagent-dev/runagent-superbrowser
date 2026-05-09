/**
 * Tests for capturePageRef + compareViewportShift — the page-shift
 * detection layer.
 *
 * compareViewportShift is a pure function with full coverage; capturePageRef
 * is exercised via a fake page (matches the click-stability pattern). The
 * end-to-end "did /click reject when scroll shifted between vision and
 * dispatch" assertion lives in the Python smoke suite.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { Page } from 'puppeteer-core';
import {
  capturePageRef,
  compareViewportShift,
  type PageRef,
} from '../src/browser/page-readiness.js';

interface FakePage {
  evaluate: (fn: unknown, args: unknown) => Promise<unknown>;
  next: unknown;
  throwOnce?: Error;
  calls: number;
}

function makeFakePage(next: unknown): FakePage {
  const fake: FakePage = {
    calls: 0,
    next,
    evaluate: async (): Promise<unknown> => {
      fake.calls += 1;
      if (fake.throwOnce) {
        const e = fake.throwOnce;
        fake.throwOnce = undefined;
        throw e;
      }
      return fake.next;
    },
  };
  return fake;
}

const ENV_KEYS = [
  'VIEWPORT_SHIFT_DISABLE',
  'VIEWPORT_SHIFT_PX',
  'VIEWPORT_SHIFT_HEIGHT_PX',
  'VIEWPORT_SHIFT_VIEWPORT_PX',
];

const REF_BASE: PageRef = {
  scrollY: 100,
  scrollHeight: 4000,
  viewportHeight: 800,
  viewportWidth: 1280,
};

describe('capturePageRef', () => {
  it('returns the evaluated reference frame', async () => {
    const ref: PageRef = {
      scrollY: 250, scrollHeight: 5000, viewportHeight: 800, viewportWidth: 1280,
    };
    const page = makeFakePage(ref);
    const got = await capturePageRef(page as unknown as Page);
    expect(got).toEqual(ref);
    expect(page.calls).toBe(1);
  });

  it('returns zero snapshot on evaluate error (no throw)', async () => {
    const page = makeFakePage(REF_BASE);
    page.throwOnce = new Error('Execution context was destroyed');
    const got = await capturePageRef(page as unknown as Page);
    expect(got).toEqual({
      scrollY: 0, scrollHeight: 0, viewportHeight: 0, viewportWidth: 0,
    });
  });
});

describe('compareViewportShift', () => {
  let savedEnv: Record<string, string | undefined>;

  beforeEach(() => {
    savedEnv = {};
    for (const k of ENV_KEYS) savedEnv[k] = process.env[k];
  });

  afterEach(() => {
    for (const k of ENV_KEYS) {
      if (savedEnv[k] === undefined) delete process.env[k];
      else process.env[k] = savedEnv[k];
    }
  });

  describe('kill switch and missing baseline', () => {
    it('returns disabled when VIEWPORT_SHIFT_DISABLE=1', () => {
      process.env.VIEWPORT_SHIFT_DISABLE = '1';
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, scrollY: 99999,
      });
      expect(result.shifted).toBe(false);
      expect(result.reason).toBe('disabled');
    });

    it('returns no_baseline when stored is null', () => {
      const result = compareViewportShift(null, REF_BASE);
      expect(result.shifted).toBe(false);
      expect(result.reason).toBe('no_baseline');
    });
  });

  describe('scrollY axis', () => {
    it('does not flag a 1px scroll jitter', () => {
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, scrollY: 101,
      });
      expect(result.shifted).toBe(false);
      expect(result.reason).toBe('no_shift');
    });

    it('does not flag a 12px (boundary) scroll', () => {
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, scrollY: 112,
      });
      expect(result.shifted).toBe(false);
    });

    it('flags a 13px scroll (just over default threshold)', () => {
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, scrollY: 113,
      });
      expect(result.shifted).toBe(true);
      expect(result.reason).toBe('scroll');
      expect(result.delta.scrollY).toBe(13);
    });

    it('flags a 200px scroll (clearly shifted)', () => {
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, scrollY: 300,
      });
      expect(result.shifted).toBe(true);
      expect(result.reason).toBe('scroll');
    });

    it('flags a negative scroll (page scrolled up)', () => {
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, scrollY: 50,
      });
      expect(result.shifted).toBe(true);
      expect(result.reason).toBe('scroll');
      expect(result.delta.scrollY).toBe(-50);
    });

    it('respects VIEWPORT_SHIFT_PX env override', () => {
      process.env.VIEWPORT_SHIFT_PX = '50';
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, scrollY: 130,
      });
      expect(result.shifted).toBe(false);
    });
  });

  describe('scrollHeight axis', () => {
    it('does not flag a 50px height growth (font/image settle)', () => {
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, scrollHeight: 4050,
      });
      expect(result.shifted).toBe(false);
    });

    it('flags a 200px height growth (banner inserted)', () => {
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, scrollHeight: 4200,
      });
      expect(result.shifted).toBe(true);
      expect(result.reason).toBe('height');
    });

    it('flags a height shrink (modal/banner removed)', () => {
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, scrollHeight: 3800,
      });
      expect(result.shifted).toBe(true);
      expect(result.reason).toBe('height');
    });

    it('respects VIEWPORT_SHIFT_HEIGHT_PX env override', () => {
      process.env.VIEWPORT_SHIFT_HEIGHT_PX = '50';
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, scrollHeight: 4060,
      });
      expect(result.shifted).toBe(true);
      expect(result.reason).toBe('height');
    });
  });

  describe('viewport axis', () => {
    it('does not flag a tiny viewport jitter', () => {
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, viewportHeight: 810,
      });
      expect(result.shifted).toBe(false);
    });

    it('flags a 100px viewport height change (window resize)', () => {
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, viewportHeight: 700,
      });
      expect(result.shifted).toBe(true);
      expect(result.reason).toBe('viewport');
    });

    it('flags a viewport width change', () => {
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, viewportWidth: 1024,
      });
      expect(result.shifted).toBe(true);
      expect(result.reason).toBe('viewport');
    });
  });

  describe('priority ordering', () => {
    it('reports scroll first when both scroll and height shifted', () => {
      // When multiple axes shift, scroll wins (most actionable signal).
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, scrollY: 200, scrollHeight: 4500,
      });
      expect(result.shifted).toBe(true);
      expect(result.reason).toBe('scroll');
    });

    it('reports height before viewport when scroll is unchanged', () => {
      const result = compareViewportShift(REF_BASE, {
        ...REF_BASE, scrollHeight: 4500, viewportHeight: 600,
      });
      expect(result.shifted).toBe(true);
      expect(result.reason).toBe('height');
    });
  });

  describe('opts win over env', () => {
    it('inline scrollPx overrides env', () => {
      process.env.VIEWPORT_SHIFT_PX = '5';
      const result = compareViewportShift(
        REF_BASE,
        { ...REF_BASE, scrollY: 110 },
        { scrollPx: 50 },
      );
      expect(result.shifted).toBe(false);
    });
  });

  describe('delta is always populated', () => {
    it('records signed deltas across all axes', () => {
      const result = compareViewportShift(REF_BASE, {
        scrollY: 50,
        scrollHeight: 4500,
        viewportHeight: 850,
        viewportWidth: 1300,
      });
      expect(result.delta.scrollY).toBe(-50);
      expect(result.delta.scrollHeight).toBe(500);
      expect(result.delta.viewportHeight).toBe(50);
      expect(result.delta.viewportWidth).toBe(20);
    });
  });
});
