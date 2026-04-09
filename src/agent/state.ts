/**
 * Browser state formatting for LLM consumption.
 *
 * Combines DOM tree, accessibility tree, dialogs, and console errors
 * into a structured state message.
 */

import type { PageState } from '../browser/dom.js';
import type { ChatMessage } from '../llm/types.js';
import type { StepInfo } from './types.js';

/**
 * Format the browser state as a text message for the LLM.
 */
export function formatStateMessage(state: PageState, stepInfo: StepInfo): string {
  let msg = `[Current state starts here]
Current URL: ${state.url}
Page Title: ${state.title}
Scroll: ${state.scrollY}/${state.scrollHeight} (viewport: ${state.viewportHeight})
Current step: ${stepInfo.current}/${stepInfo.max}

Interactive elements:
${state.elementTree.clickableElementsToString()}`;

  // Include accessibility tree when available (BrowserOS semantic fallback)
  if (state.accessibilityTree) {
    msg += `\n\nAccessibility tree (semantic view):\n${state.accessibilityTree}`;
  }

  // Include pending dialogs if any (BrowserOS)
  if (state.pendingDialogs && state.pendingDialogs.length > 0) {
    msg += `\n\nPending dialogs:\n${state.pendingDialogs.map((d) => `- ${d.type}: "${d.message}"`).join('\n')}`;
  }

  // Include recent console errors (BrowserOS - helps diagnose failures)
  if (state.consoleErrors && state.consoleErrors.length > 0) {
    msg += `\n\nRecent console errors:\n${state.consoleErrors.slice(-3).map((e) => `- ${e}`).join('\n')}`;
  }

  msg += '\n[Current state ends here]';
  return msg;
}

/**
 * Build a chat message from the browser state, optionally including a screenshot.
 */
export function buildStateMessage(
  state: PageState,
  useVision: boolean,
  stepInfo: StepInfo,
): ChatMessage {
  const text = formatStateMessage(state, stepInfo);

  if (useVision && state.screenshot) {
    return {
      role: 'user',
      content: [
        { type: 'text', text },
        {
          type: 'image_url',
          image_url: { url: `data:image/jpeg;base64,${state.screenshot}` },
        },
      ],
    };
  }

  return { role: 'user', content: text };
}
