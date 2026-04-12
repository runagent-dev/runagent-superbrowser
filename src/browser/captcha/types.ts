/**
 * Strategy interface + shared types for the captcha subsystem.
 *
 * Each strategy handles one or more CaptchaTypes and declares its priority,
 * cost, and requirements so the orchestrator can pick the cheapest
 * sufficient path for a given captcha.
 */

import type { Page } from 'puppeteer-core';
import type { LLMProvider } from '../../llm/provider.js';
import type {
  CaptchaInfo,
  CaptchaSolveResult,
  CaptchaSolverConfig,
  CaptchaType,
} from '../captcha.js';
import type { VisionMemory } from './vision-memory.js';

export interface StrategyContext {
  page: Page;
  llm?: LLMProvider;
  config: CaptchaSolverConfig;
  /** Accumulates state across vision rounds (tile history, drag attempts). */
  memory: VisionMemory;
  /** Starting wall-clock time — strategies may use for deadlines. */
  startTime: number;
  /** Max solver wallclock; default 60s. */
  deadlineMs: number;
}

export interface RichSolveResult extends CaptchaSolveResult {
  /** Canonical captcha type reported by detection. */
  captchaType?: CaptchaType;
  /** Vendor name — 'cloudflare', 'google', 'hcaptcha', 'custom-geetest', ... */
  vendorDetected?: string;
  /** Sub-method within the strategy: 'checkbox_autopass', 'grid_3x3', 'drag_with_feedback', ... */
  subMethod?: string;
  /** Total rounds executed. */
  totalRounds?: number;
  /** Wallclock duration of this strategy's run. */
  durationMs?: number;
  /** Paths to screenshots captured during solve (absolute paths). */
  screenshots?: string[];
  /** Per-round trace for post-mortem / dataset generation. */
  visionTrace?: Array<{
    round: number;
    action: unknown;
    screenshotPath?: string;
    llmOutput?: string;
  }>;
  /** reCAPTCHA/hCaptcha/Turnstile site key if extracted. */
  siteKey?: string;
  iframeUrl?: string;
}

/**
 * A captcha-solving strategy. Strategies register with the registry and are
 * dispatched in priority order until one returns solved=true.
 *
 * canHandle is consulted BEFORE run — strategies should be conservative
 * (only return true when they're reasonably likely to succeed) so the
 * registry can try the next one quickly.
 */
export interface CaptchaStrategy {
  /** Human-readable identifier, used in CaptchaSolveResult.method. */
  readonly name: string;
  /** Captcha types this strategy can handle. */
  readonly supportedTypes: readonly CaptchaType[];
  /** Higher = tried first. Cheap/reliable strategies get higher priority. */
  readonly priority: number;
  /** Rough cost in cents (0 for local, ~0.3 for Claude vision, ~3 for 2captcha). */
  readonly estimatedCostCents: number;
  /** True if this strategy needs an LLM provider in ctx.llm. */
  readonly requiresLLM: boolean;
  /** True if this strategy needs config.apiKey for an external service. */
  readonly requiresApiKey: boolean;

  canHandle(info: CaptchaInfo, ctx: StrategyContext): boolean | Promise<boolean>;
  run(info: CaptchaInfo, ctx: StrategyContext): Promise<RichSolveResult>;
}
