/**
 * Element coordinate resolution with 3-tier fallback from BrowserOS.
 *
 * Resolves the center coordinates of a DOM element using:
 * 1. DOM.getContentQuads (most precise)
 * 2. DOM.getBoxModel (content box fallback)
 * 3. Runtime.callFunctionOn with getBoundingClientRect (JS fallback)
 */

import type { CDPSession, Page } from 'puppeteer-core';

export interface ElementCoords {
  x: number;
  y: number;
}

/**
 * Get the center coordinates of an element via CDP.
 * Uses 3-tier fallback for maximum reliability.
 *
 * @param client CDP session
 * @param backendNodeId CDP backend node ID (from AX tree or DOM)
 */
export async function getElementCenter(
  client: CDPSession,
  backendNodeId: number,
): Promise<ElementCoords> {
  // Scroll into view first
  try {
    await client.send('DOM.scrollIntoViewIfNeeded', { backendNodeId });
  } catch {
    // scrollIntoView may fail for some elements — continue
  }

  // Tier 1: DOM.getContentQuads (most precise)
  try {
    const { quads } = (await client.send('DOM.getContentQuads', {
      backendNodeId,
    })) as { quads: number[][] };

    if (quads && quads.length > 0 && quads[0].length >= 8) {
      const quad = quads[0];
      // Quad is 4 points (8 values): x1,y1, x2,y2, x3,y3, x4,y4
      const x = (quad[0] + quad[2] + quad[4] + quad[6]) / 4;
      const y = (quad[1] + quad[3] + quad[5] + quad[7]) / 4;
      if (x > 0 || y > 0) return { x: Math.round(x), y: Math.round(y) };
    }
  } catch {
    // Fallthrough to next tier
  }

  // Tier 2: DOM.getBoxModel (content box)
  try {
    const { model } = (await client.send('DOM.getBoxModel', {
      backendNodeId,
    })) as { model: { content: number[] } };

    if (model && model.content && model.content.length >= 8) {
      const c = model.content;
      const x = (c[0] + c[2] + c[4] + c[6]) / 4;
      const y = (c[1] + c[3] + c[5] + c[7]) / 4;
      if (x > 0 || y > 0) return { x: Math.round(x), y: Math.round(y) };
    }
  } catch {
    // Fallthrough to next tier
  }

  // Tier 3: JS fallback via Runtime.callFunctionOn
  try {
    // Resolve backendNodeId to a remote object
    const { object } = (await client.send('DOM.resolveNode', {
      backendNodeId,
    })) as { object: { objectId: string } };

    if (object?.objectId) {
      const result = (await client.send('Runtime.callFunctionOn', {
        objectId: object.objectId,
        functionDeclaration: `function() {
          const rect = this.getBoundingClientRect();
          return {
            x: Math.round(rect.left + rect.width / 2),
            y: Math.round(rect.top + rect.height / 2),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
          };
        }`,
        returnByValue: true,
      })) as { result: { value: { x: number; y: number; width: number; height: number } } };

      if (result?.result?.value) {
        return { x: result.result.value.x, y: result.result.value.y };
      }
    }
  } catch {
    // All tiers failed
  }

  throw new Error(`Could not resolve coordinates for element (backendNodeId: ${backendNodeId})`);
}

/**
 * Get element center coordinates from the page using a CSS selector.
 * Falls back to evaluate getBoundingClientRect.
 */
export async function getElementCenterBySelector(
  page: Page,
  selector: string,
): Promise<ElementCoords | null> {
  try {
    const result = await page.evaluate((sel: string) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const rect = el.getBoundingClientRect();
      return {
        x: Math.round(rect.left + rect.width / 2),
        y: Math.round(rect.top + rect.height / 2),
      };
    }, selector);
    return result;
  } catch {
    return null;
  }
}

/**
 * Scroll an element into view using CDP.
 */
export async function scrollIntoView(
  client: CDPSession,
  backendNodeId: number,
): Promise<void> {
  try {
    await client.send('DOM.scrollIntoViewIfNeeded', { backendNodeId });
  } catch {
    // Try JS fallback
    try {
      const { object } = (await client.send('DOM.resolveNode', {
        backendNodeId,
      })) as { object: { objectId: string } };

      if (object?.objectId) {
        await client.send('Runtime.callFunctionOn', {
          objectId: object.objectId,
          functionDeclaration: `function() { this.scrollIntoView({ block: 'center', behavior: 'instant' }); }`,
        });
      }
    } catch {
      // scrollIntoView not critical
    }
  }
}

/**
 * Focus an element via CDP DOM.focus.
 * Pattern from BrowserOS browser.ts.
 */
export async function focusElement(
  client: CDPSession,
  backendNodeId: number,
): Promise<void> {
  await scrollIntoView(client, backendNodeId);
  try {
    await client.send('DOM.focus', { backendNodeId });
  } catch {
    // Fallback: JS focus
    try {
      const { object } = (await client.send('DOM.resolveNode', {
        backendNodeId,
      })) as { object: { objectId: string } };
      if (object?.objectId) {
        await client.send('Runtime.callFunctionOn', {
          objectId: object.objectId,
          functionDeclaration: `function() { this.focus(); }`,
        });
      }
    } catch {
      // Focus may not work on all elements
    }
  }
}

/**
 * Perform a search in the DOM via CDP.
 * Supports CSS selectors, XPath, and text search.
 * Pattern from BrowserOS dom.ts.
 */
export async function domSearchCDP(
  client: CDPSession,
  query: string,
  limit: number = 20,
): Promise<Array<{ nodeId: number; outerHTML: string }>> {
  const results: Array<{ nodeId: number; outerHTML: string }> = [];

  try {
    // Get document root
    const { root } = (await client.send('DOM.getDocument', { depth: 0 })) as {
      root: { nodeId: number };
    };

    // Perform search
    const { searchId, resultCount } = (await client.send('DOM.performSearch', {
      query,
      includeUserAgentShadowDOM: false,
    })) as { searchId: string; resultCount: number };

    if (resultCount > 0) {
      const count = Math.min(resultCount, limit);
      const { nodeIds } = (await client.send('DOM.getSearchResults', {
        searchId,
        fromIndex: 0,
        toIndex: count,
      })) as { nodeIds: number[] };

      for (const nodeId of nodeIds) {
        try {
          const { outerHTML } = (await client.send('DOM.getOuterHTML', {
            nodeId,
          })) as { outerHTML: string };
          results.push({ nodeId, outerHTML: outerHTML.substring(0, 500) });
        } catch {
          // Node may have been removed
        }
      }

      // Clean up search
      await client.send('DOM.discardSearchResults', { searchId }).catch(() => {});
    }
  } catch {
    // DOM search not available
  }

  return results;
}

/**
 * Result of selectOptionByLabel.
 */
export interface SelectOptionByLabelResult {
  ok: boolean;
  picked_text?: string;
  trigger_selector?: string;
  option_selector?: string;
  candidates?: string[];
  reason?: string;
  verified?: boolean;
  took_ms: number;
  message?: string;
  /** Phase C3: true when the tool auto-clicked a collapsed accordion
   *  header to expose the section before finding the trigger. The
   *  caller surfaces this so the brain learns the dialect. */
  auto_expanded?: boolean;
}

/**
 * Select an option in any dropdown — native <select>, ARIA combobox/listbox,
 * or Headless-UI/Reach style custom widget — by matching a *label* to the
 * trigger and a *value* (visible text) to the option.
 *
 * The LLM never sees a DOM index or vision-bbox V-index; index churn is
 * absorbed entirely server-side. Returns either a successful pick or the
 * candidate list so the caller can retry with a corrected value.
 */
export async function selectOptionByLabel(
  page: Page,
  opts: {
    label: string;
    value: string;
    fuzzy?: boolean;
    timeout?: number;
    extraOpenSelectors?: string[];
    extraOptionSelectors?: string[];
    /**
     * Phase C1: scope the label search to a specific section subtree.
     * When provided, the label search runs ONLY within DOM containers
     * whose heading/aria-label matches `section`. This is the fix for
     * the wineaccess.com failure where `selectOptionByLabel('Region',
     * 'Oregon')` matched the SORT dropdown ("For You / Most Popular")
     * because the trigger search walked all interactive elements with
     * no section context.
     */
    section?: string;
    /**
     * Phase C1: when true (default), refuse with `requires_section`
     * if the label matches triggers in 2+ distinct sections AND no
     * section= is supplied. When false, the caller has accepted that
     * the global search may pick the wrong dropdown.
     */
    requireSectionOnAmbiguity?: boolean;
    /**
     * Phase C3: when true (default), auto-click the section's
     * expand-control and retry the trigger search if the requested
     * section exists but appears collapsed (aria-expanded="false" or
     * <details> without [open]). Capped at one auto-expand per call.
     */
    autoExpandSection?: boolean;
  },
): Promise<SelectOptionByLabelResult> {
  const start = Date.now();
  const label = opts.label;
  const value = opts.value;
  const fuzzy = opts.fuzzy !== false;
  const timeout = Math.max(500, Math.min(20000, opts.timeout ?? 4000));
  const extraOptionSelectors = opts.extraOptionSelectors ?? [];
  const section = opts.section?.trim() || '';
  const requireSection = opts.requireSectionOnAmbiguity !== false;
  const autoExpandSection = opts.autoExpandSection !== false;
  let autoExpanded = false;

  // 1. Locate trigger by label text (label-for, aria-labelledby, aria-label,
  //    or text). Tag it with data-sb-trigger so subsequent calls survive
  //    DOM-index renumbering.
  //
  // Phase C1: when `section` is provided, the label search is scoped to
  // the subtree of the matching section element (heading text or
  // aria-labelledby match). The brain is responsible for naming the
  // section ("Region", "Filter by", "Pairings") — we walk ancestors
  // looking for sections, headings, fieldsets, summaries, or
  // [role="region"] elements whose label contains it.
  type TriggerShape = {
    selector: string;
    isNativeSelect: boolean;
    currentText: string;
    role: string;
    ariaExpanded: string;
  };
  const triggerResult: { trigger: TriggerShape | null; sections: string[]; sectionFound: boolean } = await page.evaluate(
    (cfg: { lbl: string; section: string }) => {
      const norm = (s: string) => (s || '').replace(/\s+/g, ' ').trim();
      const lower = (s: string) => norm(s).toLowerCase();
      const target = lower(cfg.lbl);
      const sectionTarget = lower(cfg.section);
      if (!target) return { trigger: null, sections: [], sectionFound: false };

      const tagEl = (el: Element) => {
        const id = `sb-trigger-${Math.random().toString(36).slice(2, 10)}`;
        el.setAttribute('data-sb-trigger', id);
        const isSelect = el.tagName.toLowerCase() === 'select';
        return {
          selector: `[data-sb-trigger="${id}"]`,
          isNativeSelect: isSelect,
          currentText: norm((el as HTMLElement).textContent || '').slice(0, 200),
          role: el.getAttribute('role') || '',
          ariaExpanded: el.getAttribute('aria-expanded') || '',
        };
      };

      // Walk an element's ancestors collecting section labels.
      const sectionPathOf = (el: Element): string[] => {
        const path: string[] = [];
        let cur: Element | null = el;
        let depth = 0;
        while (cur && cur !== document.body && depth < 16) {
          const t = cur.tagName.toLowerCase();
          const role = cur.getAttribute('role') || '';
          if (
            t === 'section' || t === 'aside' || t === 'nav'
            || t === 'fieldset' || t === 'details'
            || role === 'region' || role === 'group'
            || role === 'tablist' || role === 'navigation'
            || (cur.classList && (
              cur.classList.contains('filter') || cur.classList.contains('accordion') || cur.classList.contains('sidebar')
              || cur.classList.contains('section')
            ))
          ) {
            const labelledby = cur.getAttribute('aria-labelledby');
            if (labelledby) {
              const labelEl = document.getElementById(labelledby);
              if (labelEl) {
                path.unshift(((labelEl.textContent || '').trim().slice(0, 60)));
                cur = cur.parentElement;
                depth++;
                continue;
              }
            }
            const heading = cur.querySelector('h1,h2,h3,h4,h5,h6,legend,summary,[role="heading"]');
            if (heading) {
              path.unshift(((heading.textContent || '').trim().slice(0, 60)));
            } else {
              const aria = cur.getAttribute('aria-label');
              if (aria) path.unshift(aria.slice(0, 60));
            }
          }
          cur = cur.parentElement;
          depth++;
        }
        return path.filter(Boolean);
      };

      // Section-scoped variant — Phase F1 word-boundary match.
      //
      // Earlier behaviour did substring matching on the section path,
      // which let "Type and Sort Options" match `inSection('type')`
      // and pulled the SORT dropdown into the scoped candidate set on
      // wineaccess.com. Word-boundary matching makes the predicate
      // refuse that path: "type" must appear as a standalone token
      // in the path string, not as a substring of another word.
      //
      // We additionally accept exact equality (case-insensitive) on
      // any single path segment as a strong-signal match.
      const tokenizeSection = (s: string): Set<string> => {
        const tokens = new Set<string>();
        for (const m of s.toLowerCase().match(/[a-z0-9]+/g) || []) {
          tokens.add(m);
        }
        return tokens;
      };
      const sectionTokens = sectionTarget
        ? tokenizeSection(sectionTarget)
        : new Set<string>();
      const inSection = (el: Element): boolean => {
        if (!sectionTarget) return true;
        const path = sectionPathOf(el);
        for (const seg of path) {
          const segLower = seg.toLowerCase().trim();
          if (segLower === sectionTarget) return true;
          // Token superset check: every token of the target must be a
          // distinct word in the path segment. "Variety" target tokens
          // = {variety}; segment "Wine Variety" tokens = {wine,variety}
          // → match. Segment "Type and Sort Options" tokens
          // = {type,and,sort,options}; target "Type" tokens = {type}
          // → match. The latter is intended: the user explicitly named
          // 'Type' as the section, and the heading says "Type and Sort
          // Options" — that IS the type section with a wider label.
          // For the wineaccess bug, the issue was the SORT dropdown
          // also being inside that container; F2 + F4 below filter
          // that out via Strategy-D demotion + filter-section
          // preference.
          const segTokens = tokenizeSection(seg);
          let allFound = sectionTokens.size > 0;
          for (const t of sectionTokens) {
            if (!segTokens.has(t)) {
              allFound = false;
              break;
            }
          }
          if (allFound) return true;
        }
        return false;
      };

      // Phase F4: filter-section predicate. Real filter sidebars live
      // under <aside>, [role="region"] with aria-label containing
      // "filter"/"refine"/"facet", or classes like .filter / .facets.
      // The wineaccess SORT dropdown lives in a top-level <header> /
      // <form> banner. When section= is specified AND the page has at
      // least one "real filter" container, prefer candidates inside
      // those containers; de-rank candidates inside <header>/<form>.
      const isInFilterContainer = (el: Element): boolean => {
        let cur: Element | null = el;
        let depth = 0;
        while (cur && cur !== document.body && depth < 16) {
          const t = cur.tagName.toLowerCase();
          const role = cur.getAttribute('role') || '';
          const aria = (cur.getAttribute('aria-label') || '').toLowerCase();
          const classList = cur.classList ? Array.from(cur.classList) : [];
          if (t === 'aside') return true;
          if (role === 'region' && /(filter|refine|facet|search)/.test(aria)) {
            return true;
          }
          if (
            classList.some((c) => /filter|facet|refine|sidebar/i.test(c))
          ) {
            return true;
          }
          cur = cur.parentElement;
          depth++;
        }
        return false;
      };
      const isInChromeContainer = (el: Element): boolean => {
        // Sort dropdowns + result-page chrome usually live in <header>
        // or generic <form> banners. Demote those when scoped.
        let cur: Element | null = el;
        let depth = 0;
        while (cur && cur !== document.body && depth < 16) {
          const t = cur.tagName.toLowerCase();
          const role = cur.getAttribute('role') || '';
          if (t === 'header' || role === 'banner') return true;
          cur = cur.parentElement;
          depth++;
        }
        return false;
      };

      // Collect ALL trigger candidates first, then apply section
      // scoping at the end. This lets us:
      //   * detect ambiguity (multiple distinct sections match)
      //   * report what sections DID exist when the request was scoped
      const candidates: Array<{ el: Element; matchKind: string; sectionPath: string[]; inFilterContainer: boolean; inChrome: boolean }> = [];
      const seen = new Set<Element>();
      const consider = (el: Element | null, matchKind: string) => {
        if (!el || seen.has(el)) return;
        if ((el as HTMLElement).offsetParent === null && el.tagName !== 'SELECT') return;
        seen.add(el);
        candidates.push({
          el,
          matchKind,
          sectionPath: sectionPathOf(el),
          inFilterContainer: isInFilterContainer(el),
          inChrome: isInChromeContainer(el),
        });
      };

      // A. <label for="x">Lbl</label> + #x
      for (const lab of Array.from(document.querySelectorAll<HTMLLabelElement>('label[for]'))) {
        if (lower(lab.textContent || '').includes(target)) {
          const ctrl = document.getElementById(lab.htmlFor);
          consider(ctrl, 'label_for');
        }
      }
      // B. aria-labelledby
      for (const el of Array.from(document.querySelectorAll<HTMLElement>('[aria-labelledby]'))) {
        const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
        const labText = ids
          .map((id) => lower(document.getElementById(id)?.textContent || ''))
          .join(' ');
        if (labText.includes(target)) consider(el, 'aria_labelledby');
      }
      // C. aria-label on combobox/button/listbox/input
      const interactive = Array.from(document.querySelectorAll<HTMLElement>(
        '[role="combobox"], [role="listbox"], [role="button"], [aria-haspopup], button, select, input',
      ));
      for (const el of interactive) {
        const aria = lower(el.getAttribute('aria-label') || '');
        if (aria.includes(target)) consider(el, 'aria_label');
      }
      // D. Visible text on interactive element
      for (const el of interactive) {
        const txt = lower((el as HTMLElement).textContent || '').slice(0, 200);
        if (txt.includes(target)) consider(el, 'visible_text');
      }
      // E. Wrapping <label>
      for (const lab of Array.from(document.querySelectorAll<HTMLLabelElement>('label'))) {
        if (lower(lab.textContent || '').includes(target)) {
          const ctrl = lab.querySelector<HTMLElement>('select, input, [role="combobox"], [role="listbox"]');
          consider(ctrl, 'wrap_label');
        }
      }

      // Distinct sections seen across all candidates — for ambiguity
      // detection and for the error-message candidate list.
      const allSections = new Set<string>();
      for (const c of candidates) {
        const head = c.sectionPath.length > 0 ? c.sectionPath[c.sectionPath.length - 1] : '';
        if (head) allSections.add(head);
      }

      // Section-scoped pick: Phase F2/F4 logic.
      //
      // F2 — Strategy D (visible-text) is the broadest and the source
      // of the wineaccess Sort/Type collision: any interactive whose
      // textContent contains "type" was admitted. When section= is
      // set, demote Strategy D candidates UNLESS they're inside a
      // real filter container (aside / [role=region][aria-label*=
      // "filter"] / .filter classes). Strategies A/B/C/E are more
      // semantically anchored.
      //
      // F4 — among the survivors, prefer candidates inside filter
      // containers over candidates inside <header> banners. The
      // wineaccess sort dropdown is in <header>; the real filter
      // sidebar is in <aside>.
      if (sectionTarget) {
        const inSectionAll = candidates.filter((c) => inSection(c.el));
        const filterPreferred = inSectionAll.filter((c) => {
          // Always allow strong-signal strategies in the filter scope.
          if (c.matchKind !== 'visible_text') return true;
          // Strategy D candidates require filter-container backing.
          return c.inFilterContainer;
        });
        // F4: among survivors, prefer non-chrome candidates.
        const nonChrome = filterPreferred.filter((c) => !c.inChrome);
        const ranked = nonChrome.length > 0 ? nonChrome : filterPreferred;
        if (ranked.length > 0) {
          // Stable sort: filter-container candidates first within the
          // ranked set, so a filter-anchored Strategy A beats a non-
          // filter Strategy A in the same scope.
          ranked.sort((a, b) => {
            const aScore = (a.inFilterContainer ? 2 : 0) + (a.inChrome ? -1 : 0);
            const bScore = (b.inFilterContainer ? 2 : 0) + (b.inChrome ? -1 : 0);
            return bScore - aScore;
          });
          return {
            trigger: tagEl(ranked[0].el),
            sections: Array.from(allSections),
            sectionFound: true,
          };
        }
        // F3: section requested but no high-quality match. Return
        // sectionFound=false; the outer code refuses with
        // section_not_found / section_match_no_value, listing
        // sectionsSeen so the brain can re-call with a real section.
        return {
          trigger: null,
          sections: Array.from(allSections),
          sectionFound: false,
        };
      }

      // No section scoping requested — pick the first candidate but
      // also report how many distinct sections matched (for ambiguity).
      if (candidates.length === 0) {
        return { trigger: null, sections: [], sectionFound: false };
      }
      return {
        trigger: tagEl(candidates[0].el),
        sections: Array.from(allSections),
        sectionFound: true,
      };
    },
    { lbl: label, section },
  );

  let trigger = triggerResult.trigger;
  let sectionsSeen = triggerResult.sections;

  // Phase C3: auto-expand collapsed accordion. If the requested
  // section exists in the DOM (or we couldn't find the trigger inside
  // it because it's hidden) AND we haven't already retried, try to
  // locate + click the section's expander control. Then re-run the
  // initial trigger search exactly once. Capped at one auto-expand
  // per call so a chronically-broken page can't trigger an
  // expand-then-collapse-then-expand storm.
  if (
    autoExpandSection
    && section
    && !trigger
    && !autoExpanded
  ) {
    const expandResult = await page.evaluate((sec: string) => {
      const norm = (s: string) => (s || '').replace(/\s+/g, ' ').trim();
      const lower = (s: string) => norm(s).toLowerCase();
      const want = lower(sec);
      if (!want) return { ok: false, reason: 'empty_section' };
      // Find any element whose accessible name matches `section` AND is
      // an expand control. Strategies, in order:
      //   1. <button aria-expanded="false"> with text matching want
      //   2. <summary> inside <details> not [open] with matching text
      //   3. [aria-controls=X] on an element whose text matches want,
      //      where #X has [aria-expanded="false"] OR is hidden
      const candidates: Array<{ el: Element; rank: number }> = [];
      // 1. aria-expanded buttons
      for (const el of Array.from(document.querySelectorAll<HTMLElement>('[aria-expanded="false"]'))) {
        const txt = lower(el.textContent || '');
        const aria = lower(el.getAttribute('aria-label') || '');
        if (txt.includes(want) || aria.includes(want)) {
          if (el.offsetParent !== null) candidates.push({ el, rank: 1 });
        }
      }
      // 2. <summary> inside collapsed <details>
      for (const sm of Array.from(document.querySelectorAll<HTMLElement>('details:not([open]) > summary'))) {
        const txt = lower(sm.textContent || '');
        if (txt.includes(want) && sm.offsetParent !== null) {
          candidates.push({ el: sm, rank: 2 });
        }
      }
      // 3. [aria-controls] heuristic
      for (const el of Array.from(document.querySelectorAll<HTMLElement>('[aria-controls]'))) {
        const ctrlId = el.getAttribute('aria-controls') || '';
        if (!ctrlId) continue;
        const target = document.getElementById(ctrlId);
        if (!target) continue;
        const collapsed = target.getAttribute('aria-expanded') === 'false'
          || (window.getComputedStyle(target).display === 'none');
        if (!collapsed) continue;
        const txt = lower(el.textContent || '');
        const aria = lower(el.getAttribute('aria-label') || '');
        if ((txt.includes(want) || aria.includes(want)) && el.offsetParent !== null) {
          candidates.push({ el, rank: 3 });
        }
      }
      if (candidates.length === 0) return { ok: false, reason: 'no_expander' };
      candidates.sort((a, b) => a.rank - b.rank);
      const target = candidates[0].el;
      try {
        (target as HTMLElement).click();
      } catch (err) {
        return { ok: false, reason: `click_failed:${err}` };
      }
      return {
        ok: true,
        expandedSelector: target.tagName.toLowerCase()
          + ((target as HTMLElement).id ? `#${(target as HTMLElement).id}` : '')
          + (target.getAttribute('aria-controls') ? `[aria-controls="${target.getAttribute('aria-controls')}"]` : ''),
        expandedText: ((target.textContent || '').trim().slice(0, 60)),
      };
    }, section);

    if (expandResult.ok) {
      autoExpanded = true;
      // Brief settle so the expanded content lays out before re-searching.
      await new Promise((r) => setTimeout(r, 250));
      // Re-run the initial label search exactly as before. We extract
      // it into a small helper here rather than refactor the whole
      // function — copy-paste of the block above with the same
      // cfg/return shape.
      type TriggerShape2 = {
        selector: string;
        isNativeSelect: boolean;
        currentText: string;
        role: string;
        ariaExpanded: string;
      };
      const retry: { trigger: TriggerShape2 | null; sections: string[]; sectionFound: boolean } = await page.evaluate(
        (cfg: { lbl: string; section: string }) => {
          const norm = (s: string) => (s || '').replace(/\s+/g, ' ').trim();
          const lower = (s: string) => norm(s).toLowerCase();
          const target = lower(cfg.lbl);
          const sectionTarget = lower(cfg.section);
          const tagEl = (el: Element) => {
            const id = `sb-trigger-${Math.random().toString(36).slice(2, 10)}`;
            el.setAttribute('data-sb-trigger', id);
            const isSelect = el.tagName.toLowerCase() === 'select';
            return {
              selector: `[data-sb-trigger="${id}"]`,
              isNativeSelect: isSelect,
              currentText: norm((el as HTMLElement).textContent || '').slice(0, 200),
              role: el.getAttribute('role') || '',
              ariaExpanded: el.getAttribute('aria-expanded') || '',
            };
          };
          const sectionPathOf = (el: Element): string[] => {
            const path: string[] = [];
            let cur: Element | null = el;
            let depth = 0;
            while (cur && cur !== document.body && depth < 16) {
              const t = cur.tagName.toLowerCase();
              const role = cur.getAttribute('role') || '';
              if (
                t === 'section' || t === 'aside' || t === 'nav'
                || t === 'fieldset' || t === 'details'
                || role === 'region' || role === 'group'
                || role === 'tablist' || role === 'navigation'
                || (cur.classList && (
                  cur.classList.contains('filter') || cur.classList.contains('accordion')
                  || cur.classList.contains('sidebar') || cur.classList.contains('section')
                ))
              ) {
                const labelledby = cur.getAttribute('aria-labelledby');
                if (labelledby) {
                  const labelEl = document.getElementById(labelledby);
                  if (labelEl) {
                    path.unshift(((labelEl.textContent || '').trim().slice(0, 60)));
                    cur = cur.parentElement;
                    depth++;
                    continue;
                  }
                }
                const heading = cur.querySelector('h1,h2,h3,h4,h5,h6,legend,summary,[role="heading"]');
                if (heading) {
                  path.unshift(((heading.textContent || '').trim().slice(0, 60)));
                } else {
                  const aria = cur.getAttribute('aria-label');
                  if (aria) path.unshift(aria.slice(0, 60));
                }
              }
              cur = cur.parentElement;
              depth++;
            }
            return path.filter(Boolean);
          };
          // Phase F1 retry: word-boundary tokenized matching.
          const tokenizeSection = (s: string): Set<string> => {
            const tokens = new Set<string>();
            for (const m of s.toLowerCase().match(/[a-z0-9]+/g) || []) {
              tokens.add(m);
            }
            return tokens;
          };
          const sectionTokens = sectionTarget
            ? tokenizeSection(sectionTarget)
            : new Set<string>();
          const inSection = (el: Element): boolean => {
            if (!sectionTarget) return true;
            const path = sectionPathOf(el);
            for (const seg of path) {
              const segLower = seg.toLowerCase().trim();
              if (segLower === sectionTarget) return true;
              const segTokens = tokenizeSection(seg);
              let allFound = sectionTokens.size > 0;
              for (const t of sectionTokens) {
                if (!segTokens.has(t)) {
                  allFound = false;
                  break;
                }
              }
              if (allFound) return true;
            }
            return false;
          };
          // Phase F4 retry: filter-vs-chrome predicates.
          const isInFilterContainer = (el: Element): boolean => {
            let cur: Element | null = el;
            let depth = 0;
            while (cur && cur !== document.body && depth < 16) {
              const t = cur.tagName.toLowerCase();
              const role = cur.getAttribute('role') || '';
              const aria = (cur.getAttribute('aria-label') || '').toLowerCase();
              const classList = cur.classList ? Array.from(cur.classList) : [];
              if (t === 'aside') return true;
              if (role === 'region' && /(filter|refine|facet|search)/.test(aria)) return true;
              if (classList.some((c) => /filter|facet|refine|sidebar/i.test(c))) return true;
              cur = cur.parentElement;
              depth++;
            }
            return false;
          };
          const isInChromeContainer = (el: Element): boolean => {
            let cur: Element | null = el;
            let depth = 0;
            while (cur && cur !== document.body && depth < 16) {
              const t = cur.tagName.toLowerCase();
              const role = cur.getAttribute('role') || '';
              if (t === 'header' || role === 'banner') return true;
              cur = cur.parentElement;
              depth++;
            }
            return false;
          };
          const interactive = Array.from(document.querySelectorAll<HTMLElement>(
            '[role="combobox"], [role="listbox"], [role="button"], [aria-haspopup], button, select, input',
          ));
          const candidates: Array<{ el: Element; matchKind: string; sectionPath: string[]; inFilterContainer: boolean; inChrome: boolean }> = [];
          const seen = new Set<Element>();
          const consider = (el: Element | null, matchKind: string) => {
            if (!el || seen.has(el)) return;
            if ((el as HTMLElement).offsetParent === null && el.tagName !== 'SELECT') return;
            seen.add(el);
            candidates.push({
              el,
              matchKind,
              sectionPath: sectionPathOf(el),
              inFilterContainer: isInFilterContainer(el),
              inChrome: isInChromeContainer(el),
            });
          };
          for (const lab of Array.from(document.querySelectorAll<HTMLLabelElement>('label[for]'))) {
            if (lower(lab.textContent || '').includes(target)) consider(document.getElementById(lab.htmlFor), 'label_for');
          }
          for (const el of Array.from(document.querySelectorAll<HTMLElement>('[aria-labelledby]'))) {
            const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
            const labText = ids.map((id) => lower(document.getElementById(id)?.textContent || '')).join(' ');
            if (labText.includes(target)) consider(el, 'aria_labelledby');
          }
          for (const el of interactive) {
            const aria = lower(el.getAttribute('aria-label') || '');
            if (aria.includes(target)) consider(el, 'aria_label');
          }
          for (const el of interactive) {
            const txt = lower((el as HTMLElement).textContent || '').slice(0, 200);
            if (txt.includes(target)) consider(el, 'visible_text');
          }
          for (const lab of Array.from(document.querySelectorAll<HTMLLabelElement>('label'))) {
            if (lower(lab.textContent || '').includes(target)) {
              const ctrl = lab.querySelector<HTMLElement>('select, input, [role="combobox"], [role="listbox"]');
              consider(ctrl, 'wrap_label');
            }
          }
          const allSections = new Set<string>();
          for (const c of candidates) {
            const head = c.sectionPath.length > 0 ? c.sectionPath[c.sectionPath.length - 1] : '';
            if (head) allSections.add(head);
          }
          if (sectionTarget) {
            const inSectionAll = candidates.filter((c) => inSection(c.el));
            const filterPreferred = inSectionAll.filter((c) => c.matchKind !== 'visible_text' || c.inFilterContainer);
            const nonChrome = filterPreferred.filter((c) => !c.inChrome);
            const ranked = nonChrome.length > 0 ? nonChrome : filterPreferred;
            if (ranked.length > 0) {
              ranked.sort((a, b) => {
                const aScore = (a.inFilterContainer ? 2 : 0) + (a.inChrome ? -1 : 0);
                const bScore = (b.inFilterContainer ? 2 : 0) + (b.inChrome ? -1 : 0);
                return bScore - aScore;
              });
              return {
                trigger: tagEl(ranked[0].el),
                sections: Array.from(allSections),
                sectionFound: true,
              };
            }
            return { trigger: null, sections: Array.from(allSections), sectionFound: false };
          }
          if (candidates.length === 0) {
            return { trigger: null, sections: [], sectionFound: false };
          }
          return {
            trigger: tagEl(candidates[0].el),
            sections: Array.from(allSections),
            sectionFound: true,
          };
        },
        { lbl: label, section },
      );
      if (retry.trigger) {
        trigger = retry.trigger;
        sectionsSeen = retry.sections;
      }
    }
  }

  // Phase C1: section was requested but not found / contained no match.
  if (section && !triggerResult.sectionFound && !trigger) {
    return {
      ok: false,
      reason: sectionsSeen.length > 0 ? 'section_match_no_value' : 'section_not_found',
      took_ms: Date.now() - start,
      candidates: sectionsSeen,
      message: sectionsSeen.length > 0
        ? `Section ${JSON.stringify(section)} found, but no element matching ${JSON.stringify(label)} inside it. Sections detected on this page: ${sectionsSeen.slice(0, 8).join(', ')}.`
        : `No section matching ${JSON.stringify(section)} on this page. Sections detected: ${sectionsSeen.slice(0, 8).join(', ') || '(none)'}.`,
    };
  }

  // Phase C1: ambiguity refusal — when no section= supplied and the
  // label matches triggers in 2+ distinct sections, the caller likely
  // meant a specific one. The wineaccess "Region" / Sort case where
  // both sections legitimately had triggers matching "Region" would
  // hit this. The brain should re-call with section= set.
  if (!section && requireSection && sectionsSeen.length >= 2) {
    return {
      ok: false,
      reason: 'requires_section',
      took_ms: Date.now() - start,
      candidates: sectionsSeen,
      message: `Label ${JSON.stringify(label)} matches triggers in ${sectionsSeen.length} distinct sections: ${sectionsSeen.slice(0, 8).join(', ')}. Re-call with section=<one of these> to disambiguate. (Set requireSectionOnAmbiguity=false to accept the first match — risks picking the wrong dropdown.)`,
    };
  }

  if (!trigger) {
    return { ok: false, reason: 'trigger_not_found', took_ms: Date.now() - start };
  }

  // 2. Native <select>: dispatch a real change event via page.select().
  if (trigger.isNativeSelect) {
    try {
      const optionValue = await page.evaluate((sel: string, val: string) => {
        const norm = (s: string) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
        const tgt = norm(val);
        const sel_el = document.querySelector(sel) as HTMLSelectElement | null;
        if (!sel_el) return null;
        const optsArr = Array.from(sel_el.options);
        let m = optsArr.find((o) => o.value.toLowerCase() === tgt);
        if (!m) m = optsArr.find((o) => norm(o.textContent || '') === tgt);
        if (!m) m = optsArr.find((o) => norm(o.textContent || '').startsWith(tgt));
        if (!m) m = optsArr.find((o) => norm(o.textContent || '').includes(tgt));
        return m ? m.value : null;
      }, trigger.selector, value);

      if (optionValue == null) {
        const candidates = await page.evaluate((sel: string) => {
          const sel_el = document.querySelector(sel) as HTMLSelectElement | null;
          return sel_el ? Array.from(sel_el.options).slice(0, 30).map((o) => (o.textContent || '').trim()) : [];
        }, trigger.selector);
        return {
          ok: false, reason: 'option_not_in_native_select',
          trigger_selector: trigger.selector, candidates,
          took_ms: Date.now() - start,
        };
      }
      await page.select(trigger.selector, optionValue);
      return {
        ok: true, picked_text: value,
        trigger_selector: trigger.selector, verified: true,
        took_ms: Date.now() - start,
        auto_expanded: autoExpanded,
      };
    } catch (e) {
      return {
        ok: false, reason: `native_select_failed: ${(e as Error).message}`,
        trigger_selector: trigger.selector, took_ms: Date.now() - start,
      };
    }
  }

  // 3. Custom widget: scroll trigger into view and click it.
  try {
    await page.$eval(trigger.selector, (el: Element) =>
      (el as HTMLElement).scrollIntoView({ block: 'center' }),
    );
    await page.click(trigger.selector);
  } catch (e) {
    return {
      ok: false, reason: `trigger_click_failed: ${(e as Error).message}`,
      trigger_selector: trigger.selector, took_ms: Date.now() - start,
    };
  }

  // 4. Wait for listbox/menu to render.
  const optionSelectors = [
    '[role="listbox"]:not([aria-hidden="true"]) [role="option"]',
    '[role="menu"]:not([aria-hidden="true"]) [role="menuitem"]',
    'ul[role="listbox"] li[role="option"]',
    '[data-headlessui-state] [role="option"]',
    ...extraOptionSelectors,
  ];

  const checkOptionsPresent = () => page.evaluate((sels: string[]) => {
    for (const s of sels) {
      try {
        if (document.querySelectorAll(s).length > 0) return true;
      } catch { /* bad selector — continue */ }
    }
    return false;
  }, optionSelectors);

  let rendered = false;
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await checkOptionsPresent().catch(() => false)) { rendered = true; break; }
    await new Promise((r) => setTimeout(r, 100));
  }
  if (!rendered) {
    // Headless-UI / Reach UI sometimes need keyboard activation.
    try {
      await page.focus(trigger.selector);
      await page.keyboard.press('ArrowDown');
      await new Promise((r) => setTimeout(r, 300));
      rendered = await checkOptionsPresent().catch(() => false);
    } catch { /* nope */ }
  }
  if (!rendered) {
    return {
      ok: false, reason: 'options_did_not_render',
      trigger_selector: trigger.selector, took_ms: Date.now() - start,
    };
  }

  // 5. Pick option by exact-ci → startsWith → contains → fuzzy.
  const pick = await page.evaluate((sels: string[], val: string, fz: boolean) => {
    const norm = (s: string) => (s || '').replace(/\s+/g, ' ').trim();
    const lower = (s: string) => norm(s).toLowerCase();
    const tgt = lower(val);

    const seen: HTMLElement[] = [];
    for (const s of sels) {
      try {
        for (const el of Array.from(document.querySelectorAll<HTMLElement>(s))) {
          if (!seen.includes(el)) seen.push(el);
        }
      } catch { /* skip bad selector */ }
    }
    const items = seen
      .map((el) => ({ el, txt: norm(el.textContent || '') }))
      .filter((it) => it.txt.length > 0);
    if (items.length === 0) {
      return { ok: false as const, reason: 'no_options_collected', candidates: [] as string[] };
    }
    let match = items.find((it) => lower(it.txt) === tgt);
    if (!match) match = items.find((it) => lower(it.txt).startsWith(tgt));
    if (!match) match = items.find((it) => lower(it.txt).includes(tgt));
    if (!match && fz) {
      const lev = (a: string, b: string): number => {
        if (a === b) return 0;
        const m = a.length, n = b.length;
        if (m === 0) return n; if (n === 0) return m;
        const dp: number[][] = Array.from({ length: m + 1 }, () => new Array<number>(n + 1).fill(0));
        for (let i = 0; i <= m; i++) dp[i][0] = i;
        for (let j = 0; j <= n; j++) dp[0][j] = j;
        for (let i = 1; i <= m; i++) {
          for (let j = 1; j <= n; j++) {
            const c = a[i-1] === b[j-1] ? 0 : 1;
            dp[i][j] = Math.min(dp[i-1][j]+1, dp[i][j-1]+1, dp[i-1][j-1]+c);
          }
        }
        return dp[m][n];
      };
      let best: { it: { el: HTMLElement; txt: string }; score: number } | null = null;
      for (const it of items) {
        const a = lower(it.txt);
        if (!a) continue;
        const d = lev(a, tgt);
        const sim = 1 - d / Math.max(a.length, tgt.length);
        if (sim >= 0.7 && (!best || sim > best.score)) best = { it, score: sim };
      }
      if (best) match = best.it;
    }

    if (!match) {
      return {
        ok: false as const,
        reason: 'no_option_match',
        candidates: items.slice(0, 25).map((it) => it.txt),
      };
    }

    const optTag = `sb-opt-${Math.random().toString(36).slice(2, 10)}`;
    match.el.setAttribute('data-sb-opt', optTag);
    return {
      ok: true as const,
      option_selector: `[data-sb-opt="${optTag}"]`,
      picked_text: match.txt,
    };
  }, optionSelectors, value, fuzzy);

  if (!pick.ok) {
    return {
      ok: false, reason: pick.reason, candidates: pick.candidates,
      trigger_selector: trigger.selector, took_ms: Date.now() - start,
    };
  }

  // 6. Click the chosen option.
  try {
    await page.$eval(pick.option_selector, (el: Element) =>
      (el as HTMLElement).scrollIntoView({ block: 'center' }),
    );
    await page.click(pick.option_selector);
  } catch (e) {
    return {
      ok: false, reason: `option_click_failed: ${(e as Error).message}`,
      trigger_selector: trigger.selector, option_selector: pick.option_selector,
      took_ms: Date.now() - start,
    };
  }

  // 7. Settle, then verify trigger reflects the pick.
  await new Promise((r) => setTimeout(r, 250));
  const verify = await page.evaluate((sel: string, picked: string) => {
    const norm = (s: string) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const el = document.querySelector(sel) as HTMLElement | null;
    if (!el) return { contains_pick: false, current_text: '', closed: true };
    const cur = norm(el.textContent || '');
    return {
      closed: el.getAttribute('aria-expanded') !== 'true',
      contains_pick: cur.includes(norm(picked)),
      current_text: cur.slice(0, 200),
    };
  }, trigger.selector, pick.picked_text);

  return {
    ok: true,
    picked_text: pick.picked_text,
    trigger_selector: trigger.selector,
    option_selector: pick.option_selector,
    verified: verify.contains_pick,
    took_ms: Date.now() - start,
    auto_expanded: autoExpanded,
  };
}
