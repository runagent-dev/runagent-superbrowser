/**
 * Scroll-surface routing.
 *
 * The bug these guard: `window.scrollBy` moves the document and
 * succeeds on nearly every page, so a blind "scroll down" always moved
 * the main column, and a sidebar carrying its own `overflow-y` could
 * never be reached. The agent then repeated the scroll, saw the rail
 * unmoved, and reported progress that had not happened.
 */

import { describe, it, expect } from 'vitest';
import { chooseSurface, type ScrollSurface } from '../src/browser/scroll-surfaces.js';

const page = (over: Partial<ScrollSurface> = {}): ScrollSurface => ({
  selector: '',
  region: 'page',
  label: 'page (document)',
  rect: { x: 0, y: 0, w: 1280, h: 800 },
  scrollTop: 0,
  scrollHeight: 14000,
  clientHeight: 800,
  remainingDown: 13200,
  remainingUp: 0,
  ...over,
});

const rail = (over: Partial<ScrollSurface> = {}): ScrollSurface => ({
  selector: '#filters',
  region: 'left',
  label: 'aside.filters',
  rect: { x: 0, y: 0, w: 280, h: 800 },
  scrollTop: 0,
  scrollHeight: 4400,
  clientHeight: 800,
  remainingDown: 3600,
  remainingUp: 0,
  ...over,
});

describe('chooseSurface', () => {
  it('routes an explicit region to that pane rather than the page', () => {
    expect(chooseSurface([page(), rail()], { region: 'left' })?.selector).toBe('#filters');
  });

  it("keeps region='page' on the document even when rails exist", () => {
    expect(chooseSurface([page(), rail()], { region: 'page' })?.selector).toBe('');
  });

  it('falls through to the page cascade when the named region has no pane', () => {
    expect(chooseSurface([page(), rail()], { region: 'right' })).toBe(null);
  });

  it('sends target text that lives in a rail to that rail', () => {
    const got = chooseSurface([rail({ containsTarget: true }), page()], { hasTargetText: true });
    expect(got?.selector).toBe('#filters');
  });

  it('leaves target text owned by the document on the existing page path', () => {
    const got = chooseSurface([page({ containsTarget: true }), rail()], { hasTargetText: true });
    expect(got).toBe(null);
  });

  it('lets an explicit container selector override region and target', () => {
    const got = chooseSurface([page(), rail()], {
      containerSelector: '#other',
      region: 'left',
      hasTargetText: true,
    });
    expect(got?.selector).toBe('#other');
  });

  it('honours an unknown container selector so the caller sees a real error', () => {
    // Swallowing it here would scroll the page instead and look like success.
    expect(chooseSurface([page()], { containerSelector: '#nope' })?.selector).toBe('#nope');
  });

  it('changes nothing when the caller gives no hint', () => {
    expect(chooseSurface([page(), rail()], {})).toBe(null);
  });

  it('prefers a pane holding the target over a larger pane that does not', () => {
    const big = page({ containsTarget: false });
    const small = rail({ containsTarget: true });
    expect(chooseSurface([big, small], { hasTargetText: true })?.selector).toBe('#filters');
  });
});
