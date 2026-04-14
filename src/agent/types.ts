/**
 * Core agent types and interfaces.
 */

/**
 * Why a tool failed. Enables the LLM to pick a different tactic
 * without re-screenshotting to re-diagnose.
 */
export type FailureReason =
  | 'element_not_found'   // selectorMap[index] returned null
  | 'detached'            // element removed from DOM between read and act
  | 'not_visible'         // display:none / visibility:hidden / zero-size
  | 'off_viewport'        // scrolled out; scroll-into-view retry also failed
  | 'element_covered'     // elementFromPoint returns a different node
  | 'disabled'            // has disabled attr / aria-disabled
  | 'stale_selector'      // selector no longer matches
  | 'nav_pending'         // navigation in flight, action would race
  | 'unknown';

export interface ActionResult {
  success: boolean;
  extractedContent?: string;
  error?: string;
  /** Machine-readable failure category (only on !success). */
  reason?: FailureReason;
  /** Which fallback tiers were attempted (e.g. ['cdp', 'puppeteer']). */
  tried?: string[];
  /** Concrete alternative tactics the LLM should try next. */
  alternatives?: string[];
  isDone?: boolean;
  includeInMemory?: boolean;
}

export interface AgentOptions {
  maxSteps: number;
  maxActionsPerStep: number;
  maxFailures: number;
  useVision: boolean;
  planningInterval: number;
}

export const DEFAULT_OPTIONS: AgentOptions = {
  maxSteps: 100,
  maxActionsPerStep: 10,
  maxFailures: 3,
  useVision: true,
  planningInterval: 3,
};

export interface StepInfo {
  current: number;
  max: number;
}

export interface ExecutorResult {
  success: boolean;
  finalAnswer?: string;
  error?: string;
  screenshots?: string[];
}

export interface PlannerOutput {
  observation: string;
  challenges: string;
  done: boolean;
  nextSteps: string;
  finalAnswer: string;
  reasoning: string;
  webTask: boolean;
}

export interface NavigatorResponse {
  current_state: {
    evaluation_previous_goal: string;
    memory: string;
    next_goal: string;
  };
  action: Array<Record<string, Record<string, unknown>>>;
}
