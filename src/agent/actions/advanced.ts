/**
 * Advanced actions from BrowserOS patterns:
 * handle_dialog, upload_file, evaluate_script.
 */

import { z } from 'zod';
import { Action } from './registry.js';

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
  description: 'Execute a JavaScript snippet in the page and return the result',
  schema: z.object({
    script: z.string().describe('JavaScript code to evaluate'),
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
