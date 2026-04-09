/**
 * Control actions: done, wait, cache_content.
 */

import { z } from 'zod';
import { Action } from './registry.js';

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
