/**
 * Main execution loop: Navigator + Planner orchestration.
 *
 * Directly adapted from nanobrowser's Executor class.
 */

import type { LLMProvider } from '../llm/provider.js';
import type { PageWrapper } from '../browser/page.js';
import type { ActionResult, AgentOptions, ExecutorResult, PlannerOutput } from './types.js';
import { DEFAULT_OPTIONS } from './types.js';
import { NavigatorAgent } from './navigator.js';
import { PlannerAgent } from './planner.js';
import { MessageManager } from './messages.js';
import { ActionRegistry } from './actions/registry.js';
import { buildDefaultActionRegistry } from './action-builder.js';
import { EventManager, ExecutionState, Actor, type EventCallback } from './events.js';
import { HumanInputManager, type HumanInputRequest, type HumanInputResponse } from './human-input.js';

export class BrowserExecutor {
  private navigator: NavigatorAgent;
  private planner: PlannerAgent;
  private actionRegistry: ActionRegistry;
  private eventManager: EventManager;
  private humanInput: HumanInputManager;
  private consecutiveFailures = 0;
  private nSteps = 0;
  private historySummary: string[] = [];
  private stopped = false;
  private paused = false;

  constructor(
    private page: PageWrapper,
    private llm: LLMProvider,
    private options: AgentOptions = DEFAULT_OPTIONS,
  ) {
    this.humanInput = new HumanInputManager();
    this.actionRegistry = buildDefaultActionRegistry(this.humanInput);
    this.eventManager = new EventManager();
    const messageManager = new MessageManager();
    this.navigator = new NavigatorAgent(llm, this.actionRegistry, messageManager, options);
    this.planner = new PlannerAgent(llm);
  }

  /** Subscribe to execution events (from nanobrowser). */
  onEvent(callback: EventCallback): void {
    this.eventManager.subscribe(callback);
  }

  /** Add a follow-up task (from nanobrowser addFollowUpTask). */
  addFollowUpTask(task: string): void {
    this.navigator.getMessageManager().addNewTask(task);
  }

  /** Pause execution. */
  pause(): void {
    this.paused = true;
    this.eventManager.emit(Actor.SYSTEM, ExecutionState.TASK_PAUSE, 'Paused');
  }

  /** Resume execution. */
  resume(): void {
    this.paused = false;
    this.eventManager.emit(Actor.SYSTEM, ExecutionState.TASK_RESUME, 'Resumed');
  }

  /** Cancel execution. */
  cancel(): void {
    this.stopped = true;
    this.eventManager.emit(Actor.SYSTEM, ExecutionState.TASK_CANCEL, 'Cancelled');
  }

  /**
   * Execute a browser task end-to-end.
   *
   * Core loop (from nanobrowser Executor):
   * 1. Run planner every N steps or when navigator signals done
   * 2. Run navigator to decide and execute actions
   * 3. Track failures and stop if max exceeded
   */
  async executeTask(task: string): Promise<ExecutorResult> {
    const MAX_TOTAL_TIME = parseInt(process.env.TASK_TIMEOUT || '300000', 10); // 5 min default
    const startTime = Date.now();

    console.log(`🚀 Executing task: ${task}`);
    this.eventManager.emit(Actor.SYSTEM, ExecutionState.TASK_START, task);
    this.navigator.initTask(task);

    // Setup dialog and console handlers
    await this.page.setupDialogHandler();
    await this.page.enableConsoleCapture();

    let navigatorDone = false;
    let latestPlan: PlannerOutput | null = null;

    for (let step = 0; step < this.options.maxSteps; step++) {
      // Total time check
      if (Date.now() - startTime > MAX_TOTAL_TIME) {
        this.eventManager.emit(Actor.SYSTEM, ExecutionState.TASK_FAIL, 'Total timeout exceeded');
        return { success: false, error: `Execution timeout: exceeded ${MAX_TOTAL_TIME / 1000}s` };
      }

      // Pause/stop checks (from nanobrowser executor)
      if (this.stopped) {
        return { success: false, error: 'Task cancelled' };
      }
      while (this.paused) {
        await new Promise((r) => setTimeout(r, 200));
        if (this.stopped) return { success: false, error: 'Task cancelled' };
      }

      console.log(`🔄 Step ${step + 1}/${this.options.maxSteps}`);
      this.eventManager.emit(Actor.SYSTEM, ExecutionState.STEP_START, `Step ${step + 1}`);

      this.navigator.setStepInfo({
        current: step + 1,
        max: this.options.maxSteps,
      });

      // Run planner periodically or when navigator signals done
      if (step % this.options.planningInterval === 0 || navigatorDone) {
        navigatorDone = false;

        try {
          const state = await this.page.getState({
            useVision: this.options.useVision,
            includeConsole: true,
          });

          latestPlan = await this.planner.plan(
            task,
            state,
            this.historySummary.join('\n'),
            { current: step + 1, max: this.options.maxSteps },
          );

          console.log(`📋 Planner: ${latestPlan.done ? '✅ DONE' : latestPlan.nextSteps.substring(0, 100)}`);

          if (latestPlan.done) {
            this.eventManager.emit(Actor.PLANNER, ExecutionState.TASK_OK, latestPlan.finalAnswer);
            return {
              success: true,
              finalAnswer: latestPlan.finalAnswer,
            };
          }

          // Add plan guidance to navigator's message history
          this.navigator.getMessageManager().addPlanMessage(
            JSON.stringify({
              next_steps: latestPlan.nextSteps,
              challenges: latestPlan.challenges,
            }),
          );
        } catch (err) {
          console.error(`Planner error: ${err instanceof Error ? err.message : err}`);
          this.consecutiveFailures++;
          if (this.consecutiveFailures >= this.options.maxFailures) {
            return {
              success: false,
              error: `Planner failed ${this.options.maxFailures} times consecutively`,
            };
          }
        }
      }

      // Run navigator
      try {
        const { results, done } = await this.navigator.execute(this.page);
        this.nSteps++;

        // Record history
        const summary = results
          .filter((r) => r.extractedContent || r.error)
          .map((r) => r.error ? `❌ ${r.error}` : r.extractedContent)
          .join('; ');
        if (summary) {
          this.historySummary.push(`Step ${step + 1}: ${summary.substring(0, 200)}`);
          // Keep history manageable
          if (this.historySummary.length > 20) {
            this.historySummary = this.historySummary.slice(-15);
          }
        }

        if (done) {
          navigatorDone = true;
          console.log('🔄 Navigator signals completion — planner will validate');
        }

        // Track failures
        if (results.every((r: ActionResult) => !r.success)) {
          this.consecutiveFailures++;
          console.log(`⚠️ Consecutive failures: ${this.consecutiveFailures}/${this.options.maxFailures}`);

          if (this.consecutiveFailures >= this.options.maxFailures) {
            let screenshot: string | undefined;
            try {
              screenshot = await this.page.screenshotBase64();
            } catch {
              // Screenshot may fail
            }
            return {
              success: false,
              error: 'Max consecutive failures reached',
              screenshots: screenshot ? [screenshot] : undefined,
            };
          }
        } else {
          this.consecutiveFailures = 0;
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error(`Navigator error: ${msg}`);
        this.consecutiveFailures++;
        if (this.consecutiveFailures >= this.options.maxFailures) {
          return {
            success: false,
            error: `Navigator failed: ${msg}`,
          };
        }
      }
    }

    return {
      success: false,
      error: `Max steps (${this.options.maxSteps}) reached without completing the task`,
    };
  }

  /** Get the number of steps executed. */
  getStepCount(): number {
    return this.nSteps;
  }

  /** Get the pending human input request (if any). */
  getPendingHumanInput(): HumanInputRequest | null {
    return this.humanInput.getPendingRequest();
  }

  /** Provide a response to the pending human input request. */
  provideHumanInput(response: HumanInputResponse): boolean {
    return this.humanInput.provideInput(response);
  }

  /** Check if the executor is waiting for human input. */
  get isWaitingForHuman(): boolean {
    return this.humanInput.hasPending;
  }
}
