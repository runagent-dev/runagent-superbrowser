/**
 * Tests for action registry and validation.
 */

import { describe, it, expect } from 'vitest';
import { buildDefaultActionRegistry } from '../src/agent/action-builder.js';

describe('ActionRegistry', () => {
  it('should register all default actions', () => {
    const registry = buildDefaultActionRegistry();
    const names = registry.names();

    // Core actions
    expect(names).toContain('done');
    expect(names).toContain('navigate');
    expect(names).toContain('search_google');
    expect(names).toContain('go_back');
    expect(names).toContain('click_element');
    expect(names).toContain('input_text');
    expect(names).toContain('select_option');
    expect(names).toContain('send_keys');
    expect(names).toContain('scroll_down');
    expect(names).toContain('scroll_up');
    expect(names).toContain('scroll_to_percent');
    expect(names).toContain('wait');
    expect(names).toContain('cache_content');

    // BrowserOS actions
    expect(names).toContain('handle_dialog');
    expect(names).toContain('upload_file');
    expect(names).toContain('evaluate_script');
    expect(names).toContain('run_script');
    expect(names).toContain('extract_markdown');
    expect(names).toContain('export_pdf');
    expect(names).toContain('dom_search');
    expect(names).toContain('wait_for_condition');
    expect(names).toContain('get_console_errors');
    expect(names).toContain('get_accessibility_tree');
  });

  it('should return error for unknown action', async () => {
    const registry = buildDefaultActionRegistry();
    const result = await registry.execute('nonexistent', {}, {} as never, {} as never);
    expect(result.success).toBe(false);
    expect(result.error).toContain('Unknown action');
  });

  it('should generate action prompt', () => {
    const registry = buildDefaultActionRegistry();
    const prompt = registry.getPrompt();

    expect(prompt).toContain('navigate');
    expect(prompt).toContain('click_element');
    expect(prompt).toContain('done');
    expect(prompt.length).toBeGreaterThan(500);
  });
});
