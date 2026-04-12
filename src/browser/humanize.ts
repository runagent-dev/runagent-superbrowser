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
 * Human-like drag: move → press → Bezier path with sigmoid velocity → release.
 *
 * Key differences from a straight-line dispatchDrag:
 *  - Press dwell (Gaussian 80-300ms) — humans pause before starting to drag
 *  - Sigmoid velocity profile (slow-fast-slow) — not uniform speed
 *  - Optional overshoot (cursor passes target 5-15% then comes back)
 *  - Micro-pauses mid-drag (3-8%/step chance, 40-120ms)
 *  - Release at EXACT target (no jitter — slider validation breaks with jitter)
 */
export async function humanDrag(
  client: CDPSession,
  fromX: number,
  fromY: number,
  toX: number,
  toY: number,
  options?: {
    /** mouseDown dwell before drag starts. Default: Gaussian(180,60)ms, clamped [80,300]. */
    pressDwellMs?: number;
    /** mouseUp dwell at target before release. Default: Gaussian(110,40)ms, clamped [60,200]. */
    releaseDwellMs?: number;
    /** Enable overshoot: cursor goes 5-15% past target then returns. Default false. */
    overshoot?: boolean;
    /** Chance per step of a micro-pause. Default 0.05 (5%). */
    microPauseChance?: number;
    /** Total step count along the path. Auto-computed from distance by default. */
    steps?: number;
    /** Bezier arc magnitude (fraction of distance, 0=straight line). Default 0.12. */
    arc?: number;
  },
): Promise<void> {
  const pressDwell = options?.pressDwellMs ?? gaussianInt(180, 60, 80, 300);
  const releaseDwell = options?.releaseDwellMs ?? gaussianInt(110, 40, 60, 200);
  const microPauseChance = options?.microPauseChance ?? 0.05;
  const dist = distance(fromX, fromY, toX, toY);
  const arcMag = options?.arc ?? 0.12;

  // Approach the handle first (move without button down)
  await humanMouseMove(client, fromX + randomOffset(8), fromY + randomOffset(8), fromX, fromY);

  // Press — small jitter tolerable here, handle usually has a grab region
  const pressX = fromX + randomOffset(1);
  const pressY = fromY + randomOffset(1);
  await client.send('Input.dispatchMouseEvent', {
    type: 'mousePressed',
    x: pressX,
    y: pressY,
    button: 'left',
    clickCount: 1,
  });
  await sleep(pressDwell);

  // If overshoot requested, pick an intermediate target 5-15% past the real target.
  const overshootFrac = options?.overshoot ? 0.05 + Math.random() * 0.1 : 0;
  const overshootX = toX + (toX - fromX) * overshootFrac;
  const overshootY = toY + (toY - fromY) * overshootFrac;

  // Primary drag leg: from press point to (possibly overshot) target
  await dragAlongPath(
    client,
    pressX,
    pressY,
    overshootFrac ? overshootX : toX,
    overshootFrac ? overshootY : toY,
    { steps: options?.steps, arc: arcMag, microPauseChance },
  );

  // Overshoot correction: drift back to the true target with a gentler curve
  if (overshootFrac) {
    await dragAlongPath(client, overshootX, overshootY, toX, toY, {
      steps: 6 + Math.floor(Math.random() * 4),
      arc: 0.04,
      microPauseChance: 0,
    });
  }

  // Settle dwell at target (hand "stabilizes" before release)
  await sleep(releaseDwell);

  // Release at EXACT target — slider validators check the release point
  // to within 1-2px, so no jitter here.
  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased',
    x: toX,
    y: toY,
    button: 'left',
    clickCount: 1,
  });

  // Brief post-release pause before next action (humans don't instantly act)
  await sleep(60 + Math.random() * 80);
  void dist; // reserved for future duration-adaptive timing
}

/** Bezier drag segment with sigmoid velocity. Button is already pressed. */
async function dragAlongPath(
  client: CDPSession,
  x0: number,
  y0: number,
  x1: number,
  y1: number,
  options: { steps?: number; arc: number; microPauseChance: number },
): Promise<void> {
  const dist = distance(x0, y0, x1, y1);
  const numSteps = options.steps || Math.max(12, Math.round(dist / 12));

  // Bezier control points perpendicular to the path for a natural curve
  const dx = x1 - x0;
  const dy = y1 - y0;
  const perpX = -dy;
  const perpY = dx;
  const perpLen = Math.sqrt(perpX * perpX + perpY * perpY) || 1;
  const arcMag = dist * options.arc * (Math.random() < 0.5 ? 1 : -1);
  const cp1x = x0 + dx * 0.33 + (perpX / perpLen) * arcMag + randomOffset(4);
  const cp1y = y0 + dy * 0.33 + (perpY / perpLen) * arcMag + randomOffset(4);
  const cp2x = x0 + dx * 0.66 + (perpX / perpLen) * arcMag * 0.7 + randomOffset(4);
  const cp2y = y0 + dy * 0.66 + (perpY / perpLen) * arcMag * 0.7 + randomOffset(4);

  for (let i = 1; i <= numSteps; i++) {
    // Sigmoid-remapped t: slow start, fast middle, slow end (different curve
    // shape from mouseMove's sine — emphasizes a stronger decel at the end,
    // which is what human drags look like when the target has a snap region).
    const linT = i / numSteps;
    const t = sigmoidEase(linT);
    const p = cubicBezier(t, x0, y0, cp1x, cp1y, cp2x, cp2y, x1, y1);

    await client.send('Input.dispatchMouseEvent', {
      type: 'mouseMoved',
      x: Math.round(p.x + randomOffset(0.5)),
      y: Math.round(p.y + randomOffset(0.5)),
      button: 'left',
      buttons: 1,
    });

    // Per-step delay scales with (1 - |dt/dlinT|) — fast where sigmoid is steep.
    const stepDelay = 8 + Math.round((1 - sigmoidDerivative(linT)) * 18) + Math.random() * 5;
    await sleep(stepDelay);

    // Occasional micro-pause mid-drag
    if (Math.random() < options.microPauseChance) {
      await sleep(40 + Math.random() * 80);
    }
  }
}

/** Sigmoid easing in [0,1] — parameterized for natural-looking drags. */
function sigmoidEase(t: number): number {
  // Scaled logistic: at t=0 returns ~0, at t=1 returns ~1, with steep middle.
  const k = 8;
  const s = 1 / (1 + Math.exp(-k * (t - 0.5)));
  const s0 = 1 / (1 + Math.exp(-k * (0 - 0.5)));
  const s1 = 1 / (1 + Math.exp(-k * (1 - 0.5)));
  return (s - s0) / (s1 - s0);
}

/** Approximate derivative of sigmoidEase at t. Used to vary step timing. */
function sigmoidDerivative(t: number): number {
  const k = 8;
  const s = 1 / (1 + Math.exp(-k * (t - 0.5)));
  return 4 * s * (1 - s); // normalized peak at t=0.5
}

/** Gaussian sample (Box-Muller) as int, clamped to [lo, hi]. */
function gaussianInt(mean: number, stddev: number, lo: number, hi: number): number {
  const u1 = Math.max(Math.random(), 1e-9);
  const u2 = Math.random();
  const z = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
  const v = Math.round(mean + z * stddev);
  return Math.max(lo, Math.min(hi, v));
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
