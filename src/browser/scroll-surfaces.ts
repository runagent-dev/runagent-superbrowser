/**
 * Scroll-surface survey.
 *
 * A page usually has more than one thing that scrolls: the document
 * itself, plus rails, filter panels, result lists and modals that carry
 * their own `overflow-y`. `window.scrollBy` only ever moves the
 * document, and it succeeds on nearly every page, so a blind "scroll
 * down" moves the main column even when the agent meant the left rail.
 * The agent then repeats the scroll, sees the same rail unmoved, and
 * loops.
 *
 * This module gives the caller the thing it was missing: an explicit,
 * ranked list of what can be scrolled, where each surface sits on
 * screen, and how much room is left in each direction. With that, the
 * scroll endpoint can (a) target a named region, (b) pick the surface
 * that actually contains the text the caller is looking for, and
 * (c) report which surface really moved, so a mis-aimed scroll is
 * visible instead of silent.
 */

import type { Page } from 'puppeteer-core';

export type SurfaceRegion = 'page' | 'left' | 'right' | 'main' | 'modal';

export type ScrollSurface = {
  /** Stable-ish CSS selector; '' for the page surface. */
  selector: string;
  region: SurfaceRegion;
  /** Short human label, e.g. 'nav.filters' or 'aside[aria-label="Filters"]'. */
  label: string;
  rect: { x: number; y: number; w: number; h: number };
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
  /** Pixels still scrollable in each direction. */
  remainingDown: number;
  remainingUp: number;
  /** True when the caller's targetText was found inside this surface. */
  containsTarget?: boolean;
};

/** Elements this small are scrollbars-on-a-chip, not panes worth naming. */
const MIN_CLIENT_HEIGHT = 80;
const MIN_OVERFLOW = 24;
/** Element walk for panes. Measured at ~3.5 ms per 4k nodes, so 12k
 *  keeps a right rail that sits after a long main list findable while
 *  staying well inside the budget of a call that already screenshots. */
const SCAN_CAP = 12000;
/** The text hunt is a second pass and only runs when a target is given;
 *  keep it tighter. */
const TEXT_SCAN_CAP = 4000;
/** Bound on how many text matches feed the innermost-match search,
 *  which is O(n^2) in matches. A common word can match hundreds of
 *  nodes; the deepest one is always near the end of document order. */
const MAX_TEXT_MATCHES = 120;
const MAX_SURFACES = 8;

/**
 * Browser-side survey. Kept as one self-contained function string so it
 * can be handed to `page.evaluate` without bundler help.
 */
/* eslint-disable @typescript-eslint/no-explicit-any */
function surveyInPage(
  targetText: string,
  minClientHeight: number,
  minOverflow: number,
  scanCap: number,
  textScanCap: number,
  maxTextMatches: number,
  maxSurfaces: number,
): any {
  const norm = (s: string) => s.replace(/\s+/g, ' ').trim().toLowerCase();
  const needle = norm(targetText || '');

  const isVisible = (el: Element): boolean => {
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) return false;
    const cs = window.getComputedStyle(el as HTMLElement);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
  };

  // A short, reasonably stable selector. Prefers identity attributes
  // over structural paths, because structural paths break the moment a
  // framework re-renders a list above the element.
  const selectorFor = (el: HTMLElement): string => {
    if (el.id && /^[A-Za-z][\w-]*$/.test(el.id)) return `#${el.id}`;
    const testid = el.getAttribute('data-testid') || el.getAttribute('data-test-id');
    if (testid) return `[data-testid="${CSS.escape(testid)}"]`;
    const aria = el.getAttribute('aria-label');
    if (aria && aria.length < 60) return `${el.tagName.toLowerCase()}[aria-label="${CSS.escape(aria)}"]`;
    const role = el.getAttribute('role');
    // Structural fallback: tag + nth-of-type chain, capped in depth.
    const parts: string[] = [];
    let cur: HTMLElement | null = el;
    let depth = 0;
    while (cur && cur !== document.body && depth < 4) {
      const tag = cur.tagName.toLowerCase();
      const parent: HTMLElement | null = cur.parentElement;
      if (!parent) { parts.unshift(tag); break; }
      const sibs = Array.from(parent.children).filter((c) => c.tagName === cur!.tagName);
      parts.unshift(sibs.length > 1 ? `${tag}:nth-of-type(${sibs.indexOf(cur) + 1})` : tag);
      cur = parent;
      depth += 1;
    }
    const path = parts.join(' > ');
    return role ? `${path}[role="${CSS.escape(role)}"]`.replace(/^ > /, '') : path;
  };

  const labelFor = (el: HTMLElement): string => {
    const aria = el.getAttribute('aria-label');
    if (aria) return `${el.tagName.toLowerCase()}[${aria.slice(0, 32)}]`;
    const cls = (el.className || '').toString().split(/\s+/).filter(Boolean)[0];
    return cls ? `${el.tagName.toLowerCase()}.${cls.slice(0, 24)}` : el.tagName.toLowerCase();
  };

  // Where does this pane sit? Horizontal thirds, with modal detected
  // first because a dialog's position is incidental.
  const regionFor = (el: HTMLElement, r: DOMRect): string => {
    let cur: HTMLElement | null = el;
    let depth = 0;
    while (cur && depth < 6) {
      const role = cur.getAttribute('role');
      if (role === 'dialog' || role === 'alertdialog' || cur.tagName === 'DIALOG') return 'modal';
      if (window.getComputedStyle(cur).position === 'fixed' && depth <= 2) {
        const cr = cur.getBoundingClientRect();
        if (cr.width < window.innerWidth * 0.75) return 'modal';
      }
      cur = cur.parentElement;
      depth += 1;
    }
    const vw = window.innerWidth || 1;
    const centre = r.left + r.width / 2;
    // A pane covering most of the width is the main column whatever its centre.
    if (r.width >= vw * 0.6) return 'main';
    if (centre < vw * 0.38) return 'left';
    if (centre > vw * 0.62) return 'right';
    return 'main';
  };

  const surfaces: any[] = [];

  // The document surface always exists conceptually; report it only
  // when it can actually move.
  const docEl = document.scrollingElement || document.documentElement;
  const docRemaining = Math.max(0, docEl.scrollHeight - docEl.clientHeight - docEl.scrollTop);
  const docScrollable = docEl.scrollHeight > docEl.clientHeight + minOverflow;
  if (docScrollable) {
    surfaces.push({
      selector: '', region: 'page', label: 'page (document)',
      rect: { x: 0, y: 0, w: window.innerWidth, h: window.innerHeight },
      scrollTop: Math.round(docEl.scrollTop),
      scrollHeight: Math.round(docEl.scrollHeight),
      clientHeight: Math.round(docEl.clientHeight),
      remainingDown: Math.round(docRemaining),
      remainingUp: Math.round(docEl.scrollTop),
    });
  }

  const all = document.querySelectorAll<HTMLElement>('*');
  const n = Math.min(all.length, scanCap);
  for (let i = 0; i < n; i++) {
    const el = all[i];
    if (el === document.body || el === document.documentElement) continue;
    if (el.clientHeight < minClientHeight) continue;
    if (el.scrollHeight <= el.clientHeight + minOverflow) continue;
    const cs = window.getComputedStyle(el);
    if (cs.overflowY !== 'auto' && cs.overflowY !== 'scroll') continue;
    if (!isVisible(el)) continue;
    const r = el.getBoundingClientRect();
    // Off-screen panes are not what the caller means by "the sidebar".
    if (r.bottom < 0 || r.top > window.innerHeight) continue;
    surfaces.push({
      selector: selectorFor(el),
      region: regionFor(el, r),
      label: labelFor(el),
      rect: { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) },
      scrollTop: Math.round(el.scrollTop),
      scrollHeight: Math.round(el.scrollHeight),
      clientHeight: Math.round(el.clientHeight),
      remainingDown: Math.round(Math.max(0, el.scrollHeight - el.clientHeight - el.scrollTop)),
      remainingUp: Math.round(el.scrollTop),
    });
  }

  // Which surface holds the text the caller is hunting for? Answering
  // this is what turns "scroll down" into "scroll the thing that can
  // actually reveal it".
  if (needle) {
    // Take the DEEPEST element whose text matches, not the first in
    // document order. Every ancestor of the match also "contains" the
    // string — including the very pane we are trying to identify — so a
    // first-match scan returns the sidebar itself and the containment
    // test below then finds nothing.
    const matches: HTMLElement[] = [];
    const cands = document.querySelectorAll<HTMLElement>(
      'a, button, input, select, textarea, label, summary, [role], [aria-label],'
      + ' [data-testid], h1, h2, h3, h4, h5, li, td, th, span, div, p',
    );
    const cap = Math.min(cands.length, textScanCap);
    for (let i = 0; i < cap; i++) {
      const el = cands[i];
      const txt = norm(`${el.innerText || el.textContent || ''} ${el.getAttribute('aria-label') || ''}`);
      if (txt && txt.indexOf(needle) !== -1) {
        matches.push(el);
        // Keep the tail: ancestors match first in document order, so the
        // deepest element is always among the last seen.
        if (matches.length > maxTextMatches) matches.shift();
      }
    }
    let match: HTMLElement | null = null;
    for (const m of matches) {
      if (!matches.some((o) => o !== m && m.contains(o))) { match = m; break; }
    }
    if (!match && matches.length) match = matches[matches.length - 1];
    if (match) {
      // Innermost pane wins: for nested scrollers the tighter one is the
      // surface that can actually bring the text into view.
      let best: any = null;
      for (const s of surfaces) {
        if (!s.selector) continue;
        const host = document.querySelector(s.selector);
        if (host && host.contains(match)) {
          if (!best || (host as HTMLElement).clientHeight <= best.h) {
            best = { s, h: (host as HTMLElement).clientHeight };
          }
        }
      }
      if (best) best.s.containsTarget = true;
      // Nothing inner claimed it, so the document owns it.
      if (!surfaces.some((s: any) => s.containsTarget)) {
        const pageSurface = surfaces.find((s: any) => s.region === 'page');
        if (pageSurface) pageSurface.containsTarget = true;
      }
    }
  }

  // Rank: a surface holding the target first, then by scrollable area.
  surfaces.sort((a: any, b: any) => {
    if (!!b.containsTarget !== !!a.containsTarget) return b.containsTarget ? 1 : -1;
    return (b.clientHeight * (b.scrollHeight - b.clientHeight))
         - (a.clientHeight * (a.scrollHeight - a.clientHeight));
  });
  return surfaces.slice(0, maxSurfaces);
}
/* eslint-enable @typescript-eslint/no-explicit-any */

export async function surveyScrollSurfaces(
  page: Page,
  opts: { targetText?: string } = {},
): Promise<ScrollSurface[]> {
  try {
    const out = await page.evaluate(
      surveyInPage as unknown as (...a: unknown[]) => unknown,
      opts.targetText || '',
      MIN_CLIENT_HEIGHT,
      MIN_OVERFLOW,
      SCAN_CAP,
      TEXT_SCAN_CAP,
      MAX_TEXT_MATCHES,
      MAX_SURFACES,
    );
    return (out as ScrollSurface[]) || [];
  } catch {
    return [];
  }
}

/**
 * Choose the surface a scroll should act on.
 *
 * Precedence, strongest first:
 *   1. an explicit container selector from the caller;
 *   2. an explicit region name ('left', 'right', 'main', 'modal', 'page');
 *   3. the surface that contains `targetText`, when one does and it is
 *      not the page — this is the case the old code got wrong;
 *   4. nothing, meaning the caller should use the existing page-scroll
 *      cascade and keep today's behaviour.
 */
export function chooseSurface(
  surfaces: ScrollSurface[],
  opts: { containerSelector?: string; region?: string; hasTargetText?: boolean },
): ScrollSurface | null {
  const sel = (opts.containerSelector || '').trim();
  if (sel) {
    return surfaces.find((s) => s.selector === sel)
      ?? { selector: sel, region: 'main', label: sel, rect: { x: 0, y: 0, w: 0, h: 0 },
           scrollTop: 0, scrollHeight: 0, clientHeight: 0, remainingDown: 0, remainingUp: 0 };
  }
  const region = (opts.region || '').trim().toLowerCase();
  if (region) {
    if (region === 'page' || region === 'document') {
      return surfaces.find((s) => s.region === 'page') ?? null;
    }
    const inRegion = surfaces.filter((s) => s.region === region && s.selector);
    if (inRegion.length) return inRegion[0];
    return null;
  }
  if (opts.hasTargetText) {
    const owner = surfaces.find((s) => s.containsTarget);
    if (owner && owner.selector) return owner;   // page surface keeps the old path
  }
  return null;
}


/**
 * A compact fingerprint of where every pane is scrolled to.
 *
 * Observation identity is built from the interactive-element listing,
 * which does not change when a pane scrolls: the same rows are in the
 * DOM, only their offsets moved. Without this the screenshot dedup sees
 * "no change" and refuses the shot the agent needs, and the vision cache
 * serves bboxes captured before the pane moved. This is the same reason
 * `iframeSignature` exists for in-frame mutations.
 *
 * Offsets are bucketed to 50px so scroll jitter does not thrash the
 * cache while real movement always does.
 */
export function scrollSignatureOf(surfaces: ScrollSurface[]): string {
  if (!surfaces.length) return '';
  return surfaces
    .map((s) => `${s.selector || 'page'}:${Math.round(s.scrollTop / 50)}`)
    .sort()
    .join('|');
}
