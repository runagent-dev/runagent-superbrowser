/**
 * Navigation actions: navigate, search_google, go_back.
 */

import { z } from 'zod';
import { Action } from './registry.js';

export const navigateAction = new Action({
  name: 'navigate',
  description: 'Navigate to a URL',
  schema: z.object({
    url: z.string().describe('URL to navigate to'),
  }),
  handler: async (input, page) => {
    const { url } = input as { url: string };
    await page.navigate(url);
    return {
      success: true,
      extractedContent: `Navigated to ${url}`,
      includeInMemory: true,
    };
  },
});

export const searchGoogleAction = new Action({
  name: 'search_google',
  description: 'Search Google for a query',
  schema: z.object({
    query: z.string().describe('Search query'),
  }),
  handler: async (input, page) => {
    const { query } = input as { query: string };
    await page.navigate(`https://www.google.com/search?q=${encodeURIComponent(query)}`);
    return {
      success: true,
      extractedContent: `Searched Google for: ${query}`,
      includeInMemory: true,
    };
  },
});

export const goBackAction = new Action({
  name: 'go_back',
  description: 'Go back to the previous page',
  schema: z.object({}),
  handler: async (_input, page) => {
    await page.goBack();
    const url = await page.getUrl();
    return {
      success: true,
      extractedContent: `Went back to ${url}`,
      includeInMemory: true,
    };
  },
});
