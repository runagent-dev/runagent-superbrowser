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
}

/** Process-wide singleton. */
export const inputEventBus = new InputEventBus();
