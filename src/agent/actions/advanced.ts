/**
 * Advanced actions from BrowserOS patterns:
 * handle_dialog, upload_file, evaluate_script.
 */

import { z } from 'zod';
import { Action } from './registry.js';
import { runPuppeteerScript } from '../../browser/script-runner.js';

export const handleDialogAction = new Action({
  name: 'handle_dialog',
  description: 'Accept or dismiss a pending dialog (alert, confirm, prompt)',
  schema: z.object({
    accept: z.boolean().describe('true to accept, false to dismiss'),
    text: z.string().optional().describe('Text to enter for prompt dialogs'),
  }),
  handler: async (input, page) => {
    const { accept, text } = input as { accept: boolean; text?: string };
    await page.handleDialog(accept, text);
    return {
      success: true,
      extractedContent: `Dialog ${accept ? 'accepted' : 'dismissed'}${text ? ` with text "${text}"` : ''}`,
      includeInMemory: true,
    };
  },
});

export const uploadFileAction = new Action({
  name: 'upload_file',
  description: 'Upload a file to a file input element',
  schema: z.object({
    index: z.number().describe('Element highlight index of the file input'),
    file_path: z.string().describe('Absolute path to the file to upload'),
  }),
  hasIndex: true,
  handler: async (input, page, state) => {
    const { index, file_path } = input as { index: number; file_path: string };
    const element = state.selectorMap.get(index);
    if (!element) {
      return { success: false, error: `Element [${index}] not found` };
    }
    await page.uploadFile(element, [file_path]);
    return {
      success: true,
      extractedContent: `Uploaded file to [${index}]`,
      includeInMemory: true,
    };
  },
});

export const evaluateScriptAction = new Action({
  name: 'evaluate_script',
  description: 'Execute a JavaScript snippet in the page DOM context (document.querySelector, reading values). For full Puppeteer API access (page.goto, page.click, page.type), use run_script instead.',
  schema: z.object({
    script: z.string().describe('JavaScript code to evaluate in the browser DOM context'),
  }),
  handler: async (input, page) => {
    const { script } = input as { script: string };
    const result = await page.evaluateScript(script);
    const resultStr = typeof result === 'object' ? JSON.stringify(result) : String(result);
    return {
      success: true,
      extractedContent: `Script result: ${resultStr.substring(0, 500)}`,
      includeInMemory: true,
    };
  },
});

export const runScriptAction = new Action({
  name: 'run_script',
  description:
    'Execute a Puppeteer script with full page API access (page.goto, page.waitForSelector, page.screenshot). Use for complex multi-step browser automation that standard actions cannot handle. The script body receives: page (Puppeteer Page), context (optional data), helpers (sleep, log, screenshot). By default, the sandbox blocks page mutations (click/type/input) — isTrusted=false clicks are bot-detected, so synthesizing them via JS is counterproductive. Use click_element / input_text / select_option instead. Set `mutates=true` only when no cursor action can achieve the goal (rare).',
  schema: z.object({
    script: z
      .string()
      .describe(
        'Puppeteer script body. Available: page (Page API), context (data), helpers.sleep(ms), helpers.log(...), helpers.screenshot(path?). Example: await page.goto("https://example.com"); return await page.title();',
      ),
    context: z
      .record(z.unknown())
      .optional()
      .describe('Optional context data passed to the script'),
    mutates: z
      .boolean()
      .optional()
      .describe(
        'Opt into page-mutation APIs (click, type, dispatchEvent, value setter). '
        + 'Default false; cursor-based actions (click_element / input_text) are preferred.',
      ),
  }),
  handler: async (input, page) => {
    const { script, context, mutates } = input as {
      script: string;
      context?: Record<string, unknown>;
      mutates?: boolean;
    };
    const rawPage = page.getRawPage();
    const scriptResult = await runPuppeteerScript(
      rawPage, script, context, 60000, { mutates: Boolean(mutates) },
    );

    if (!scriptResult.success) {
      return {
        success: false,
        error: `Script failed: ${scriptResult.error}`,
      };
    }

    const resultStr =
      typeof scriptResult.result === 'object'
        ? JSON.stringify(scriptResult.result)
        : String(scriptResult.result ?? 'undefined');

    const logStr =
      scriptResult.logs.length > 0 ? `\nLogs: ${scriptResult.logs.join('\n')}` : '';

    return {
      success: true,
      extractedContent: `Script result (${scriptResult.duration}ms): ${resultStr.substring(0, 500)}${logStr.substring(0, 200)}`,
      includeInMemory: true,
    };
  },
});
