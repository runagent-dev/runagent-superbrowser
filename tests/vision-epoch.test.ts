import { describe, it, expect, beforeEach } from 'vitest';
import { VisionEpoch } from '../src/agent/vision-epoch.js';

describe('VisionEpoch', () => {
  let epoch: VisionEpoch;

  beforeEach(() => {
    epoch = new VisionEpoch();
  });

  it('is not stale immediately after freeze', () => {
    epoch.freeze('https://example.com');
    const check = epoch.isStale('https://example.com');
    expect(check.stale).toBe(false);
  });

  it('is stale after incrementing brain turn beyond MAX_AGE_TURNS', () => {
    epoch.freeze('https://example.com');
    epoch.incrementBrainTurn();
    epoch.incrementBrainTurn();
    const check = epoch.isStale('https://example.com');
    expect(check.stale).toBe(true);
    expect(check.reason).toContain('actions since last observation');
  });

  it('is stale when URL changes', () => {
    epoch.freeze('https://example.com');
    const check = epoch.isStale('https://other.com');
    expect(check.stale).toBe(true);
    expect(check.reason).toContain('URL changed');
  });

  it('resets staleness after re-freeze', () => {
    epoch.freeze('https://example.com');
    epoch.incrementBrainTurn();
    epoch.incrementBrainTurn();
    expect(epoch.isStale('https://example.com').stale).toBe(true);

    epoch.freeze('https://example.com');
    expect(epoch.isStale('https://example.com').stale).toBe(false);
  });

  it('increments epochId on each freeze', () => {
    expect(epoch.getEpochId()).toBe(0);
    epoch.freeze('https://a.com');
    expect(epoch.getEpochId()).toBe(1);
    epoch.freeze('https://b.com');
    expect(epoch.getEpochId()).toBe(2);
  });

  it('allows exactly MAX_AGE_TURNS actions before going stale', () => {
    epoch.freeze('https://example.com');
    epoch.incrementBrainTurn();
    // At exactly MAX_AGE_TURNS (1), should still be stale (> not >=)
    const check = epoch.isStale('https://example.com');
    // 1 action since epoch, MAX_AGE_TURNS=1, so 1 > 1 is false
    expect(check.stale).toBe(false);
  });

  it('reset clears all state', () => {
    epoch.freeze('https://example.com');
    epoch.incrementBrainTurn();
    epoch.reset();
    expect(epoch.getEpochId()).toBe(0);
    // After reset + freeze, should be fresh
    epoch.freeze('https://new.com');
    expect(epoch.isStale('https://new.com').stale).toBe(false);
  });
});
