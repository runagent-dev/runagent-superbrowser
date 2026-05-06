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
  description:
    'Jump to an ABSOLUTE position on the page (0=top, 100=bottom). '
    + '`percent: 20` jumps to 20% of the page from the top — it is '
    + 'NOT "scroll down by 20% from current position". For incremental '
    + 'motion use scroll_pixels or scroll_down.',
  schema: z.object({
    percent: z.number().min(0).max(100).describe(
      'ABSOLUTE position 0..100 (0=top, 100=bottom). NOT incremental.',
    ),
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

/** Incremental pixel scroll — explicit unit, no percent confusion. */
export const scrollPixelsAction = new Action({
  name: 'scroll_pixels',
  description:
    'Incremental scroll by an explicit pixel distance. Use this for '
    + 'fine nudges below a control that is just past the fold.',
  schema: z.object({
    direction: z.enum(['up', 'down']).describe('Direction to scroll'),
    pixels: z.number().int().positive().describe(
      'Pixels to scroll (positive). Combine with `direction` for sign.',
    ),
  }),
  handler: async (input, page) => {
    const { direction, pixels } = input as { direction: 'up' | 'down'; pixels: number };
    await page.scrollByPixels(direction, pixels);
    return {
      success: true,
      extractedContent: `Scrolled ${direction} ${pixels}px`,
      includeInMemory: true,
    };
  },
});

/**
 * Scroll INSIDE an open popup/listbox/menu/modal — page-level scroll
 * does not move a popup's internal scroll, so dropdowns whose options
 * extend below the visible menu need this. Auto-detects the most
 * recently opened scrollable popup when container_selector is absent.
 */
export const scrollWithinAction = new Action({
  name: 'scroll_within',
  description:
    'Scroll inside an open popup/listbox/menu/modal (NOT the page). '
    + 'Use when a dropdown\'s options extend below its visible menu '
    + 'area. Auto-detects the most recently opened popup when no '
    + 'container_selector is given.',
  schema: z.object({
    container_selector: z.string().optional().describe(
      'CSS selector for the scroll container; auto-detect if omitted.',
    ),
    target_text: z.string().optional().describe(
      'If set, walks the container until this text appears.',
    ),
    direction: z.enum(['up', 'down']).optional(),
    amount: z.union([z.literal('page'), z.literal('half'), z.number()]).optional().describe(
      "'page' (default), 'half', or explicit pixels. Ignored when target_text is set.",
    ),
    max_iterations: z.number().int().positive().max(40).optional(),
  }),
  handler: async (input, page) => {
    const opts = input as {
      container_selector?: string;
      target_text?: string;
      direction?: 'up' | 'down';
      amount?: 'page' | 'half' | number;
      max_iterations?: number;
    };
    const result = await page.scrollWithin({
      containerSelector: opts.container_selector,
      targetText: opts.target_text,
      direction: opts.direction,
      amount: opts.amount,
      maxIterations: opts.max_iterations,
    });
    if (result.reason === 'no_container') {
      return {
        success: false,
        error:
          'No scrollable popup found. Make sure a dropdown/menu/modal '
          + 'is open, or pass an explicit container_selector.',
      };
    }
    const summary = result.found
      ? `Scrolled within ${result.resolvedContainer} → matched "${result.matchedText ?? ''}"`
      : `Scrolled within ${result.resolvedContainer}: ${result.reason} `
        + `(scrolled ${result.scrolledPx}px)`;
    return {
      success: true,
      extractedContent: summary,
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
