/**
 * Planner agent — high-level strategy and task validation.
 *
 * Pattern from nanobrowser (agents/planner.ts).
 */

import type { LLMProvider } from '../llm/provider.js';
import type { PageState } from '../browser/dom.js';
import type { PlannerOutput, StepInfo } from './types.js';
import { formatStateMessage } from './state.js';
import { getPlannerSystemPrompt } from './prompts/planner.js';
import { jsonrepair } from 'jsonrepair';

export class PlannerAgent {
  constructor(private llm: LLMProvider) {}

  /**
   * Evaluate task progress and provide strategic guidance.
   */
  async plan(
    task: string,
    state: PageState,
    historySummary: string,
    stepInfo: StepInfo,
  ): Promise<PlannerOutput> {
    const stateText = formatStateMessage(state, stepInfo);

    const messages = [
      { role: 'system' as const, content: getPlannerSystemPrompt() },
      {
        role: 'user' as const,
        content: `Task: ${task}\n\nCurrent page state:\n${stateText}\n\nProgress so far:\n${historySummary || 'Just started'}`,
      },
    ];

    const response = await this.llm.chatWithRetry(messages, {
      temperature: 0.2,
      maxTokens: 2048,
    });

    return parsePlannerResponse(response.content);
  }
}

/**
 * Parse the planner's JSON response.
 */
function parsePlannerResponse(content: string): PlannerOutput {
  const defaultOutput: PlannerOutput = {
    observation: '',
    challenges: '',
    done: false,
    nextSteps: '',
    finalAnswer: '',
    reasoning: '',
    webTask: true,
  };

  try {
    const parsed = JSON.parse(content);
    return { ...defaultOutput, ...mapKeys(parsed) };
  } catch {
    // Try markdown extraction
    const jsonMatch = content.match(/```(?:json)?\s*([\s\S]*?)```/);
    if (jsonMatch) {
      try {
        const parsed = JSON.parse(jsonMatch[1].trim());
        return { ...defaultOutput, ...mapKeys(parsed) };
      } catch {
        // Fall through
      }
    }

    // Try repair
    try {
      const repaired = jsonrepair(content);
      const parsed = JSON.parse(repaired);
      return { ...defaultOutput, ...mapKeys(parsed) };
    } catch {
      console.error('Failed to parse planner response:', content.substring(0, 200));
      return {
        ...defaultOutput,
        observation: 'Failed to parse planner response',
        nextSteps: 'Continue with previous plan',
      };
    }
  }
}

/** Map snake_case keys to camelCase. */
function mapKeys(obj: Record<string, unknown>): Partial<PlannerOutput> {
  return {
    observation: String(obj.observation || ''),
    challenges: String(obj.challenges || ''),
    done: Boolean(obj.done),
    nextSteps: String(obj.next_steps || obj.nextSteps || ''),
    finalAnswer: String(obj.final_answer || obj.finalAnswer || ''),
    reasoning: String(obj.reasoning || ''),
    webTask: obj.web_task !== undefined ? Boolean(obj.web_task) : (obj.webTask !== undefined ? Boolean(obj.webTask) : true),
  };
}
