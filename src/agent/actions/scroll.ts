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
    if (result.found) {
      return {
        success: true,
        extractedContent:
          `Scrolled within ${result.resolvedContainer} → matched "${result.matchedText ?? ''}"`,
        includeInMemory: true,
      };
    }
    // Without target_text, terminal reasons like page_end / page_start /
    // max_iterations just mean "scrolled what you asked"; treat as success.
    // WITH target_text, those reasons mean target wasn't found — surface
    // as failure so the brain doesn't proceed to click on a wrong option.
    const hadTarget = (opts.target_text ?? '').trim().length > 0;
    if (!hadTarget && (result.reason === 'page_end'
        || result.reason === 'page_start'
        || result.reason === 'max_iterations')) {
      return {
        success: true,
        extractedContent:
          `Scrolled within ${result.resolvedContainer} (${result.reason}, `
          + `${result.scrolledPx}px)`,
        includeInMemory: true,
      };
    }
    return {
      success: false,
      error:
        `scroll_within(${JSON.stringify((opts.target_text ?? '').slice(0, 60))}) `
        + `inside ${result.resolvedContainer} ended with reason=${result.reason} `
        + `after scrolling ${result.scrolledPx}px. Target not found in this `
        + `container — try a synonym, opposite direction, or close+re-open the popup.`,
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

    const result = await page.getRawPage().evaluate(
      (searchText: string, n: number) => {
        const norm = (s: string) =>
          s.replace(/\s+/g, ' ').trim().toLowerCase();
        const needle = norm(searchText);
        if (!needle) return { found: false as const, reason: 'empty_text' };

        // Pass 1: text-node walker — case-folded, whitespace-collapsed.
        const walker = document.createTreeWalker(
          document.body, NodeFilter.SHOW_TEXT, null,
        );
        let count = 0;
        let matched: HTMLElement | null = null;
        let node: Node | null;
        while ((node = walker.nextNode())) {
          if (norm(node.textContent || '').includes(needle)) {
            count++;
            if (count === n) { matched = node.parentElement; break; }
          }
        }

        // Pass 2: parent innerText walker — catches multi-textnode splits
        // like <span>Sign</span> <span>In</span> for "Sign In", and skips
        // hidden subtrees automatically.
        if (!matched) {
          count = 0;
          const els = document.body.querySelectorAll<HTMLElement>('*');
          for (const el of els) {
            const tc = norm(el.textContent || '');
            if (!tc.includes(needle.slice(0, 3))) continue;
            if (norm(el.innerText || '').includes(needle)) {
              let deepest = el;
              for (const c of Array.from(el.querySelectorAll<HTMLElement>('*'))) {
                if (norm(c.innerText || '').includes(needle)) deepest = c;
              }
              count++;
              if (count === n) { matched = deepest; break; }
            }
          }
        }
        if (!matched) return { found: false as const, reason: 'not_found' };

        // Pre/post geometry: scrollIntoView is silently a no-op inside
        // collapsed <details>, hidden tabs, or non-scrollable ancestors.
        const beforeY = window.scrollY;
        const beforeRect = matched.getBoundingClientRect();
        const beforeInView = beforeRect.top >= 0
          && beforeRect.bottom <= window.innerHeight
          && beforeRect.width > 0 && beforeRect.height > 0;
        try {
          matched.scrollIntoView({ block: 'center', behavior: 'instant' });
        } catch { /* older engines */ }
        const afterY = window.scrollY;
        const afterRect = matched.getBoundingClientRect();
        const afterInView = afterRect.top >= 0
          && afterRect.bottom <= window.innerHeight
          && afterRect.width > 0 && afterRect.height > 0;
        const movedPx = Math.abs(afterY - beforeY)
          + Math.abs(afterRect.top - beforeRect.top);

        if (afterInView || movedPx > 1) {
          return {
            found: true as const,
            moved: movedPx,
            already_in_view: beforeInView && movedPx < 2,
            text: (matched.innerText || matched.textContent || '').trim().slice(0, 80),
          };
        }
        return { found: false as const, reason: 'scroll_no_op' };
      },
      text, occurrence,
    );

    if (!result.found) {
      const reason = (result as { reason: string }).reason;
      if (reason === 'scroll_no_op') {
        return {
          success: false,
          error:
            `Text "${text}" was located but scrollIntoView did not bring `
            + `it into view — the element is likely inside a collapsed `
            + `<details>, a hidden tab, or a non-scrollable container. `
            + `Open the parent disclosure first, or use scroll_within `
            + `with container_selector.`,
        };
      }
      return {
        success: false,
        error: `Text "${text}" (occurrence ${occurrence}) not found on page`,
      };
    }
    const r = result as { moved: number; already_in_view: boolean; text: string };
    return {
      success: true,
      extractedContent:
        `Scrolled to "${text}" (occurrence ${occurrence}) — moved ${r.moved}px`
        + `${r.already_in_view ? ' (already in view)' : ''}, matched: ${r.text}`,
      includeInMemory: true,
    };
  },
});
