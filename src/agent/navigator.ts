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

// Actions that likely change the page — stop sequence after them
const PAGE_CHANGING_ACTIONS = new Set([
  'click_element', 'navigate', 'search_google', 'go_back', 'open_tab',
]);

export class NavigatorAgent {
  private stepInfo: StepInfo = { current: 0, max: 100 };

  constructor(
    private llm: LLMProvider,
    private actionRegistry: ActionRegistry,
    private messageManager: MessageManager,
    private options: AgentOptions,
  ) {}

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
   * 1. Get page state
   * 2. Call LLM
   * 3. Parse and execute actions
   */
  async execute(page: PageWrapper): Promise<{ results: ActionResult[]; done: boolean }> {
    // 1. Get current page state
    const state = await page.getState({
      useVision: this.options.useVision,
      includeConsole: true,
    });

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

      // 1-second delay between actions (from nanobrowser)
      if (i > 0) {
        await new Promise((r) => setTimeout(r, 1000));

        // DOM stability check (from nanobrowser): break if new elements appeared
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
