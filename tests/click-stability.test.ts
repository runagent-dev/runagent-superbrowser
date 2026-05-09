/**
 * Tests for waitForTargetStable() — the pre-click stability gate.
 *
 * The existing test suite is pure-unit (no Puppeteer launch), so these
 * tests exercise the TS-side wrapper around `page.evaluate`: kill
 * switch, env-knob clamping, target-shape passthrough, and the
 * navigated-mid-evaluate fallback. The in-page poll loop itself
 * (RAF/setTimeout, bounds-equality check, mutation-counter coupling)
 * is verified end-to-end by the Python smoke suite at
 * `nanobot/test_superbrowser.py` and by manual runs against
 * heavy-rendering sites.
 *
 * If a future change wires up Puppeteer-launching Vitest fixtures,
 * promote these to in-browser cases.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { Page } from 'puppeteer-core';
import { waitForTargetStable } from '../src/browser/page-readiness.js';

interface FakePage {
  evaluate: (
    fn: unknown,
    args: unknown,
  ) => Promise<unknown>;
  /** Records every call to evaluate so tests can assert what was passed. */
  calls: Array<{ args: unknown }>;
  /** Override the next return value. */
  next: unknown;
  /** When set, the next evaluate throws this error (simulates page nav). */
  throwOnce?: Error;
}

function makeFakePage(initialNext: unknown = {
  stable: true,
  lastBounds: { x: 100, y: 100, w: 50, h: 30 },
  samples: 3,
  reason: 'stable',
}): FakePage {
  const fake: FakePage = {
    calls: [],
    next: initialNext,
    evaluate: async (_fn: unknown, args: unknown): Promise<unknown> => {
      fake.calls.push({ args });
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
  'CLICK_STABILITY_DISABLE',
  'CLICK_STABILITY_MAX_MS',
  'CLICK_STABILITY_QUIET_MS',
  'CLICK_STABILITY_PIXEL_EPSILON',
];

describe('waitForTargetStable', () => {
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

  describe('kill switch', () => {
    it('returns disabled immediately when CLICK_STABILITY_DISABLE=1', async () => {
      process.env.CLICK_STABILITY_DISABLE = '1';
      const page = makeFakePage();
      const result = await waitForTargetStable(
        page as unknown as Page,
        { kind: 'xpath', xpath: '/html[1]/body[1]' },
      );
      expect(result.stable).toBe(true);
      expect(result.reason).toBe('disabled');
      expect(result.lastBounds).toBeNull();
      expect(result.samples).toBe(0);
      // Critical: kill switch must short-circuit BEFORE any CDP work.
      expect(page.calls).toHaveLength(0);
    });

    it('runs the helper when CLICK_STABILITY_DISABLE is unset', async () => {
      delete process.env.CLICK_STABILITY_DISABLE;
      const page = makeFakePage();
      await waitForTargetStable(
        page as unknown as Page,
        { kind: 'xpath', xpath: '/html[1]/body[1]' },
      );
      expect(page.calls).toHaveLength(1);
    });

    it('treats CLICK_STABILITY_DISABLE=0 as not-disabled', async () => {
      process.env.CLICK_STABILITY_DISABLE = '0';
      const page = makeFakePage();
      await waitForTargetStable(
        page as unknown as Page,
        { kind: 'xpath', xpath: '/html[1]/body[1]' },
      );
      expect(page.calls).toHaveLength(1);
    });
  });

  describe('env clamping', () => {
    it('clamps maxMs into [60, 3000]', async () => {
      process.env.CLICK_STABILITY_MAX_MS = '99999';
      const page = makeFakePage();
      await waitForTargetStable(
        page as unknown as Page,
        { kind: 'xpath', xpath: '/html[1]/body[1]' },
      );
      const args = page.calls[0]!.args as { maxMs: number };
      expect(args.maxMs).toBe(3000);

      process.env.CLICK_STABILITY_MAX_MS = '5';
      const page2 = makeFakePage();
      await waitForTargetStable(
        page2 as unknown as Page,
        { kind: 'xpath', xpath: '/html[1]/body[1]' },
      );
      const args2 = page2.calls[0]!.args as { maxMs: number };
      expect(args2.maxMs).toBe(60);
    });

    it('clamps quietMs into [50, 2000]', async () => {
      process.env.CLICK_STABILITY_QUIET_MS = '99999';
      const page = makeFakePage();
      await waitForTargetStable(
        page as unknown as Page,
        { kind: 'xpath', xpath: '/x' },
      );
      const args = page.calls[0]!.args as { quietMs: number };
      expect(args.quietMs).toBe(2000);

      process.env.CLICK_STABILITY_QUIET_MS = '5';
      const page2 = makeFakePage();
      await waitForTargetStable(
        page2 as unknown as Page,
        { kind: 'xpath', xpath: '/x' },
      );
      const args2 = page2.calls[0]!.args as { quietMs: number };
      expect(args2.quietMs).toBe(50);
    });

    it('clamps pixelEpsilon into [0.5, 20]', async () => {
      process.env.CLICK_STABILITY_PIXEL_EPSILON = '999';
      const page = makeFakePage();
      await waitForTargetStable(
        page as unknown as Page,
        { kind: 'xpath', xpath: '/x' },
      );
      const args = page.calls[0]!.args as { pixelEps: number };
      expect(args.pixelEps).toBe(20);

      process.env.CLICK_STABILITY_PIXEL_EPSILON = '0.01';
      const page2 = makeFakePage();
      await waitForTargetStable(
        page2 as unknown as Page,
        { kind: 'xpath', xpath: '/x' },
      );
      const args2 = page2.calls[0]!.args as { pixelEps: number };
      expect(args2.pixelEps).toBe(0.5);
    });

    it('uses defaults when no env or opts provided', async () => {
      delete process.env.CLICK_STABILITY_MAX_MS;
      delete process.env.CLICK_STABILITY_QUIET_MS;
      delete process.env.CLICK_STABILITY_PIXEL_EPSILON;
      const page = makeFakePage();
      await waitForTargetStable(
        page as unknown as Page,
        { kind: 'xpath', xpath: '/x' },
      );
      const args = page.calls[0]!.args as {
        maxMs: number; quietMs: number; pixelEps: number;
      };
      expect(args.maxMs).toBe(600);
      expect(args.quietMs).toBe(150);
      expect(args.pixelEps).toBe(2);
    });

    it('opts win over env', async () => {
      process.env.CLICK_STABILITY_MAX_MS = '600';
      const page = makeFakePage();
      await waitForTargetStable(
        page as unknown as Page,
        { kind: 'xpath', xpath: '/x' },
        { maxMs: 1200, quietMs: 300, pixelEpsilon: 4 },
      );
      const args = page.calls[0]!.args as {
        maxMs: number; quietMs: number; pixelEps: number;
      };
      expect(args.maxMs).toBe(1200);
      expect(args.quietMs).toBe(300);
      expect(args.pixelEps).toBe(4);
    });
  });

  describe('target shape passthrough', () => {
    it('passes xpath target through to evaluate', async () => {
      const page = makeFakePage();
      await waitForTargetStable(
        page as unknown as Page,
        { kind: 'xpath', xpath: '/html[1]/body[1]/div[3]' },
      );
      const args = page.calls[0]!.args as { target: { kind: string; xpath: string } };
      expect(args.target.kind).toBe('xpath');
      expect(args.target.xpath).toBe('/html[1]/body[1]/div[3]');
    });

    it('passes point target through to evaluate', async () => {
      const page = makeFakePage();
      await waitForTargetStable(
        page as unknown as Page,
        { kind: 'point', x: 320, y: 240 },
      );
      const args = page.calls[0]!.args as {
        target: { kind: string; x: number; y: number };
      };
      expect(args.target.kind).toBe('point');
      expect(args.target.x).toBe(320);
      expect(args.target.y).toBe(240);
    });
  });

  describe('result passthrough', () => {
    it('returns the evaluate result for the stable case', async () => {
      const page = makeFakePage({
        stable: true,
        lastBounds: { x: 10, y: 20, w: 100, h: 30 },
        samples: 4,
        reason: 'stable',
      });
      const result = await waitForTargetStable(
        page as unknown as Page,
        { kind: 'xpath', xpath: '/x' },
      );
      expect(result).toEqual({
        stable: true,
        lastBounds: { x: 10, y: 20, w: 100, h: 30 },
        samples: 4,
        reason: 'stable',
      });
    });

    it('returns timeout result with last bounds preserved', async () => {
      const page = makeFakePage({
        stable: false,
        lastBounds: { x: 10, y: 20, w: 100, h: 30 },
        samples: 12,
        reason: 'timeout',
      });
      const result = await waitForTargetStable(
        page as unknown as Page,
        { kind: 'xpath', xpath: '/x' },
      );
      expect(result.stable).toBe(false);
      expect(result.lastBounds).not.toBeNull();
      expect(result.reason).toBe('timeout');
    });

    it('returns mutating_environment when surrounding tree is churning', async () => {
      const page = makeFakePage({
        stable: false,
        lastBounds: { x: 50, y: 60, w: 80, h: 24 },
        samples: 12,
        reason: 'mutating_environment',
      });
      const result = await waitForTargetStable(
        page as unknown as Page,
        { kind: 'point', x: 90, y: 72 },
      );
      expect(result.reason).toBe('mutating_environment');
      expect(result.lastBounds).not.toBeNull();
    });

    it('returns no_target when target cannot be resolved', async () => {
      const page = makeFakePage({
        stable: false,
        lastBounds: null,
        samples: 0,
        reason: 'no_target',
      });
      const result = await waitForTargetStable(
        page as unknown as Page,
        { kind: 'xpath', xpath: '/missing' },
      );
      expect(result.stable).toBe(false);
      expect(result.lastBounds).toBeNull();
      expect(result.reason).toBe('no_target');
    });
  });

  describe('navigation mid-evaluate', () => {
    it('treats an evaluate exception as no_target without throwing', async () => {
      const page = makeFakePage();
      page.throwOnce = new Error('Execution context was destroyed');
      const result = await waitForTargetStable(
        page as unknown as Page,
        { kind: 'xpath', xpath: '/x' },
      );
      expect(result.stable).toBe(false);
      expect(result.lastBounds).toBeNull();
      expect(result.reason).toBe('no_target');
      // Crucial: never throw — the click pipeline depends on graceful
      // degradation through clickInBbox's Phase 1/2/3 cascade.
    });
  });
});
