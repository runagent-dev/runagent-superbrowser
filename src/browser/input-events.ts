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
  emitMouseMove(sessionId: string, x: number, y: number): void {
    this.emit('mouse_move', {
      sessionId,
      x: Math.round(x),
      y: Math.round(y),
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
    this.emit('click_target', {
      sessionId,
      x: Math.round(x),
      y: Math.round(y),
      snapped,
      bbox,
      target,
      ts: Date.now(),
    } satisfies ClickTargetEvent);
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
