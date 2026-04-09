/**
 * Mouse and keyboard humanization.
 *
 * Makes browser interactions look more human-like to avoid bot detection:
 * - Bezier curve mouse movements instead of instant teleport
 * - Random delays between keystrokes
 * - Random micro-pauses during interactions
 * - Slight coordinate jitter
 */

import type { CDPSession } from 'puppeteer-core';

/**
 * Move the mouse along a Bezier curve from current position to target.
 * Much more human-like than instant teleport.
 */
export async function humanMouseMove(
  client: CDPSession,
  fromX: number,
  fromY: number,
  toX: number,
  toY: number,
  steps?: number,
): Promise<void> {
  const numSteps = steps || Math.max(10, Math.round(distance(fromX, fromY, toX, toY) / 20));

  // Generate Bezier control points for natural curve
  const cp1x = fromX + (toX - fromX) * 0.25 + randomOffset(30);
  const cp1y = fromY + (toY - fromY) * 0.1 + randomOffset(30);
  const cp2x = fromX + (toX - fromX) * 0.75 + randomOffset(20);
  const cp2y = fromY + (toY - fromY) * 0.9 + randomOffset(20);

  for (let i = 0; i <= numSteps; i++) {
    const t = i / numSteps;
    const point = cubicBezier(t, fromX, fromY, cp1x, cp1y, cp2x, cp2y, toX, toY);

    await client.send('Input.dispatchMouseEvent', {
      type: 'mouseMoved',
      x: Math.round(point.x),
      y: Math.round(point.y),
    });

    // Variable speed: slower at start and end, faster in middle
    const speed = Math.sin(t * Math.PI);
    const delay = Math.max(2, Math.round((1 - speed * 0.7) * 12 + Math.random() * 5));
    await sleep(delay);
  }
}

/**
 * Click with human-like mouse movement to the target first.
 */
export async function humanClick(
  client: CDPSession,
  x: number,
  y: number,
  options?: {
    fromX?: number;
    fromY?: number;
    button?: 'left' | 'right' | 'middle';
  },
): Promise<void> {
  const fromX = options?.fromX ?? x + randomOffset(200);
  const fromY = options?.fromY ?? y + randomOffset(150);
  const button = options?.button || 'left';

  // Move mouse naturally
  await humanMouseMove(client, fromX, fromY, x, y);

  // Small pause before click (humans hesitate)
  await sleep(50 + Math.random() * 100);

  // Add slight jitter to click position
  const jitterX = x + randomOffset(2);
  const jitterY = y + randomOffset(2);

  // Press
  await client.send('Input.dispatchMouseEvent', {
    type: 'mousePressed',
    x: jitterX,
    y: jitterY,
    button,
    clickCount: 1,
  });

  // Hold for a human duration
  await sleep(50 + Math.random() * 80);

  // Release
  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased',
    x: jitterX,
    y: jitterY,
    button,
    clickCount: 1,
  });
}

/**
 * Type text with human-like variable delays between keystrokes.
 */
export async function humanType(
  client: CDPSession,
  text: string,
  options?: {
    minDelay?: number;
    maxDelay?: number;
    mistakeRate?: number;
  },
): Promise<void> {
  const minDelay = options?.minDelay ?? 30;
  const maxDelay = options?.maxDelay ?? 120;
  const mistakeRate = options?.mistakeRate ?? 0.02; // 2% chance of typo + correction

  for (let i = 0; i < text.length; i++) {
    const char = text[i];

    // Occasional typo + backspace (human error simulation)
    if (Math.random() < mistakeRate && char.match(/[a-zA-Z]/)) {
      const wrongChar = nearbyKey(char);
      await typeChar(client, wrongChar);
      await sleep(100 + Math.random() * 200); // Pause to "notice" mistake
      await typeKey(client, 'Backspace');
      await sleep(50 + Math.random() * 100);
    }

    if (char === '\n') {
      await typeKey(client, 'Enter');
    } else {
      await typeChar(client, char);
    }

    // Variable delay: longer after spaces and punctuation
    let delay = minDelay + Math.random() * (maxDelay - minDelay);
    if (char === ' ') delay *= 1.3;
    if ('.!?,;:'.includes(char)) delay *= 2;

    // Occasional longer pause (thinking)
    if (Math.random() < 0.05) delay *= 3;

    await sleep(delay);
  }
}

/**
 * Random scroll with natural speed variation.
 */
export async function humanScroll(
  client: CDPSession,
  x: number,
  y: number,
  totalDeltaY: number,
  steps?: number,
): Promise<void> {
  const numSteps = steps || Math.max(3, Math.round(Math.abs(totalDeltaY) / 80));

  for (let i = 0; i < numSteps; i++) {
    // Variable scroll amount per step
    const fraction = (1 + Math.random() * 0.5) / numSteps;
    const deltaY = Math.round(totalDeltaY * fraction);

    await client.send('Input.dispatchMouseEvent', {
      type: 'mouseWheel',
      x: x + randomOffset(5),
      y: y + randomOffset(5),
      deltaX: 0,
      deltaY,
    });

    await sleep(30 + Math.random() * 60);
  }
}

// --- Internal helpers ---

function cubicBezier(
  t: number,
  x0: number, y0: number,
  x1: number, y1: number,
  x2: number, y2: number,
  x3: number, y3: number,
): { x: number; y: number } {
  const u = 1 - t;
  const tt = t * t;
  const uu = u * u;
  const uuu = uu * u;
  const ttt = tt * t;

  return {
    x: uuu * x0 + 3 * uu * t * x1 + 3 * u * tt * x2 + ttt * x3,
    y: uuu * y0 + 3 * uu * t * y1 + 3 * u * tt * y2 + ttt * y3,
  };
}

function distance(x1: number, y1: number, x2: number, y2: number): number {
  return Math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2);
}

function randomOffset(range: number): number {
  return (Math.random() - 0.5) * 2 * range;
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function typeChar(client: CDPSession, char: string): Promise<void> {
  const keyCode = char.charCodeAt(0);
  await client.send('Input.dispatchKeyEvent', { type: 'keyDown', key: char, code: `Key${char.toUpperCase()}`, windowsVirtualKeyCode: keyCode });
  await client.send('Input.dispatchKeyEvent', { type: 'char', text: char, key: char });
  await client.send('Input.dispatchKeyEvent', { type: 'keyUp', key: char, code: `Key${char.toUpperCase()}`, windowsVirtualKeyCode: keyCode });
}

async function typeKey(client: CDPSession, key: string): Promise<void> {
  await client.send('Input.dispatchKeyEvent', { type: 'keyDown', key });
  await client.send('Input.dispatchKeyEvent', { type: 'keyUp', key });
}

/** Get a nearby key on QWERTY keyboard for typo simulation. */
function nearbyKey(char: string): string {
  const keyboard: Record<string, string> = {
    q: 'wa', w: 'qe', e: 'wr', r: 'et', t: 'ry', y: 'tu', u: 'yi', i: 'uo', o: 'ip', p: 'ol',
    a: 'sq', s: 'ad', d: 'sf', f: 'dg', g: 'fh', h: 'gj', j: 'hk', k: 'jl', l: 'kp',
    z: 'xa', x: 'zc', c: 'xv', v: 'cb', b: 'vn', n: 'bm', m: 'nk',
  };
  const lower = char.toLowerCase();
  const neighbors = keyboard[lower] || lower;
  const chosen = neighbors[Math.floor(Math.random() * neighbors.length)];
  return char === char.toUpperCase() ? chosen.toUpperCase() : chosen;
}
