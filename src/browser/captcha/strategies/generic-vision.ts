/**
 * Generic vision-action loop with refusal detection + fallback model swap.
 *
 * Wraps solveWithVisionGeneric. Adds:
 *  - Refusal detection: if the LLM response triggers policy refusal phrases
 *    and contains no JSON action, retry with a re-framed prompt, then swap
 *    to CAPTCHA_FALLBACK_MODEL if configured.
 *  - Refusal telemetry: appended to /tmp/superbrowser/refusals.jsonl for
 *    prompt tuning.
 *
 * NOTE: The underlying generic solver owns its conversation loop; the
 * refusal swap operates at the strategy boundary — if the first generic
 * run fails with an error mentioning "refuse"/"policy"/"cannot", we retry
 * with a different LLM provider if available.
 */

import fs from 'fs';
import path from 'path';
import type { LLMProvider } from '../../../llm/provider.js';
import type { CaptchaInfo } from '../../captcha.js';
import { solveWithVisionGeneric } from '../../captcha.js';
import type { CaptchaStrategy, RichSolveResult, StrategyContext } from '../types.js';

const REFUSAL_TELEMETRY_PATH = '/tmp/superbrowser/refusals.jsonl';
const REFUSAL_PHRASES = [
  'i cannot help', "i can't help", "i'm not able",
  'i cannot solve captchas', 'against my guidelines',
  'violates', 'refuse', "not comfortable",
  'unable to assist with', 'policy',
];

function isRefusal(errOrText: string | undefined): boolean {
  if (!errOrText) return false;
  const lower = errOrText.toLowerCase();
  return REFUSAL_PHRASES.some((p) => lower.includes(p)) && !lower.includes('{');
}

function logRefusal(entry: Record<string, unknown>): void {
  try {
    fs.mkdirSync(path.dirname(REFUSAL_TELEMETRY_PATH), { recursive: true });
    fs.appendFileSync(REFUSAL_TELEMETRY_PATH, JSON.stringify({ ts: Date.now(), ...entry }) + '\n');
  } catch {
    // Telemetry failure is non-fatal.
  }
}

/**
 * The fallback LLM provider is resolved from env CAPTCHA_FALLBACK_MODEL.
 * Intentionally dynamic: runtime wires in its provider factory to keep
 * this module free of hard LLM SDK deps.
 */
let fallbackProviderFactory: (() => LLMProvider) | null = null;

/** Register a factory for the refusal-fallback provider. */
export function registerFallbackProviderFactory(f: () => LLMProvider): void {
  fallbackProviderFactory = f;
}

export const genericVisionStrategy: CaptchaStrategy = {
  name: 'generic_vision',
  supportedTypes: ['slider', 'visual_puzzle', 'image', 'text', 'unknown'],
  // Last-resort automation: ~99% empirical failure rate on real captchas;
  // kept as catch-all for `unknown` types but tried after every specialised
  // strategy and after the paid grid API. Only above human-handoff so the
  // user isn't asked unnecessarily on novel captcha types.
  priority: 20,
  estimatedCostCents: 3,
  requiresLLM: true,
  requiresApiKey: false,

  canHandle(_info: CaptchaInfo, ctx: StrategyContext): boolean {
    return Boolean(ctx.llm);
  },

  async run(info: CaptchaInfo, ctx: StrategyContext): Promise<RichSolveResult> {
    const start = Date.now();
    if (!ctx.llm) {
      return { solved: false, method: 'generic_vision', attempts: 0, error: 'no LLM' };
    }

    // Attempt 1: primary LLM
    try {
      const r1 = await solveWithVisionGeneric(ctx.page, ctx.llm);
      if (r1.solved) {
        return {
          ...r1,
          method: 'generic_vision',
          subMethod: 'primary_llm',
          durationMs: Date.now() - start,
        };
      }
      if (!isRefusal(r1.error)) {
        return {
          ...r1,
          method: 'generic_vision',
          subMethod: 'primary_llm',
          durationMs: Date.now() - start,
        };
      }
      // Refusal — log and try fallback
      logRefusal({ captchaType: info.type, provider: 'primary', error: r1.error });
    } catch (e) {
      const msg = (e as Error).message;
      if (!isRefusal(msg)) {
        return {
          solved: false, method: 'generic_vision', attempts: 1, error: msg,
          durationMs: Date.now() - start,
        };
      }
      logRefusal({ captchaType: info.type, provider: 'primary', error: msg });
    }

    // Attempt 2: fallback LLM (different model/provider — different refusal policy)
    if (!fallbackProviderFactory) {
      return {
        solved: false,
        method: 'generic_vision',
        subMethod: 'refused_no_fallback',
        attempts: 1,
        error: 'primary LLM refused; no fallback provider configured',
        durationMs: Date.now() - start,
      };
    }

    try {
      const fallback = fallbackProviderFactory();
      const r2 = await solveWithVisionGeneric(ctx.page, fallback);
      if (r2.solved) {
        return {
          ...r2,
          method: 'generic_vision',
          subMethod: 'fallback_llm',
          durationMs: Date.now() - start,
        };
      }
      if (isRefusal(r2.error)) {
        logRefusal({ captchaType: info.type, provider: 'fallback', error: r2.error });
      }
      return {
        ...r2,
        method: 'generic_vision',
        subMethod: 'fallback_llm',
        durationMs: Date.now() - start,
      };
    } catch (e) {
      return {
        solved: false,
        method: 'generic_vision',
        subMethod: 'fallback_failed',
        attempts: 2,
        error: (e as Error).message,
        durationMs: Date.now() - start,
      };
    }
  },
};
