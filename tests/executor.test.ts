/**
 * Tests for the message manager and state formatting.
 */

import { describe, it, expect } from 'vitest';
import { MessageManager } from '../src/agent/messages.js';
import { formatStateMessage } from '../src/agent/state.js';
import { DOMElementNode } from '../src/browser/dom.js';
import type { PageState } from '../src/browser/dom.js';

describe('MessageManager', () => {
  it('should initialize with system prompt, task, and history marker', () => {
    const mm = new MessageManager();
    mm.initTask('You are an agent', 'Search for AI news');

    const messages = mm.getMessages();
    // system + task (wrapped in nano_user_request) + history marker = 3
    expect(messages.length).toBe(3);
    expect(messages[0].role).toBe('system');
    expect(messages[1].role).toBe('user');
    expect(messages[1].content).toContain('Search for AI news');
    expect(messages[1].content).toContain('nano_user_request');
  });

  it('should add model output as assistant message', () => {
    const mm = new MessageManager();
    mm.initTask('System', 'Task');
    mm.addModelOutput('{"action": []}');

    const messages = mm.getMessages();
    expect(messages.length).toBe(4);
    expect(messages[3].role).toBe('assistant');
  });

  it('should add action results', () => {
    const mm = new MessageManager();
    mm.initTask('System', 'Task');
    mm.addActionResults([
      { success: true, extractedContent: 'Found data', includeInMemory: true },
      { success: false, error: 'Element not found' },
    ]);

    const messages = mm.getMessages();
    expect(messages.length).toBe(4);
    const content = messages[3].content as string;
    expect(content).toContain('Found data');
    expect(content).toContain('Element not found');
  });
});

describe('formatStateMessage', () => {
  it('should format state with all sections', () => {
    const tree = new DOMElementNode(
      'div', '/div[1]', {}, '', false, true, true, null,
      [
        new DOMElementNode('button', '/button[1]', {}, 'Click Me', true, true, true, 0, []),
      ],
    );

    const state: PageState = {
      elementTree: tree,
      selectorMap: new Map([[0, tree.children[0] as DOMElementNode]]),
      url: 'https://example.com',
      title: 'Example',
      scrollY: 0,
      scrollHeight: 2000,
      viewportHeight: 1100,
      pendingDialogs: [{ type: 'alert', message: 'Hello!' }],
      consoleErrors: ['TypeError: null is not an object'],
    };

    const formatted = formatStateMessage(state, { current: 1, max: 100 });

    expect(formatted).toContain('https://example.com');
    expect(formatted).toContain('Example');
    expect(formatted).toContain('[0]<button');
    expect(formatted).toContain('Click Me');
    expect(formatted).toContain('alert: "Hello!"');
    expect(formatted).toContain('TypeError');
    expect(formatted).toContain('Current step: 1/100');
  });
});
