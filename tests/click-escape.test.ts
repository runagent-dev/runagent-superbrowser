/**
 * Tests for normalizeIdSelector() — the brain-side escape recovery for
 * React `useId()` selectors at the /click-selector HTTP boundary.
 *
 * Brain sometimes emits `#radix-:r13:` (unescaped) and sometimes
 * `#radix-\:r13\:` (escaped). The helper must produce a single
 * canonical form that querySelector accepts, regardless of input.
 */

import { describe, it, expect } from 'vitest';
import { normalizeIdSelector } from '../src/server/http.js';

describe('normalizeIdSelector', () => {
  it('escapes a raw useId selector', () => {
    expect(normalizeIdSelector('#:r13:')).toBe('#\\:r13\\:');
  });

  it('escapes radix-prefixed useId', () => {
    expect(normalizeIdSelector('#radix-:r13:')).toBe('#radix-\\:r13\\:');
  });

  it('is idempotent on already-escaped input', () => {
    const escaped = '#radix-\\:r13\\:';
    expect(normalizeIdSelector(escaped)).toBe(escaped);
  });

  it('handles tag prefix', () => {
    expect(normalizeIdSelector('button#radix-:r13:')).toBe(
      'button#radix-\\:r13\\:',
    );
  });

  it('handles trailing combinator', () => {
    expect(normalizeIdSelector('#radix-:r13: > button')).toBe(
      '#radix-\\:r13\\: > button',
    );
  });

  it('handles trailing class', () => {
    expect(normalizeIdSelector('#radix-:r13:.foo')).toBe(
      '#radix-\\:r13\\:.foo',
    );
  });

  it('leaves pseudo-class colons alone', () => {
    expect(normalizeIdSelector('button:hover')).toBe('button:hover');
    expect(normalizeIdSelector('a:not([disabled])')).toBe('a:not([disabled])');
  });

  it('leaves attribute-selector colons alone', () => {
    expect(normalizeIdSelector('[data-id=":r13:"]')).toBe(
      '[data-id=":r13:"]',
    );
  });

  it('does not touch non-useId IDs', () => {
    expect(normalizeIdSelector('#email')).toBe('#email');
    expect(normalizeIdSelector('#radix-trigger-main')).toBe(
      '#radix-trigger-main',
    );
  });

  it('handles useId without trailing colon', () => {
    // `:r13` (no trailing `:`) — React can emit this for sub-IDs.
    expect(normalizeIdSelector('#radix-:r13')).toBe('#radix-\\:r13');
  });
});
