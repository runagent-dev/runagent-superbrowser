/**
 * Pixel-scroll target probe + newly-visible label trace.
 *
 * Closes the open-loop pixel-scroll anti-hallucination gap: after a
 * /session/:id/scroll dispatch, this probe answers "did target_text
 * land in the new viewport?" via direct DOM measurement, plus emits a
 * top-N viewport-sorted list of newly visible interactive labels.
 *
 * Lifts the matching closure from page.ts:scrollUntil so both pixel-
 * scroll and word-scroll paths share one matcher.
 */

import type { Page } from 'puppeteer-core';

export type ProbeResult = {
  in_viewport: boolean;
  fully_in_viewport: boolean;
  anywhere_in_dom: boolean;
  above_fold: boolean;
  below_fold: boolean;
  sticky_candidate?: boolean;
  matched_text?: string;
  matched_selector?: string;
};

export type BboxRect = { top: number; left: number; w: number; h: number };
export type PreScrollBbox = { selector: string; rect: BboxRect };

export type ProbeOptions = {
  targetText?: string;
  preBboxes?: PreScrollBbox[];
  collectNewlyVisible: boolean;
};

const INTERACTIVE_SELECTOR =
  'a, button, input, select, textarea, label, summary, '
  + '[role], [aria-label], [data-testid], h1, h2, h3, h4, h5, '
  + 'li, td, th';

const FIND_MATCH_SELECTOR = INTERACTIVE_SELECTOR + ', span, div';

const SCAN_CAP = 500;

export async function runScrollProbe(
  page: Page,
  opts: ProbeOptions,
): Promise<{ probe: ProbeResult | null; newly_visible: string[] }> {
  const targetText = (opts.targetText ?? '').trim();
  let isRegex = false;
  if (targetText) {
    try {
      new RegExp(targetText, 'i');
      isRegex = true;
    } catch {
      isRegex = false;
    }
  }
  return (await page.evaluate(
    (args: {
      regexSrc: string;
      isRegex: boolean;
      collectNewlyVisible: boolean;
      preBboxes: PreScrollBbox[];
      scanCap: number;
      selector: string;
    }): { probe: ProbeResult | null; newly_visible: string[] } => {
      const { regexSrc, isRegex: ir, collectNewlyVisible, preBboxes, scanCap, selector } = args;
      const matchText = (txt: string): boolean => {
        if (!regexSrc) return false;
        if (ir) {
          try {
            return new RegExp(regexSrc, 'i').test(txt);
          } catch {
            return txt.toLowerCase().includes(regexSrc.toLowerCase());
          }
        }
        return txt.toLowerCase().includes(regexSrc.toLowerCase());
      };
      const isHiddenByCollapse = (el: Element): boolean => {
        let walker: Element | null = el.parentElement;
        let depth = 0;
        while (walker && walker !== document.body && depth < 12) {
          if (
            walker.tagName === 'DETAILS'
            && !(walker as HTMLDetailsElement).open
          ) return true;
          if (walker.getAttribute('aria-expanded') === 'false') return true;
          walker = walker.parentElement;
          depth += 1;
        }
        return false;
      };
      const cssVisible = (el: Element): boolean => {
        const cs = window.getComputedStyle(el as HTMLElement);
        return cs.visibility !== 'hidden' && cs.display !== 'none';
      };
      const shorten = (s: string): string => {
        const norm = s.replace(/\s+/g, ' ').trim();
        if (!norm) return '';
        return norm.length > 28 ? `${norm.slice(0, 26)}…` : norm;
      };
      const buildSelector = (el: HTMLElement): string => {
        const bits: string[] = [el.tagName.toLowerCase()];
        const id = el.getAttribute('id');
        if (id) bits.push(`#${id}`);
        const dt = el.getAttribute('data-testid');
        if (dt) bits.push(`[data-testid="${dt}"]`);
        return bits.join('');
      };

      const vpH = window.innerHeight;
      const vpW = window.innerWidth;
      const interactive = (Array.from(
        document.querySelectorAll(selector),
      ) as HTMLElement[]).slice(0, scanCap);

      type Candidate = {
        el: HTMLElement;
        topY: number;
        inViewport: boolean;
        fullyInViewport: boolean;
        aboveFold: boolean;
        belowFold: boolean;
        composite: string;
      };
      const visible: Candidate[] = [];
      const anywhere: Candidate[] = [];

      for (const el of interactive) {
        if (!cssVisible(el)) continue;
        if (isHiddenByCollapse(el)) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        const horiz = rect.right > 0 && rect.left < vpW;
        const inViewport = horiz && rect.bottom > 0 && rect.top < vpH;
        const fullyInViewport = horiz && rect.top >= 0 && rect.bottom <= vpH;
        const aboveFold = rect.bottom <= 0;
        const belowFold = rect.top >= vpH;
        const aria = el.getAttribute('aria-label') || '';
        const txt = ((el as HTMLElement).innerText || el.textContent || '').trim();
        const placeholder = el.getAttribute('placeholder') || '';
        const composite = `${txt}\n${aria}\n${placeholder}`.trim();
        const cand: Candidate = {
          el,
          topY: rect.top,
          inViewport,
          fullyInViewport,
          aboveFold,
          belowFold,
          composite,
        };
        anywhere.push(cand);
        if (inViewport) visible.push(cand);
      }

      let probe: ProbeResult | null = null;
      if (regexSrc) {
        let chosen: Candidate | null = null;
        // Priority: fully-in-viewport → partially → anywhere in DOM.
        for (const c of visible) {
          if (c.fullyInViewport && matchText(c.composite)) {
            chosen = c;
            break;
          }
        }
        if (!chosen) {
          for (const c of visible) {
            if (matchText(c.composite)) {
              chosen = c;
              break;
            }
          }
        }
        if (!chosen) {
          for (const c of anywhere) {
            if (matchText(c.composite)) {
              chosen = c;
              break;
            }
          }
        }
        if (chosen) {
          const matchedSelector = buildSelector(chosen.el);
          let sticky = false;
          if (chosen.inViewport && preBboxes.length > 0) {
            for (const pre of preBboxes) {
              if (pre.selector === matchedSelector
                  && Math.abs(pre.rect.top - chosen.topY) < 4) {
                sticky = true;
                break;
              }
            }
          }
          probe = {
            in_viewport: chosen.inViewport,
            fully_in_viewport: chosen.fullyInViewport,
            anywhere_in_dom: true,
            above_fold: chosen.aboveFold,
            below_fold: chosen.belowFold,
            matched_text: chosen.composite.slice(0, 120),
            matched_selector: matchedSelector,
          };
          if (sticky) probe.sticky_candidate = true;
        } else {
          probe = {
            in_viewport: false,
            fully_in_viewport: false,
            anywhere_in_dom: false,
            above_fold: false,
            below_fold: false,
          };
        }
      }

      let newly_visible: string[] = [];
      if (collectNewlyVisible) {
        const fully = visible.filter((c) => c.fullyInViewport);
        fully.sort((a, b) => a.topY - b.topY);
        const seen = new Set<string>();
        for (const c of fully) {
          if (newly_visible.length >= 5) break;
          const lbl = shorten(c.composite);
          if (!lbl || seen.has(lbl)) continue;
          seen.add(lbl);
          newly_visible.push(lbl);
        }
      }

      return { probe, newly_visible };
    },
    {
      regexSrc: targetText,
      isRegex,
      collectNewlyVisible: opts.collectNewlyVisible,
      preBboxes: opts.preBboxes ?? [],
      scanCap: SCAN_CAP,
      selector: INTERACTIVE_SELECTOR,
    },
  )) as { probe: ProbeResult | null; newly_visible: string[] };
}

export async function capturePreScrollBboxes(
  page: Page,
  targetText: string,
): Promise<PreScrollBbox[]> {
  const trimmed = targetText.trim();
  if (!trimmed) return [];
  let isRegex = false;
  try {
    new RegExp(trimmed, 'i');
    isRegex = true;
  } catch {
    isRegex = false;
  }
  return (await page.evaluate(
    (args: {
      regexSrc: string;
      isRegex: boolean;
      scanCap: number;
      selector: string;
    }): PreScrollBbox[] => {
      const { regexSrc, isRegex: ir, scanCap, selector } = args;
      const matchText = (txt: string): boolean => {
        if (ir) {
          try {
            return new RegExp(regexSrc, 'i').test(txt);
          } catch {
            return txt.toLowerCase().includes(regexSrc.toLowerCase());
          }
        }
        return txt.toLowerCase().includes(regexSrc.toLowerCase());
      };
      const buildSelector = (el: HTMLElement): string => {
        const bits: string[] = [el.tagName.toLowerCase()];
        const id = el.getAttribute('id');
        if (id) bits.push(`#${id}`);
        const dt = el.getAttribute('data-testid');
        if (dt) bits.push(`[data-testid="${dt}"]`);
        return bits.join('');
      };
      const out: PreScrollBbox[] = [];
      const interactive = (Array.from(
        document.querySelectorAll(selector),
      ) as HTMLElement[]).slice(0, scanCap);
      for (const el of interactive) {
        if (out.length >= 8) break;
        const aria = el.getAttribute('aria-label') || '';
        const txt = ((el as HTMLElement).innerText || el.textContent || '').trim();
        const placeholder = el.getAttribute('placeholder') || '';
        const composite = `${txt}\n${aria}\n${placeholder}`.trim();
        if (!matchText(composite)) continue;
        const r = el.getBoundingClientRect();
        out.push({
          selector: buildSelector(el),
          rect: { top: r.top, left: r.left, w: r.width, h: r.height },
        });
      }
      return out;
    },
    {
      regexSrc: trimmed,
      isRegex,
      scanCap: SCAN_CAP,
      selector: INTERACTIVE_SELECTOR,
    },
  )) as PreScrollBbox[];
}

/**
 * Lifted from page.ts:scrollUntil's findMatch closure. Returns the first
 * visible interactive element whose composite text matches targetText
 * (substring or regex) AND whose role matches targetRole (when set).
 * Scoped to containerSelector when provided. Shared by browser_scroll
 * probe path and browser_scroll_until.
 */
export async function findFirstInteractiveMatch(
  page: Page,
  opts: { targetText?: string; targetRole?: string; containerSelector?: string },
): Promise<{ selector: string; text: string } | null> {
  const targetText = (opts.targetText ?? '').trim();
  const targetRole = (opts.targetRole ?? '').trim();
  const containerSelector = (opts.containerSelector ?? '').trim() || undefined;
  if (!targetText && !targetRole) return null;
  let isRegex = false;
  if (targetText) {
    try {
      new RegExp(targetText, 'i');
      isRegex = true;
    } catch {
      isRegex = false;
    }
  }
  return (await page.evaluate(
    (args: {
      regexSrc: string;
      isRegex: boolean;
      role: string;
      container?: string;
      selector: string;
    }) => {
      const { regexSrc: rs, isRegex: ir, role, container, selector } = args;
      const matchText = (txt: string): boolean => {
        if (!rs) return true;
        if (ir) {
          try {
            return new RegExp(rs, 'i').test(txt);
          } catch {
            return txt.toLowerCase().includes(rs.toLowerCase());
          }
        }
        return txt.toLowerCase().includes(rs.toLowerCase());
      };
      const root: ParentNode = container
        ? (document.querySelector(container) as HTMLElement | null) ?? document
        : document;
      const containerEl = container ? (root as HTMLElement) : null;
      const isHiddenByCollapse = (el: Element): boolean => {
        let walker: Element | null = el.parentElement;
        let depth = 0;
        while (walker && walker !== document.body && depth < 12) {
          if (
            walker.tagName === 'DETAILS'
            && !(walker as HTMLDetailsElement).open
          ) return true;
          if (walker.getAttribute('aria-expanded') === 'false') return true;
          walker = walker.parentElement;
          depth += 1;
        }
        return false;
      };
      const isVisible = (el: Element): boolean => {
        const r = (el as HTMLElement).getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        if (containerEl) {
          const cr = containerEl.getBoundingClientRect();
          if (r.bottom < cr.top || r.top > cr.bottom) return false;
          if (r.right < cr.left || r.left > cr.right) return false;
        } else {
          const vpH = window.innerHeight;
          const vpW = window.innerWidth;
          if (r.bottom < 0 || r.top > vpH) return false;
          if (r.right < 0 || r.left > vpW) return false;
        }
        const cs = window.getComputedStyle(el as HTMLElement);
        if (cs.visibility === 'hidden' || cs.display === 'none') return false;
        if (isHiddenByCollapse(el)) return false;
        return true;
      };
      const interactive = Array.from(root.querySelectorAll(selector));
      for (const el of interactive) {
        if (!isVisible(el)) continue;
        if (role) {
          const elRole = (el.getAttribute('role') || el.tagName.toLowerCase()).toLowerCase();
          if (elRole !== role.toLowerCase()) continue;
        }
        const txt = ((el as HTMLElement).innerText || (el as HTMLElement).textContent || '').trim();
        const ariaLbl = el.getAttribute('aria-label') || '';
        const placeholder = el.getAttribute('placeholder') || '';
        const composite = `${txt}\n${ariaLbl}\n${placeholder}`.trim();
        if (matchText(composite)) {
          const selectorBits: string[] = [el.tagName.toLowerCase()];
          const id = el.getAttribute('id');
          if (id) selectorBits.push(`#${id}`);
          const dt = el.getAttribute('data-testid');
          if (dt) selectorBits.push(`[data-testid="${dt}"]`);
          return {
            selector: selectorBits.join(''),
            text: composite.slice(0, 120),
          };
        }
      }
      return null;
    },
    {
      regexSrc: targetText,
      isRegex,
      role: targetRole,
      container: containerSelector,
      selector: FIND_MATCH_SELECTOR,
    },
  )) as { selector: string; text: string } | null;
}
