/**
 * In-process event bus for cross-subsystem coordination.
 *
 * Problem this solves: the navigator, captcha solvers, and the Python
 * nanobot main loop historically ran in parallel with no awareness of
 * each other's state. Symptoms observed:
 *   - nanobot fires a click during an active captcha solve → race
 *   - vision lands on an error page → nanobot blindly tries to click it
 *   - reward signals emitted by one subsystem were invisible to others
 *
 * This bus is strictly in-process on the TS side. A websocket bridge
 * (src/server/websocket.ts) forwards events to the Python bridge, which
 * mirrors them into a local state dict the nanobot tools consult before
 * dispatching.
 *
 * Keep it narrow: only events that another subsystem can *act on* live
 * here. Logging / telemetry go elsewhere.
 */

import { EventEmitter } from 'events';
import type { StepObservation } from './step-observation.js';

export interface ErrorPageEventDetail {
  url: string;
  kind: string;
  detail: string;
}

export type FeedbackEvent =
  | { kind: 'captcha_active'; host: string; strategy: string }
  | { kind: 'captcha_done';   host: string; solved: boolean; strategy?: string }
  | { kind: 'error_page';     detail: ErrorPageEventDetail }
  | { kind: 'error_cleared' }
  | { kind: 'action_reward';  obs: StepObservation };

export interface FeedbackState {
  captchaActive: boolean;
  captchaStrategy: string | null;
  errorPage: ErrorPageEventDetail | null;
  lastReward: number | null;
  lastRewardAt: number | null;
}

class FeedbackBus extends EventEmitter {
  private state: FeedbackState = {
    captchaActive: false,
    captchaStrategy: null,
    errorPage: null,
    lastReward: null,
    lastRewardAt: null,
  };

  publish(event: FeedbackEvent): void {
    // Update derived state first so listeners observing via getState()
    // see the new truth, not the old one.
    switch (event.kind) {
      case 'captcha_active':
        this.state.captchaActive = true;
        this.state.captchaStrategy = event.strategy;
        break;
      case 'captcha_done':
        this.state.captchaActive = false;
        this.state.captchaStrategy = null;
        break;
      case 'error_page':
        this.state.errorPage = event.detail;
        break;
      case 'error_cleared':
        this.state.errorPage = null;
        break;
      case 'action_reward':
        this.state.lastReward = event.obs.reward;
        this.state.lastRewardAt = event.obs.timestampMs;
        break;
    }
    this.emit('event', event);
  }

  /** Shallow snapshot. Safe to read from anywhere; don't mutate. */
  getState(): Readonly<FeedbackState> {
    return { ...this.state };
  }

  /** For tests + the WS bridge to clear state between sessions. */
  reset(): void {
    this.state = {
      captchaActive: false,
      captchaStrategy: null,
      errorPage: null,
      lastReward: null,
      lastRewardAt: null,
    };
  }
}

/** Process-wide singleton. Strategies / navigator / page all publish here. */
export const feedbackBus = new FeedbackBus();
