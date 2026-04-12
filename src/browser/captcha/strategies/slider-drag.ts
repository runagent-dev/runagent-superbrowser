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
import type { CaptchaStrategy, RichSolveResult, StrategyContext } from '../types.js';

const MAX_ROUNDS = 3;
const NUDGE_MAX_PX = 30;

interface Coords {
  handle: { x: number; y: number };
  target: { x: number; y: number };
}

/** DOM-side lookup for common slider handle/track classes. */
async function findHandleAndTargetViaDom(page: Page): Promise<Coords | null> {
  return page.evaluate(() => {
    const handleSelectors = [
      '[class*="slider-handle"]',
      '[class*="slider__handle"]',
      '[class*="slider-btn"]',
      '[class*="drag-handle"]',
      '[class*="captcha-slider"]',
      '.geetest_slider_button',
    ];
    const trackSelectors = [
      '[class*="slider-track"]',
      '[class*="slider-rail"]',
      '[class*="slider-bg"]',
      '.geetest_slider_track',
      '[class*="verify-track"]',
    ];
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
  });
}

/** Vision fallback: ask LLM to identify handle + target coordinates from screenshot. */
async function findHandleAndTargetViaVision(
  ctx: StrategyContext,
): Promise<Coords | null> {
  if (!ctx.llm) return null;
  const buffer = await ctx.page.screenshot({ type: 'jpeg', quality: 80, fullPage: false });
  const b64 = Buffer.from(buffer).toString('base64');
  ctx.memory.setChallengeHash(
    (await import('../vision-memory.js'))
      .VisionMemory.hashChallenge('slider_find', b64),
  );

  const prompt =
    'This is a slider captcha. Return ONLY a JSON object of the form ' +
    '{"handleX":<int>,"handleY":<int>,"targetX":<int>,"targetY":<int>} ' +
    'giving viewport pixel coordinates of the handle center and the target gap/notch ' +
    'that the handle should land on. ' +
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
    ], { temperature: 0.1, maxTokens: 256 });
    const text = resp.content;
    const match = text.match(/\{[^}]*"handleX"[^}]*\}/);
    if (!match) return null;
    const parsed = JSON.parse(match[0]);
    if (typeof parsed.handleX !== 'number' || typeof parsed.targetX !== 'number') return null;
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
 */
async function measureAlignment(ctx: StrategyContext): Promise<{ aligned: boolean; offsetPx: number }> {
  if (!ctx.llm) return { aligned: false, offsetPx: 0 };
  const buffer = await ctx.page.screenshot({ type: 'jpeg', quality: 80 });
  const b64 = Buffer.from(buffer).toString('base64');
  const prompt =
    'Look at the slider captcha in this screenshot. Is the handle aligned with the target gap/notch? ' +
    'Respond ONLY with JSON: {"aligned":true|false,"offsetPx":<signed int>} where offsetPx is how ' +
    'many pixels the handle needs to move to the right (negative = move left). If already aligned, offsetPx=0.';
  try {
    const resp = await ctx.llm.chat([
      {
        role: 'user',
        content: [
          { type: 'text', text: prompt },
          { type: 'image_url', image_url: { url: `data:image/jpeg;base64,${b64}` } },
        ],
      },
    ], { temperature: 0.1, maxTokens: 128 });
    const text = resp.content;
    const match = text.match(/\{[^}]*"aligned"[^}]*\}/);
    if (!match) return { aligned: false, offsetPx: 0 };
    const parsed = JSON.parse(match[0]);
    return {
      aligned: Boolean(parsed.aligned),
      offsetPx: Math.round(Number(parsed.offsetPx) || 0),
    };
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
  priority: 80,
  estimatedCostCents: 6, // 1 vision call for find + up to 3 alignment checks
  requiresLLM: false, // DOM path works without LLM; vision fallback needs one
  requiresApiKey: false,

  canHandle(info: CaptchaInfo): boolean {
    return info.type === 'slider';
  },

  async run(info: CaptchaInfo, ctx: StrategyContext): Promise<RichSolveResult> {
    const start = Date.now();
    const trace: NonNullable<RichSolveResult['visionTrace']> = [];

    // Step 1: find coordinates — DOM first
    let coords = await findHandleAndTargetViaDom(ctx.page);
    let foundVia: 'dom' | 'vision' = 'dom';
    if (!coords && ctx.llm) {
      coords = await findHandleAndTargetViaVision(ctx);
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

    // Step 2: primary drag
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

    // Step 3: verify + nudge rounds
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

      const { aligned, offsetPx } = await measureAlignment(ctx);
      trace.push({ round, action: { phase: 'measure', aligned, offsetPx } });

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
        // Gross mis-estimate — don't thrash the slider with huge corrections.
        ctx.memory.recordRejected(
          `drag to x=${curX}`,
          `alignment measurement reported ${offsetPx}px off (> nudge limit ${NUDGE_MAX_PX}px)`,
        );
        break;
      }

      if (offsetPx === 0) {
        // LLM said aligned but captcha still present — give up, try next strategy.
        break;
      }

      const newX = curX + offsetPx;
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
