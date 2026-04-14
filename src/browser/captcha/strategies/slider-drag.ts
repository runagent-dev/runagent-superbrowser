/**
 * Slider captcha solver with vision feedback loop.
 *
 * The common failure mode of vision-based slider solvers is that the LLM
 * estimates target X coords off by 20-50px. Instead of hoping for a
 * one-shot win, this strategy:
 *
 *   1. Locate handle + target — DOM first (known selector classes), vision
 *      fallback if DOM hints aren't present.
 *   2. Execute a primary humanDrag (Bezier + sigmoid + release dwell).
 *   3. Screenshot after drag. Ask LLM: "aligned? if not, by how many px?"
 *   4. If off by <=30px, nudge-drag the handle by that delta (fine trim).
 *   5. Repeat up to 3 rounds. Fall through to 2captcha slider API if all
 *      nudges fail.
 *
 * VisionMemory records each attempt so the LLM doesn't repeat a known-bad
 * target estimate.
 */

import type { Page } from 'puppeteer-core';
import type { CaptchaInfo } from '../../captcha.js';
import { humanDrag } from '../../humanize.js';
import {
  clampToRect,
  parseSliderCoordsStrict,
  strictJson,
  type Rect,
} from '../validation.js';
import type { CaptchaStrategy, RichSolveResult, StrategyContext } from '../types.js';

const MAX_ROUNDS = 3;
const NUDGE_MAX_PX = 30;

interface Coords {
  handle: { x: number; y: number };
  target: { x: number; y: number };
}

const HANDLE_SELECTORS = [
  '[class*="slider-handle"]',
  '[class*="slider__handle"]',
  '[class*="slider-btn"]',
  '[class*="drag-handle"]',
  '[class*="captcha-slider"]',
  '.geetest_slider_button',
];
const TRACK_SELECTORS = [
  '[class*="slider-track"]',
  '[class*="slider-rail"]',
  '[class*="slider-bg"]',
  '.geetest_slider_track',
  '[class*="verify-track"]',
];

/** DOM-side lookup for common slider handle/track classes. */
async function findHandleAndTargetViaDom(page: Page): Promise<Coords | null> {
  return page.evaluate((handleSelectors: string[], trackSelectors: string[]) => {
    const handle = handleSelectors
      .map((s) => document.querySelector(s) as HTMLElement | null)
      .find((el) => el && el.getBoundingClientRect().width > 10);
    const track = trackSelectors
      .map((s) => document.querySelector(s) as HTMLElement | null)
      .find((el) => el && el.getBoundingClientRect().width > 30);
    if (!handle || !track) return null;
    const hRect = handle.getBoundingClientRect();
    const tRect = track.getBoundingClientRect();
    return {
      handle: {
        x: Math.round(hRect.left + hRect.width / 2),
        y: Math.round(hRect.top + hRect.height / 2),
      },
      // Default target: rightmost end of the track, minus half handle width
      // so the handle lands within the track.
      target: {
        x: Math.round(tRect.right - hRect.width / 2 - 2),
        y: Math.round(hRect.top + hRect.height / 2),
      },
    };
  }, HANDLE_SELECTORS, TRACK_SELECTORS);
}

/** DOM-truth read of the current handle center after a drag. Returns null
 *  when the handle isn't found (rare for sites where selectors matched
 *  before but the widget re-rendered). */
async function readHandleCenterViaDom(page: Page): Promise<{ x: number; y: number } | null> {
  return page.evaluate((handleSelectors: string[]) => {
    const handle = handleSelectors
      .map((s) => document.querySelector(s) as HTMLElement | null)
      .find((el) => el && el.getBoundingClientRect().width > 10);
    if (!handle) return null;
    const r = handle.getBoundingClientRect();
    return {
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
    };
  }, HANDLE_SELECTORS);
}

/** Find the slider widget's rect for screenshot cropping & coord clamping. */
async function findSliderRect(page: Page): Promise<Rect | null> {
  return page.evaluate((trackSelectors: string[]) => {
    const sliderSelectors = [
      ...trackSelectors,
      '[class*="slide-verify" i]',
      '[class*="slider" i][class*="captcha" i]',
      '[class*="geetest" i]',
    ];
    for (const sel of sliderSelectors) {
      const el = document.querySelector(sel) as HTMLElement | null;
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 30) continue;
      // Expand vertically to include the puzzle image above the track.
      const y = Math.max(0, Math.round(r.top - 220));
      const x = Math.max(0, Math.round(r.left - 20));
      const right = Math.min(window.innerWidth, Math.round(r.right + 20));
      const bottom = Math.min(window.innerHeight, Math.round(r.bottom + 20));
      return { x, y, width: right - x, height: bottom - y };
    }
    return null;
  }, TRACK_SELECTORS);
}

/**
 * Vision fallback: ask LLM to identify handle + target coordinates.
 *
 * Screenshot is cropped to the slider rect when one can be found — full-page
 * screenshots cause vision LLMs to confabulate non-slider page elements as
 * the handle. Returned coords are clamped to the rect; out-of-rect coords
 * are treated as hallucination and the call is retried once.
 */
async function findHandleAndTargetViaVision(
  ctx: StrategyContext,
  rect: Rect | null,
): Promise<Coords | null> {
  if (!ctx.llm) return null;
  const clipOpt = rect ? { clip: rect } : {};
  const buffer = await ctx.page.screenshot({ type: 'jpeg', quality: 80, fullPage: false, ...clipOpt });
  const b64 = Buffer.from(buffer).toString('base64');
  ctx.memory.setChallengeHash(
    (await import('../vision-memory.js'))
      .VisionMemory.hashChallenge(
        'slider_find',
        b64,
        rect ? `r=${rect.x},${rect.y},${rect.width},${rect.height}` : 'fullpage',
      ),
  );

  const rectHint = rect
    ? `The screenshot is cropped to the slider widget. Coordinates are absolute viewport pixels: ` +
      `the widget occupies x=${rect.x}..${rect.x + rect.width}, y=${rect.y}..${rect.y + rect.height}. ` +
      `Both handle and target MUST be inside that range.`
    : 'Coordinates are absolute viewport pixels.';

  const prompt =
    'This is a slider captcha. Return a JSON object containing ONLY these four fields: ' +
    '{"handleX": <int>, "handleY": <int>, "targetX": <int>, "targetY": <int>}. ' +
    'Handle center and the target gap/notch the handle must land on. ' +
    rectHint + ' Do not include any other fields or prose. ' +
    ctx.memory.toPromptFragment();

  try {
    const resp = await ctx.llm.chat([
      {
        role: 'user',
        content: [
          { type: 'text', text: prompt },
          { type: 'image_url', image_url: { url: `data:image/jpeg;base64,${b64}` } },
        ],
      },
    ], { temperature: 0.1, maxTokens: 256, responseFormat: 'json_object' });
    const parsed = parseSliderCoordsStrict(resp.content);
    if (!parsed) return null;
    if (rect) {
      const h = clampToRect(parsed.handleX, parsed.handleY, rect);
      const t = clampToRect(parsed.targetX, parsed.targetY, rect);
      if (!h || !t) return null;
      return { handle: { x: h.x, y: h.y }, target: { x: t.x, y: t.y } };
    }
    return {
      handle: { x: Math.round(parsed.handleX), y: Math.round(parsed.handleY) },
      target: { x: Math.round(parsed.targetX), y: Math.round(parsed.targetY) },
    };
  } catch {
    return null;
  }
}

/**
 * Ask the LLM whether the slider is aligned after the drag, and if not
 * return the px offset (positive = need to move further right).
 *
 * Cropped to the slider rect to reduce hallucination. Strict JSON shape
 * check via strictJson — the previous lenient regex accepted any JSON-ish
 * substring, including nested braces inside reasoning prose.
 */
async function measureAlignment(
  ctx: StrategyContext,
  rect: Rect | null,
): Promise<{ aligned: boolean; offsetPx: number }> {
  if (!ctx.llm) return { aligned: false, offsetPx: 0 };
  const clipOpt = rect ? { clip: rect } : {};
  const buffer = await ctx.page.screenshot({ type: 'jpeg', quality: 80, ...clipOpt });
  const b64 = Buffer.from(buffer).toString('base64');
  const prompt =
    'Look at the slider captcha in this screenshot. Is the handle aligned with the target gap/notch? ' +
    'Respond with a JSON object containing ONLY these two fields: ' +
    '{"aligned": <bool>, "offsetPx": <signed int>}. ' +
    'offsetPx is how many pixels the handle needs to move to the right (negative = move left). ' +
    'If already aligned, offsetPx=0. Do not include any other fields or prose.';
  try {
    const resp = await ctx.llm.chat([
      {
        role: 'user',
        content: [
          { type: 'text', text: prompt },
          { type: 'image_url', image_url: { url: `data:image/jpeg;base64,${b64}` } },
        ],
      },
    ], { temperature: 0.1, maxTokens: 128, responseFormat: 'json_object' });
    const parsed = strictJson<{ aligned?: unknown; offsetPx?: unknown }>(resp.content);
    if (!parsed || typeof parsed.aligned !== 'boolean') {
      return { aligned: false, offsetPx: 0 };
    }
    const off = typeof parsed.offsetPx === 'number' && Number.isFinite(parsed.offsetPx)
      ? Math.round(parsed.offsetPx)
      : 0;
    return { aligned: parsed.aligned, offsetPx: off };
  } catch {
    return { aligned: false, offsetPx: 0 };
  }
}

/** Has the page navigated past the captcha? */
async function captchaGone(page: Page): Promise<boolean> {
  const { detectCaptcha } = await import('../../captcha.js');
  const info = await detectCaptcha(page);
  return !info || info.solved;
}

export const sliderDragStrategy: CaptchaStrategy = {
  name: 'slider_drag_feedback',
  supportedTypes: ['slider'],
  // Below the cheap reCAPTCHA-checkbox path and external token solvers,
  // but the only realistic option for sliders. DOM-truth nudges keep the
  // success rate above the vision-only fallback.
  priority: 70,
  estimatedCostCents: 6, // 1 vision call for find + up to 3 alignment checks
  requiresLLM: false, // DOM path works without LLM; vision fallback needs one
  requiresApiKey: false,

  canHandle(info: CaptchaInfo): boolean {
    return info.type === 'slider';
  },

  async run(info: CaptchaInfo, ctx: StrategyContext): Promise<RichSolveResult> {
    const start = Date.now();
    const trace: NonNullable<RichSolveResult['visionTrace']> = [];
    const sliderRect = await findSliderRect(ctx.page);

    // Step 1: find coordinates — DOM first (ground truth, no LLM needed).
    let coords = await findHandleAndTargetViaDom(ctx.page);
    let foundVia: 'dom' | 'vision' = 'dom';
    if (!coords && ctx.llm) {
      coords = await findHandleAndTargetViaVision(ctx, sliderRect);
      foundVia = 'vision';
    }
    if (!coords) {
      return {
        solved: false,
        method: 'slider_drag_feedback',
        attempts: 0,
        error: 'could not locate slider handle or target',
        durationMs: Date.now() - start,
      };
    }
    trace.push({ round: 0, action: { phase: 'locate', via: foundVia, coords } });

    // Step 2: primary drag.
    const client = await ctx.page.createCDPSession();
    let curX = coords.handle.x;
    const y = coords.handle.y;
    await humanDrag(client, curX, y, coords.target.x, coords.target.y, { overshoot: false });
    ctx.memory.recordDrag({
      from: coords.handle,
      to: coords.target,
      result: 'unknown',
    });
    curX = coords.target.x;

    // Step 3: verify + nudge rounds. Each round prefers DOM ground truth
    // for handle position over the LLM's self-report — the LLM has been
    // observed claiming "aligned" while the handle visibly sat 40px short.
    for (let round = 1; round <= MAX_ROUNDS; round++) {
      if (await captchaGone(ctx.page)) {
        return {
          solved: true,
          method: 'slider_drag_feedback',
          subMethod: `${foundVia}_rounds=${round}`,
          attempts: round,
          totalRounds: round,
          durationMs: Date.now() - start,
          visionTrace: trace,
        };
      }

      // DOM ground truth: where did the handle ACTUALLY end up?
      const actualHandle = await readHandleCenterViaDom(ctx.page);
      let offsetPx: number;
      let aligned: boolean;
      if (actualHandle) {
        // Compute offset against the original target — no LLM call needed.
        offsetPx = coords.target.x - actualHandle.x;
        aligned = Math.abs(offsetPx) <= 4;
        trace.push({ round, action: { phase: 'dom-measure', actualHandle, target: coords.target, offsetPx } });
        // Keep curX honest with where the handle really is.
        curX = actualHandle.x;
      } else {
        // No DOM handle — fall back to LLM measure.
        const m = await measureAlignment(ctx, sliderRect);
        offsetPx = m.offsetPx;
        aligned = m.aligned;
        trace.push({ round, action: { phase: 'vision-measure', aligned, offsetPx } });
      }

      if (aligned) {
        // Sometimes aligned but the widget hasn't yet emitted success —
        // give the page one more tick before reporting solved.
        await new Promise((r) => setTimeout(r, 600));
        if (await captchaGone(ctx.page)) {
          return {
            solved: true,
            method: 'slider_drag_feedback',
            subMethod: `${foundVia}_aligned_no_trigger`,
            attempts: round,
            totalRounds: round,
            durationMs: Date.now() - start,
            visionTrace: trace,
          };
        }
      }

      if (Math.abs(offsetPx) > NUDGE_MAX_PX) {
        ctx.memory.recordRejected(
          `drag to x=${curX}`,
          `alignment off by ${offsetPx}px (> nudge limit ${NUDGE_MAX_PX}px)`,
        );
        break;
      }

      if (offsetPx === 0 && !aligned) {
        // Measure said aligned=false but offset=0 — give up, try next strategy.
        break;
      }
      if (offsetPx === 0) break;

      let newX = curX + offsetPx;
      // Clamp the nudge target to the slider rect so a hallucinated
      // offsetPx can't drag the handle off the widget.
      if (sliderRect) {
        const c = clampToRect(newX, y, sliderRect);
        if (c) newX = c.x;
      }
      await humanDrag(client, curX, y, newX, y, {
        pressDwellMs: 100,
        releaseDwellMs: 80,
      });
      ctx.memory.recordDrag({
        from: { x: curX, y },
        to: { x: newX, y },
        result: 'unknown',
        offsetPx,
      });
      curX = newX;
    }

    return {
      solved: false,
      method: 'slider_drag_feedback',
      subMethod: `${foundVia}_feedback_exhausted`,
      attempts: MAX_ROUNDS,
      totalRounds: MAX_ROUNDS,
      durationMs: Date.now() - start,
      error: 'slider not aligned after feedback rounds',
      visionTrace: trace,
    };
  },
};
