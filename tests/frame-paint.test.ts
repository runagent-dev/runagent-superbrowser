/**
 * Blank-frame detection.
 *
 * The bug these guard: nothing in the capture path ever asked whether a
 * screenshot painted. A frame caught mid-re-render — DOM committed,
 * pixels not yet — was stored, keyed and analysed like any other, and
 * because the vision cache key is DOM-derived rather than pixel-derived,
 * its empty bboxes were then re-served for the settled page.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';
import { measurePaint } from '../src/browser/frame-paint.js';

const W = 1280;
const H = 800;

/** A flat fill, as produced by a page that has not committed a frame. */
async function flat(tone: number): Promise<Buffer> {
  return sharp({
    create: { width: W, height: H, channels: 3, background: { r: tone, g: tone, b: tone } },
  }).jpeg({ quality: 70 }).toBuffer();
}

/** A flat page carrying `bars` dark stripes — stands in for real content. */
async function withContent(bars: number, barH = 24): Promise<Buffer> {
  const rects = Array.from({ length: bars }, (_, i) => ({
    input: {
      create: { width: 900, height: barH, channels: 3, background: { r: 20, g: 20, b: 20 } },
    },
    top: 40 + i * (barH + 16),
    left: 60,
  }));
  return sharp({
    create: { width: W, height: H, channels: 3, background: { r: 255, g: 255, b: 255 } },
  }).composite(rects as never).jpeg({ quality: 70 }).toBuffer();
}

describe('measurePaint', () => {
  it('calls an all-white capture blank', async () => {
    expect((await measurePaint(await flat(255))).blank).toBe(true);
  });

  it('calls the mid-grey no-frame capture blank', async () => {
    // What CDP hands back when the surface has no committed frame.
    expect((await measurePaint(await flat(128))).blank).toBe(true);
  });

  it('calls an all-black capture blank', async () => {
    expect((await measurePaint(await flat(0))).blank).toBe(true);
  });

  it('does not call a page with real content blank', async () => {
    expect((await measurePaint(await withContent(12))).blank).toBe(false);
  });

  it('does not call a sparse page blank', async () => {
    // A near-empty search page is still a page. Flagging it would cost
    // re-captures and, worse, suppress its vision caching.
    expect((await measurePaint(await withContent(2))).blank).toBe(false);
  });

  it('calls a lone spinner on white blank, not painted', async () => {
    // A spinner is what a loading page shows; it must read as blank so
    // the capture is retried rather than reasoned over.
    const spinner = await sharp({
      create: { width: W, height: H, channels: 3, background: { r: 255, g: 255, b: 255 } },
    }).composite([{
      input: {
        create: { width: 32, height: 32, channels: 3, background: { r: 60, g: 60, b: 60 } },
      },
      top: H / 2, left: W / 2,
    }] as never).jpeg({ quality: 70 }).toBuffer();
    expect((await measurePaint(spinner)).blank).toBe(true);
  });

  it('scores a gradient as painted even though it has no dominant edge', async () => {
    // Guards the choice of modal tone over mean: a gradient has a low ink
    // count against the mean but spreads across histogram buckets.
    const px = Buffer.alloc(W * H * 3);
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        const v = Math.floor((x / W) * 255);
        const o = (y * W + x) * 3;
        px[o] = v; px[o + 1] = v; px[o + 2] = v;
      }
    }
    const grad = await sharp(px, { raw: { width: W, height: H, channels: 3 } })
      .jpeg({ quality: 70 }).toBuffer();
    expect((await measurePaint(grad)).blank).toBe(false);
  });

  it('fails open on an undecodable buffer', async () => {
    // A sharp hiccup must never cost a good capture.
    expect((await measurePaint(Buffer.from('not an image'))).blank).toBe(false);
  });

  it('reports every frame painted when disabled', async () => {
    process.env.BLANK_FRAME_DISABLE = '1';
    try {
      expect((await measurePaint(await flat(255))).blank).toBe(false);
    } finally {
      delete process.env.BLANK_FRAME_DISABLE;
    }
  });
});

describe('measurePaint on a real capture', () => {
  it('calls a page that rendered only its consent bar blank', async () => {
    // Captured live from the chase.com 401(k) calculator while it was
    // serving an empty document under a full-width cookie banner — the
    // exact frame the agent was reasoning over as though it were the
    // page. The banner alone scores ~4.8% ink, ten times the blank
    // threshold, so this is the case that a global ink measure gets
    // wrong and the band-distribution test gets right.
    const fixture = fileURLToPath(
      new URL('./fixtures/blank-page-with-consent-bar.jpg', import.meta.url),
    );
    const stats = await measurePaint(readFileSync(fixture));
    expect(stats.blank).toBe(true);
    expect(stats.contentRun).toBe(1);
    // Guards the regression specifically: total ink is NOT low here.
    expect(stats.ink).toBeGreaterThan(0.04);
  });

  it('calls a synthetic header-plus-footer-only page blank', async () => {
    // Same shape as the fixture, built rather than captured, so the
    // intent survives even if the fixture is ever replaced. Two bands
    // carry ink here, so a plain band COUNT would pass this skeleton —
    // only ignoring the outer bands catches it.
    const bar = (top: number) => ({
      input: {
        create: { width: 1280, height: 60, channels: 3, background: { r: 30, g: 30, b: 30 } },
      },
      top, left: 0,
    });
    const chrome = await sharp({
      create: { width: 1280, height: 800, channels: 3, background: { r: 255, g: 255, b: 255 } },
    }).composite([bar(0), bar(740)] as never).jpeg({ quality: 70 }).toBuffer();
    const stats = await measurePaint(chrome);
    expect(stats.blank).toBe(true);
  });

  it('calls a page whose content spans the viewport painted', async () => {
    const rows = Array.from({ length: 10 }, (_, i) => ({
      input: {
        create: { width: 800, height: 20, channels: 3, background: { r: 30, g: 30, b: 30 } },
      },
      top: 30 + i * 72, left: 80,
    }));
    const full = await sharp({
      create: { width: 1280, height: 800, channels: 3, background: { r: 255, g: 255, b: 255 } },
    }).composite(rows as never).jpeg({ quality: 70 }).toBuffer();
    const stats = await measurePaint(full);
    expect(stats.blank).toBe(false);
    expect(stats.contentRun).toBeGreaterThanOrEqual(2);
  });
});
