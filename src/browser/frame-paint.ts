/**
 * Blank-frame detection and cross-origin frame paint waiting.
 *
 * Two holes this closes, both of which produce a screenshot the agent
 * then reasons over as though it were the page:
 *
 *   1. Nothing downstream ever asked whether a capture actually painted.
 *      `page.screenshot()` hands back whatever the compositor holds at
 *      that instant, and `sanitizeImageBuffer` only re-encodes it. A
 *      white frame was stored, cached and sent to the vision model
 *      exactly like a good one.
 *   2. `waitForVisualStable` is scoped to the top document — fonts,
 *      above-fold `<img>` decodes, layout-shift idle. A cross-origin
 *      iframe is an opaque replaced box to its parent: the widget's own
 *      fonts, images and hydration raise no layout shift in the parent
 *      and contribute nothing to `document.fonts`. The parent reports
 *      'stable' while the iframe is still a blank rectangle. On pages
 *      whose entire payload lives in such a frame (bank calculators
 *      served off a CDN, embedded checkout, most third-party widgets)
 *      that is the whole screenshot.
 *
 * Both helpers are pure reads and fail open: an undecodable buffer is
 * never called blank, and a frame that refuses to report is never
 * allowed to hold up the capture past its budget.
 */

import sharp from 'sharp';
import type { Frame, Page } from 'puppeteer-core';

/** Sampling grid for the blankness measure. Small on purpose: we want a
 *  tone histogram, not detail, and 96px keeps the decode under a ms. */
const SAMPLE_PX = 96;

/** Luminance distance from the modal tone before a pixel counts as ink.
 *  8 clears JPEG ringing around flat fills without swallowing real text. */
const INK_DELTA = 8;

/** Fraction of sampled pixels that must be ink for the frame to count as
 *  painted. Deliberately low (0.5%): a false "blank" only costs a bounded
 *  re-capture, whereas a missed blank poisons the vision cache. */
const DEFAULT_INK_MIN = 0.005;

/** Horizontal bands the frame is divided into for the distribution test. */
const BANDS = 10;

/** Ink fraction WITHIN a band before that band counts as carrying content. */
const BAND_INK_MIN = 0.01;

/** Consecutive content bands required before the frame counts as painted.
 *  Page chrome — a consent bar, a sticky header, a skeleton footer — is a
 *  single strip and scores a run of one wherever it sits. Rendered content
 *  flows down the viewport and scores more. */
const MIN_CONTENT_RUN = 2;

export interface PaintStats {
  /** True when the frame carries no meaningful content. */
  blank: boolean;
  /** Fraction of sampled pixels differing from the modal tone (0..1). */
  ink: number;
  /** Bands carrying more than `BAND_INK_MIN` ink. */
  contentBands: number;
  /** Longest run of CONSECUTIVE content bands. The discriminating
   *  statistic: chrome scores 1, rendered content scores more. */
  contentRun: number;
  /** Luminance standard deviation across the grid. Diagnostic only. */
  stdev: number;
}

function inkMin(): number {
  const raw = parseFloat(process.env.BLANK_FRAME_INK_MIN || '');
  return Number.isFinite(raw) && raw > 0 && raw < 1 ? raw : DEFAULT_INK_MIN;
}

/**
 * Decide whether a screenshot buffer actually painted anything.
 *
 * Two tests, either of which condemns the frame.
 *
 * Total ink, measured against the MODAL tone rather than the mean: a page
 * mid-teardown is one flat colour (white during hydration, mid-grey when
 * the compositor has no frame for the surface), so almost every pixel
 * lands in one histogram bucket and `ink` collapses toward zero. A
 * gradient, which has little ink against the *mean*, scores high here
 * because no single bucket dominates.
 *
 * Ink DISTRIBUTION, because total ink alone is not enough and says so
 * loudly on real pages. The observed case: chase.com served its consent
 * bar and nothing else — an empty document under a full-width banner
 * pinned to the bottom. That banner alone scored 4.8% ink, ten times the
 * blank threshold, so a purely global measure called an empty page
 * painted. Page chrome is the general shape of this: a cookie bar, a
 * sticky header, a skeleton footer. All of it lives at the top or the
 * bottom of the viewport, so the frame is split into horizontal bands
 * and only the INTERIOR ones are allowed to vouch for the page. A
 * skeleton with both a header and a footer still reads as blank, which
 * a simple "how many bands have ink" count gets wrong.
 *
 * The two tests are complementary and both are needed: the distribution
 * test clears a lone consent bar, and the total-ink test clears a
 * centred loading spinner, which sits in an interior band and would
 * otherwise vouch for an empty page.
 *
 * `BLANK_FRAME_DISABLE=1` forces every frame to report painted.
 */
export async function measurePaint(buf: Buffer): Promise<PaintStats> {
  if (process.env.BLANK_FRAME_DISABLE === '1') {
    return { blank: false, ink: 1, contentBands: BANDS, contentRun: BANDS, stdev: 255 };
  }
  try {
    const { data, info } = await sharp(buf)
      .greyscale()
      .resize(SAMPLE_PX, SAMPLE_PX, { fit: 'inside' })
      .raw()
      .toBuffer({ resolveWithObject: true });
    const { width, height } = info;
    const n = width * height;
    if (!n) return { blank: false, ink: 1, contentBands: BANDS, contentRun: BANDS, stdev: 255 };

    // 32-bucket luminance histogram → modal tone.
    const hist = new Uint32Array(32);
    for (let i = 0; i < n; i++) hist[data[i] >> 3]++;
    let mode = 0;
    for (let b = 1; b < 32; b++) if (hist[b] > hist[mode]) mode = b;
    const modeTone = (mode << 3) + 4;

    const bandInk = new Uint32Array(BANDS);
    const bandPx = new Uint32Array(BANDS);
    let ink = 0;
    let sum = 0;
    let sumSq = 0;
    for (let y = 0; y < height; y++) {
      // Proportional, not `y / ceil(height / BANDS)`: at a sampled
      // height of 82 the latter yields nine bands of nine rows and a
      // tenth of one, so the bottom tenth of the page lands in band 8
      // and reads as interior. That mis-scored the very capture this
      // was written against.
      const band = Math.min(BANDS - 1, Math.floor((y * BANDS) / height));
      bandPx[band] += width;
      const row = y * width;
      for (let x = 0; x < width; x++) {
        const v = data[row + x];
        sum += v;
        sumSq += v * v;
        if (Math.abs(v - modeTone) > INK_DELTA) {
          ink++;
          bandInk[band]++;
        }
      }
    }
    const mean = sum / n;
    const stdev = Math.sqrt(Math.max(0, sumSq / n - mean * mean));
    const inkFrac = ink / n;

    let contentBands = 0;
    let contentRun = 0;
    let run = 0;
    for (let b = 0; b < BANDS; b++) {
      const px = bandPx[b];
      if (px > 0 && bandInk[b] / px > BAND_INK_MIN) {
        contentBands++;
        run++;
        if (run > contentRun) contentRun = run;
      } else {
        run = 0;
      }
    }

    const blank = inkFrac < inkMin() || contentRun < MIN_CONTENT_RUN;
    return { blank, ink: inkFrac, contentBands, contentRun, stdev };
  } catch {
    // A buffer we cannot decode is not a buffer we should discard — a
    // sharp hiccup must never cost a good capture.
    return { blank: false, ink: 1, contentBands: BANDS, contentRun: BANDS, stdev: 255 };
  }
}

/** Frames below this on either side are pixels, beacons and consent
 *  shims — never the content, and frequently never "ready". */
const MIN_FRAME_PX = 120;

interface FrameProbe {
  ready: boolean;
  w: number;
  h: number;
}

async function probeFrame(frame: Frame): Promise<FrameProbe | null> {
  try {
    return await frame.evaluate(() => {
      const w = window.innerWidth || 0;
      const h = window.innerHeight || 0;
      const body = document.body;
      // "Painted" for our purposes means the document finished loading
      // AND committed something renderable. A frame parked on
      // about:blank reports complete with an empty body, which is the
      // exact state we are waiting out.
      const hasContent = !!body
        && (body.innerText.trim().length > 0 || body.childElementCount > 0);
      return { ready: document.readyState === 'complete' && hasContent, w, h };
    }) as FrameProbe;
  } catch {
    // Cross-origin frames mid-navigation throw on evaluate. Treat an
    // unreachable frame as not-our-problem rather than blocking on it.
    return null;
  }
}

/**
 * Wait until every content-sized child frame has loaded and committed
 * something, or until the budget runs out.
 *
 * Returns 'none' when the page has no qualifying child frames, which is
 * the common case — that path costs one `page.frames()` read and a
 * single probe round, so ordinary pages pay essentially nothing for
 * this. Only genuinely iframe-hosted pages spend the budget.
 *
 * Configurable via env:
 *   VISUAL_STABLE_FRAME_MS (default 4000) — budget for this step
 *   VISUAL_STABLE_FRAME_DISABLE=1         — skip entirely
 */
export async function waitForFramesPainted(
  page: Page,
  maxMsArg?: number,
): Promise<'painted' | 'timeout' | 'none'> {
  if (process.env.VISUAL_STABLE_FRAME_DISABLE === '1') return 'none';
  const envMax = parseInt(process.env.VISUAL_STABLE_FRAME_MS || '', 10);
  const maxMs = Math.max(
    0,
    Math.min(30000, maxMsArg ?? (Number.isFinite(envMax) ? envMax : 4000)),
  );
  if (maxMs === 0) return 'none';

  const deadline = Date.now() + maxMs;
  let sawCandidate = false;

  while (Date.now() < deadline) {
    let children: Frame[];
    try {
      children = page.frames().filter((f) => f !== page.mainFrame() && !f.isDetached());
    } catch {
      return sawCandidate ? 'timeout' : 'none';
    }
    if (children.length === 0) return sawCandidate ? 'painted' : 'none';

    const probes = await Promise.all(children.map((f) => probeFrame(f)));
    let pending = 0;
    for (const p of probes) {
      if (!p) continue;
      if (p.w < MIN_FRAME_PX || p.h < MIN_FRAME_PX) continue;
      sawCandidate = true;
      if (!p.ready) pending++;
    }
    if (pending === 0) return sawCandidate ? 'painted' : 'none';
    await new Promise((r) => setTimeout(r, 120));
  }
  return sawCandidate ? 'timeout' : 'none';
}
