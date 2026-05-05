/**
 * Vision epoch tracker — prevents the brain from acting on stale element indices.
 *
 * After each getState(), the epoch is "frozen" (capturing the URL and turn counter).
 * Before any indexed action, isStale() checks if mutations have occurred since
 * the last observation. If so, the action is rejected and the brain must re-observe.
 */

export class VisionEpoch {
  private epochId = 0;
  private epochTurn = 0;
  private brainTurnCounter = 0;
  private epochUrl = '';
  private epochTimestamp = 0;

  static MAX_AGE_TURNS = parseInt(process.env.VISION_MAX_AGE_TURNS || '1', 10);

  freeze(url: string): number {
    this.epochId++;
    this.epochTurn = this.brainTurnCounter;
    this.epochUrl = url;
    this.epochTimestamp = Date.now();
    return this.epochId;
  }

  incrementBrainTurn(): number {
    return ++this.brainTurnCounter;
  }

  isStale(currentUrl: string): { stale: boolean; reason?: string } {
    const ageTurns = this.brainTurnCounter - this.epochTurn;
    if (ageTurns > VisionEpoch.MAX_AGE_TURNS) {
      return {
        stale: true,
        reason: `${ageTurns} actions since last observation (max ${VisionEpoch.MAX_AGE_TURNS})`,
      };
    }
    if (currentUrl !== this.epochUrl) {
      return {
        stale: true,
        reason: `URL changed since last observation`,
      };
    }
    return { stale: false };
  }

  getEpochId(): number {
    return this.epochId;
  }

  reset(): void {
    this.epochId = 0;
    this.epochTurn = 0;
    this.brainTurnCounter = 0;
    this.epochUrl = '';
    this.epochTimestamp = 0;
  }
}
