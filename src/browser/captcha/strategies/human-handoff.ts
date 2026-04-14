/**
 * Human handoff strategy — the final fallback when auto-solve fails.
 *
 * When every programmatic strategy has exhausted (token / AI vision /
 * 2captcha / generic vision), we pause the agent and surface the challenge
 * to the real user via a `HumanInputManager.requestInput()` call. The user
 * opens the remote view URL in their own browser, clicks the captcha like
 * a human, the captcha clears, and the agent resumes.
 *
 * We concurrently poll `detectCaptcha(page)` so that when the user solves
 * the challenge by clicking in the view UI, we detect the resolution from
 * the page state itself — no explicit "Done" signal required. The user may
 * still click "Done" in the UI to break out early; either resolves us.
 *
 * Gates:
 *   - Only runs when ctx.humanInput is present (i.e., session was created
 *     with enableHumanHandoff: true).
 *   - Only runs when ctx.humanHandoffBudget > 0 — default 1 per session,
 *     configurable via SUPERBROWSER_MAX_HUMAN_HANDOFFS.
 *
 * Why last: it's the highest-friction UX (pauses for up to 5 minutes while
 * a human is summoned), so the orchestrator only reaches it when automation
 * has genuinely failed.
 */

import { detectCaptcha, type CaptchaInfo } from '../../captcha.js';
import { captchaWatchdog } from '../../captcha-watchdog.js';
import { getHandoffLedger } from '../handoff-ledger.js';
import { hostKey } from '../domain-stats.js';
import { sanitizeImageBuffer } from '../../image-safety.js';
import { feedbackBus } from '../../../agent/feedback-bus.js';
import { saveDomainCookies } from '../cookie-jar.js';
import type { CaptchaStrategy, RichSolveResult, StrategyContext } from '../types.js';

const HANDOFF_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes
const POLL_INTERVAL_MS = 2000;

/**
 * Print an unmissable banner so a human watching CLI logs sees the URL.
 * The previous implementation buried it in a single `console.log` line that
 * was easy to scroll past in a noisy worker stream.
 */
function printHandoffBanner(viewUrl: string, captchaType: string): void {
  const lines = [
    '',
    '================ HUMAN HANDOFF NEEDED ================',
    `  Captcha type: ${captchaType}`,
    `  Open this URL to solve it:`,
    `    ${viewUrl}`,
    `  Timeout: ${Math.round(HANDOFF_TIMEOUT_MS / 60000)} min`,
    '======================================================',
    '',
  ];
  // stderr so it survives stdout buffering and is greppable in worker logs.
  for (const line of lines) process.stderr.write(line + '\n');
}

/**
 * Best-effort webhook POST when the handoff fires. Lets a downstream
 * notifier (WhatsApp/Slack bridge) push the URL to the user out-of-band
 * without coupling this module to any specific transport. Failures are
 * swallowed — if the webhook is down, the CLI banner is the fallback.
 */
async function fireHandoffWebhook(payload: Record<string, unknown>): Promise<void> {
  const url = process.env.HANDOFF_WEBHOOK_URL;
  if (!url) return;
  try {
    await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      // Don't block a captcha solve waiting on a slow webhook.
      signal: AbortSignal.timeout(5000),
    });
  } catch (e) {
    process.stderr.write(`[human-handoff] webhook POST failed: ${(e as Error).message}\n`);
  }
}

export const humanHandoffStrategy: CaptchaStrategy = {
  name: 'human_handoff',
  supportedTypes: [
    'recaptcha',
    'hcaptcha',
    'turnstile',
    'slider',
    'image',
    'text',
    'visual_puzzle',
    'unknown',
  ],
  // Lowest priority — only tried after every auto strategy has failed.
  priority: 5,
  // No API cost (but has real human-time cost, which is why the budget caps
  // how often it fires per session).
  estimatedCostCents: 0,
  requiresLLM: false,
  requiresApiKey: false,

  canHandle(_info: CaptchaInfo, ctx: StrategyContext): boolean {
    if (!ctx.humanInput) return false;
    const budget = ctx.humanHandoffBudget ?? 0;
    if (budget <= 0) return false;
    // Cross-session continuity: if the user already cleared this domain
    // for this task within RECENT_WINDOW_MS, don't re-prompt them. The
    // captcha clearly came back (cookies expired or didn't propagate), so
    // we'd rather let the automated strategies try once more or fail the
    // task than ask the same human to solve the same wall twice.
    try {
      const domain = hostKey(ctx.page.url());
      const scope = process.env.SUPERBROWSER_TASK_ID ?? ctx.sessionId ?? domain;
      if (getHandoffLedger().wasRecentlySolved(scope, domain)) return false;
    } catch { /* ledger is advisory */ }
    return true;
  },

  async run(info: CaptchaInfo, ctx: StrategyContext): Promise<RichSolveResult> {
    const start = Date.now();
    const manager = ctx.humanInput;
    if (!manager) {
      return {
        solved: false, method: 'human_handoff', attempts: 0,
        error: 'no human-input manager on context',
      };
    }

    // View URL surfaced to the user. Falls back to a bare path if no public
    // base URL was configured — the orchestrator will still log the path,
    // but the user will need to know the host to open it.
    const viewUrl = ctx.sessionId
      ? (ctx.publicBaseUrl
        ? `${ctx.publicBaseUrl.replace(/\/$/, '')}/session/${ctx.sessionId}/view`
        : `/session/${ctx.sessionId}/view`)
      : '(view URL unavailable — no sessionId in context)';

    if (ctx.sessionId && !ctx.publicBaseUrl) {
      // Most users hitting handoff are on a remote host; relative paths
      // aren't openable from a phone. Loud warning so this gets configured.
      process.stderr.write(
        '[human-handoff] WARNING: PUBLIC_BASE_URL is not set — view URL is ' +
        'relative and not openable from outside the server.\n',
      );
    }

    // Capture a screenshot for UI fallbacks that can't open the live view.
    let screenshot: string | undefined;
    try {
      const buf = await ctx.page.screenshot({ type: 'jpeg', quality: 70, fullPage: false });
      const san = await sanitizeImageBuffer(Buffer.from(buf));
      screenshot = san.buffer.toString('base64');
    } catch {
      // Screenshot is best-effort.
    }

    // Signal watchdog so other tool calls pause while the human works.
    if (ctx.sessionId) {
      captchaWatchdog.notifySolveStarted(ctx.sessionId, HANDOFF_TIMEOUT_MS);
    }

    // Surface to humans through every available channel: bordered banner
    // on stderr (CLI users) + optional webhook (WhatsApp / Slack / etc.)
    // + feedbackBus push so any connected WebSocket client gets notified
    // without polling.
    printHandoffBanner(viewUrl, info.type);
    let pageUrl = '';
    try { pageUrl = ctx.page.url(); } catch { /* page may have closed */ }
    let pageTitle = '';
    try { pageTitle = await ctx.page.title(); } catch { /* page may have closed */ }

    if (ctx.sessionId) {
      feedbackBus.publish({
        kind: 'awaiting_human',
        detail: {
          sessionId: ctx.sessionId,
          viewUrl,
          captchaType: info.type,
          timeoutMs: HANDOFF_TIMEOUT_MS,
          pageUrl,
        },
      });
    }

    // WhatsApp-ready payload: pre-formatted caption the bot can forward
    // verbatim, plus explicit MIME so receivers don't have to sniff.
    let hostname = '';
    try { hostname = pageUrl ? new URL(pageUrl).hostname : ''; } catch { /* */ }
    const caption = hostname
      ? `Captcha on ${hostname} — tap ${viewUrl} to solve (${Math.round(HANDOFF_TIMEOUT_MS / 60000)} min).`
      : `Captcha needs a human — tap ${viewUrl} to solve (${Math.round(HANDOFF_TIMEOUT_MS / 60000)} min).`;
    void fireHandoffWebhook({
      event: 'human_handoff_required',
      url: viewUrl,
      sessionId: ctx.sessionId,
      taskId: process.env.SUPERBROWSER_TASK_ID,
      captchaType: info.type,
      pageUrl,
      pageTitle,
      timeoutMs: HANDOFF_TIMEOUT_MS,
      caption,
      // Keep the screenshot small in the webhook payload — receivers that
      // only want a thumbnail can upscale; ones that don't can ignore.
      screenshot,
      screenshotMimeType: screenshot ? 'image/jpeg' : undefined,
    });

    const message =
      `Auto-solve exhausted for ${info.type} captcha. ` +
      `Please open this URL in your browser and click through the challenge:\n  ${viewUrl}\n` +
      `I'll detect when the captcha clears and resume automatically. ` +
      `(Timeout: ${Math.round(HANDOFF_TIMEOUT_MS / 60000)} min.)`;

    // Fire-and-wait the human-input request in parallel with the page-state
    // poll loop. Whichever resolves first wins; the other is cancelled.
    let settled = false;
    let finalOutcome: RichSolveResult | null = null;

    const requestPromise = manager.requestInput('captcha', message, {
      screenshot,
      timeout: HANDOFF_TIMEOUT_MS,
    });

    // Poll the page for captcha clearance. This is the primary success
    // signal — the user just clicks the captcha, and we detect it gone.
    const pollPromise = (async (): Promise<RichSolveResult> => {
      while (!settled && (Date.now() - start) < HANDOFF_TIMEOUT_MS) {
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
        if (settled) break;
        try {
          const current = await detectCaptcha(ctx.page);
          if (!current) {
            return {
              solved: true,
              method: 'human_handoff',
              subMethod: 'page_poll_cleared',
              attempts: 1,
              durationMs: Date.now() - start,
            };
          }
          // Some solvers (reCAPTCHA autosolve marker) tag `solved=true` on
          // the CaptchaInfo while leaving the widget visible — honor that.
          if (current.solved) {
            return {
              solved: true,
              method: 'human_handoff',
              subMethod: 'page_poll_flagged_solved',
              attempts: 1,
              durationMs: Date.now() - start,
            };
          }
        } catch {
          // detectCaptcha may throw during navigation — ignore and keep polling.
        }
      }
      return {
        solved: false,
        method: 'human_handoff',
        subMethod: 'page_poll_timeout',
        attempts: 1,
        durationMs: Date.now() - start,
        error: 'timed out waiting for human to clear captcha',
      };
    })();

    try {
      finalOutcome = await Promise.race([
        (async (): Promise<RichSolveResult> => {
          const r = await requestPromise;
          if (!r || r.cancelled) {
            // User cancelled or timeout fired. If it was a timeout, the
            // poll branch is also about to resolve — race above will pick
            // whichever is first.
            return {
              solved: false,
              method: 'human_handoff',
              subMethod: r?.cancelled ? 'user_cancelled' : 'request_timeout',
              attempts: 1,
              durationMs: Date.now() - start,
              error: r?.cancelled ? 'user cancelled' : 'request timed out',
            };
          }
          // User clicked "Done". Re-check captcha to confirm.
          try {
            const current = await detectCaptcha(ctx.page);
            if (!current || current.solved) {
              return {
                solved: true,
                method: 'human_handoff',
                subMethod: 'user_done_confirmed',
                attempts: 1,
                durationMs: Date.now() - start,
              };
            }
            return {
              solved: false,
              method: 'human_handoff',
              subMethod: 'user_done_but_captcha_present',
              attempts: 1,
              durationMs: Date.now() - start,
              error: 'user clicked done but captcha still detected',
            };
          } catch {
            // If we can't re-detect, trust the user's signal.
            return {
              solved: true,
              method: 'human_handoff',
              subMethod: 'user_done_unverified',
              attempts: 1,
              durationMs: Date.now() - start,
            };
          }
        })(),
        pollPromise,
      ]);
    } finally {
      settled = true;
      // Cancel the opposite pending side so it doesn't leak a timer.
      if (manager.hasPending) {
        try { manager.cancel(); } catch { /* ignore */ }
      }
      if (ctx.sessionId) {
        captchaWatchdog.notifySolveFinished(ctx.sessionId, {
          outcome: finalOutcome?.solved ? 'success' : 'failed',
          vendor: 'human',
          detail: finalOutcome?.subMethod,
        });
      }
      // Record a successful handoff in the ledger so a later session for
      // the same task on the same domain doesn't re-prompt the user.
      // Scope: prefer SUPERBROWSER_TASK_ID (set by the python bridge when
      // delegating between worker re-spawns), then sessionId, then bare
      // domain as a last resort.
      if (finalOutcome?.solved) {
        try {
          const domain = hostKey(ctx.page.url());
          const scope = process.env.SUPERBROWSER_TASK_ID ?? ctx.sessionId ?? domain;
          getHandoffLedger().record(scope, domain);
        } catch { /* ledger is best-effort */ }
        // The human just proved they're real on this domain — persist the
        // bot-protection cookies (cf_clearance, datadome, ...) so a later
        // session on the same task+domain starts already cleared. Gated
        // internally on SUPERBROWSER_COOKIE_JAR=1.
        try {
          await saveDomainCookies(ctx.page, ctx.page.url());
        } catch { /* best-effort */ }
      }
    }

    return finalOutcome!;
  },
};
