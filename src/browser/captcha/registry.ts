/**
 * Captcha strategy registry + priority-ordered dispatcher.
 *
 * Pattern adapted from ReCAP-Agent's model_profiles / SOLVER_MAP: each
 * strategy declares supported types and cost; orchestrator walks the
 * sorted candidate list, stopping on first success.
 */

import type { CaptchaInfo } from '../captcha.js';
import type { CaptchaStrategy, RichSolveResult, StrategyContext } from './types.js';

export class CaptchaStrategyRegistry {
  private strategies: CaptchaStrategy[] = [];

  register(strategy: CaptchaStrategy): this {
    this.strategies.push(strategy);
    // Sort highest priority first, then lowest cost.
    this.strategies.sort((a, b) => {
      if (b.priority !== a.priority) return b.priority - a.priority;
      return a.estimatedCostCents - b.estimatedCostCents;
    });
    return this;
  }

  /** All registered strategies, in dispatch order. */
  list(): readonly CaptchaStrategy[] {
    return this.strategies;
  }

  /** Candidates whose supportedTypes includes info.type AND who pass canHandle. */
  async candidatesFor(info: CaptchaInfo, ctx: StrategyContext): Promise<CaptchaStrategy[]> {
    const candidates: CaptchaStrategy[] = [];
    for (const s of this.strategies) {
      if (!s.supportedTypes.includes(info.type)) continue;
      if (s.requiresLLM && !ctx.llm) continue;
      if (s.requiresApiKey && !ctx.config.apiKey) continue;
      try {
        const ok = await s.canHandle(info, ctx);
        if (ok) candidates.push(s);
      } catch {
        // canHandle failed — skip this strategy, don't crash dispatch
      }
    }
    return candidates;
  }

  /**
   * Try candidates in priority order. First success wins.
   * Each failure is captured in result.error and reported in the trace so
   * the orchestrator can learn which method actually worked.
   */
  async dispatch(info: CaptchaInfo, ctx: StrategyContext): Promise<RichSolveResult> {
    const candidates = await this.candidatesFor(info, ctx);
    if (candidates.length === 0) {
      return {
        solved: false,
        method: 'none',
        attempts: 0,
        captchaType: info.type,
        error: `No strategy registered for captcha type '${info.type}'`,
      };
    }

    const trace: RichSolveResult['visionTrace'] = [];
    let lastError: string | undefined;

    for (const strategy of candidates) {
      if (Date.now() - ctx.startTime > ctx.deadlineMs) {
        lastError = `solver deadline ${ctx.deadlineMs}ms exceeded`;
        break;
      }
      const stratStart = Date.now();
      try {
        const result = await strategy.run(info, ctx);
        const duration = Date.now() - stratStart;
        trace?.push({
          round: trace.length,
          action: { strategy: strategy.name, solved: result.solved },
          llmOutput: result.error,
        });
        if (result.solved) {
          return {
            ...result,
            method: result.method || strategy.name,
            captchaType: info.type,
            durationMs: duration,
            visionTrace: [...(trace || []), ...(result.visionTrace || [])],
          };
        }
        lastError = result.error || `strategy ${strategy.name} returned solved=false`;
      } catch (e) {
        lastError = `strategy ${strategy.name} threw: ${(e as Error).message}`;
      }
    }

    return {
      solved: false,
      method: 'all_strategies_failed',
      attempts: candidates.length,
      captchaType: info.type,
      error: lastError || 'unknown',
      visionTrace: trace,
    };
  }
}
