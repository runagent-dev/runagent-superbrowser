/**
 * Captcha orchestrator: builds the strategy registry and dispatches.
 *
 * Legacy `solveCaptchaFull(method='auto')` now delegates here. Explicit
 * method overrides (token/ai_vision/grid/vision_generic) bypass the
 * registry and call the underlying function directly — preserves
 * backwards compatibility for callers that pin a specific method.
 */

import type { Page } from 'puppeteer-core';
import type { LLMProvider } from '../../llm/provider.js';
import type { HumanInputManager } from '../../agent/human-input.js';
import type { CaptchaInfo, CaptchaSolverConfig } from '../captcha.js';
import { CaptchaStrategyRegistry } from './registry.js';
import type { RichSolveResult } from './types.js';
import { VisionMemory } from './vision-memory.js';
import { turnstileStrategy } from './strategies/turnstile.js';
import { tokenExternalStrategy } from './strategies/token-external.js';
import {
  recaptchaCheckboxStrategy,
  recaptchaAIGridStrategy,
  recaptchaGridApiStrategy,
} from './strategies/recaptcha-v2.js';
import { sliderDragStrategy } from './strategies/slider-drag.js';
import { genericVisionStrategy } from './strategies/generic-vision.js';
import { humanHandoffStrategy } from './strategies/human-handoff.js';

/** Build the default registry with all production strategies. */
export function buildDefaultRegistry(): CaptchaStrategyRegistry {
  return new CaptchaStrategyRegistry()
    .register(turnstileStrategy)
    .register(tokenExternalStrategy)
    .register(recaptchaCheckboxStrategy)
    .register(recaptchaAIGridStrategy)
    .register(recaptchaGridApiStrategy)
    .register(sliderDragStrategy)
    .register(genericVisionStrategy)
    // Human handoff is registered LAST and has the lowest priority so it's
    // only dispatched when every automated strategy has declined or failed.
    // It also self-gates on ctx.humanInput presence + budget > 0.
    .register(humanHandoffStrategy);
}

/**
 * Solve a captcha via the strategy registry. Orchestrator-level entry point.
 * Returns a RichSolveResult carrying method, subMethod, trace, and timing
 * so callers (e.g., nanobot) can learn which approach worked per-domain.
 */
export async function solveCaptchaViaRegistry(
  page: Page,
  captcha: CaptchaInfo,
  config: CaptchaSolverConfig,
  llmProvider?: LLMProvider | null,
  options?: {
    registry?: CaptchaStrategyRegistry;
    deadlineMs?: number;
    /** Session id (used by human_handoff to build the view URL). */
    sessionId?: string;
    /** Per-session HumanInputManager (enables the human_handoff strategy). */
    humanInput?: HumanInputManager;
    /** Remaining handoffs allowed on this session. */
    humanHandoffBudget?: number;
    /** Public base URL of the view UI (e.g. https://browser.example.com). */
    publicBaseUrl?: string;
  },
): Promise<RichSolveResult> {
  const registry = options?.registry ?? buildDefaultRegistry();
  const ctx = {
    page,
    llm: llmProvider ?? undefined,
    config,
    memory: new VisionMemory(),
    startTime: Date.now(),
    deadlineMs: options?.deadlineMs ?? config.timeout ?? 90000,
    sessionId: options?.sessionId,
    humanInput: options?.humanInput,
    humanHandoffBudget: options?.humanHandoffBudget,
    publicBaseUrl: options?.publicBaseUrl,
  };
  return registry.dispatch(captcha, ctx);
}
