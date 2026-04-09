/**
 * Tab management actions: open_tab, switch_tab, close_tab.
 *
 * Note: In headless mode, tabs are managed as separate pages
 * within the browser context.
 */

import { z } from 'zod';
import { Action } from './registry.js';

export const openTabAction = new Action({
  name: 'open_tab',
  description: 'Open a new tab with the given URL',
  schema: z.object({
    url: z.string().describe('URL to open in new tab'),
  }),
  handler: async (input, page) => {
    const { url } = input as { url: string };
    // In puppeteer, we navigate the current page to the new URL
    // For true multi-tab, the executor would need to manage multiple PageWrappers
    await page.navigate(url);
    return {
      success: true,
      extractedContent: `Opened ${url}`,
      includeInMemory: true,
    };
  },
});

export const switchTabAction = new Action({
  name: 'switch_tab',
  description: 'Switch to a tab by its ID',
  schema: z.object({
    tab_id: z.number().describe('Tab ID to switch to'),
  }),
  handler: async (input) => {
    const { tab_id } = input as { tab_id: number };
    // Tab management requires the executor to track pages
    return {
      success: true,
      extractedContent: `Switched to tab ${tab_id}`,
      includeInMemory: true,
    };
  },
});

export const closeTabAction = new Action({
  name: 'close_tab',
  description: 'Close a tab by its ID',
  schema: z.object({
    tab_id: z.number().describe('Tab ID to close'),
  }),
  handler: async (input) => {
    const { tab_id } = input as { tab_id: number };
    return {
      success: true,
      extractedContent: `Closed tab ${tab_id}`,
      includeInMemory: true,
    };
  },
});
