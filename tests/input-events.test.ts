/**
 * Tests for InputEventBus.emitSweep + lastCursor tracking.
 *
 * The sweep is the cosmetic "cursor visibly travels to the click
 * site" path for linear-mode clicks (selectorClick default, Tier 2
 * Puppeteer, autocomplete linear branch). It updates only the WS
 * overlay (no CDP), so the only thing to assert here is the emit
 * cadence and the lastCursor bookkeeping.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { inputEventBus, type MouseMoveEvent } from '../src/browser/input-events.js';

describe('InputEventBus.emitSweep', () => {
  const sid = 'test-session';

  beforeEach(() => {
    inputEventBus.clearSession(sid);
    inputEventBus.removeAllListeners('mouse_move');
  });

  it('emits N steps ending at the target', async () => {
    const seen: MouseMoveEvent[] = [];
    inputEventBus.on('mouse_move', (e: MouseMoveEvent) => {
      if (e.sessionId === sid) seen.push(e);
    });

    await inputEventBus.emitSweep(sid, 300, 200, { steps: 5, stepDelayMs: 5 });

    expect(seen).toHaveLength(5);
    expect(seen[seen.length - 1]).toMatchObject({ x: 300, y: 200 });
  });

  it('updates lastCursor on every emit', async () => {
    await inputEventBus.emitSweep(sid, 100, 100, { steps: 4, stepDelayMs: 1 });
    expect(inputEventBus.getLastCursor(sid)).toEqual({ x: 100, y: 100 });

    await inputEventBus.emitSweep(sid, 500, 400, { steps: 4, stepDelayMs: 1 });
    expect(inputEventBus.getLastCursor(sid)).toEqual({ x: 500, y: 400 });
  });

  it('starts from the last known cursor position', async () => {
    inputEventBus.emitMouseMove(sid, 50, 50);
    const seen: MouseMoveEvent[] = [];
    inputEventBus.on('mouse_move', (e: MouseMoveEvent) => {
      if (e.sessionId === sid) seen.push(e);
    });

    await inputEventBus.emitSweep(sid, 250, 150, { steps: 4, stepDelayMs: 1 });

    // First emit is one step (25%) toward target from (50,50).
    expect(seen[0]).toMatchObject({ x: 100, y: 75 });
    // Last emit is the target.
    expect(seen[seen.length - 1]).toMatchObject({ x: 250, y: 150 });
  });

  it('falls back to an offset origin when no cursor history', async () => {
    const seen: MouseMoveEvent[] = [];
    inputEventBus.on('mouse_move', (e: MouseMoveEvent) => {
      if (e.sessionId === sid) seen.push(e);
    });

    await inputEventBus.emitSweep(sid, 300, 300, { steps: 3, stepDelayMs: 1 });

    // Default origin is target - (150, 120) when no history.
    // Step 1 of 3 brings us 1/3 of the way from (150, 180) to (300, 300).
    expect(seen[0]).toMatchObject({ x: 200, y: 220 });
    expect(seen[seen.length - 1]).toMatchObject({ x: 300, y: 300 });
  });

  it('skips entirely when origin already at target', async () => {
    inputEventBus.emitMouseMove(sid, 100, 100);
    const seen: MouseMoveEvent[] = [];
    inputEventBus.on('mouse_move', (e: MouseMoveEvent) => {
      if (e.sessionId === sid) seen.push(e);
    });

    await inputEventBus.emitSweep(sid, 100, 100, { steps: 5, stepDelayMs: 1 });

    expect(seen).toHaveLength(0);
  });

  it('clearSession evicts the entry', () => {
    inputEventBus.emitMouseMove(sid, 10, 20);
    expect(inputEventBus.getLastCursor(sid)).toEqual({ x: 10, y: 20 });
    inputEventBus.clearSession(sid);
    expect(inputEventBus.getLastCursor(sid)).toBeUndefined();
  });

  it('emitClickTarget also updates lastCursor', () => {
    inputEventBus.emitClickTarget(sid, 77, 88, true);
    expect(inputEventBus.getLastCursor(sid)).toEqual({ x: 77, y: 88 });
  });
});
