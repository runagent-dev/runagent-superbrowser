/**
 * Captcha strategy registry + priority-ordered dispatcher.
 *
 * Pattern adapted from ReCAP-Agent's model_profiles / SOLVER_MAP: each
 * strategy declares supported types and cost; orchestrator walks the
 * sorted candidate list, stopping on first success.
 */

import type { CaptchaInfo } from '../captcha.js';
import type { CaptchaStrategy, RichSolveResult, StrategyContext } from './types.js';
import { getDomainStats, hostKey } from './domain-stats.js';

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
   * Try candidates in priority order, skewed by per-host history.
   *
   * Order is computed each dispatch as `score(strategy) = blend(static
   * priority, beta(wins+1,losses+1))` for the current page's hostname —
   * strategies that have won here before run earlier; strategies that have
   * lost ≥5 times in a row are skipped for 30 minutes (cooldown).
   *
   * Outcomes (success or failure) are recorded after each run so future
   * dispatches benefit. The static priority is preserved as the prior so
   * untried strategies still respect the hand-tuned ladder.
   */
  async dispatch(info: CaptchaInfo, ctx: StrategyContext): Promise<RichSolveResult> {
    const allCandidates = await this.candidatesFor(info, ctx);
    if (allCandidates.length === 0) {
      return {
        solved: false,
        method: 'none',
        attempts: 0,
        captchaType: info.type,
        error: `No strategy registered for captcha type '${info.type}'`,
      };
    }

    const stats = getDomainStats();
    let host = '_unknown';
    try { host = hostKey(ctx.page.url()); } catch { /* keep _unknown */ }

    // Score each candidate; skip ones in cooldown (but keep human-handoff
    // selectable — a real user can override any cooldown).
    const scored = allCandidates
      .filter((s) => s.name === 'human_handoff' || !stats.isInCooldown(host, s.name))
      .map((s) => ({
        strategy: s,
        // Normalize static priority to ~[0,1] with a soft cap at 100.
        score: stats.scoreFor(host, s.name, Math.min(1, s.priority / 100)),
      }))
      .sort((a, b) => b.score - a.score);

    if (scored.length === 0) {
      return {
        solved: false,
        method: 'all_strategies_in_cooldown',
        attempts: 0,
        captchaType: info.type,
        error: `every candidate strategy is in cooldown on ${host}`,
      };
    }

    const trace: RichSolveResult['visionTrace'] = [];
    let lastError: string | undefined;

    for (const { strategy, score } of scored) {
      if (Date.now() - ctx.startTime > ctx.deadlineMs) {
        lastError = `solver deadline ${ctx.deadlineMs}ms exceeded`;
        break;
      }
      const stratStart = Date.now();
      try {
        const result = await strategy.run(info, ctx);
        const duration = Date.now() - stratStart;
        stats.recordOutcome(host, strategy.name, result.solved, duration);
        trace?.push({
          round: trace.length,
          action: { strategy: strategy.name, solved: result.solved, score: Number(score.toFixed(3)) },
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
        const duration = Date.now() - stratStart;
        stats.recordOutcome(host, strategy.name, false, duration);
        lastError = `strategy ${strategy.name} threw: ${(e as Error).message}`;
      }
    }

    return {
      solved: false,
      method: 'all_strategies_failed',
      attempts: scored.length,
      captchaType: info.type,
      error: lastError || 'unknown',
      visionTrace: trace,
    };
  }
}
