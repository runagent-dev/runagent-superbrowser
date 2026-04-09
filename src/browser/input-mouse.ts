/**
 * Low-level CDP mouse input dispatch from BrowserOS.
 *
 * Uses Input.dispatchMouseEvent for precise mouse control
 * instead of puppeteer's high-level page.click() which can
 * miss elements in shadow DOM, iframes, or custom components.
 */

import type { CDPSession } from 'puppeteer-core';

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
 * Dispatch a drag operation: press at start, move to end, release.
 */
export async function dispatchDrag(
  client: CDPSession,
  startX: number,
  startY: number,
  endX: number,
  endY: number,
  options?: { steps?: number; delay?: number },
): Promise<void> {
  const steps = options?.steps || 10;
  const delay = options?.delay || 20;

  // Move to start
  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseMoved',
    x: startX,
    y: startY,
  });

  await sleep(50);

  // Press at start
  await client.send('Input.dispatchMouseEvent', {
    type: 'mousePressed',
    x: startX,
    y: startY,
    button: 'left',
    clickCount: 1,
  });

  // Move in steps to end
  for (let i = 1; i <= steps; i++) {
    const x = startX + ((endX - startX) * i) / steps;
    const y = startY + ((endY - startY) * i) / steps;
    await client.send('Input.dispatchMouseEvent', {
      type: 'mouseMoved',
      x: Math.round(x),
      y: Math.round(y),
      button: 'left',
    });
    await sleep(delay);
  }

  // Release at end
  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased',
    x: endX,
    y: endY,
    button: 'left',
    clickCount: 1,
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
