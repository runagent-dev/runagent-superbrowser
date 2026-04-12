/**
 * Captcha watchdog — blocks agent tool calls while a solve is in progress.
 *
 * Pattern adapted from browser-use's captcha_watchdog: when the server
 * detects a captcha (on navigate / open / explicit detect), it marks the
 * session as "solving". Agent tools call waitIfCaptchaSolving() before
 * executing, so they pause while the solver finishes rather than
 * interleaving (which would thrash cookies, move the mouse, or trigger
 * anti-bot scoring).
 *
 * The solver emits structured outcomes (success/failed/timeout + vendor +
 * duration). Agent-facing wrappers inject these into LLM history so the
 * agent knows the captcha was handled and can proceed.
 */

export type CaptchaWaitOutcome = 'success' | 'failed' | 'timeout' | 'not_solving';

export interface CaptchaWaitResult {
  outcome: CaptchaWaitOutcome;
  /** 'cloudflare' | 'google' | 'hcaptcha' | 'custom' — populated when known. */
  vendor?: string;
  /** Duration spent waiting (ms). */
  durationMs: number;
  /** Optional detail from the solver (method, subMethod, error). */
  detail?: string;
}

interface SolverEntry {
  startedAt: number;
  timer: NodeJS.Timeout | null;
  /** All waiters currently blocked on this session's solve. */
  waiters: Array<(r: CaptchaWaitResult) => void>;
}

class CaptchaWatchdog {
  /** sessionId → active solve entry (populated while solve in progress). */
  private solving = new Map<string, SolverEntry>();

  /** Register that a solve has started for sessionId. Safe to call repeatedly. */
  notifySolveStarted(sessionId: string, timeoutMs = 90000): void {
    if (this.solving.has(sessionId)) return;
    const startedAt = Date.now();
    const timer = setTimeout(() => {
      this.notifySolveFinished(sessionId, {
        outcome: 'timeout',
        detail: `watchdog timeout after ${timeoutMs}ms`,
      });
    }, timeoutMs);
    this.solving.set(sessionId, { startedAt, timer, waiters: [] });
  }

  /** Solver reports completion — resolves all waiters on this sessionId. */
  notifySolveFinished(
    sessionId: string,
    result: Omit<CaptchaWaitResult, 'durationMs'> & { durationMs?: number },
  ): void {
    const entry = this.solving.get(sessionId);
    if (!entry) return;
    if (entry.timer) clearTimeout(entry.timer);
    const finalResult: CaptchaWaitResult = {
      ...result,
      durationMs: result.durationMs ?? (Date.now() - entry.startedAt),
    };
    // Resolve all pending waiters with the same outcome.
    for (const w of entry.waiters) {
      try { w(finalResult); } catch { /* ignore callback errors */ }
    }
    this.solving.delete(sessionId);
  }

  /** Is a solve currently in progress for sessionId? */
  isSolving(sessionId: string): boolean {
    return this.solving.has(sessionId);
  }

  /**
   * Block until solving finishes (or timeout). Returns 'not_solving' if
   * nothing is in progress so callers can short-circuit cheaply.
   */
  async waitIfSolving(sessionId: string, maxWaitMs = 90000): Promise<CaptchaWaitResult> {
    const entry = this.solving.get(sessionId);
    if (!entry) return { outcome: 'not_solving', durationMs: 0 };
    return new Promise<CaptchaWaitResult>((resolve) => {
      const start = Date.now();
      let settled = false;
      const localTimer = setTimeout(() => {
        if (settled) return;
        settled = true;
        resolve({
          outcome: 'timeout',
          durationMs: Date.now() - start,
          detail: `waitIfSolving: local ${maxWaitMs}ms cap reached`,
        });
      }, maxWaitMs);
      entry.waiters.push((r) => {
        if (settled) return;
        settled = true;
        clearTimeout(localTimer);
        resolve(r);
      });
    });
  }
}

/** Process-wide singleton. Solver subsystem + agent tool layer share it. */
export const captchaWatchdog = new CaptchaWatchdog();

/** Format a wait result into the agent-facing guidance string. */
export function formatWaitResult(r: CaptchaWaitResult): string {
  if (r.outcome === 'not_solving') return '';
  const vendor = r.vendor ? ` ${r.vendor}` : '';
  if (r.outcome === 'success') {
    return `[captcha] Waited ${(r.durationMs / 1000).toFixed(1)}s for${vendor} captcha — solved. Continuing.`;
  }
  if (r.outcome === 'failed') {
    return `[captcha] Waited ${(r.durationMs / 1000).toFixed(1)}s for${vendor} captcha — FAILED (${r.detail || 'unknown'}). You may need to retry or ask_user.`;
  }
  return `[captcha] Waited ${(r.durationMs / 1000).toFixed(1)}s for${vendor} captcha — TIMEOUT.`;
}
