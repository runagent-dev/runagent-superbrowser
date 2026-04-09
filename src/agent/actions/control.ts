/**
 * Control actions: done, wait, cache_content, ask_human.
 */

import { z } from 'zod';
import { Action } from './registry.js';
import type { HumanInputManager, HumanInputType } from '../human-input.js';

export const doneAction = new Action({
  name: 'done',
  description: 'Mark the task as complete with a final answer/summary',
  schema: z.object({
    text: z.string().describe('Final answer or task completion summary'),
  }),
  handler: async (input) => {
    const { text } = input as { text: string };
    return {
      success: true,
      isDone: true,
      extractedContent: text,
      includeInMemory: true,
    };
  },
});

export const waitAction = new Action({
  name: 'wait',
  description: 'Wait for a specified number of seconds (max 10)',
  schema: z.object({
    seconds: z.number().min(0.5).max(10).describe('Seconds to wait'),
  }),
  handler: async (input) => {
    const { seconds } = input as { seconds: number };
    await new Promise((r) => setTimeout(r, seconds * 1000));
    return {
      success: true,
      extractedContent: `Waited ${seconds} seconds`,
    };
  },
});

export const cacheContentAction = new Action({
  name: 'cache_content',
  description: 'Save extracted content to memory for later reference',
  schema: z.object({
    content: z.string().describe('Content to save to memory'),
  }),
  handler: async (input) => {
    const { content } = input as { content: string };
    return {
      success: true,
      extractedContent: content,
      includeInMemory: true,
    };
  },
});

/**
 * Ask the user for input. Pauses execution until user responds.
 *
 * Use this when you need:
 * - Login credentials (username/password)
 * - Payment card details
 * - OTP / 2FA code
 * - Confirmation before purchase/submit
 * - Captcha solving (when auto-solve fails)
 * - Any information you don't have
 *
 * The executor will pause, send the request to the user (via HTTP/nanobot/UI),
 * wait for their response, and resume execution with the provided data.
 */
export function createAskHumanAction(humanInput: HumanInputManager): Action {
  return new Action({
    name: 'ask_human',
    description: 'Ask the user for input. Pauses until they respond. Use for: login credentials, payment info, OTP/2FA, captcha help, purchase confirmation, or any missing information.',
    schema: z.object({
      type: z.enum(['credentials', 'captcha', 'confirmation', 'otp', 'card', 'text', 'choice'])
        .describe('What kind of input: credentials, captcha, confirmation, otp, card, text, choice'),
      message: z.string().describe('Clear message to the user explaining what you need and why'),
      options: z.array(z.string()).optional().describe('For choice type: list of options to pick from'),
      fields: z.array(z.string()).optional().describe('For credentials/card: which fields (e.g., ["username", "password"])'),
    }),
    handler: async (input, page) => {
      const { type, message, options, fields } = input as {
        type: HumanInputType;
        message: string;
        options?: string[];
        fields?: string[];
      };

      // Take a screenshot so the user sees context
      let screenshot: string | undefined;
      try {
        screenshot = await page.screenshotBase64();
      } catch {
        // Screenshot may fail
      }

      // Request input — this blocks until user responds or times out
      const response = await humanInput.requestInput(type, message, {
        screenshot,
        options,
        fields,
      });

      if (!response || response.cancelled) {
        return {
          success: false,
          error: 'User cancelled or did not respond in time',
          includeInMemory: true,
        };
      }

      // Format the response for the agent's memory
      const dataStr = Object.entries(response.data)
        .map(([k, v]) => `${k}: ${v}`)
        .join(', ');

      return {
        success: true,
        extractedContent: `User provided: ${dataStr}`,
        includeInMemory: true,
      };
    },
  });
}
