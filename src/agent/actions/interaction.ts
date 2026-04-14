/**
 * Element interaction actions: click, type, select, send_keys.
 */

import { z } from 'zod';
import { Action } from './registry.js';

export const clickElementAction = new Action({
  name: 'click_element',
  description: 'Click on an interactive element by its index',
  schema: z.object({
    index: z.number().describe('Element highlight index'),
  }),
  hasIndex: true,
  handler: async (input, page, state) => {
    const { index } = input as { index: number };
    const element = state.selectorMap.get(index);
    if (!element) {
      return {
        success: false,
        reason: 'element_not_found',
        error: `Element [${index}] not found in current page`,
        alternatives: ['Re-read the interactive elements list — indices may have shifted'],
      };
    }
    const result = await page.clickElement(element);
    if (!result.success) {
      return {
        success: false,
        reason: result.reason,
        tried: result.tried,
        alternatives: result.alternatives,
        error: `Click [${index}] failed (${result.reason ?? 'unknown'}): ${result.error ?? ''}`,
      };
    }
    const text = element.getAllTextTillNextClickableElement(2);
    return {
      success: true,
      extractedContent: `Clicked [${index}] "${text.substring(0, 50)}"`,
      tried: result.tried,
      includeInMemory: true,
    };
  },
});

export const inputTextAction = new Action({
  name: 'input_text',
  description: 'Type text into an input element',
  schema: z.object({
    index: z.number().describe('Element highlight index'),
    text: z.string().describe('Text to type'),
  }),
  hasIndex: true,
  handler: async (input, page, state) => {
    const { index, text } = input as { index: number; text: string };
    const element = state.selectorMap.get(index);
    if (!element) {
      return {
        success: false,
        reason: 'element_not_found',
        error: `Element [${index}] not found`,
        alternatives: ['Re-read interactive elements list — index may be stale'],
      };
    }
    const result = await page.typeText(element, text);
    if (!result.success) {
      return {
        success: false,
        reason: result.reason,
        tried: result.tried,
        alternatives: result.alternatives,
        error: `Type into [${index}] failed (${result.reason ?? 'unknown'}): ${result.error ?? ''}`,
      };
    }
    return {
      success: true,
      extractedContent: `Typed "${text}" into [${index}]`,
      tried: result.tried,
      includeInMemory: true,
    };
  },
});

export const selectOptionAction = new Action({
  name: 'select_option',
  description: 'Select an option in a dropdown by value',
  schema: z.object({
    index: z.number().describe('Element highlight index of the select element'),
    value: z.string().describe('Option value to select'),
  }),
  hasIndex: true,
  handler: async (input, page, state) => {
    const { index, value } = input as { index: number; value: string };
    const element = state.selectorMap.get(index);
    if (!element) {
      return { success: false, error: `Element [${index}] not found` };
    }
    await page.selectOption(element, value);
    return {
      success: true,
      extractedContent: `Selected "${value}" in [${index}]`,
      includeInMemory: true,
    };
  },
});

export const sendKeysAction = new Action({
  name: 'send_keys',
  description: 'Send keyboard keys (e.g., Enter, ArrowDown, Tab, Control+a)',
  schema: z.object({
    keys: z.string().describe('Keys to send (e.g., "Enter", "ArrowDown", "Control+a")'),
  }),
  handler: async (input, page) => {
    const { keys } = input as { keys: string };
    await page.sendKeys(keys);
    return {
      success: true,
      extractedContent: `Sent keys: ${keys}`,
    };
  },
});

/**
 * Get dropdown options — from nanobrowser get_dropdown_options action.
 * Lists all available options for a select element.
 */
export const getDropdownOptionsAction = new Action({
  name: 'get_dropdown_options',
  description: 'List all available options in a dropdown/select element',
  schema: z.object({
    index: z.number().describe('Element highlight index of the select element'),
  }),
  hasIndex: true,
  handler: async (input, page, state) => {
    const { index } = input as { index: number };
    const element = state.selectorMap.get(index);
    if (!element) {
      return { success: false, error: `Element [${index}] not found` };
    }
    const selector = element.enhancedCssSelectorForElement();
    const options = await page.getRawPage().evaluate((sel: string) => {
      const el = document.querySelector(sel) as HTMLSelectElement;
      if (!el || el.tagName !== 'SELECT') return [];
      return Array.from(el.options).map((o) => ({
        value: o.value,
        text: o.textContent?.trim() || '',
        selected: o.selected,
      }));
    }, selector);

    if (options.length === 0) {
      return { success: true, extractedContent: `No options found in [${index}]` };
    }

    const formatted = options.map((o: { value: string; text: string; selected: boolean }) =>
      `${o.selected ? '→ ' : '  '}"${o.text}" (value="${o.value}")`
    ).join('\n');

    return {
      success: true,
      extractedContent: `Dropdown [${index}] options:\n${formatted}`,
      includeInMemory: true,
    };
  },
});

/**
 * Select dropdown by visible text — from nanobrowser select_dropdown_option.
 * More reliable than selecting by value since the agent sees text, not values.
 */
export const selectDropdownByTextAction = new Action({
  name: 'select_dropdown_by_text',
  description: 'Select a dropdown option by its visible text (use get_dropdown_options first to see available options)',
  schema: z.object({
    index: z.number().describe('Element highlight index of the select element'),
    text: z.string().describe('Exact visible text of the option to select'),
  }),
  hasIndex: true,
  handler: async (input, page, state) => {
    const { index, text } = input as { index: number; text: string };
    const element = state.selectorMap.get(index);
    if (!element) {
      return { success: false, error: `Element [${index}] not found` };
    }
    const selector = element.enhancedCssSelectorForElement();
    const result = await page.getRawPage().evaluate((sel: string, optionText: string) => {
      const el = document.querySelector(sel) as HTMLSelectElement;
      if (!el) return { success: false, error: 'Element not found' };

      for (const option of el.options) {
        if (option.textContent?.trim() === optionText) {
          el.value = option.value;
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return { success: true, value: option.value };
        }
      }
      return { success: false, error: `Option "${optionText}" not found` };
    }, selector, text);

    if (!result.success) {
      return { success: false, error: result.error };
    }
    return {
      success: true,
      extractedContent: `Selected "${text}" in [${index}]`,
      includeInMemory: true,
    };
  },
});
