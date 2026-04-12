/**
 * Low-level CDP mouse input dispatch from BrowserOS.
 *
 * Uses Input.dispatchMouseEvent for precise mouse control
 * instead of puppeteer's high-level page.click() which can
 * miss elements in shadow DOM, iframes, or custom components.
 */

import type { CDPSession } from 'puppeteer-core';
import { humanDrag } from './humanize.js';

/** Modifier key bitmask (from BrowserOS keyboard.ts). */
export const Modifiers = {
  Alt: 1,
  Control: 2,
  Meta: 4,
  Shift: 8,
} as const;

export type MouseButton = 'left' | 'right' | 'middle';

/**
 * Dispatch a full click sequence via CDP: moveTo → press → release.
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
  },
): Promise<void> {
  const button = options?.button || 'left';
  const clickCount = options?.clickCount || 1;
  const modifiers = options?.modifiers || 0;
  const delay = options?.delay || 50;

  // Move to position
  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseMoved',
    x,
    y,
    modifiers,
  });

  await sleep(delay);

  // Press
  await client.send('Input.dispatchMouseEvent', {
    type: 'mousePressed',
    x,
    y,
    button,
    clickCount,
    modifiers,
  });

  await sleep(delay);

  // Release
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
  },
): Promise<void> {
  if (!options?.linear) {
    await humanDrag(client, startX, startY, endX, endY, {
      steps: options?.steps,
      overshoot: options?.overshoot,
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
 */
export async function dispatchScroll(
  client: CDPSession,
  x: number,
  y: number,
  deltaX: number,
  deltaY: number,
): Promise<void> {
  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseWheel',
    x,
    y,
    deltaX,
    deltaY,
  });
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
