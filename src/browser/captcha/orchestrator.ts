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

/** Build the default registry with all production strategies. */
export function buildDefaultRegistry(): CaptchaStrategyRegistry {
  return new CaptchaStrategyRegistry()
    .register(turnstileStrategy)
    .register(tokenExternalStrategy)
    .register(recaptchaCheckboxStrategy)
    .register(recaptchaAIGridStrategy)
    .register(recaptchaGridApiStrategy)
    .register(sliderDragStrategy)
    .register(genericVisionStrategy);
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
  options?: { registry?: CaptchaStrategyRegistry; deadlineMs?: number },
): Promise<RichSolveResult> {
  const registry = options?.registry ?? buildDefaultRegistry();
  const ctx = {
    page,
    llm: llmProvider ?? undefined,
    config,
    memory: new VisionMemory(),
    startTime: Date.now(),
    deadlineMs: options?.deadlineMs ?? config.timeout ?? 90000,
  };
  return registry.dispatch(captcha, ctx);
}
