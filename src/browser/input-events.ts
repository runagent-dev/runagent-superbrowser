/**
 * Input event bus for broadcasting mouse and keystroke events.
 *
 * Process-wide singleton that humanize.ts and input-keyboard.ts emit to
 * when a sessionId is provided. The WebSocket server subscribes and
 * forwards events to connected view clients for real-time cursor overlay
 * and typing indicator.
 *
 * Separate from feedbackBus because mouse events fire at ~100 Hz —
 * mixing them would overwhelm the feedbackBus Python bridge listeners.
 */

import { EventEmitter } from 'events';

export interface MouseMoveEvent {
  sessionId: string;
  x: number;
  y: number;
  ts: number;
}

export interface KeystrokeEvent {
  sessionId: string;
  key: string;
  type: 'char' | 'keyDown' | 'keyUp';
  ts: number;
}

/**
 * Resolved click target — emitted right before the click fires.
 *
 * When a click came from a vision bbox payload, `bbox` is the source
 * rectangle and `snapped` is true if we found an interactive element
 * inside it. Lets the live-view UI show a crosshair where the cursor
 * actually landed (not where the LLM guessed) so misses are visible
 * during debugging.
 */
export interface ClickTargetEvent {
  sessionId: string;
  x: number;
  y: number;
  snapped: boolean;
  bbox?: { x0: number; y0: number; x1: number; y1: number };
  target?: string;
  ts: number;
}

/**
 * Vision-pass result — emitted after the vision agent (Gemini) returns
 * its bbox set. Live viewers flash all detected interactive regions
 * for ~1.5s so the user can see what the model "saw" and judge whether
 * a missed click was a vision miss vs a snap miss.
 *
 * Coordinates are CSS pixels of the rendered viewport (already
 * denormalized from Gemini's [0, 1000] box_2d by the Python bridge).
 */
export interface VisionBboxesEvent {
  sessionId: string;
  bboxes: Array<{
    x0: number; y0: number; x1: number; y1: number;
    label?: string;
    role?: string;
    clickable?: boolean;
    intent_relevant?: boolean;
    index?: number;  // V_n the brain sees for this bbox
  }>;
  imageWidth?: number;
  imageHeight?: number;
  /** URL the screenshot was captured on. UI drops bboxes whose URL
   *  no longer matches the current screencast frame. */
  url?: string;
  /** Model's self-report of screenshot freshness. UI dims the overlay
   *  when this is not "fresh". */
  freshness?: 'fresh' | 'uncertain' | 'stale';
  /** Vision-agent round-trip in milliseconds (debug). */
  latencyMs?: number;
  ts: number;
}

/**
 * Vision-pass dispatched — emitted when the Python bridge kicks off a
 * vision call. Live viewers render a transient "vision updating…"
 * indicator so the user can see the overlay is about to refresh rather
 * than assume it's stuck.
 */
export interface VisionPendingEvent {
  sessionId: string;
  dispatchedAt: number;
  ts: number;
}

class InputEventBus extends EventEmitter {
  /**
   * Last known cursor position per session. Updated on every
   * `emitMouseMove` and `emitClickTarget`. Used by `emitSweep` to
   * compute the start of a cosmetic cursor travel for linear-mode
   * clicks (which otherwise teleport without intermediate frames).
   *
   * Entries are dropped via `clearSession` from the WS cleanup hook.
   * Headless-only sessions that never had a viewer accumulate one
   * entry each — the size is bounded by the session lifetime cap.
   */
  private lastCursor = new Map<string, { x: number; y: number }>();

  emitMouseMove(sessionId: string, x: number, y: number): void {
    const rx = Math.round(x);
    const ry = Math.round(y);
    this.lastCursor.set(sessionId, { x: rx, y: ry });
    this.emit('mouse_move', {
      sessionId,
      x: rx,
      y: ry,
      ts: Date.now(),
    } satisfies MouseMoveEvent);
  }

  emitKeystroke(sessionId: string, key: string, type: 'char' | 'keyDown' | 'keyUp'): void {
    this.emit('keystroke', {
      sessionId,
      key,
      type,
      ts: Date.now(),
    } satisfies KeystrokeEvent);
  }

  emitClickTarget(
    sessionId: string,
    x: number,
    y: number,
    snapped: boolean,
    bbox?: { x0: number; y0: number; x1: number; y1: number },
    target?: string,
  ): void {
    const rx = Math.round(x);
    const ry = Math.round(y);
    this.lastCursor.set(sessionId, { x: rx, y: ry });
    this.emit('click_target', {
      sessionId,
      x: rx,
      y: ry,
      snapped,
      bbox,
      target,
      ts: Date.now(),
    } satisfies ClickTargetEvent);
  }

  /**
   * Last known cursor position, or `undefined` if none recorded yet.
   * Read by `emitSweep` to compute the travel-from coords.
   */
  getLastCursor(sessionId: string): { x: number; y: number } | undefined {
    return this.lastCursor.get(sessionId);
  }

  /**
   * Drop per-session state. Call from the WS cleanup path when the
   * last viewer for a session disconnects.
   */
  clearSession(sessionId: string): void {
    this.lastCursor.delete(sessionId);
  }

  /**
   * Emit a short interpolated mouse-move sweep ending at `(toX, toY)`.
   *
   * Purely cosmetic — drives the live-view cursor overlay only,
   * does NOT touch CDP or affect what the page sees. Used at the
   * three visible click sites that go through linear/deterministic
   * dispatch (`clickSelector` default, autocomplete in `clickInBbox`,
   * and the Tier 2 Puppeteer fallback in `clickElement`).
   *
   * Defaults: 5 emits with ~30ms spacing (matches the 30 FPS WS
   * throttle in `websocket.ts:213` — more emits get dropped). Total
   * wall-clock travel ~150ms, in line with `humanClick`'s 100–200ms
   * pre-click hesitation.
   *
   * If no prior cursor position is recorded, starts from ~150px
   * up-and-left of the target (mirrors `humanClick`'s
   * `randomOffset(200)`).
   */
  async emitSweep(
    sessionId: string,
    toX: number,
    toY: number,
    opts?: { steps?: number; stepDelayMs?: number },
  ): Promise<void> {
    const steps = Math.max(2, opts?.steps ?? 5);
    const stepDelay = Math.max(20, opts?.stepDelayMs ?? 30);
    const target = { x: Math.round(toX), y: Math.round(toY) };
    const origin = this.lastCursor.get(sessionId) ?? {
      x: target.x - 150,
      y: target.y - 120,
    };
    if (origin.x === target.x && origin.y === target.y) return;
    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      const px = Math.round(origin.x + (target.x - origin.x) * t);
      const py = Math.round(origin.y + (target.y - origin.y) * t);
      this.emitMouseMove(sessionId, px, py);
      if (i < steps) {
        await new Promise<void>((r) => setTimeout(r, stepDelay));
      }
    }
  }

  emitVisionBboxes(
    sessionId: string,
    bboxes: VisionBboxesEvent['bboxes'],
    imageWidth?: number,
    imageHeight?: number,
    extras?: {
      url?: string;
      freshness?: VisionBboxesEvent['freshness'];
      latencyMs?: number;
    },
  ): void {
    this.emit('vision_bboxes', {
      sessionId,
      bboxes,
      imageWidth,
      imageHeight,
      url: extras?.url,
      freshness: extras?.freshness,
      latencyMs: extras?.latencyMs,
      ts: Date.now(),
    } satisfies VisionBboxesEvent);
  }

  emitVisionPending(sessionId: string, dispatchedAt?: number): void {
    this.emit('vision_pending', {
      sessionId,
      dispatchedAt: dispatchedAt ?? Date.now(),
      ts: Date.now(),
    } satisfies VisionPendingEvent);
  }
}

/** Process-wide singleton. */
export const inputEventBus = new InputEventBus();
