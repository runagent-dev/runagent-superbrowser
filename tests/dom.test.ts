/**
 * Tests for DOM tree building and element formatting.
 */

import { describe, it, expect } from 'vitest';
import { DOMElementNode, DOMTextNode } from '../src/browser/dom.js';

describe('DOMElementNode', () => {
  it('should format clickable elements as string', () => {
    const button = new DOMElementNode(
      'button', '/html/body/button[1]',
      { type: 'submit' }, 'Submit',
      true, true, true, 0, [],
    );

    const input = new DOMElementNode(
      'input', '/html/body/input[1]',
      { type: 'text', placeholder: 'Search...' }, '',
      true, true, true, 1, [],
    );

    const root = new DOMElementNode(
      'div', '/html/body',
      {}, '',
      false, true, true, null,
      [button, input],
    );

    const formatted = root.clickableElementsToString();
    expect(formatted).toContain('[0]<button');
    expect(formatted).toContain('Submit');
    expect(formatted).toContain('[1]<input');
    expect(formatted).toContain('placeholder=Search...');
  });

  it('should get text till next clickable element', () => {
    const text1 = new DOMTextNode('Hello ', true);
    const text2 = new DOMTextNode('World', true);
    const link = new DOMElementNode(
      'a', '/a[1]', { href: '/test' }, 'Click me',
      true, true, true, 1, [],
    );

    const span = new DOMElementNode(
      'span', '/span[1]', {}, '',
      false, true, true, null,
      [text1, text2, link],
    );

    const parentEl = new DOMElementNode(
      'div', '/div[1]', {}, '',
      true, true, true, 0,
      [span],
    );

    const text = parentEl.getAllTextTillNextClickableElement();
    // Should not include text from the link (next clickable)
    expect(text).toContain('Hello');
    expect(text).toContain('World');
  });

  it('should generate CSS selector from xpath', () => {
    const el = new DOMElementNode(
      'input', '/html[1]/body[1]/div[2]/form[1]/input[3]',
      { type: 'text', name: 'query' }, '',
      true, true, true, 0, [],
    );

    const selector = el.enhancedCssSelectorForElement();
    expect(selector).toContain('input');
    expect(selector).toContain('name="query"');
  });

  it('should mark new elements with asterisk', () => {
    const el = new DOMElementNode(
      'button', '/button[1]', {}, 'New Button',
      true, true, true, 0, [], true,
    );

    const root = new DOMElementNode(
      'div', '/div[1]', {}, '',
      false, true, true, null, [el],
    );

    const formatted = root.clickableElementsToString();
    expect(formatted).toContain('*[0]');
  });

  it('should attach group context to tabs inside a tablist (aria-label)', () => {
    const tabNew = new DOMElementNode(
      'button', '/tablist/button[1]',
      { role: 'tab' }, 'New',
      true, true, true, 32, [],
    );
    const tabOpenBox = new DOMElementNode(
      'button', '/tablist/button[2]',
      { role: 'tab' }, 'Open-Box',
      true, true, true, 33, [],
    );
    const tablist = new DOMElementNode(
      'div', '/tablist',
      { role: 'tablist', 'aria-label': 'Condition' }, '',
      false, true, true, null, [tabNew, tabOpenBox],
    );
    const root = new DOMElementNode(
      'div', '/root', {}, '',
      false, true, true, null, [tablist],
    );

    const formatted = root.clickableElementsToString();
    expect(formatted).toContain('[32]<button');
    expect(formatted).toContain('[33]<button');
    expect(formatted).toContain('group=Condition');
    // Both tabs should carry the group attribution.
    const groupCount = (formatted.match(/group=Condition/g) || []).length;
    expect(groupCount).toBe(2);
  });

  it('should resolve group label from <fieldset><legend>', () => {
    const radioM = new DOMElementNode(
      'input', '/fieldset/input[1]',
      { type: 'radio', name: 'g' }, '',
      true, true, true, 0, [],
    );
    const radioL = new DOMElementNode(
      'input', '/fieldset/input[2]',
      { type: 'radio', name: 'g' }, '',
      true, true, true, 1, [],
    );
    const legend = new DOMElementNode(
      'legend', '/fieldset/legend', {}, 'Size',
      false, true, true, null, [],
    );
    const fieldset = new DOMElementNode(
      'fieldset', '/fieldset', {}, '',
      false, true, true, null, [legend, radioM, radioL],
    );
    const root = new DOMElementNode(
      'div', '/root', {}, '',
      false, true, true, null, [fieldset],
    );

    const formatted = root.clickableElementsToString();
    expect(formatted).toContain('group=Size');
  });

  it('should NOT attach group to elements outside a group container', () => {
    const button = new DOMElementNode(
      'button', '/button[1]', {}, 'Submit',
      true, true, true, 0, [],
    );
    const root = new DOMElementNode(
      'div', '/root', {}, '',
      false, true, true, null, [button],
    );

    const formatted = root.clickableElementsToString();
    expect(formatted).not.toContain('group=');
  });
});
