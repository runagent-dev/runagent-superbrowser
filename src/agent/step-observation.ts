/**
 * Structured per-action observation + continuous reward signal.
 *
 * The existing domain-stats pipeline records strategy-level wins/losses
 * (binary). This adds finer-grained, cross-session telemetry:
 *   - per-coord band (which 100px cell of the viewport was clicked)
 *   - partial credit for progress (slider moved halfway → reward 0.5)
 *   - "noop" vs "error" distinction (noop = click landed but nothing
 *     changed; error = action threw)
 *
 * Consumers today: the navigator, vision captcha solver, and slider
 * strategy emit StepObservation instances; domain-stats persists the
 * reward; the FeedbackBus (§4) broadcasts them so the Python side can
 * act on them mid-step.
 */

export type ActionKind =
  | 'click'
  | 'drag'
  | 'type'
  | 'scroll'
  | 'navigate';

export type StepOutcome =
  | 'success'
  | 'partial'
  | 'noop'
  | 'error'
  | 'blocked';

export type VisibleChange =
  | 'none'
  | 'minor'
  | 'major'
  | 'navigation';

export interface StepObservation {
  action: ActionKind;
  coords?: { x: number; y: number; endX?: number; endY?: number };
  /** Human-readable target (selector, aria-label, etc.). */
  target?: string;
  outcome: StepOutcome;
  /**
   * How far from the intended landing point the action ended up. Only
   * meaningful for drags / slider strategies; clicks either landed or
   * didn't. Used for partial-credit reward.
   */
  deltaFromTarget?: number;
  visibleChange: VisibleChange;
  /** 0..1 — continuous reward. computeReward fills this. */
  reward: number;
  timestampMs: number;
  /** Hostname (`domain-stats.hostKey(url)`) — keys the persisted band stats. */
  host: string;
  /** 'navigator' | 'vision_generic' | 'slider_drag' | 'recaptcha_grid' | ... */
  strategy?: string;
}

export interface ComputeRewardInput extends Omit<StepObservation, 'reward'> {
  /** Expected movement in px for partial credit. Falls back to 100. */
  expectedDelta?: number;
}

/**
 * Reward function. Contract:
 *   - success → 1.0
 *   - partial → 1 - min(1, delta/expected) ∈ [0, 1)
 *   - visibleChange='major' without success → 0.3 (something happened,
 *     but we don't know if it was progress)
 *   - visibleChange='navigation' without success → 0.5 (URL changed, so
 *     the action had an effect, even if we didn't mean to navigate)
 *   - noop → 0.0
 *   - error / blocked → 0.0 (clamped; no negative rewards in this
 *     pipeline — domain-stats doesn't handle signed values today)
 *
 * The clamps/floors are calibrated so persisting mean(reward) per band
 * gives a usable ranking: >0.7 "reliable", 0.3-0.7 "sometimes works",
 * <0.3 "avoid".
 */
export function computeReward(obs: ComputeRewardInput): number {
  if (obs.outcome === 'success') return 1.0;
  if (obs.outcome === 'partial') {
    const delta = obs.deltaFromTarget ?? 0;
    const expected = obs.expectedDelta ?? 100;
    if (expected <= 0) return 0.5;
    const ratio = Math.min(1, delta / expected);
    return clamp(1 - ratio, 0, 1);
  }
  if (obs.outcome === 'blocked' || obs.outcome === 'error') return 0;
  // noop
  if (obs.visibleChange === 'navigation') return 0.5;
  if (obs.visibleChange === 'major') return 0.3;
  if (obs.visibleChange === 'minor') return 0.15;
  return 0;
}

export function buildObservation(input: ComputeRewardInput): StepObservation {
  const reward = computeReward(input);
  return {
    action: input.action,
    coords: input.coords,
    target: input.target,
    outcome: input.outcome,
    deltaFromTarget: input.deltaFromTarget,
    visibleChange: input.visibleChange,
    reward,
    timestampMs: input.timestampMs ?? Date.now(),
    host: input.host,
    strategy: input.strategy,
  };
}

/** 100px bands: enough resolution to distinguish grid cells, coarse
 *  enough that the stat store doesn't blow up with unique keys. */
export function coordBand(x: number, y: number): string {
  return `${Math.floor(x / 100)}x${Math.floor(y / 100)}`;
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}
