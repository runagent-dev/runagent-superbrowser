/**
 * Low-level CDP mouse input dispatch from BrowserOS.
 *
 * Uses Input.dispatchMouseEvent for precise mouse control
 * instead of puppeteer's high-level page.click() which can
 * miss elements in shadow DOM, iframes, or custom components.
 */

import type { CDPSession } from 'puppeteer-core';
import { humanClick, humanDrag, humanScroll } from './humanize.js';

/** Modifier key bitmask (from BrowserOS keyboard.ts). */
export const Modifiers = {
  Alt: 1,
  Control: 2,
  Meta: 4,
  Shift: 8,
} as const;

export type MouseButton = 'left' | 'right' | 'middle';

/** One-shot latch so the linear-mode-ignored warning doesn't spam every click. */
let warnedLinearModeIgnored = false;

/**
 * Dispatch a full click sequence via CDP.
 *
 * Defaults to humanClick (Bezier mouse path + pre-click hesitation + 1-2px
 * jitter + human-duration hold). Bot-detection scripts that hash mouse-move
 * patterns flag straight-teleport clicks; the humanized path is the correct
 * default.
 *
 * Set `{ linear: true }` to opt out for cases where determinism matters:
 *  - Captcha grid tile clicks that rely on server-mapped pixel centers.
 *  - Multi-click sequences (clickCount>1) where jitter would miss the target.
 *  - Modifier-key chords (Ctrl+click, Shift+click) that `humanClick` doesn't
 *    currently forward.
 *
 * When `linear` is set, or when `clickCount>1` / `modifiers` are present,
 * we fall back to the deterministic teleport-click for correctness.
 */
export async function dispatchClick(
  client: CDPSession,
  x: number,
  y: number,
  options?: {
    button?: MouseButton;
    clickCount?: number;
    modifiers?: number;
    delay?: number;
    /** Opt out of humanization (deterministic teleport click). */
    linear?: boolean;
    /** Session ID for broadcasting to live view. */
    sessionId?: string;
  },
): Promise<void> {
  const button = options?.button || 'left';
  const clickCount = options?.clickCount || 1;
  const modifiers = options?.modifiers || 0;
  const delay = options?.delay || 50;

  // humanClick doesn't forward modifiers or clickCount>1. Fall back to the
  // deterministic path when those are needed or when the caller opted out.
  // Env escape hatch: SUPERBROWSER_CLICK_MODE=linear kills humanization
  // for the process — useful during vision-loop debugging. Restricted to
  // NODE_ENV=test so a prod config can't silently disable stealth; outside
  // tests we warn once and ignore.
  const envLinear =
    process.env.SUPERBROWSER_CLICK_MODE === 'linear'
    && process.env.NODE_ENV === 'test';
  if (
    process.env.SUPERBROWSER_CLICK_MODE === 'linear'
    && process.env.NODE_ENV !== 'test'
    && !warnedLinearModeIgnored
  ) {
    console.warn(
      '[input-mouse] SUPERBROWSER_CLICK_MODE=linear ignored: only honored '
      + 'when NODE_ENV=test. Humanized click path stays active to avoid '
      + 'bot-detection flags. Unset the env var to silence this warning.',
    );
    warnedLinearModeIgnored = true;
  }
  const useHumanized = !options?.linear && !envLinear
    && clickCount === 1 && modifiers === 0;

  if (useHumanized) {
    await humanClick(client, x, y, { button, sessionId: options?.sessionId });
    return;
  }

  // Deterministic teleport click — for chord-clicks, double-clicks, or
  // internal automation paths that opt out via { linear: true }.
  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseMoved',
    x,
    y,
    modifiers,
  });

  await sleep(delay);

  await client.send('Input.dispatchMouseEvent', {
    type: 'mousePressed',
    x,
    y,
    button,
    clickCount,
    modifiers,
  });

  await sleep(delay);

  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased',
    x,
    y,
    button,
    clickCount,
    modifiers,
  });
}

/**
 * Dispatch a hover (mouseMoved without click).
 */
export async function dispatchHover(
  client: CDPSession,
  x: number,
  y: number,
): Promise<void> {
  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseMoved',
    x,
    y,
  });
}

/**
 * Dispatch a drag operation from start to end coordinates.
 *
 * Delegates to humanDrag() by default (Bezier path, sigmoid velocity, dwell,
 * optional overshoot, micro-pauses). Bot-detection scripts on slider/drag
 * captchas reject straight-line uniform drags, so the humanized path is the
 * correct default. Set { linear: true } to opt out in contexts where the
 * old straight-line behavior is needed (e.g., internal UI automation).
 */
export async function dispatchDrag(
  client: CDPSession,
  startX: number,
  startY: number,
  endX: number,
  endY: number,
  options?: {
    steps?: number;
    delay?: number;
    overshoot?: boolean;
    /** Opt out of humanization for internal (non-stealth-critical) contexts. */
    linear?: boolean;
    /** Session ID for broadcasting to live view. */
    sessionId?: string;
  },
): Promise<void> {
  if (!options?.linear) {
    await humanDrag(client, startX, startY, endX, endY, {
      steps: options?.steps,
      overshoot: options?.overshoot,
      sessionId: options?.sessionId,
    });
    return;
  }

  const steps = options.steps || 10;
  const delay = options.delay || 20;

  // Legacy straight-line path — preserved for opt-in callers only.
  await client.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: startX, y: startY });
  await sleep(50);
  await client.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: startX, y: startY, button: 'left', clickCount: 1,
  });

  for (let i = 1; i <= steps; i++) {
    const x = startX + ((endX - startX) * i) / steps;
    const y = startY + ((endY - startY) * i) / steps;
    await client.send('Input.dispatchMouseEvent', {
      type: 'mouseMoved', x: Math.round(x), y: Math.round(y), button: 'left',
    });
    await sleep(delay);
  }

  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: endX, y: endY, button: 'left', clickCount: 1,
  });
}

/**
 * Dispatch a mouse wheel scroll.
 *
 * Splits a single logical scroll into 3-4 smaller wheel events with gaussian
 * spacing so timing variance resembles a real trackpad / wheel turn. Single
 * large mouseWheel events with zero inter-event delay are a canonical bot
 * signature. Horizontal-scroll (deltaX) falls back to the straight-line
 * path since humanScroll currently only models vertical cadence.
 *
 * Set `{ linear: true }` to opt out — keeps determinism for internal
 * automation that doesn't need humanization.
 */
export async function dispatchScroll(
  client: CDPSession,
  x: number,
  y: number,
  deltaX: number,
  deltaY: number,
  options?: { linear?: boolean },
): Promise<void> {
  if (options?.linear || deltaX !== 0 || deltaY === 0) {
    await client.send('Input.dispatchMouseEvent', {
      type: 'mouseWheel',
      x,
      y,
      deltaX,
      deltaY,
    });
    return;
  }
  await humanScroll(client, x, y, deltaY);
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
