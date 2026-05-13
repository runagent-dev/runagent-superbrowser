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
  /** v6 H1: true when the source CSS selector matched 2+ elements
   *  and only the FIRST match's coords were returned. Click validation
   *  (A1) tightens to require exact-element match in this case to
   *  avoid clicking the wrong matching element. */
  ambiguous?: boolean;
  /** v6 H1: actual count of matching elements (>=1 when ambiguous=true). */
  matchCount?: number;
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
      const matches = document.querySelectorAll(sel);
      if (matches.length === 0) return null;
      const el = matches[0];
      const rect = el.getBoundingClientRect();
      return {
        x: Math.round(rect.left + rect.width / 2),
        y: Math.round(rect.top + rect.height / 2),
        ambiguous: matches.length > 1,
        matchCount: matches.length,
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
 * Diagnostic shape for an ambiguous trigger candidate, surfaced when
 * the picker can't confidently pick one trigger over another.
 */
export interface TriggerCandidate {
  selector: string;
  score: number;
  currentText: string;
  role: string;
  ariaPopup: string;
  tag: string;
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
  // Diagnostics (Phase D) — surfaced on failure so the LLM can pick a
  // different tool instead of looping.
  tried?: string[];        // open strategies attempted: 'click','mousedown',...
  dom_changed?: boolean;   // did anything new become visible after open?
  new_classes?: string[];  // top sample of newly-visible class names
  trigger_score?: number;  // how confident the trigger picker was (Phase C)
  // Hardening additions:
  ambiguous_trigger?: { candidates: TriggerCandidate[] };
  ambiguous_option?: { candidates: string[] };
  navigated_to?: { url: string; path: string; hash: string; titleChanged: boolean };
  verified_focus_target?: string;
  selected_index?: number;
}

/**
 * Frozen snapshot of page state used to detect navigation between two
 * points in time — strictly more sensitive than the historical URL+title
 * pair: also flags pushState SPA route changes (different pathname/hash
 * with same URL string), pushState-with-same-URL (history grew), and
 * full-content swaps where the URL stays identical but the visible
 * `<main>` body changed substantially.
 */
export interface NavFingerprint {
  url: string;
  pathname: string;
  hash: string;
  title: string;
  historyLength: number;
  contentHash: string;
}

/**
 * Capture a single navigation fingerprint for the page. One round trip.
 * Failure-tolerant: any throw returns an all-zero fingerprint that
 * compares unequal so we conservatively flag "navigation" rather than
 * miss it.
 */
export async function navFingerprint(page: Page): Promise<NavFingerprint> {
  try {
    return await page.evaluate(() => {
      const main = (document.querySelector('main')
        || document.body) as HTMLElement | null;
      const head = main
        ? (main.innerText || main.textContent || '').slice(0, 2048)
        : '';
      let h = 5381 >>> 0;
      for (let i = 0; i < head.length; i++) {
        h = (((h << 5) + h) ^ head.charCodeAt(i)) >>> 0;
      }
      return {
        url: location.href,
        pathname: location.pathname,
        hash: location.hash,
        title: document.title,
        historyLength: history.length,
        contentHash: h.toString(16),
      };
    });
  } catch {
    return {
      url: '', pathname: '', hash: '',
      title: '', historyLength: 0, contentHash: '',
    };
  }
}

/**
 * Decide whether two NavFingerprints represent a navigation event.
 * Conservative: any of pathname / hash / title (with no popup) /
 * historyLength + same URL / 1KB+ content delta counts as navigated.
 */
function navFingerprintChanged(
  pre: NavFingerprint,
  post: NavFingerprint,
  popupAppeared: boolean,
): { changed: boolean; kind: string } {
  if (!pre.url && !post.url) return { changed: false, kind: '' };
  if (pre.pathname !== post.pathname) return { changed: true, kind: 'pathname' };
  if (pre.hash !== post.hash) return { changed: true, kind: 'hash' };
  if (
    pre.title !== post.title
    && !popupAppeared
    && pre.title !== ''
  ) {
    return { changed: true, kind: 'title' };
  }
  // pushState with identical URL bumps historyLength.
  if (
    pre.url === post.url
    && post.historyLength > pre.historyLength
  ) {
    return { changed: true, kind: 'historyLength' };
  }
  // Full content swap with no URL change. Require both contentHashes
  // to be non-empty (zero hash means we failed to capture).
  if (
    pre.contentHash
    && post.contentHash
    && pre.contentHash !== post.contentHash
    && !popupAppeared
  ) {
    return { changed: true, kind: 'content' };
  }
  return { changed: false, kind: '' };
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
     * When provided, skip the label-based trigger picker entirely and
     * use this selector as the trigger. The element MUST already be
     * tagged (e.g. with a `data-sb-trigger` attribute) so the rest of
     * the function's `data-sb-trigger` lookups still work. Used by
     * `selectOptionByVisionBbox` to bypass DOM-text ambiguity.
     */
    triggerSelector?: string;
  },
): Promise<SelectOptionByLabelResult> {
  const start = Date.now();
  const label = opts.label;
  const value = opts.value;
  const fuzzy = opts.fuzzy !== false;
  // Bumped 4s→6s default. Slow networks / cold caches that fetch
  // option lists on first open routinely need >4s. Caller can still
  // override via opts.timeout for known-fast widgets.
  const timeout = Math.max(500, Math.min(20000, opts.timeout ?? 6000));
  const extraOptionSelectors = opts.extraOptionSelectors ?? [];

  // 1. Locate trigger by label. SCORED picker with affordance gate +
  //    tiered match priority + ambiguity detection.
  //
  //    Affordance gate: a candidate must show real popup affordance
  //    (native <select>, role=combobox/listbox/menu, aria-haspopup,
  //    aria-expanded, datalist input, or chevron-shaped button) — not
  //    just text-contains the label. Eliminates the "heading-shaped
  //    <a> titled Region" silent misfire where a navigation card
  //    shaped like a card stole the click.
  //
  //    Tiered priority: exactAria > exactText > exactPlaceholder >
  //    startsWithAria > startsWithText > containsAria > containsText
  //    (with word boundaries on the contains tier). "Region Settings"
  //    no longer outranks "Region" via plain substring contains.
  //
  //    Ambiguity: when top-1 vs top-2 gap is < 4 AND top-2 score >= 0,
  //    return ambiguous_trigger with candidate list. Brain narrows the
  //    label or passes vision_index instead of guessing.
  // Fast path: trigger already resolved by caller (e.g.
  // selectOptionByVisionBbox snapped to a bbox). Skip the label
  // picker; just read metadata.
  let preResolvedTrigger:
    | {
        kind: 'pick';
        trigger: {
          selector: string;
          isNativeSelect: boolean;
          currentText: string;
          role: string;
          ariaExpanded: string;
          score: number;
        };
      }
    | { kind: 'none' }
    | null = null;
  if (opts.triggerSelector) {
    const meta = await page.evaluate((sel: string) => {
      const el = document.querySelector(sel) as HTMLElement | null;
      if (!el) return null;
      return {
        isNativeSelect: el.tagName.toLowerCase() === 'select',
        currentText: ((el.textContent || '').replace(/\s+/g, ' ').trim()).slice(0, 200),
        role: el.getAttribute('role') || '',
        ariaExpanded: el.getAttribute('aria-expanded') || '',
      };
    }, opts.triggerSelector);
    if (meta) {
      preResolvedTrigger = {
        kind: 'pick',
        trigger: {
          selector: opts.triggerSelector,
          isNativeSelect: meta.isNativeSelect,
          currentText: meta.currentText,
          role: meta.role,
          ariaExpanded: meta.ariaExpanded,
          score: 100,  // synthetic — caller asserted this is the right element
        },
      };
    } else {
      preResolvedTrigger = { kind: 'none' };
    }
  }

  const triggerResult = preResolvedTrigger ?? await page.evaluate((lbl: string) => {
    const norm = (s: string) => (s || '').replace(/\s+/g, ' ').trim();
    const lower = (s: string) => norm(s).toLowerCase();
    const target = lower(lbl);
    if (!target) return { kind: 'none' as const };

    const isVisuallyHeading = (el: HTMLElement): boolean => {
      const r = el.getBoundingClientRect();
      if (r.width > window.innerWidth * 0.7 && r.height < 40) return true;
      const tag = el.tagName.toLowerCase();
      if (tag === 'h1' || tag === 'h2' || tag === 'h3') return true;
      return false;
    };

    const isVisible = (el: HTMLElement): boolean => {
      if (el.tagName === 'SELECT') return true;
      if (el.offsetParent === null) return false;
      const cs = window.getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none') return false;
      return true;
    };

    /**
     * Affordance gate — is this candidate plausibly a dropdown trigger?
     * Returns the affordance score component (>0) or 0 if it fails the
     * gate. Candidates returning 0 are skipped from scoring.
     */
    const affordance = (el: HTMLElement): number => {
      const role = (el.getAttribute('role') || '').toLowerCase();
      const ariaPopup = (el.getAttribute('aria-haspopup') || '').toLowerCase();
      const tag = el.tagName.toLowerCase();
      let score = 0;
      let gateOpen = false;
      if (tag === 'select') { score += 14; gateOpen = true; }
      if (
        ariaPopup === 'listbox' || ariaPopup === 'menu'
        || ariaPopup === 'true' || ariaPopup === 'dialog'
        || ariaPopup === 'tree' || ariaPopup === 'grid'
      ) { score += 14; gateOpen = true; }
      if (role === 'combobox') { score += 12; gateOpen = true; }
      if (role === 'listbox') { score += 10; gateOpen = true; }
      if (role === 'menu') { score += 8; gateOpen = true; }
      if (el.hasAttribute('aria-expanded')) { score += 4; gateOpen = true; }
      if (
        tag === 'input' && el.hasAttribute('list')
        && document.getElementById(el.getAttribute('list') || '')
      ) { score += 8; gateOpen = true; }
      // Chevron descendant on a button-like host.
      const isChevronHost = (
        (tag === 'button' || (tag === 'div' && role === 'button'))
        && el.querySelector(
          '[class*="chevron"], [class*="caret"], [class*="arrow-down"], '
          + 'svg[class*="chevron"], svg[class*="caret"]',
        ) != null
      );
      if (isChevronHost) { score += 6; gateOpen = true; }
      // Wrapping picker/dropdown class — only when this el is the sole
      // interactive child of that wrapper.
      const parent = el.parentElement;
      const pcls = (parent && typeof parent.className === 'string'
        ? parent.className : '').toLowerCase();
      if (
        /\b(picker|chooser|select|dropdown|combobox)\b/.test(pcls)
        && parent
      ) {
        const interactives = parent.querySelectorAll(
          'button, select, input, a[href], [role="button"], '
          + '[role="combobox"], [role="listbox"], [tabindex]:not([tabindex="-1"])',
        );
        if (interactives.length === 1 && interactives[0] === el) {
          score += 4;
          gateOpen = true;
        }
      }
      // Mid signals — class hint on element itself.
      const cls = ((typeof el.className === 'string' ? el.className : '')
        || '').toLowerCase();
      if (/\b(select|dropdown|combobox|picker|chooser)\b/.test(cls)) {
        score += 3;
        gateOpen = true;
      }
      return gateOpen ? score : 0;
    };

    /**
     * Negative signals — rule out elements that look like dropdown
     * matches but are clearly something else (real anchor links,
     * plain buttons with no popup hint, headings).
     */
    const negativeSignals = (el: HTMLElement): number => {
      let penalty = 0;
      const tag = el.tagName.toLowerCase();
      const ariaPopup = (el.getAttribute('aria-haspopup') || '').toLowerCase();
      if (tag === 'a' && el.hasAttribute('href') && !ariaPopup) {
        const href = el.getAttribute('href') || '';
        if (!href.startsWith('#')) penalty += 10;
      }
      if (
        tag === 'button' && !ariaPopup
        && !el.hasAttribute('aria-expanded')
      ) {
        const cls = ((typeof el.className === 'string' ? el.className : '')
          || '').toLowerCase();
        if (!/\b(picker|dropdown|combobox|select)\b/.test(cls)) {
          penalty += 4;
        }
      }
      if (isVisuallyHeading(el)) penalty += 6;
      return penalty;
    };

    /**
     * Word-boundary contains — Unicode-aware. "Region" inside
     * "Region Settings" matches; "tion" inside "Selection" doesn't.
     */
    const wordBoundary = (s: string, frag: string): boolean => {
      if (!frag) return false;
      const i = s.indexOf(frag);
      if (i < 0) return false;
      const before = s[i - 1];
      const after = s[i + frag.length];
      const isWord = (c: string | undefined): boolean =>
        c != null && /[\p{L}\p{N}_]/u.test(c);
      return !isWord(before) && !isWord(after);
    };

    /**
     * Tier-based match score. Returns the tier bonus (24/20/18/14/
     * 10/6/4/0) for the first tier the candidate hits, or 0 if no
     * label match at all (caller uses 0 to filter the candidate out).
     * Higher tier = more confident match.
     */
    const tierMatch = (
      el: HTMLElement,
    ): { tier: number; bonus: number } => {
      const aria = lower(el.getAttribute('aria-label') || '');
      const txt = lower((el.innerText || el.textContent || '')).slice(0, 200);
      const placeholder = lower(el.getAttribute('placeholder') || '');
      // Tier 0: exact aria.
      if (aria === target) return { tier: 0, bonus: 24 };
      // Tier 1: exact text.
      if (txt === target) return { tier: 1, bonus: 20 };
      // Tier 2: exact placeholder.
      if (placeholder === target) return { tier: 2, bonus: 18 };
      // Tier 3: startsWith aria (with word boundary at end of frag).
      const startsWithBoundary = (s: string): boolean => {
        if (!s.startsWith(target)) return false;
        const after = s[target.length];
        return after == null || !/[\p{L}\p{N}_]/u.test(after);
      };
      if (startsWithBoundary(aria)) return { tier: 3, bonus: 14 };
      if (startsWithBoundary(txt)) return { tier: 4, bonus: 10 };
      // Tier 5: contains aria with word boundary.
      if (wordBoundary(aria, target)) return { tier: 5, bonus: 6 };
      if (wordBoundary(txt, target)) return { tier: 6, bonus: 4 };
      // Tier 7: contains placeholder with word boundary.
      if (wordBoundary(placeholder, target)) return { tier: 7, bonus: 3 };
      return { tier: -1, bonus: 0 };
    };

    const sizePref = (el: HTMLElement): number => {
      const r = el.getBoundingClientRect();
      const area = Math.max(1, r.width * r.height);
      return Math.max(0, 3 - Math.log10(area));
    };

    const tagTrigger = (
      el: Element, score: number,
    ): { kind: 'pick'; trigger: { selector: string; isNativeSelect: boolean; currentText: string; role: string; ariaExpanded: string; score: number } } => {
      const id = `sb-trigger-${Math.random().toString(36).slice(2, 10)}`;
      el.setAttribute('data-sb-trigger', id);
      const isSelect = el.tagName.toLowerCase() === 'select';
      return {
        kind: 'pick',
        trigger: {
          selector: `[data-sb-trigger="${id}"]`,
          isNativeSelect: isSelect,
          currentText: norm((el as HTMLElement).textContent || '').slice(0, 200),
          role: el.getAttribute('role') || '',
          ariaExpanded: el.getAttribute('aria-expanded') || '',
          score,
        },
      };
    };

    const tagAmbiguous = (
      cands: Array<{ el: HTMLElement; score: number }>,
    ): { kind: 'ambiguous'; candidates: Array<{ selector: string; score: number; currentText: string; role: string; ariaPopup: string; tag: string }> } => {
      return {
        kind: 'ambiguous',
        candidates: cands.slice(0, 3).map((c) => {
          const id = `sb-trigger-cand-${Math.random().toString(36).slice(2, 10)}`;
          c.el.setAttribute('data-sb-trigger-cand', id);
          return {
            selector: `[data-sb-trigger-cand="${id}"]`,
            score: c.score,
            currentText: norm(c.el.textContent || '').slice(0, 120),
            role: c.el.getAttribute('role') || '',
            ariaPopup: c.el.getAttribute('aria-haspopup') || '',
            tag: c.el.tagName.toLowerCase(),
          };
        }),
      };
    };

    // A. <label for="x">Lbl</label> + #x — explicit association.
    //    Prefer EXACT label-text match; substring fallback is lower
    //    score so a more specific contender from C/D can win.
    let labelForBest: { el: HTMLElement; score: number } | null = null;
    for (const lab of Array.from(
      document.querySelectorAll<HTMLLabelElement>('label[for]'),
    )) {
      const labText = lower(lab.textContent || '');
      const ctrl = document.getElementById(lab.htmlFor);
      if (!ctrl || !isVisible(ctrl)) continue;
      let bonus = 0;
      if (labText === target) bonus = 14;
      else if (wordBoundary(labText, target)) bonus = 10;
      else if (labText.includes(target)) bonus = 6;
      if (bonus === 0) continue;
      const aff = affordance(ctrl);
      const total = (aff > 0 ? aff : 0) + bonus - negativeSignals(ctrl);
      if (!labelForBest || total > labelForBest.score) {
        labelForBest = { el: ctrl, score: total };
      }
    }

    // B. aria-labelledby — explicit association.
    let ariaLbBest: { el: HTMLElement; score: number } | null = null;
    for (const el of Array.from(
      document.querySelectorAll<HTMLElement>('[aria-labelledby]'),
    )) {
      if (!isVisible(el)) continue;
      const ids = (el.getAttribute('aria-labelledby') || '')
        .split(/\s+/).filter(Boolean);
      const labText = ids
        .map((id) => lower(document.getElementById(id)?.textContent || ''))
        .join(' ');
      let bonus = 0;
      if (labText === target) bonus = 16;
      else if (wordBoundary(labText, target)) bonus = 12;
      else if (labText.includes(target)) bonus = 8;
      if (bonus === 0) continue;
      const aff = affordance(el);
      const total = (aff > 0 ? aff : 0) + bonus - negativeSignals(el);
      if (!ariaLbBest || total > ariaLbBest.score) {
        ariaLbBest = { el, score: total };
      }
    }

    // C+D. Score-based pick across affordance-gated interactive
    //      candidates with tiered label matching. Collect ALL passing
    //      candidates so we can detect ambiguity at the top.
    const interactiveSelector = (
      '[role="combobox"], [role="listbox"], [role="button"], '
      + '[aria-haspopup], [aria-expanded], button, select, input, '
      + '[role="textbox"], [role="menu"], [tabindex]:not([tabindex="-1"])'
    );
    const interactive = Array.from(
      document.querySelectorAll<HTMLElement>(interactiveSelector),
    );
    const candidates: Array<{ el: HTMLElement; score: number; tier: number }> = [];
    for (const el of interactive) {
      if (!isVisible(el)) continue;
      const aff = affordance(el);
      if (aff <= 0) continue;  // affordance gate
      const tm = tierMatch(el);
      if (tm.tier < 0) continue;
      const total = aff + tm.bonus + sizePref(el) - negativeSignals(el);
      candidates.push({ el, score: total, tier: tm.tier });
    }
    candidates.sort((a, b) => b.score - a.score);

    // E. Wrapping <label> fallback — <label>Lbl <input/></label>.
    let wrapLabelBest: { el: HTMLElement; score: number } | null = null;
    for (const lab of Array.from(
      document.querySelectorAll<HTMLLabelElement>('label'),
    )) {
      const ctrl = lab.querySelector<HTMLElement>(
        'select, input, [role="combobox"], [role="listbox"]',
      );
      if (!ctrl || !isVisible(ctrl)) continue;
      const labText = lower(lab.textContent || '');
      let bonus = 0;
      if (labText === target) bonus = 13;
      else if (wordBoundary(labText, target)) bonus = 9;
      else if (labText.includes(target)) bonus = 5;
      if (bonus === 0) continue;
      const aff = affordance(ctrl);
      const total = (aff > 0 ? aff : 0) + bonus - negativeSignals(ctrl);
      if (!wrapLabelBest || total > wrapLabelBest.score) {
        wrapLabelBest = { el: ctrl, score: total };
      }
    }

    // Pool every candidate path (A, B, C/D, E). Dedupe by element.
    const pooled = new Map<HTMLElement, number>();
    const consider = (
      x: { el: HTMLElement; score: number } | null,
    ): void => {
      if (!x) return;
      const cur = pooled.get(x.el);
      if (cur == null || x.score > cur) pooled.set(x.el, x.score);
    };
    consider(labelForBest);
    consider(ariaLbBest);
    for (const c of candidates) consider({ el: c.el, score: c.score });
    consider(wrapLabelBest);

    if (pooled.size === 0) {
      return { kind: 'none' as const };
    }
    const ranked = Array.from(pooled.entries())
      .map(([el, score]) => ({ el, score }))
      .sort((a, b) => b.score - a.score);

    const top = ranked[0];
    const second = ranked[1];

    // Ambiguity: top-1 vs top-2 gap < 4 AND top-2 score >= 0.
    if (
      second
      && top.score - second.score < 4
      && second.score >= 0
    ) {
      return tagAmbiguous(ranked);
    }

    // Section F equivalent: only return when the best score is >= 0.
    if (top.score < 0) {
      return { kind: 'none' as const };
    }
    return tagTrigger(top.el, top.score);
  }, label);

  if (!triggerResult || triggerResult.kind === 'none') {
    return { ok: false, reason: 'trigger_not_found', took_ms: Date.now() - start };
  }

  if (triggerResult.kind === 'ambiguous') {
    return {
      ok: false,
      reason: 'ambiguous_trigger',
      ambiguous_trigger: { candidates: triggerResult.candidates },
      candidates: triggerResult.candidates.map(
        (c) => `${c.tag}<${c.role || '?'}>['${c.currentText}'] score=${c.score.toFixed(1)}`,
      ),
      took_ms: Date.now() - start,
    };
  }

  const trigger = triggerResult.trigger;

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
      // Confirm the option is now actually selected. page.select can
      // resolve before the page acknowledges the change on slow react/
      // vue forms; explicitly reading selectedIndex / option.selected
      // closes that gap and avoids reporting false success.
      const post = await page.evaluate(
        (sel: string, val: string) => {
          const sel_el = document.querySelector(sel) as HTMLSelectElement | null;
          if (!sel_el) return null;
          const idx = sel_el.selectedIndex;
          const opt = (idx >= 0 ? sel_el.options[idx] : null) as HTMLOptionElement | null;
          return {
            selectedIndex: idx,
            optionValue: opt ? opt.value : '',
            optionText: opt ? (opt.textContent || '').trim() : '',
            matchesRequested: opt != null && opt.value === val,
          };
        },
        trigger.selector,
        optionValue,
      );
      return {
        ok: true,
        picked_text: post?.optionText || value,
        trigger_selector: trigger.selector,
        verified: !!post?.matchesRequested,
        selected_index:
          post && typeof post.selectedIndex === 'number'
            ? post.selectedIndex
            : undefined,
        took_ms: Date.now() - start,
      };
    } catch (e) {
      return {
        ok: false, reason: `native_select_failed: ${(e as Error).message}`,
        trigger_selector: trigger.selector, took_ms: Date.now() - start,
      };
    }
  }

  // 3. Custom widget. Capture pre-click URL/title so we can distinguish
  //    "trigger opens a popup" from "trigger navigates a new page"
  //    (Best Buy trade-in brand picker is a grid of clickable cards
  //    labelled 'Brand' — clicking navigates, no listbox ever appears).
  // Capture a navigation fingerprint BEFORE the first open-strategy.
  // We re-fingerprint after each strategy and after the final option
  // click, comparing both via navFingerprintChanged() so SPA pushState
  // routes (different pathname/hash with same URL string) and
  // pushState-with-content-swap also count as navigation.
  const preNavFp: NavFingerprint = await navFingerprint(page);
  try {
    await page.$eval(trigger.selector, (el: Element) =>
      (el as HTMLElement).scrollIntoView({ block: 'center' }),
    );
  } catch { /* best-effort */ }

  // Pre-snapshot (Phase A): tag every currently-visible candidate-shaped
  // element. After the open-attempt we'll diff this set to find newly-
  // visible options, even when the widget renders option markup without
  // ARIA roles. Limited to the candidate-shaped tags so we don't paint
  // tens of thousands of attributes onto the DOM.
  await page.evaluate(() => {
    document.querySelectorAll('[data-sb-pre-visible]').forEach(
      (el) => el.removeAttribute('data-sb-pre-visible'),
    );
    document.querySelectorAll('[data-sb-opt-candidate]').forEach(
      (el) => el.removeAttribute('data-sb-opt-candidate'),
    );
    const isVisible = (el: HTMLElement): boolean => {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return false;
      const cs = window.getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none') return false;
      return true;
    };
    const sels = (
      'li, button, a, [role="option"], [role="menuitem"], '
      + '[data-value], [data-option-value], [data-test-id], '
      + '[data-testid], [data-state]'
    );
    for (const el of Array.from(document.querySelectorAll<HTMLElement>(sels))) {
      if (isVisible(el)) el.setAttribute('data-sb-pre-visible', '1');
    }
  });

  // 4. Multi-strategy popup-open (Phase B). Try increasingly aggressive
  //    open-actions until something popup-shaped appears OR options
  //    materialize. Stops on the first strategy that succeeds, so the
  //    fast happy path still costs a single page.click. Records the
  //    sequence in `tried` for diagnostics.
  const standardOptionSelectors = [
    '[role="listbox"]:not([aria-hidden="true"]) [role="option"]',
    '[role="menu"]:not([aria-hidden="true"]) [role="menuitem"]',
    '[role="combobox"][aria-expanded="true"] + * [role="option"]',
    'ul[role="listbox"] li[role="option"]',
    '[data-headlessui-state="open"] [role="option"]',
    '[data-state="open"] [role="option"]',
    '[class*="MuiMenu-list"] [role="menuitem"]',
    '[class*="MuiAutocomplete"] [role="option"]',
    '[id$="-listbox"] [role="option"]',
    'div[role="presentation"] [role="option"]',
    'select option:not([disabled])',
    ...extraOptionSelectors,
  ];

  // Tag both standard-role matches AND DOM-diff option-shaped elements
  // with `data-sb-opt-candidate`. Returns the count tagged so the
  // wait loop knows when it can stop. The pickAttempt and listbox-
  // scroll helpers below all read this single tag.
  //
  // Popup-scoping (added 2026-05-10): when the open dropdown lives in
  // a recognisable container — ARIA listbox/menu/dialog, library
  // state attrs (Headless UI / Radix / data-state="open"), or a
  // newly-mounted container near the trigger — restrict candidate
  // tagging to descendants of that container. Without this scope the
  // DOM-diff fallback below was vacuuming up homepage buttons whose
  // pre-visible tag had been lost across a React re-render (SpotHero
  // homepage time picker returned 'Use current location' / 'View All
  // Stadiums' as candidates). Falls back to whole-document search
  // when no root can be identified — preserves the existing happy
  // path for ARIA-clean dropdowns.
  const collectAndTagCandidates = (): Promise<number> => page.evaluate(
    (args: { sels: string[]; triggerSel: string }) => {
      const { sels, triggerSel } = args;
      // Clear prior tags from the same call (multi-strategy may re-run).
      document.querySelectorAll('[data-sb-opt-candidate]').forEach(
        (el) => el.removeAttribute('data-sb-opt-candidate'),
      );
      const isVisible = (el: HTMLElement): boolean => {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        const cs = window.getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none') return false;
        return true;
      };
      const triggerEl = triggerSel ? document.querySelector(triggerSel) : null;

      // ----- popup-root detection -----
      // Cascade A → B → C, first hit wins.
      let popupRoot: Element | null = null;

      // A1. aria-controls on the trigger points at the popup id.
      if (triggerEl) {
        const controlsId = triggerEl.getAttribute('aria-controls');
        if (controlsId) {
          const ctrl = document.getElementById(controlsId);
          if (ctrl && isVisible(ctrl as HTMLElement)) popupRoot = ctrl;
        }
      }

      // A2. ARIA role-based popups visible somewhere on the page.
      if (!popupRoot) {
        const ariaPopupSelectors = [
          '[role="listbox"]:not([aria-hidden="true"])',
          '[role="menu"]:not([aria-hidden="true"])',
          '[role="dialog"]:not([aria-hidden="true"])',
          '[role="grid"]:not([aria-hidden="true"])',
          '[role="tree"]:not([aria-hidden="true"])',
        ];
        for (const sel of ariaPopupSelectors) {
          let found: Element | null = null;
          try {
            for (const c of Array.from(document.querySelectorAll(sel))) {
              if (!isVisible(c as HTMLElement)) continue;
              if (c === triggerEl) continue;
              if (triggerEl && c.contains(triggerEl)) continue;
              found = c; break;
            }
          } catch { /* selector unsupported — skip */ }
          if (found) { popupRoot = found; break; }
        }
      }

      // B. Library state attrs — Headless UI / Radix / generic `data-state`.
      if (!popupRoot) {
        const libSelectors = [
          '[data-headlessui-state~="open"]',
          '[data-state="open"]',
          '[data-radix-popper-content-wrapper]',
          '[data-floating-ui-portal]',
        ];
        for (const sel of libSelectors) {
          let found: Element | null = null;
          try {
            for (const c of Array.from(document.querySelectorAll(sel))) {
              if (!isVisible(c as HTMLElement)) continue;
              if (c === triggerEl) continue;
              if (triggerEl && c.contains(triggerEl)) continue;
              found = c; break;
            }
          } catch { /* skip */ }
          if (found) { popupRoot = found; break; }
        }
      }

      // C. Geometric/diff fallback — find the newly-mounted container
      //    closest to the trigger that contains option-shaped children.
      //    Required for SpotHero-style custom pickers that emit no
      //    ARIA roles and no library state attrs.
      if (!popupRoot && triggerEl) {
        const triggerRect = (triggerEl as HTMLElement).getBoundingClientRect();
        const triggerCx = triggerRect.left + triggerRect.width / 2;
        const triggerCy = triggerRect.top + triggerRect.height / 2;
        const MAX_DISTANCE = 800;
        const containerCandidates = Array.from(document.querySelectorAll<HTMLElement>(
          'div, section, ul, [class*="popper"], [class*="menu"], '
          + '[class*="dropdown"], [class*="picker"], [class*="modal"], '
          + '[class*="popover"], [class*="overlay"]',
        ));
        let bestRoot: Element | null = null;
        let bestArea = Infinity;
        for (const c of containerCandidates) {
          if (!isVisible(c)) continue;
          if (c.hasAttribute('data-sb-pre-visible')) continue;
          // Skip elements that contain the trigger — they're the page
          // layout itself, not a freshly opened popup.
          if (c.contains(triggerEl)) continue;
          const r = c.getBoundingClientRect();
          if (r.width < 100 || r.height < 60) continue;
          const ccx = r.left + r.width / 2;
          const ccy = r.top + r.height / 2;
          const dist = Math.hypot(ccx - triggerCx, ccy - triggerCy);
          if (dist > MAX_DISTANCE) continue;
          // Must contain newly-mounted interactive children — that's
          // what makes it a popup vs. a long-standing layout box.
          const newChildren = c.querySelectorAll(
            'button:not([data-sb-pre-visible]), '
            + '[role="option"]:not([data-sb-pre-visible]), '
            + '[role="menuitem"]:not([data-sb-pre-visible]), '
            + 'li:not([data-sb-pre-visible]), '
            + 'a:not([data-sb-pre-visible])',
          );
          if (newChildren.length < 1) continue;
          // Prefer the SMALLEST qualifying container — tighter scope.
          const area = r.width * r.height;
          if (area < bestArea) { bestArea = area; bestRoot = c; }
        }
        if (bestRoot) popupRoot = bestRoot;
      }

      const tagSet = (el: Element): void => {
        if (el === triggerEl) return;
        if (triggerEl && triggerEl.contains(el)) return;
        // Hard-reject anything outside the popup root when we found
        // one. This is the actual fix — homepage buttons get filtered
        // out even if their pre-visible tag was lost across a re-render.
        if (popupRoot && !popupRoot.contains(el)) return;
        (el as HTMLElement).setAttribute('data-sb-opt-candidate', '1');
      };

      const searchRoot: ParentNode = popupRoot ?? document;

      // 1. Standard role-based selectors (preferred when present).
      for (const s of sels) {
        try {
          for (const el of Array.from(searchRoot.querySelectorAll(s))) {
            if (isVisible(el as HTMLElement)) tagSet(el);
          }
        } catch { /* skip bad selector */ }
      }

      // 2. DOM-diff fallback (Phase A): newly-visible option-shaped
      //    elements that the role-based selectors missed. Required for
      //    widgets that render options as <li class="dropdown-item">
      //    Dell</li> with no role attributes — the whole reason the
      //    user keeps seeing options_did_not_render even when Dell is
      //    clearly on screen.
      const isOptionShaped = (el: HTMLElement): boolean => {
        const role = (el.getAttribute('role') || '').toLowerCase();
        if (role === 'option' || role === 'menuitem' || role === 'treeitem') return true;
        if (el.hasAttribute('data-value') || el.hasAttribute('data-option-value')) return true;
        const cls = (typeof el.className === 'string' ? el.className : '').toLowerCase();
        if (/(^|[\s_-])(option|item|choice|menuitem|listitem|dropdown-item|select-item|combobox-item|menu-item)([\s_-]|$)/.test(cls)) {
          return true;
        }
        // cursor:pointer leaf-ish element with text — covers custom
        // widgets that style things as clickable but emit no role.
        const cs = window.getComputedStyle(el);
        if (cs.cursor === 'pointer') {
          const tag = el.tagName.toLowerCase();
          if (tag === 'li' || tag === 'a' || tag === 'button') return true;
          if (tag === 'div' || tag === 'span') {
            // Avoid grabbing huge container divs — require a child-text
            // ratio that suggests "this element IS the option text".
            const text = (el.innerText || el.textContent || '').trim();
            if (text.length === 0 || text.length > 120) return false;
            const childCount = el.children.length;
            if (childCount > 5) return false;  // too container-y
            return true;
          }
        }
        return false;
      };

      // Candidate pool — broaden beyond the pre-snapshot set: anything
      // that became visible AFTER tagging may be a new option even if
      // it's a freshly-mounted div outside our pre-tagged scope.
      // Scope to popupRoot when known to keep candidates tight.
      const pool = (popupRoot ?? document).querySelectorAll<HTMLElement>(
        'li, button, a, [role], [data-value], [data-option-value], div, span',
      );
      let added = 0;
      const cap = 500;  // soft cap on candidate count
      for (const el of Array.from(pool)) {
        if (added >= cap) break;
        if (el.hasAttribute('data-sb-opt-candidate')) continue;
        // pre-visible filter: redundant inside a confirmed popupRoot
        // (anything inside a freshly-opened popup is by construction
        // newly visible) but harmless and protects us when the popup
        // root mounts a previously-hidden subtree.
        if (el.hasAttribute('data-sb-pre-visible')) continue;
        if (!isVisible(el)) continue;
        if (!isOptionShaped(el)) continue;
        if (el === triggerEl) continue;
        if (triggerEl && triggerEl.contains(el)) continue;
        tagSet(el);
        added += 1;
      }
      return document.querySelectorAll('[data-sb-opt-candidate]').length;
    },
    { sels: standardOptionSelectors, triggerSel: trigger.selector },
  ) as Promise<number>;

  const popupShapedSelector = (
    '[role="listbox"]:not([aria-hidden="true"]),'
    + '[role="menu"]:not([aria-hidden="true"]),'
    + '[role="dialog"]:not([aria-hidden="true"]),'
    + '[aria-expanded="true"],'
    + '[data-headlessui-state="open"],'
    + '[data-state="open"]'
  );
  const checkPopupDetected = (): Promise<boolean> => page.evaluate(
    (sel: string) => document.querySelectorAll(sel).length > 0,
    popupShapedSelector,
  ) as Promise<boolean>;

  type OpenStrategy =
    | 'click'
    | 'mousedown'
    | 'pointer'
    | 'space'
    | 'arrowdown';
  const strategies: OpenStrategy[] = ['click', 'mousedown', 'pointer', 'space', 'arrowdown'];
  const tried: OpenStrategy[] = [];

  const runStrategy = async (strategy: OpenStrategy): Promise<void> => {
    switch (strategy) {
      case 'click':
        await page.click(trigger.selector);
        return;
      case 'mousedown':
        await page.evaluate((sel: string) => {
          const el = document.querySelector(sel) as HTMLElement | null;
          if (!el) return;
          const opts: MouseEventInit = { bubbles: true, cancelable: true, button: 0, view: window };
          el.dispatchEvent(new MouseEvent('mousedown', opts));
          el.dispatchEvent(new MouseEvent('mouseup', opts));
          el.dispatchEvent(new MouseEvent('click', opts));
        }, trigger.selector);
        return;
      case 'pointer':
        await page.evaluate((sel: string) => {
          const el = document.querySelector(sel) as HTMLElement | null;
          if (!el) return;
          // PointerEvent isn't on every browser/headless build; guard.
          const PE = (window as unknown as { PointerEvent?: typeof PointerEvent }).PointerEvent;
          if (!PE) return;
          const opts = { bubbles: true, cancelable: true, pointerType: 'mouse', button: 0 } as PointerEventInit;
          el.dispatchEvent(new PE('pointerdown', opts));
          el.dispatchEvent(new PE('pointerup', opts));
        }, trigger.selector);
        return;
      case 'space':
        await page.focus(trigger.selector);
        await page.keyboard.press('Space');
        return;
      case 'arrowdown':
        await page.focus(trigger.selector);
        await page.keyboard.press('ArrowDown');
        return;
    }
  };

  let rendered = false;
  let popupSeen = false;
  let navFp: NavFingerprint = preNavFp;
  let navKind = '';
  // Per-strategy budget: split the total timeout across strategies, but
  // give each at least 600ms. Total can slightly exceed timeout when
  // every strategy is tried; that's acceptable.
  const perStrategyBudget = Math.max(600, Math.floor(timeout / strategies.length));

  // Idempotency check: if a popup is ALREADY open (from a previous
  // browser_select_option call that returned ambiguous_option, or an
  // earlier manual click), DO NOT run any open strategy. Clicking the
  // trigger again would toggle the popup CLOSED and we'd lose the
  // option list. Just proceed to option-matching against the popup
  // that's already showing.
  const alreadyOpen = await page.evaluate((sel: string) => {
    try {
      const trig = document.querySelector(sel);
      if (trig && trig.getAttribute('aria-expanded') === 'true') return true;
    } catch { /* ignore selector errors */ }
    // Fallback: any popup-shaped element visible on the page.
    return document.querySelectorAll(
      '[role="listbox"]:not([aria-hidden="true"]),'
      + '[role="menu"]:not([aria-hidden="true"]),'
      + '[role="dialog"]:not([aria-hidden="true"]),'
      + '[data-headlessui-state="open"],'
      + '[data-state="open"]',
    ).length > 0;
  }, trigger.selector).catch(() => false) as boolean;
  if (alreadyOpen) {
    popupSeen = true;
    const tagged = await collectAndTagCandidates().catch(() => 0);
    if (tagged > 0) rendered = true;
    // If tagging found something we're done with the open phase.
    // If not, fall through to the strategy loop — maybe the popup
    // detected isn't this trigger's, in which case opening this one
    // is the right action.
  }

  for (const strategy of strategies) {
    if (rendered) break;
    tried.push(strategy);
    try {
      await runStrategy(strategy);
    } catch { /* try next */ continue; }

    const subDeadline = Date.now() + perStrategyBudget;
    while (Date.now() < subDeadline) {
      // Tag-and-count first so popupAppeared informs nav check.
      const popupNow = await checkPopupDetected().catch(() => false);
      if (popupNow) popupSeen = true;
      const tagged = await collectAndTagCandidates().catch(() => 0);
      if (tagged > 0) { rendered = true; break; }
      // Nav fingerprint check — pathname/hash/title/historyLength/
      // contentHash deltas catch SPA pushState that page.url() alone
      // misses. Skipped when a popup just appeared (some libraries
      // pushState when the dropdown opens; we don't want to mistake
      // that for a real route change).
      const post = await navFingerprint(page);
      const change = navFingerprintChanged(preNavFp, post, popupSeen);
      if (change.changed) {
        navFp = post;
        navKind = change.kind;
        break;
      }
      await new Promise((r) => setTimeout(r, 120));
    }
    if (rendered || navKind) break;
  }

  if (navKind) {
    return {
      ok: false,
      reason: 'trigger_navigated',
      trigger_selector: trigger.selector,
      candidates: [
        `navigated_via=${navKind}`,
        `from=${preNavFp.url}`,
        `to=${navFp.url}`,
      ],
      tried,
      trigger_score: trigger.score,
      navigated_to: {
        url: navFp.url,
        path: navFp.pathname,
        hash: navFp.hash,
        titleChanged: preNavFp.title !== navFp.title,
      },
      took_ms: Date.now() - start,
    };
  }

  if (!rendered) {
    // Last-chance check after the open loop.
    const finalFp = await navFingerprint(page);
    const finalChange = navFingerprintChanged(preNavFp, finalFp, popupSeen);
    if (finalChange.changed) {
      return {
        ok: false,
        reason: 'trigger_navigated',
        trigger_selector: trigger.selector,
        candidates: [
          `navigated_via=${finalChange.kind}`,
          `from=${preNavFp.url}`,
          `to=${finalFp.url}`,
        ],
        tried,
        trigger_score: trigger.score,
        navigated_to: {
          url: finalFp.url,
          path: finalFp.pathname,
          hash: finalFp.hash,
          titleChanged: preNavFp.title !== finalFp.title,
        },
        took_ms: Date.now() - start,
      };
    }
    // Phase D diagnostics: collect a sample of newly-visible classes
    // so the LLM sees what the page DID become after click — usually
    // a strong hint that this label isn't a dropdown trigger at all.
    const diag = await page.evaluate(() => {
      const isVisible = (el: HTMLElement): boolean => {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        const cs = window.getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none') return false;
        return true;
      };
      const newClasses = new Map<string, number>();
      const all = document.querySelectorAll<HTMLElement>(
        'li, button, a, div, span, [role], [data-value]',
      );
      let newCount = 0;
      for (const el of Array.from(all)) {
        if (el.hasAttribute('data-sb-pre-visible')) continue;
        if (!isVisible(el)) continue;
        newCount += 1;
        const cls = (typeof el.className === 'string' ? el.className : '').trim();
        if (!cls) continue;
        const first = cls.split(/\s+/)[0];
        if (!first) continue;
        newClasses.set(first, (newClasses.get(first) ?? 0) + 1);
      }
      const top = Array.from(newClasses.entries())
        .sort((a, b) => b[1] - a[1])
        .slice(0, 5)
        .map(([cls, n]) => `${cls}(${n})`);
      return { domChanged: newCount > 0, newCount, top };
    }) as { domChanged: boolean; newCount: number; top: string[] };

    return {
      ok: false,
      reason: popupSeen ? 'options_did_not_render' : 'no_popup_detected',
      trigger_selector: trigger.selector,
      tried,
      trigger_score: trigger.score,
      dom_changed: diag.domChanged,
      new_classes: diag.top,
      took_ms: Date.now() - start,
    };
  }

  // 5. Pick option via tiered match cascade with word-boundary contains
  //    + raised fuzzy floor. The candidate set is whatever was tagged
  //    with `data-sb-opt-candidate` by the open-and-tag step above.
  //
  //    Tier order:
  //      1. exact-ci (lower(it.txt) === tgt) — preferred regardless of length
  //      2. startsWith-word — startsWith AND char after frag is non-word
  //      3. contains-word — wordBoundary contains
  //      4. fuzzy — Levenshtein similarity >= 0.85, target length >= 4
  //
  //    Reject < 3-char targets at tiers 2/3 unless an exact match
  //    exists (drops "Or" matches inside "Oregon"). When >1 candidates
  //    pass at the SAME tier, return ambiguous_option with the
  //    candidates so the brain narrows the value.
  const pickAttempt = (): Promise<
    | { ok: true; option_selector: string; picked_text: string }
    | { ok: false; reason: string; candidates: string[] }
  > => page.evaluate((val: string, fz: boolean) => {
    const norm = (s: string) => (s || '').replace(/\s+/g, ' ').trim();
    const lower = (s: string) => norm(s).toLowerCase();
    const tgt = lower(val);

    const seen: HTMLElement[] = Array.from(
      document.querySelectorAll<HTMLElement>('[data-sb-opt-candidate]'),
    );
    const items = seen
      .map((el) => ({ el, txt: norm(el.innerText || el.textContent || '') }))
      .filter((it) => it.txt.length > 0 && it.txt.length < 200);
    if (items.length === 0) {
      return { ok: false as const, reason: 'no_options_collected', candidates: [] as string[] };
    }
    if (!tgt) {
      return {
        ok: false as const,
        reason: 'no_option_match',
        candidates: items.slice(0, 25).map((it) => it.txt),
      };
    }

    // Word-boundary helper.
    const wordBoundary = (s: string, frag: string): boolean => {
      if (!frag) return false;
      const i = s.indexOf(frag);
      if (i < 0) return false;
      const before = s[i - 1];
      const after = s[i + frag.length];
      const isWord = (c: string | undefined): boolean =>
        c != null && /[\p{L}\p{N}_]/u.test(c);
      return !isWord(before) && !isWord(after);
    };
    const startsWithBoundary = (s: string): boolean => {
      if (!s.startsWith(tgt)) return false;
      const after = s[tgt.length];
      return after == null || !/[\p{L}\p{N}_]/u.test(after);
    };

    type Item = { el: HTMLElement; txt: string };
    const lowerItems: Array<Item & { lower: string }> = items.map(
      (it) => ({ ...it, lower: lower(it.txt) }),
    );

    // Tier 1: exact-ci.
    const exactHits = lowerItems.filter((it) => it.lower === tgt);
    if (exactHits.length === 1) {
      const optTag = `sb-opt-${Math.random().toString(36).slice(2, 10)}`;
      exactHits[0].el.setAttribute('data-sb-opt', optTag);
      return {
        ok: true as const,
        option_selector: `[data-sb-opt="${optTag}"]`,
        picked_text: exactHits[0].txt,
      };
    }
    if (exactHits.length > 1) {
      return {
        ok: false as const,
        reason: 'ambiguous_option',
        candidates: exactHits.map((it) => it.txt).slice(0, 10),
      };
    }

    // Reject 1-2 char targets beyond tier 1.
    if (tgt.length < 3) {
      return {
        ok: false as const,
        reason: 'no_option_match',
        candidates: items.slice(0, 25).map((it) => it.txt),
      };
    }

    // Tier 2: startsWith-word.
    const startsHits = lowerItems.filter((it) => startsWithBoundary(it.lower));
    if (startsHits.length >= 1) {
      // Tiebreaker: prefer shorter text (more specific).
      startsHits.sort((a, b) => a.txt.length - b.txt.length);
      if (startsHits.length > 1
          && startsHits[0].txt.length === startsHits[1].txt.length) {
        return {
          ok: false as const,
          reason: 'ambiguous_option',
          candidates: startsHits.map((it) => it.txt).slice(0, 10),
        };
      }
      const optTag = `sb-opt-${Math.random().toString(36).slice(2, 10)}`;
      startsHits[0].el.setAttribute('data-sb-opt', optTag);
      return {
        ok: true as const,
        option_selector: `[data-sb-opt="${optTag}"]`,
        picked_text: startsHits[0].txt,
      };
    }

    // Tier 3: contains-word.
    const containsHits = lowerItems.filter(
      (it) => wordBoundary(it.lower, tgt),
    );
    if (containsHits.length >= 1) {
      containsHits.sort((a, b) => a.txt.length - b.txt.length);
      if (containsHits.length > 1
          && containsHits[0].txt.length === containsHits[1].txt.length) {
        return {
          ok: false as const,
          reason: 'ambiguous_option',
          candidates: containsHits.map((it) => it.txt).slice(0, 10),
        };
      }
      const optTag = `sb-opt-${Math.random().toString(36).slice(2, 10)}`;
      containsHits[0].el.setAttribute('data-sb-opt', optTag);
      return {
        ok: true as const,
        option_selector: `[data-sb-opt="${optTag}"]`,
        picked_text: containsHits[0].txt,
      };
    }

    // Tier 4: fuzzy. Raised floor to 0.85 + tgt.length >= 4.
    if (fz && tgt.length >= 4) {
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
      type FuzzyHit = { it: { el: HTMLElement; txt: string }; score: number };
      let best: FuzzyHit | null = null;
      let runnerUp: FuzzyHit | null = null;
      for (const it of lowerItems) {
        const a = it.lower;
        if (!a) continue;
        const d = lev(a, tgt);
        const sim = 1 - d / Math.max(a.length, tgt.length);
        if (sim >= 0.85) {
          if (!best || sim > best.score) {
            runnerUp = best;
            best = { it: { el: it.el, txt: it.txt }, score: sim };
          } else if (!runnerUp || sim > runnerUp.score) {
            runnerUp = { it: { el: it.el, txt: it.txt }, score: sim };
          }
        }
      }
      if (best) {
        // Ambiguous if runner-up scored within 0.05 of best.
        if (runnerUp && (best.score - runnerUp.score) < 0.05) {
          return {
            ok: false as const,
            reason: 'ambiguous_option',
            candidates: [best.it.txt, runnerUp.it.txt].slice(0, 10),
          };
        }
        const optTag = `sb-opt-${Math.random().toString(36).slice(2, 10)}`;
        best.it.el.setAttribute('data-sb-opt', optTag);
        return {
          ok: true as const,
          option_selector: `[data-sb-opt="${optTag}"]`,
          picked_text: best.it.txt,
        };
      }
    }

    return {
      ok: false as const,
      reason: 'no_option_match',
      candidates: items.slice(0, 25).map((it) => it.txt),
    };
  }, value, fuzzy);

  // Resolve the listbox's scrollable host once — re-used across retries
  // and to scroll the matched option into the container's visible rect
  // before clicking. Uses any tagged candidate as a starting point.
  const findListboxScrollHost = (): Promise<string> => page.evaluate(() => {
    const findHost = (start: HTMLElement | null): HTMLElement | null => {
      let cur: HTMLElement | null = start;
      while (cur && cur !== document.body) {
        const cs = window.getComputedStyle(cur);
        if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
            && cur.scrollHeight > cur.clientHeight + 4) {
          return cur;
        }
        cur = cur.parentElement;
      }
      return null;
    };
    const firstOption = document.querySelector('[data-sb-opt-candidate]') as HTMLElement | null;
    if (!firstOption) return '';
    // The listbox itself sometimes IS the scroll host; otherwise walk up.
    let host: HTMLElement | null = null;
    let cur: HTMLElement | null = firstOption.parentElement;
    while (cur && cur !== document.body) {
      const role = cur.getAttribute('role') || '';
      if (role === 'listbox' || role === 'menu') {
        const cs = window.getComputedStyle(cur);
        if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
            && cur.scrollHeight > cur.clientHeight + 4) {
          host = cur;
          break;
        }
      }
      cur = cur.parentElement;
    }
    if (!host) host = findHost(firstOption);
    if (!host) return '';
    const id = `sb-listbox-host-${Math.random().toString(36).slice(2, 10)}`;
    host.setAttribute('data-sb-listbox-host', id);
    return `[data-sb-listbox-host="${id}"]`;
  });

  let pick = await pickAttempt();
  if (!pick.ok && (pick.reason === 'no_option_match' || pick.reason === 'no_options_collected')) {
    const host = await findListboxScrollHost();
    if (host) {
      // Walk the scroll host in small steps. Up to 8 iterations covers
      // ~3-4 viewport heights of options, which is enough for any list
      // that fits the "long native select" / "virtualized listbox"
      // shape this tool is meant to handle. After each step we MUST
      // re-tag candidates — virtualized lists swap nodes in/out, and
      // the DOM-diff path needs to see the new arrivals.
      const maxScrollIters = 8;
      let lastTop = -1;
      let plateau = 0;
      for (let i = 0; i < maxScrollIters; i++) {
        const nextTop = await page.evaluate((sel: string) => {
          const el = document.querySelector(sel) as HTMLElement | null;
          if (!el) return -1;
          const before = el.scrollTop;
          el.scrollBy(0, Math.max(60, Math.round(el.clientHeight * 0.85)));
          return el.scrollTop === before ? -1 : el.scrollTop;
        }, host) as number;
        if (nextTop < 0 || nextTop === lastTop) {
          plateau += 1;
          if (plateau >= 2) break;
        } else {
          plateau = 0;
        }
        lastTop = nextTop;
        await new Promise((r) => setTimeout(r, 200));
        // Re-collect: items revealed by the scroll need fresh tags.
        await collectAndTagCandidates().catch(() => 0);
        pick = await pickAttempt();
        if (pick.ok) break;
        if (!pick.ok && pick.reason !== 'no_option_match' && pick.reason !== 'no_options_collected') break;
      }
    }
  }

  if (!pick.ok) {
    if (pick.reason === 'ambiguous_option') {
      return {
        ok: false,
        reason: 'ambiguous_option',
        ambiguous_option: { candidates: pick.candidates },
        candidates: pick.candidates,
        trigger_selector: trigger.selector,
        took_ms: Date.now() - start,
      };
    }
    return {
      ok: false, reason: pick.reason, candidates: pick.candidates,
      trigger_selector: trigger.selector, took_ms: Date.now() - start,
    };
  }

  // 6. Bring the option into the container's visible rect (page-level
  //    scrollIntoView won't move a popup's internal scroll), then click.
  try {
    await page.evaluate((sel: string) => {
      const el = document.querySelector(sel) as HTMLElement | null;
      if (!el) return;
      // Find the nearest scrollable ancestor (popup/host) and adjust
      // its scrollTop so el is visible. Fallback to native scrollIntoView.
      let cur: HTMLElement | null = el.parentElement;
      let host: HTMLElement | null = null;
      while (cur && cur !== document.body) {
        const cs = window.getComputedStyle(cur);
        if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
            && cur.scrollHeight > cur.clientHeight + 4) {
          host = cur;
          break;
        }
        cur = cur.parentElement;
      }
      if (host) {
        const elRect = el.getBoundingClientRect();
        const hostRect = host.getBoundingClientRect();
        if (elRect.top < hostRect.top || elRect.bottom > hostRect.bottom) {
          host.scrollTop += (elRect.top - hostRect.top) - (host.clientHeight - el.clientHeight) / 2;
        }
      } else {
        el.scrollIntoView({ block: 'center' });
      }
    }, pick.option_selector);
    await new Promise((r) => setTimeout(r, 100));
    await page.click(pick.option_selector);
  } catch (e) {
    return {
      ok: false, reason: `option_click_failed: ${(e as Error).message}`,
      trigger_selector: trigger.selector, option_selector: pick.option_selector,
      took_ms: Date.now() - start,
    };
  }

  // 7. Settle, then verify in three layers:
  //    a) navFingerprint after — option click that navigated should
  //       NOT report success.
  //    b) trigger still in DOM — re-querying via data-attr; if gone +
  //       no nav, surface trigger_disappeared.
  //    c) trigger text contains pick AND aria-expanded != true.
  await new Promise((r) => setTimeout(r, 250));

  const postFp = await navFingerprint(page);
  const optClickNavChange = navFingerprintChanged(preNavFp, postFp, true);
  if (optClickNavChange.changed) {
    return {
      ok: false,
      reason: 'option_navigated',
      trigger_selector: trigger.selector,
      option_selector: pick.option_selector,
      candidates: [
        `navigated_via=${optClickNavChange.kind}`,
        `from=${preNavFp.url}`,
        `to=${postFp.url}`,
      ],
      navigated_to: {
        url: postFp.url,
        path: postFp.pathname,
        hash: postFp.hash,
        titleChanged: preNavFp.title !== postFp.title,
      },
      took_ms: Date.now() - start,
    };
  }

  const verify = await page.evaluate((sel: string, picked: string) => {
    const norm = (s: string) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const el = document.querySelector(sel) as HTMLElement | null;
    if (!el) {
      return {
        triggerExists: false,
        contains_pick: false,
        current_text: '',
        closed: true,
        focusTag: '',
      };
    }
    const cur = norm(el.textContent || '');
    const focused = (document.activeElement
      || document.body) as HTMLElement | null;
    const focusTag = focused
      ? `${focused.tagName.toLowerCase()}${
          focused.id ? '#' + focused.id : ''
        }${
          focused.getAttribute('role')
            ? '[role=' + focused.getAttribute('role') + ']'
            : ''
        }`
      : '';
    return {
      triggerExists: true,
      closed: el.getAttribute('aria-expanded') !== 'true',
      contains_pick: cur.includes(norm(picked)),
      current_text: cur.slice(0, 200),
      focusTag: focusTag.slice(0, 120),
    };
  }, trigger.selector, pick.picked_text);

  if (!verify.triggerExists) {
    return {
      ok: false,
      reason: 'trigger_disappeared',
      trigger_selector: trigger.selector,
      option_selector: pick.option_selector,
      took_ms: Date.now() - start,
    };
  }

  return {
    ok: true,
    picked_text: pick.picked_text,
    trigger_selector: trigger.selector,
    option_selector: pick.option_selector,
    verified: verify.contains_pick && verify.closed,
    verified_focus_target: verify.focusTag || undefined,
    took_ms: Date.now() - start,
  };
}

/**
 * Phase F — iframe-scoped select_option. Resolves the host iframe by
 * CSS selector in the main document, gets its `contentFrame()`, then
 * runs a focused trigger picker + native `<select>` value-set inside
 * `frame.evaluate()`. Works for same-origin AND cross-origin OOPIFs
 * because `contentFrame()` returns a Puppeteer Frame for both (the
 * Frame API tunnels evaluate calls through the OOPIF's CDP target).
 *
 * Scope of v1 (sufficient for coolmath4kids quizzes + most iframe
 * forms): NATIVE `<select>` elements only. ARIA combobox / listbox
 * dropdowns inside iframes are not yet supported and return a
 * structured `iframe_aria_combobox_unsupported` reason — the brain
 * should fall back to `browser_click_selector(in_iframe, ...)` on
 * the trigger then on the option.
 *
 * Trigger resolution paths, in priority order:
 *   1. <label for="X"> with text matching `label` → element #X
 *   2. <label>Label <select/></label> — wrapping label
 *   3. <select aria-label="Label"> — explicit accessible name
 *   4. <select aria-labelledby="…"> resolved against frame ids
 *   5. <select> immediately preceded by a text-bearing element whose
 *      text matches `label` (compact form layouts: <div>Label</div>
 *      <select/>)
 *
 * No popup-open / option-click logic — native <select> doesn't need
 * a UI to open. We set `.value` directly using the prototype's value
 * setter (React-safe via `_valueTracker` reset, mirroring
 * `_ATOMIC_FIX_TEXT_JS`), then dispatch `input` + `change` so
 * framework-controlled inputs react.
 */
export async function selectOptionInIframe(
  page: Page,
  opts: {
    iframeHost: string;
    label: string;
    value: string;
    fuzzy?: boolean;
    timeout?: number;
  },
): Promise<SelectOptionByLabelResult> {
  const start = Date.now();
  const hostHandle = await page.$(opts.iframeHost);
  if (!hostHandle) {
    return {
      ok: false,
      reason: 'iframe_host_not_found',
      took_ms: Date.now() - start,
    };
  }
  const frame = await hostHandle.contentFrame();
  await hostHandle.dispose();
  if (!frame) {
    return {
      ok: false,
      reason: 'iframe_contentframe_null',
      took_ms: Date.now() - start,
    };
  }

  // Frame-local trigger picker + value-set in a single round-trip.
  // Everything runs inside the iframe's document context so we don't
  // need to track frame offsets here — `select.value = ...` and the
  // event dispatches operate on the inner document directly.
  const result = await frame.evaluate(
    (args: { lbl: string; val: string; fuzzy: boolean }) => {
      const norm = (s: string) => (s || '').replace(/\s+/g, ' ').trim();
      const lower = (s: string) => norm(s).toLowerCase();
      const target = lower(args.lbl);
      if (!target) {
        return {
          ok: false as const,
          reason: 'empty_label' as const,
        };
      }

      const isVisible = (el: HTMLElement): boolean => {
        if (el.tagName === 'SELECT') return true;
        if (el.offsetParent === null) return false;
        const cs = window.getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none') return false;
        return true;
      };

      const matches = (text: string): boolean => {
        const t = lower(text);
        if (!t) return false;
        if (t === target) return true;
        if (args.fuzzy) {
          if (t.includes(target)) return true;
          if (target.includes(t) && t.length >= 3) return true;
        }
        return false;
      };

      // 1. <label for="X"> → element #X
      let foundSelect: HTMLSelectElement | null = null;
      let foundVia = '';
      for (const lab of Array.from(
        document.querySelectorAll<HTMLLabelElement>('label[for]'),
      )) {
        if (!matches(lab.textContent || '')) continue;
        const ctrl = document.getElementById(lab.htmlFor);
        if (!ctrl) continue;
        if (ctrl.tagName.toLowerCase() !== 'select') continue;
        if (!isVisible(ctrl as HTMLElement)) continue;
        foundSelect = ctrl as HTMLSelectElement;
        foundVia = 'label_for';
        break;
      }

      // 2. <label>Label<select/></label> — wrapping label.
      if (!foundSelect) {
        for (const lab of Array.from(
          document.querySelectorAll<HTMLLabelElement>('label'),
        )) {
          // Skip <label for=...> already-handled cases.
          if (lab.hasAttribute('for')) continue;
          const sel = lab.querySelector<HTMLSelectElement>('select');
          if (!sel || !isVisible(sel)) continue;
          // Take label text MINUS the select's option text (the inner
          // select contributes its currentText to label.textContent).
          const labText = (lab.textContent || '').replace(
            sel.textContent || '',
            '',
          );
          if (!matches(labText)) continue;
          foundSelect = sel;
          foundVia = 'wrap_label';
          break;
        }
      }

      // 3. <select aria-label="...">
      if (!foundSelect) {
        for (const sel of Array.from(
          document.querySelectorAll<HTMLSelectElement>('select[aria-label]'),
        )) {
          if (!isVisible(sel)) continue;
          if (matches(sel.getAttribute('aria-label') || '')) {
            foundSelect = sel;
            foundVia = 'aria_label';
            break;
          }
        }
      }

      // 4. <select aria-labelledby="id1 id2"> → join referenced texts.
      if (!foundSelect) {
        for (const sel of Array.from(
          document.querySelectorAll<HTMLSelectElement>(
            'select[aria-labelledby]',
          ),
        )) {
          if (!isVisible(sel)) continue;
          const ids = (sel.getAttribute('aria-labelledby') || '')
            .split(/\s+/)
            .filter(Boolean);
          const joined = ids
            .map((id) => document.getElementById(id)?.textContent || '')
            .join(' ');
          if (matches(joined)) {
            foundSelect = sel;
            foundVia = 'aria_labelledby';
            break;
          }
        }
      }

      // 5. Preceding text near a <select>. Walk previousElementSibling
      //    of the select OR its parent, scanning up to 3 hops.
      if (!foundSelect) {
        for (const sel of Array.from(
          document.querySelectorAll<HTMLSelectElement>('select'),
        )) {
          if (!isVisible(sel)) continue;
          const scan = (start: Element | null): boolean => {
            let cur = start;
            for (let i = 0; i < 3 && cur; i++) {
              if (cur instanceof HTMLElement) {
                if (matches(cur.textContent || '')) return true;
              }
              cur = cur.previousElementSibling;
            }
            return false;
          };
          if (scan(sel.previousElementSibling)
              || scan(sel.parentElement?.previousElementSibling ?? null)) {
            foundSelect = sel;
            foundVia = 'preceding_text';
            break;
          }
        }
      }

      if (!foundSelect) {
        // Collect candidate <select>s for diagnostics.
        const candidates: string[] = [];
        for (const sel of Array.from(
          document.querySelectorAll<HTMLSelectElement>('select'),
        )) {
          if (!isVisible(sel)) continue;
          const al = sel.getAttribute('aria-label') || '';
          const nm = sel.getAttribute('name') || '';
          const id = sel.id ? `#${sel.id}` : '';
          candidates.push(
            `select${id}${nm ? `[name="${nm}"]` : ''}`
            + (al ? ` aria-label="${al.slice(0, 40)}"` : ''),
          );
          if (candidates.length >= 5) break;
        }
        return {
          ok: false as const,
          reason: 'trigger_not_found_in_iframe' as const,
          candidates,
        };
      }

      // Resolve the option: exact value match, then exact text, then
      // fuzzy text contains.
      let optionValue: string | null = null;
      let pickedText: string | null = null;
      const valLc = lower(args.val);
      for (const opt of Array.from(foundSelect.options)) {
        if (opt.value === args.val) {
          optionValue = opt.value;
          pickedText = opt.text || opt.value;
          break;
        }
      }
      if (optionValue == null) {
        for (const opt of Array.from(foundSelect.options)) {
          if (lower(opt.text) === valLc) {
            optionValue = opt.value;
            pickedText = opt.text;
            break;
          }
        }
      }
      if (optionValue == null && args.fuzzy) {
        for (const opt of Array.from(foundSelect.options)) {
          if (lower(opt.text).includes(valLc)
              || valLc.includes(lower(opt.text))) {
            optionValue = opt.value;
            pickedText = opt.text;
            break;
          }
        }
      }
      if (optionValue == null) {
        const options = Array.from(foundSelect.options).map(
          (o) => `${o.value}:${(o.text || '').trim().slice(0, 30)}`,
        );
        return {
          ok: false as const,
          reason: 'option_not_found_in_iframe' as const,
          available_options: options.slice(0, 20),
        };
      }

      // Set value via the React-safe prototype setter, mirroring
      // _ATOMIC_FIX_TEXT_JS. Resets `_valueTracker` so controlled
      // inputs don't short-circuit the synthetic onChange.
      try {
        const proto = HTMLSelectElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        const tracker = (foundSelect as unknown as
          { _valueTracker?: { setValue?: (v: string) => void } })._valueTracker;
        if (tracker && typeof tracker.setValue === 'function') {
          try { tracker.setValue(''); } catch (_) { /* ignore */ }
        }
        if (desc?.set) {
          desc.set.call(foundSelect, optionValue);
        } else {
          foundSelect.value = optionValue;
        }
        foundSelect.dispatchEvent(new Event('input', { bubbles: true }));
        foundSelect.dispatchEvent(new Event('change', { bubbles: true }));
      } catch (e) {
        return {
          ok: false as const,
          reason: 'value_set_exception' as const,
          error: String(e).slice(0, 120),
        };
      }

      // Verify.
      const verified = foundSelect.value === optionValue;
      const id = foundSelect.id ? `#${foundSelect.id}` : '';
      const nm = foundSelect.getAttribute('name') || '';
      const triggerSelector =
        id || (nm ? `select[name="${nm}"]` : 'select');
      return {
        ok: verified,
        picked_text: pickedText || optionValue,
        trigger_selector: triggerSelector,
        selected_index: foundSelect.selectedIndex,
        verified,
        found_via: foundVia,
        reason: verified ? undefined : 'value_set_did_not_stick',
      };
    },
    { lbl: opts.label, val: opts.value, fuzzy: opts.fuzzy !== false },
  );

  return {
    ...result,
    ok: result.ok,
    took_ms: Date.now() - start,
    ...(result.ok
      ? {
          trigger_selector: result.trigger_selector,
          picked_text: result.picked_text,
          selected_index: result.selected_index,
          verified: result.verified,
        }
      : {
          reason: result.reason,
          candidates: 'candidates' in result ? result.candidates : undefined,
        }),
  } as SelectOptionByLabelResult;
}

/**
 * Bbox-aware select_option entrypoint. Caller passes the dropdown
 * trigger's bounding box (CSS pixel coords); we snap to the
 * interactive descendant inside that bbox, gate it through the same
 * affordance check the label-path uses, and dispatch to the shared
 * open-strategy + pickOption + verify pipeline by tagging the
 * resolved element and calling selectOptionByLabel with
 * `triggerSelector` set.
 *
 * Returns `bbox_not_a_dropdown` when the snapped element fails the
 * affordance gate — the brain should then use browser_click_at on
 * the bbox instead.
 */
export async function selectOptionByVisionBbox(
  page: Page,
  opts: {
    bbox: { x0: number; y0: number; x1: number; y1: number };
    expectedLabel?: string;
    value: string;
    fuzzy?: boolean;
    timeout?: number;
    extraOptionSelectors?: string[];
  },
): Promise<SelectOptionByLabelResult> {
  const start = Date.now();
  const { bbox, value } = opts;
  const cx = (bbox.x0 + bbox.x1) / 2;
  const cy = (bbox.y0 + bbox.y1) / 2;

  // Snap to the most-interactive element inside the bbox + tag it.
  const snap = await page.evaluate(
    (args: {
      x0: number; y0: number; x1: number; y1: number;
      cx: number; cy: number;
    }) => {
      const { x0, y0, x1, y1, cx, cy } = args;
      const isVisible = (el: HTMLElement): boolean => {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        const cs = window.getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none') return false;
        return true;
      };
      const interactiveSelector = (
        '[role="combobox"], [role="listbox"], [role="button"], '
        + '[aria-haspopup], [aria-expanded], button, select, input, '
        + '[role="textbox"], [role="menu"], [tabindex]:not([tabindex="-1"])'
      );
      // Tier 1: element at center of bbox.
      let chosen: HTMLElement | null = null;
      const atCenter = document.elementFromPoint(cx, cy) as HTMLElement | null;
      if (atCenter && isVisible(atCenter)) {
        chosen = atCenter.closest(interactiveSelector) as HTMLElement | null
          ?? atCenter;
      }
      // Tier 2: best-overlap interactive descendant inside bbox.
      if (!chosen || !chosen.matches(interactiveSelector)) {
        let bestEl: HTMLElement | null = null;
        let bestArea = 0;
        const candidates = document.querySelectorAll<HTMLElement>(interactiveSelector);
        for (const el of Array.from(candidates)) {
          if (!isVisible(el)) continue;
          const r = el.getBoundingClientRect();
          // Compute overlap.
          const ox = Math.max(0, Math.min(x1, r.right) - Math.max(x0, r.left));
          const oy = Math.max(0, Math.min(y1, r.bottom) - Math.max(y0, r.top));
          const overlap = ox * oy;
          if (overlap <= 0) continue;
          if (overlap > bestArea) {
            bestArea = overlap;
            bestEl = el;
          }
        }
        if (bestEl) chosen = bestEl;
      }
      if (!chosen) return null;

      // Affordance check (mirror of selectOptionByLabel's gate).
      const role = (chosen.getAttribute('role') || '').toLowerCase();
      const ariaPopup = (chosen.getAttribute('aria-haspopup') || '').toLowerCase();
      const tag = chosen.tagName.toLowerCase();
      const hasPopupAffordance = (
        tag === 'select'
        || role === 'combobox' || role === 'listbox' || role === 'menu'
        || ariaPopup === 'listbox' || ariaPopup === 'menu'
        || ariaPopup === 'true' || ariaPopup === 'dialog'
        || ariaPopup === 'tree' || ariaPopup === 'grid'
        || chosen.hasAttribute('aria-expanded')
        || (
          tag === 'input' && chosen.hasAttribute('list')
          && document.getElementById(chosen.getAttribute('list') || '') != null
        )
        || (
          (tag === 'button' || (tag === 'div' && role === 'button'))
          && chosen.querySelector(
            '[class*="chevron"], [class*="caret"], [class*="arrow-down"], '
            + 'svg[class*="chevron"], svg[class*="caret"]',
          ) != null
        )
      );
      const id = `sb-trigger-${Math.random().toString(36).slice(2, 10)}`;
      chosen.setAttribute('data-sb-trigger', id);
      return {
        selector: `[data-sb-trigger="${id}"]`,
        hasPopupAffordance,
        tag,
        role,
      };
    },
    { x0: bbox.x0, y0: bbox.y0, x1: bbox.x1, y1: bbox.y1, cx, cy },
  );

  if (!snap) {
    return {
      ok: false,
      reason: 'bbox_no_interactive',
      took_ms: Date.now() - start,
    };
  }

  if (!snap.hasPopupAffordance) {
    return {
      ok: false,
      reason: 'bbox_not_a_dropdown',
      trigger_selector: snap.selector,
      candidates: [`tag=${snap.tag}`, `role=${snap.role || '(none)'}`],
      took_ms: Date.now() - start,
    };
  }

  // Hand off to the shared label-path. label is unused on this path
  // (triggerSelector overrides the picker) but the function still
  // requires it; pass expectedLabel as a diagnostic.
  const result = await selectOptionByLabel(page, {
    label: opts.expectedLabel ?? '',
    value,
    fuzzy: opts.fuzzy,
    timeout: opts.timeout,
    extraOptionSelectors: opts.extraOptionSelectors,
    triggerSelector: snap.selector,
  });
  return {
    ...result,
    took_ms: result.took_ms ?? (Date.now() - start),
  };
}
