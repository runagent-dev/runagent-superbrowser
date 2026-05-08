/**
 * Cross-round memory for vision-based captcha solvers.
 *
 * Problem this fixes: reCAPTCHA grid solvers often need multiple rounds
 * (click 3 tiles → new tiles appear → click more). Without memory, the LLM
 * forgets which tiles it's already clicked and can double-click or ignore
 * its own prior reasoning. Similarly for sliders where an overshoot was
 * corrected — the LLM should know "drag from 120 to 310 already attempted,
 * landed at 325, needed -15px correction".
 *
 * Reset when the challenge changes (new grid loaded, new image set).
 */

import { createHash } from 'crypto';

export interface DragAttempt {
  from: { x: number; y: number };
  to: { x: number; y: number };
  /** After drag: aligned, or offset from target in px. */
  result: 'aligned' | 'missed' | 'overshot' | 'unknown';
  offsetPx?: number;
}

export interface RejectedSolution {
  description: string;
  reason: string;
}

export class VisionMemory {
  clickedTiles: Set<number> = new Set();
  dragsAttempted: DragAttempt[] = [];
  rejectedSolutions: RejectedSolution[] = [];
  currentChallengeHash = '';
  consecutiveFailures = 0;

  /**
   * Set the active challenge hash. If it differs from the previous hash,
   * all per-challenge state (tiles, drags) is cleared — a new grid/slider
   * has loaded and past actions no longer apply.
   */
  setChallengeHash(hash: string): void {
    if (hash && hash !== this.currentChallengeHash) {
      this.clickedTiles.clear();
      this.dragsAttempted.length = 0;
      this.rejectedSolutions.length = 0;
      this.currentChallengeHash = hash;
    }
  }

  /**
   * Hash challenge instruction + full image so different grids never
   * collide.
   *
   * The previous implementation hashed only the first 2KB of base64 for
   * cost reasons. JPEG headers are largely shared across screenshots of
   * the same widget taken seconds apart, so two different tile sets would
   * hash to the same value — and the memory would feed "already-clicked
   * tiles [1,2,3]" into a brand-new grid, poisoning every subsequent
   * round.
   *
   * `context` (caller-supplied) lets us fold in viewport dims, host URL,
   * or any other disambiguator so two visually-similar screenshots on
   * different hosts don't collide either. Hashing the full image adds
   * ~1ms per round, which is invisible next to the LLM call it precedes.
   */
  static hashChallenge(
    instruction: string,
    imageBase64: string,
    context?: string,
  ): string {
    const h = createHash('sha1');
    h.update(instruction);
    if (context) h.update('|' + context);
    h.update('|' + imageBase64);
    return h.digest('hex').slice(0, 16);
  }

  recordTileClick(index: number): void {
    this.clickedTiles.add(index);
  }

  recordDrag(attempt: DragAttempt): void {
    this.dragsAttempted.push(attempt);
    if (attempt.result === 'aligned') {
      this.consecutiveFailures = 0;
    } else {
      this.consecutiveFailures += 1;
    }
  }

  recordRejected(description: string, reason: string): void {
    this.rejectedSolutions.push({ description, reason });
  }

  /** Render memory as a prompt fragment injected into every vision LLM call. */
  toPromptFragment(): string {
    const parts: string[] = [];
    if (this.clickedTiles.size > 0) {
      const tiles = Array.from(this.clickedTiles).sort((a, b) => a - b).join(', ');
      parts.push(`Already-clicked tile indices (DO NOT re-click): [${tiles}].`);
    }
    if (this.dragsAttempted.length > 0) {
      const lines = this.dragsAttempted.slice(-5).map((d, i) => {
        const off = d.offsetPx != null ? ` (off by ${d.offsetPx}px)` : '';
        return `  #${i + 1}: (${d.from.x},${d.from.y}) → (${d.to.x},${d.to.y}) = ${d.result}${off}`;
      });
      parts.push(`Previous drag attempts (use deltas, don't repeat):\n${lines.join('\n')}`);
    }
    if (this.rejectedSolutions.length > 0) {
      const recent = this.rejectedSolutions.slice(-3)
        .map((r) => `  - ${r.description}: ${r.reason}`).join('\n');
      parts.push(`Previously-rejected solutions:\n${recent}`);
    }
    if (this.consecutiveFailures >= 2) {
      parts.push(
        `You have failed ${this.consecutiveFailures} times in a row on this challenge. ` +
        `Re-examine the screenshot carefully — your model of the layout may be wrong.`,
      );
    }
    return parts.length ? parts.join('\n\n') : '';
  }
}
