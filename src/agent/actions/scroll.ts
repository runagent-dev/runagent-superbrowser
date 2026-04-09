/**
 * Scroll actions: scroll_down, scroll_up, scroll_to_percent.
 */

import { z } from 'zod';
import { Action } from './registry.js';

export const scrollDownAction = new Action({
  name: 'scroll_down',
  description: 'Scroll down by one viewport height',
  schema: z.object({}),
  handler: async (_input, page) => {
    const [scrollY, viewportH, scrollH] = await page.getScrollInfo();
    if (scrollY + viewportH >= scrollH) {
      return {
        success: true,
        extractedContent: 'Already at the bottom of the page',
        includeInMemory: true,
      };
    }
    await page.scrollPage('down');
    return {
      success: true,
      extractedContent: 'Scrolled down one page',
      includeInMemory: true,
    };
  },
});

export const scrollUpAction = new Action({
  name: 'scroll_up',
  description: 'Scroll up by one viewport height',
  schema: z.object({}),
  handler: async (_input, page) => {
    const [scrollY] = await page.getScrollInfo();
    if (scrollY <= 0) {
      return {
        success: true,
        extractedContent: 'Already at the top of the page',
        includeInMemory: true,
      };
    }
    await page.scrollPage('up');
    return {
      success: true,
      extractedContent: 'Scrolled up one page',
      includeInMemory: true,
    };
  },
});

export const scrollToPercentAction = new Action({
  name: 'scroll_to_percent',
  description: 'Scroll to a specific percentage of the page (0=top, 100=bottom)',
  schema: z.object({
    percent: z.number().min(0).max(100).describe('Scroll position as percentage'),
  }),
  handler: async (input, page) => {
    const { percent } = input as { percent: number };
    await page.scrollToPercent(percent);
    return {
      success: true,
      extractedContent: `Scrolled to ${percent}% of page`,
      includeInMemory: true,
    };
  },
});

/** Scroll to top of page — from nanobrowser scroll_to_top. */
export const scrollToTopAction = new Action({
  name: 'scroll_to_top',
  description: 'Scroll to the top of the page',
  schema: z.object({}),
  handler: async (_input, page) => {
    await page.scrollToPercent(0);
    return {
      success: true,
      extractedContent: 'Scrolled to top of page',
      includeInMemory: true,
    };
  },
});

/** Scroll to bottom of page — from nanobrowser scroll_to_bottom. */
export const scrollToBottomAction = new Action({
  name: 'scroll_to_bottom',
  description: 'Scroll to the bottom of the page',
  schema: z.object({}),
  handler: async (_input, page) => {
    await page.scrollToPercent(100);
    return {
      success: true,
      extractedContent: 'Scrolled to bottom of page',
      includeInMemory: true,
    };
  },
});

/**
 * Scroll to text — from nanobrowser scroll_to_text.
 * Finds text on the page and scrolls to its nth occurrence.
 */
export const scrollToTextAction = new Action({
  name: 'scroll_to_text',
  description: 'Find and scroll to a specific text on the page',
  schema: z.object({
    text: z.string().describe('Text to find on the page'),
    nth: z.number().optional().describe('Which occurrence (1=first, 2=second, default 1)'),
  }),
  handler: async (input, page) => {
    const { text, nth } = input as { text: string; nth?: number };
    const occurrence = nth || 1;

    const found = await page.getRawPage().evaluate((searchText: string, n: number) => {
      const walker = document.createTreeWalker(
        document.body, NodeFilter.SHOW_TEXT, null,
      );
      let count = 0;
      let node: Node | null;
      while ((node = walker.nextNode())) {
        if (node.textContent && node.textContent.includes(searchText)) {
          count++;
          if (count === n) {
            const el = node.parentElement;
            if (el) {
              el.scrollIntoView({ block: 'center', behavior: 'instant' });
              return true;
            }
          }
        }
      }
      return false;
    }, text, occurrence);

    if (!found) {
      return {
        success: false,
        error: `Text "${text}" (occurrence ${occurrence}) not found on page`,
      };
    }
    return {
      success: true,
      extractedContent: `Scrolled to "${text}" (occurrence ${occurrence})`,
      includeInMemory: true,
    };
  },
});
