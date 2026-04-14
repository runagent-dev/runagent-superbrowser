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
import { feedbackBus } from '../../agent/feedback-bus.js';
import { saveDomainCookies } from './cookie-jar.js';

/**
 * Fast-to-human reordering: keep the #1 scored non-handoff candidate at
 * the front, then move human_handoff to immediately follow it. Everything
 * else is dropped from consideration by the `autoAttemptsDone >= 1` guard
 * in dispatch(). We don't mutate the input array.
 */
function reorderForFastToHuman<T extends { strategy: CaptchaStrategy; score: number }>(
  scored: readonly T[],
): T[] {
  const handoff = scored.find((s) => s.strategy.name === 'human_handoff');
  const autos = scored.filter((s) => s.strategy.name !== 'human_handoff');
  const top = autos.slice(0, 1);
  return handoff ? [...top, handoff, ...autos.slice(1)] : [...autos];
}

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

    // Broadcast captcha-active so the Python bridge can pause other
    // tool calls for the duration. Emitted once per dispatch; the
    // individual strategies don't each fire this because that would
    // spam the bus when the dispatcher falls through strategies.
    feedbackBus.publish({ kind: 'captcha_active', host, strategy: scored[0].strategy.name });

    // Under SUPERBROWSER_CAPTCHA_POLICY=fast_to_human, the orchestrator
    // sets ctx.fastToHuman. When the first non-handoff strategy fails we
    // skip any remaining auto strategies and jump straight to human_handoff
    // — keeps the site from seeing 7 consecutive solver pings that tip it
    // into "this is a bot" mode.
    const runOrder = ctx.fastToHuman
      ? reorderForFastToHuman(scored)
      : scored;
    let autoAttemptsDone = 0;

    try {
      for (const { strategy, score } of runOrder) {
        if (Date.now() - ctx.startTime > ctx.deadlineMs) {
          lastError = `solver deadline ${ctx.deadlineMs}ms exceeded`;
          break;
        }
        if (
          ctx.fastToHuman
          && strategy.name !== 'human_handoff'
          && autoAttemptsDone >= 1
        ) {
          // One auto attempt already failed — skip the rest, let the
          // human_handoff entry at the end of runOrder take over.
          continue;
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
            feedbackBus.publish({ kind: 'captcha_done', host, solved: true, strategy: strategy.name });
            // Opportunistic cookie save — whenever any strategy clears a
            // captcha (auto or human), persist the bot-protection cookies
            // so the next session on the same task+domain doesn't need to
            // re-solve. Gated internally on SUPERBROWSER_COOKIE_JAR=1.
            void saveDomainCookies(ctx.page, ctx.page.url()).catch(() => { /* best-effort */ });
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
        if (strategy.name !== 'human_handoff') autoAttemptsDone += 1;
      }

      feedbackBus.publish({ kind: 'captcha_done', host, solved: false });
      return {
        solved: false,
        method: 'all_strategies_failed',
        attempts: scored.length,
        captchaType: info.type,
        error: lastError || 'unknown',
        visionTrace: trace,
      };
    } catch (e) {
      // Safety net: always emit captcha_done so the Python bridge
      // doesn't get stuck thinking a solve is in progress.
      feedbackBus.publish({ kind: 'captcha_done', host, solved: false });
      throw e;
    }
  }
}
