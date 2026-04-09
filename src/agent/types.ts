/**
 * Core agent types and interfaces.
 */

export interface ActionResult {
  success: boolean;
  extractedContent?: string;
  error?: string;
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
