/**
 * Tests for the element identity fingerprint.
 *
 * Locks in the bug fix: two checkboxes with identical tag/role/aria-
 * label at different on-screen positions must hash to DIFFERENT
 * fingerprints, otherwise the stale-index guard lets a re-rendered
 * filter list silently misclick.
 */

import { describe, it, expect } from 'vitest';
import { DOMElementNode, type SelectorEntry } from '../src/browser/dom.js';
import { fingerprintElement, fingerprintMap } from '../src/browser/fingerprint.js';

const mkCheckbox = (xpath: string): DOMElementNode =>
  new DOMElementNode(
    'button',
    xpath,
    { role: 'checkbox', 'aria-label': 'Food Pairing' },
    'Food Pairing',
    true, true, true,
    null, [],
  );

const mkEntry = (idx: number, xpath: string, x: number, y: number): SelectorEntry => ({
  index: idx,
  xpath,
  tagName: 'button',
  attributes: { role: 'checkbox', 'aria-label': 'Food Pairing' },
  text: 'Food Pairing',
  bounds: { x, y, width: 24, height: 24 },
});

describe('fingerprintElement', () => {
  it('hashes two sibling-identical checkboxes at different positions to different fingerprints', () => {
    const a = mkCheckbox('/html[1]/body[1]/aside[1]/ul[1]/li[3]/button[1]');
    const b = mkCheckbox('/html[1]/body[1]/aside[1]/ul[1]/li[5]/button[1]');
    const ea = mkEntry(0, a.xpath, 100, 200);
    const eb = mkEntry(1, b.xpath, 100, 320);

    const fpA = fingerprintElement(a, ea);
    const fpB = fingerprintElement(b, eb);

    expect(fpA).not.toEqual(fpB);
  });

  it('preserves stable hashes when bounds shift by less than the bucket size', () => {
    // Anti-aliasing jitter shouldn't invalidate the hash. A shift well
    // inside one 50px bucket (e.g., from x=110 → x=125) keeps the
    // bucket index the same, so the hash matches. Values are picked
    // away from bucket boundaries so a couple-px jitter at the edge
    // doesn't accidentally fail the test.
    const a = mkCheckbox('/html[1]/body[1]/div[1]/button[1]');
    const ea1 = mkEntry(0, a.xpath, 110, 220);
    const ea2 = mkEntry(0, a.xpath, 125, 235);

    expect(fingerprintElement(a, ea1)).toEqual(fingerprintElement(a, ea2));
  });

  it('changes hash when bounds shift past the bucket boundary', () => {
    // Real position move >= 50px MUST invalidate the hash so the
    // stale-index guard can catch it.
    const a = mkCheckbox('/html[1]/body[1]/div[1]/button[1]');
    const ea1 = mkEntry(0, a.xpath, 100, 200);
    const ea2 = mkEntry(0, a.xpath, 100, 320);

    expect(fingerprintElement(a, ea1)).not.toEqual(fingerprintElement(a, ea2));
  });

  it('still hashes deterministically when no SelectorEntry is provided', () => {
    // Backward-compat: existing callers that don't pass entry continue
    // working — they fall back to the pre-fix payload (without bounds).
    const a = mkCheckbox('/html[1]/body[1]/div[1]/button[1]');

    const noEntry = fingerprintElement(a);
    const withEntry = fingerprintElement(a, null);
    expect(noEntry).toEqual(withEntry);
    expect(noEntry).toMatch(/^[0-9a-f]{16}$/);
  });
});

describe('fingerprintMap', () => {
  it('produces unique fingerprints for a list of identical-aria-label checkboxes', () => {
    const items: Array<[number, DOMElementNode, SelectorEntry]> = [];
    for (let i = 0; i < 5; i++) {
      const xpath = `/html[1]/body[1]/aside[1]/ul[1]/li[${i + 1}]/button[1]`;
      const node = mkCheckbox(xpath);
      const entry = mkEntry(i, xpath, 100, 200 + i * 60);
      items.push([i, node, entry]);
    }
    const selectorMap = new Map<number, DOMElementNode>(
      items.map(([i, node]) => [i, node]),
    );
    const entries = items.map(([_, __, e]) => e);
    const fps = fingerprintMap(selectorMap, entries);
    const unique = new Set(Object.values(fps));
    expect(unique.size).toEqual(items.length);
  });
});
