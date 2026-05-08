/**
 * Navigator agent — decides and executes browser actions.
 *
 * Pattern from nanobrowser (agents/navigator.ts).
 */

import type { LLMProvider } from '../llm/provider.js';
import type { PageWrapper } from '../browser/page.js';
import type { PageState } from '../browser/dom.js';
import type { ActionResult, AgentOptions, NavigatorResponse, StepInfo } from './types.js';
import { ActionRegistry } from './actions/registry.js';
import { MessageManager } from './messages.js';
import { getNavigatorSystemPrompt } from './prompts/navigator.js';
import { jsonrepair } from 'jsonrepair';
import { detectCaptcha } from '../browser/captcha.js';
import { solveCaptchaViaRegistry } from '../browser/captcha/orchestrator.js';
import type { HumanInputManager } from './human-input.js';
import { buildObservation, coordBand, type ActionKind, type StepOutcome, type VisibleChange } from './step-observation.js';
import { getDomainStats, hostKey } from '../browser/captcha/domain-stats.js';
import { feedbackBus } from './feedback-bus.js';

// Actions that likely change the page — stop sequence after them
const PAGE_CHANGING_ACTIONS = new Set([
  'click_element', 'navigate', 'search_google', 'go_back', 'open_tab',
]);

/** Map nanobot action names → StepObservation.action kinds. Unknown
 *  action names simply skip reward emission (best-effort). */
const ACTION_KIND_MAP: Record<string, ActionKind> = {
  click_element: 'click',
  input_text: 'type',
  scroll: 'scroll',
  navigate: 'navigate',
  go_back: 'navigate',
  open_tab: 'navigate',
  search_google: 'navigate',
};

/** Pull out coord hints from action params if present; most actions
 *  don't have them, but click/scroll sometimes carry x/y fields. */
function extractCoords(params: unknown): { x: number; y: number } | undefined {
  if (!params || typeof params !== 'object') return undefined;
  const p = params as Record<string, unknown>;
  const x = typeof p.x === 'number' ? p.x : null;
  const y = typeof p.y === 'number' ? p.y : null;
  if (x != null && y != null) return { x, y };
  return undefined;
}

/** Per-URL captcha-failure tracking to escalate to human handoff. */
const FORCE_HANDOFF_AFTER = 2;

/** Optional per-session context the navigator forwards into the captcha
 *  orchestrator. None of these are required — when omitted, only the
 *  strategies that don't need a humanInput manager will run. */
export interface NavigatorCaptchaContext {
  humanInput?: HumanInputManager;
  sessionId?: string;
  publicBaseUrl?: string;
  /** Initial budget for human handoffs. Default 1. */
  humanHandoffBudget?: number;
}

export class NavigatorAgent {
  private stepInfo: StepInfo = { current: 0, max: 100 };
  /** url -> consecutive failed captcha-solve attempts on that URL. */
  private captchaFailures = new Map<string, number>();
  /** Remaining handoffs across the navigator's lifetime. */
  private handoffsRemaining: number;

  constructor(
    private llm: LLMProvider,
    private actionRegistry: ActionRegistry,
    private messageManager: MessageManager,
    private options: AgentOptions,
    private captchaCtx: NavigatorCaptchaContext = {},
  ) {
    this.handoffsRemaining = captchaCtx.humanHandoffBudget ?? 1;
  }

  /** Initialize the message history for a new task. */
  initTask(task: string): void {
    const systemPrompt = getNavigatorSystemPrompt(
      this.actionRegistry.getPrompt(),
      this.options.maxActionsPerStep,
    );
    this.messageManager.initTask(systemPrompt, task);
  }

  setStepInfo(info: StepInfo): void {
    this.stepInfo = info;
  }

  /**
   * Execute one navigator step:
   * 0. Captcha circuit breaker — if a captcha is on the page, route directly
   *    to the captcha orchestrator instead of letting the navigator LLM
   *    iterate on a frozen page (which historically burned the entire
   *    iteration budget making bogus clicks).
   * 1. Get page state
   * 2. Call LLM
   * 3. Parse and execute actions
   */
  async execute(page: PageWrapper): Promise<{ results: ActionResult[]; done: boolean }> {
    // Step 0: captcha short-circuit. We pay one detectCaptcha probe per
    // navigator step; this is cheap (single page.evaluate) compared to the
    // LLM call we save when there's a captcha to solve.
    const captchaShortCircuit = await this.maybeSolveCaptcha(page);
    if (captchaShortCircuit) {
      return captchaShortCircuit;
    }

    // 1. Get current page state
    const state = await page.getState({
      useVision: this.options.useVision,
      includeConsole: true,
    });

    // 1a. Error-page short-circuit: if the last navigation landed on
    // a chrome-error, 4xx body, DNS/TLS failure page, don't bother the
    // LLM with a broken DOM. Surface the error kind and hand back so
    // the caller can retry with a different URL or hand off.
    if (state.errorPage) {
      const { kind, detail } = state.errorPage;
      const msg = `Page reached an error state: ${kind} — ${detail}`;
      console.log(`[navigator] ${msg}`);
      this.messageManager.addActionResults([
        {
          success: false,
          error: msg,
          reason: 'unknown',
          extractedContent: msg,
          includeInMemory: true,
        },
      ]);
      return {
        results: [
          { success: false, error: msg, reason: 'unknown', includeInMemory: true },
        ],
        done: false,
      };
    }

    // 2. Add state to message history
    this.messageManager.addStateMessage(state, this.options.useVision, this.stepInfo);

    // 3. Call LLM
    const response = await this.llm.chatWithRetry(
      this.messageManager.getMessages(),
      { temperature: 0.1, maxTokens: 4096 },
    );

    // 4. Parse JSON response
    const parsed = parseNavigatorResponse(response.content);
    this.messageManager.addModelOutput(response.content);

    if (!parsed) {
      return {
        results: [{ success: false, error: 'Failed to parse navigator response' }],
        done: false,
      };
    }

    // 5. Execute actions sequentially (nanobrowser doMultiAction pattern)
    const results: ActionResult[] = [];
    const actions = parsed.action.slice(0, this.options.maxActionsPerStep);
    let consecutiveErrors = 0;

    for (let i = 0; i < actions.length; i++) {
      const actionObj = actions[i];
      const entries = Object.entries(actionObj);
      if (entries.length === 0) continue;

      const [name, params] = entries[0];

      // Settle pause between actions. The real guard against acting on a
      // stale DOM is the selectorMap-delta check below, not this sleep.
      //
      // Jitter matters: a detector scoring inter-event timing variance
      // flags constant delays as automated. We sample from a clipped
      // gaussian centered at 300ms with heavy tails — a real user's
      // "think about next action" varies from fast (~120ms) to slow (~900ms).
      if (i > 0) {
        const mean = 300;
        const stdev = 180;
        // Box-Muller gaussian, clamped to [120, 900]
        const u1 = Math.random() || Number.MIN_VALUE;
        const u2 = Math.random();
        const z = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
        const sampled = Math.round(mean + z * stdev);
        const delayMs = Math.max(120, Math.min(900, sampled));
        await new Promise((r) => setTimeout(r, delayMs));

        // DOM stability check: break if new elements appeared.
        try {
          const newState = await page.getState({ useVision: false, includeConsole: false });
          const oldCount = state.selectorMap.size;
          const newCount = newState.selectorMap.size;
          if (newCount !== oldCount) {
            // DOM changed — stop multi-action, let next step re-evaluate
            break;
          }
        } catch {
          // State check failed — continue cautiously
        }
      }

      const result = await this.actionRegistry.execute(name, params, page, state);
      results.push(result);

      // Emit a reward observation. Records per-host/strategy/coord-band
      // reward into domain-stats so future runs on the same site can bias
      // toward actions that previously worked. Intentionally cheap: the
      // record hits the in-memory store and flushes debounced.
      try {
        const kind = ACTION_KIND_MAP[name] ?? null;
        if (kind) {
          const urlNow = state.url;
          const outcome: StepOutcome = !result.success
            ? (result.reason === 'nav_pending' ? 'blocked' : 'error')
            : 'success';
          const visibleChange: VisibleChange = PAGE_CHANGING_ACTIONS.has(name)
            ? 'navigation'
            : result.success ? 'minor' : 'none';
          const coords = extractCoords(params);
          const obs = buildObservation({
            action: kind,
            coords,
            outcome,
            visibleChange,
            timestampMs: Date.now(),
            host: hostKey(urlNow),
            strategy: 'navigator',
            target: (params as { selector?: string; index?: number })?.selector ?? String((params as { index?: number })?.index ?? ''),
          });
          if (coords) {
            getDomainStats().recordAction(
              obs.host,
              'navigator',
              coordBand(coords.x, coords.y),
              obs.reward,
            );
          }
          // Broadcast the reward so the Python side / FeedbackBus
          // listeners can observe per-action effectiveness in real time.
          feedbackBus.publish({ kind: 'action_reward', obs });
        }
      } catch {
        // Reward recording is best-effort — never break the action loop.
      }

      if (result.isDone) {
        this.messageManager.addActionResults(results);
        return { results, done: true };
      }

      if (!result.success) {
        consecutiveErrors++;
        // Stop after 3 errors in sequence (from nanobrowser)
        if (consecutiveErrors >= 3) break;
        continue;
      }
      consecutiveErrors = 0;

      // Stop after page-changing actions — state needs refresh
      if (PAGE_CHANGING_ACTIONS.has(name)) break;
    }

    this.messageManager.addActionResults(results);
    return { results, done: false };
  }

  /** Add state to memory without executing (used by planner). */
  async addStateToMemory(page: PageWrapper): Promise<PageState> {
    const state = await page.getState({
      useVision: this.options.useVision,
      includeConsole: true,
    });
    this.messageManager.addStateMessage(state, this.options.useVision, this.stepInfo);
    return state;
  }

  getMessageManager(): MessageManager {
    return this.messageManager;
  }

  /**
   * Detect a captcha on the page; if present, dispatch directly to the
   * captcha registry and return the outcome as an ActionResult — the
   * navigator's LLM is never called for this step. Returns null when there
   * is no captcha (the caller proceeds with normal flow).
   *
   * Per-URL failure tracking: after FORCE_HANDOFF_AFTER consecutive failures
   * on the same URL, we top up the handoff budget and force the registry to
   * try the human-handoff strategy. This stops the worker from looping
   * forever on a captcha that no automated path can solve.
   */
  private async maybeSolveCaptcha(
    page: PageWrapper,
  ): Promise<{ results: ActionResult[]; done: boolean } | null> {
    const rawPage = page.getRawPage();
    let captcha;
    try {
      captcha = await detectCaptcha(rawPage);
    } catch {
      return null;
    }
    if (!captcha) return null;

    const url = rawPage.url();
    const failures = this.captchaFailures.get(url) ?? 0;
    const forceHandoff = failures >= FORCE_HANDOFF_AFTER && this.handoffsRemaining > 0;

    const config = {
      provider: process.env.CAPTCHA_PROVIDER,
      apiKey: process.env.CAPTCHA_API_KEY,
      timeout: 60000,
    };

    let budget = this.handoffsRemaining;
    if (forceHandoff && budget <= 0) budget = 1;

    const result = await solveCaptchaViaRegistry(rawPage, captcha, config, this.llm, {
      sessionId: this.captchaCtx.sessionId,
      humanInput: this.captchaCtx.humanInput,
      humanHandoffBudget: budget,
      publicBaseUrl: this.captchaCtx.publicBaseUrl,
    });

    // Decrement handoff budget if the solver actually used a human.
    if (result.method === 'human_handoff') {
      this.handoffsRemaining = Math.max(0, this.handoffsRemaining - 1);
    }

    let actionResult: ActionResult;
    if (result.solved) {
      this.captchaFailures.delete(url);
      const tag = result.subMethod ? `${result.method}/${result.subMethod}` : result.method;
      actionResult = {
        success: true,
        extractedContent:
          `Captcha cleared via ${tag} ` +
          `(${result.attempts ?? 1} attempt(s), ${result.durationMs ?? '?'}ms). ` +
          `Continue with the original task.`,
        includeInMemory: true,
      };
    } else {
      this.captchaFailures.set(url, failures + 1);
      const exhausted = failures + 1 >= FORCE_HANDOFF_AFTER && this.handoffsRemaining <= 0;
      actionResult = {
        success: false,
        error:
          `Captcha not solved (${result.method}): ${result.error || 'all strategies failed'}.` +
          (exhausted ? ' Human handoff budget exhausted — escalate to user out-of-band.' : ''),
        reason: 'unknown',
      };
    }

    this.messageManager.addActionResults([actionResult]);
    return { results: [actionResult], done: false };
  }
}

/**
 * Parse the navigator's JSON response, handling malformed JSON.
 */
function parseNavigatorResponse(content: string): NavigatorResponse | null {
  try {
    // Try direct parse first
    const parsed = JSON.parse(content);
    return parsed as NavigatorResponse;
  } catch {
    // Try extracting JSON from markdown code blocks
    const jsonMatch = content.match(/```(?:json)?\s*([\s\S]*?)```/);
    if (jsonMatch) {
      try {
        return JSON.parse(jsonMatch[1].trim()) as NavigatorResponse;
      } catch {
        // Fall through to repair
      }
    }

    // Try repairing malformed JSON
    try {
      const repaired = jsonrepair(content);
      return JSON.parse(repaired) as NavigatorResponse;
    } catch {
      console.error('Failed to parse navigator response:', content.substring(0, 200));
      return null;
    }
  }
}
