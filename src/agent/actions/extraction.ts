/**
 * Extraction actions from BrowserOS patterns:
 * extract_markdown, export_pdf, dom_search, wait_for_condition,
 * get_console_errors, get_accessibility_tree.
 */

import { z } from 'zod';
import { Action } from './registry.js';
import { getAccessibilitySnapshot } from '../../browser/accessibility.js';

export const extractMarkdownAction = new Action({
  name: 'extract_markdown',
  description: 'Extract the page content as clean readable markdown',
  schema: z.object({}),
  handler: async (_input, page) => {
    const markdown = await page.getMarkdownContent();
    return {
      success: true,
      extractedContent: markdown.substring(0, 5000),
      includeInMemory: true,
    };
  },
});

export const exportPdfAction = new Action({
  name: 'export_pdf',
  description: 'Export the current page as a PDF file',
  schema: z.object({
    output_path: z.string().optional().describe('Path to save PDF (defaults to /tmp/superbrowser/downloads/page.pdf)'),
  }),
  handler: async (input, page) => {
    const { output_path } = input as { output_path?: string };
    const buffer = await page.exportPdf();
    const fs = await import('fs');
    const savePath = output_path || '/tmp/superbrowser/downloads/page.pdf';
    const dir = savePath.substring(0, savePath.lastIndexOf('/'));
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(savePath, buffer);
    return {
      success: true,
      extractedContent: `PDF saved to ${savePath} (${(buffer.length / 1024).toFixed(1)} KB)`,
      includeInMemory: true,
    };
  },
});

export const domSearchAction = new Action({
  name: 'dom_search',
  description: 'Search the DOM with a CSS selector and return matching element texts',
  schema: z.object({
    selector: z.string().describe('CSS selector to search for'),
  }),
  handler: async (input, page) => {
    const { selector } = input as { selector: string };
    const results = await page.domSearch(selector);
    if (results.length === 0) {
      return {
        success: true,
        extractedContent: `No elements found matching "${selector}"`,
      };
    }
    return {
      success: true,
      extractedContent: `Found ${results.length} elements:\n${results.map((r, i) => `${i + 1}. ${r}`).join('\n')}`,
      includeInMemory: true,
    };
  },
});

export const waitForConditionAction = new Action({
  name: 'wait_for_condition',
  description: 'Wait until a JavaScript expression evaluates to true',
  schema: z.object({
    expression: z.string().describe('JavaScript expression that should return true'),
    timeout: z.number().optional().describe('Max wait time in seconds (default 10)'),
  }),
  handler: async (input, page) => {
    const { expression, timeout } = input as { expression: string; timeout?: number };
    const result = await page.waitForCondition(expression, (timeout || 10) * 1000);
    return {
      success: result,
      extractedContent: result
        ? `Condition met: ${expression}`
        : `Timed out waiting for: ${expression}`,
      includeInMemory: true,
    };
  },
});

export const getConsoleErrorsAction = new Action({
  name: 'get_console_errors',
  description: 'Get recent JavaScript console errors from the page (useful for debugging)',
  schema: z.object({}),
  handler: async (_input, page) => {
    const errors = page.getConsoleMessages('error');
    if (errors.length === 0) {
      return {
        success: true,
        extractedContent: 'No console errors',
      };
    }
    return {
      success: true,
      extractedContent: `Console errors:\n${errors.map((e) => `- ${e.text}`).join('\n')}`,
      includeInMemory: true,
    };
  },
});

export const getAccessibilityTreeAction = new Action({
  name: 'get_accessibility_tree',
  description: 'Get the accessibility tree for semantic page understanding (fallback when DOM tree is confusing)',
  schema: z.object({}),
  handler: async (_input, page) => {
    const rawPage = page.getRawPage();
    const tree = await getAccessibilitySnapshot(rawPage);
    return {
      success: true,
      extractedContent: `Accessibility tree:\n${tree}`,
      includeInMemory: true,
    };
  },
});
