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
  // Diagnostics (Phase D) — surfaced on failure so the LLM can pick a
  // different tool instead of looping.
  tried?: string[];        // open strategies attempted: 'click','mousedown',...
  dom_changed?: boolean;   // did anything new become visible after open?
  new_classes?: string[];  // top sample of newly-visible class names
  trigger_score?: number;  // how confident the trigger picker was (Phase C)
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

  // 1. Locate trigger by label. SCORED picker (Phase C): when multiple
  //    candidates contain the label text, prefer ones with explicit
  //    dropdown semantics. Skips heading-shaped elements (full-width,
  //    short height) which often steal the match on retail SPAs.
  const trigger = await page.evaluate((lbl: string) => {
    const norm = (s: string) => (s || '').replace(/\s+/g, ' ').trim();
    const lower = (s: string) => norm(s).toLowerCase();
    const target = lower(lbl);
    if (!target) return null;

    const isVisuallyHeading = (el: HTMLElement): boolean => {
      const r = el.getBoundingClientRect();
      // Heading-shaped: takes the full content width and is short.
      // These are usually section labels, not dropdown triggers.
      if (r.width > window.innerWidth * 0.7 && r.height < 40) return true;
      const tag = el.tagName.toLowerCase();
      if (tag === 'h1' || tag === 'h2' || tag === 'h3') return true;
      return false;
    };

    const isVisible = (el: HTMLElement): boolean => {
      // <select> elements often have offsetParent null even when usable;
      // treat them as visible for compatibility with the existing path.
      if (el.tagName === 'SELECT') return true;
      if (el.offsetParent === null) return false;
      const cs = window.getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none') return false;
      return true;
    };

    const scoreCandidate = (el: HTMLElement): number => {
      const role = (el.getAttribute('role') || '').toLowerCase();
      const ariaPopup = (el.getAttribute('aria-haspopup') || '').toLowerCase();
      const tag = el.tagName.toLowerCase();
      let score = 0;
      // Strong signals — explicit dropdown semantics.
      if (ariaPopup === 'listbox' || ariaPopup === 'menu' || ariaPopup === 'true' || ariaPopup === 'dialog') score += 12;
      if (role === 'combobox') score += 10;
      if (role === 'listbox') score += 8;
      if (tag === 'select') score += 9;
      if (el.hasAttribute('aria-expanded')) score += 4;
      // Mid signals — class hint on element or wrapping parent.
      const cls = ((typeof el.className === 'string' ? el.className : '') || '').toLowerCase();
      if (/\b(select|dropdown|combobox|picker|chooser)\b/.test(cls)) score += 5;
      const parent = el.parentElement;
      const pcls = (parent && typeof parent.className === 'string' ? parent.className : '').toLowerCase();
      if (/\b(select|dropdown|combobox|picker|chooser|form-field|field-group)\b/.test(pcls)) score += 3;
      // Penalty — heading-shaped elements steal matches.
      if (isVisuallyHeading(el)) score -= 6;
      // Tiny preference for smaller area (fragmented page layouts).
      const r = el.getBoundingClientRect();
      const area = Math.max(1, r.width * r.height);
      score += Math.max(0, 3 - Math.log10(area));
      return score;
    };

    const tag = (el: Element, score: number) => {
      const id = `sb-trigger-${Math.random().toString(36).slice(2, 10)}`;
      el.setAttribute('data-sb-trigger', id);
      const isSelect = el.tagName.toLowerCase() === 'select';
      return {
        selector: `[data-sb-trigger="${id}"]`,
        isNativeSelect: isSelect,
        currentText: norm((el as HTMLElement).textContent || '').slice(0, 200),
        role: el.getAttribute('role') || '',
        ariaExpanded: el.getAttribute('aria-expanded') || '',
        score,
      };
    };

    // A. <label for="x">Lbl</label> + #x — strongest signal, take it
    //    immediately if found.
    for (const lab of Array.from(document.querySelectorAll<HTMLLabelElement>('label[for]'))) {
      if (lower(lab.textContent || '').includes(target)) {
        const ctrl = document.getElementById(lab.htmlFor);
        if (ctrl) return tag(ctrl, 20);  // synthetic high score — explicit association
      }
    }

    // B. aria-labelledby — also a strong, explicit association.
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('[aria-labelledby]'))) {
      const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
      const labText = ids
        .map((id) => lower(document.getElementById(id)?.textContent || ''))
        .join(' ');
      if (labText.includes(target)) return tag(el, 18);
    }

    // C+D. Score-based pick across aria-label and visible-text candidates.
    const interactiveSelector = (
      '[role="combobox"], [role="listbox"], [role="button"], '
      + '[aria-haspopup], [aria-expanded], button, select, input, '
      + '[role="textbox"], [tabindex]:not([tabindex="-1"])'
    );
    const interactive = Array.from(document.querySelectorAll<HTMLElement>(interactiveSelector));
    let best: { el: HTMLElement; score: number } | null = null;
    for (const el of interactive) {
      if (!isVisible(el)) continue;
      const aria = lower(el.getAttribute('aria-label') || '');
      const txt = lower((el.innerText || el.textContent || '')).slice(0, 200);
      const placeholder = lower(el.getAttribute('placeholder') || '');
      const matches = aria.includes(target) || txt.includes(target) || placeholder.includes(target);
      if (!matches) continue;
      const s = scoreCandidate(el) + (aria.includes(target) ? 3 : 0);
      if (!best || s > best.score) best = { el, score: s };
    }
    if (best && best.score > 0) return tag(best.el, best.score);

    // E. Wrapping <label> — <label>Lbl <input/></label>
    for (const lab of Array.from(document.querySelectorAll<HTMLLabelElement>('label'))) {
      if (lower(lab.textContent || '').includes(target)) {
        const ctrl = lab.querySelector<HTMLElement>('select, input, [role="combobox"], [role="listbox"]');
        if (ctrl) return tag(ctrl, 15);
      }
    }

    // F. Last-ditch: best low-confidence pick (negative scores allowed).
    if (best) return tag(best.el, best.score);
    return null;
  }, label);

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
  let preNavUrl = '';
  let preNavTitle = '';
  try {
    preNavUrl = page.url();
    preNavTitle = await page.title();
  } catch { /* best-effort */ }
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
      const tagSet = (el: Element): void => {
        if (el === triggerEl) return;
        if (triggerEl && triggerEl.contains(el)) return;
        (el as HTMLElement).setAttribute('data-sb-opt-candidate', '1');
      };

      // 1. Standard role-based selectors (preferred when present).
      for (const s of sels) {
        try {
          for (const el of Array.from(document.querySelectorAll(s))) {
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
      const pool = document.querySelectorAll<HTMLElement>(
        'li, button, a, [role], [data-value], [data-option-value], div, span',
      );
      let added = 0;
      const cap = 500;  // soft cap on candidate count
      for (const el of Array.from(pool)) {
        if (added >= cap) break;
        if (el.hasAttribute('data-sb-opt-candidate')) continue;
        if (el.hasAttribute('data-sb-pre-visible')) continue;  // was visible BEFORE click
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
  let navigatedTo = '';
  // Per-strategy budget: split the total timeout across strategies, but
  // give each at least 600ms. Total can slightly exceed timeout when
  // every strategy is tried; that's acceptable.
  const perStrategyBudget = Math.max(600, Math.floor(timeout / strategies.length));

  for (const strategy of strategies) {
    tried.push(strategy);
    try {
      await runStrategy(strategy);
    } catch { /* try next */ continue; }

    const subDeadline = Date.now() + perStrategyBudget;
    while (Date.now() < subDeadline) {
      // Fast-path: navigation kills the dropdown story entirely.
      let curUrl = '';
      try { curUrl = page.url(); } catch { /* page may be navigating */ }
      if (curUrl && preNavUrl && curUrl !== preNavUrl) { navigatedTo = curUrl; break; }
      // Tag-and-count: if anything popup-shaped exists, try collecting.
      const popupNow = await checkPopupDetected().catch(() => false);
      if (popupNow) popupSeen = true;
      const tagged = await collectAndTagCandidates().catch(() => 0);
      if (tagged > 0) { rendered = true; break; }
      await new Promise((r) => setTimeout(r, 120));
    }
    if (rendered || navigatedTo) break;
  }

  if (navigatedTo) {
    return {
      ok: false,
      reason: 'trigger_navigated',
      trigger_selector: trigger.selector,
      candidates: [`navigated_to=${navigatedTo}`, `from=${preNavUrl}`],
      tried,
      trigger_score: trigger.score,
      took_ms: Date.now() - start,
    };
  }

  if (!rendered) {
    // Final post-loop title check — SPA pushState navigation that
    // didn't change page.url() but updated the title.
    let postTitle = '';
    try { postTitle = await page.title(); } catch { /* nope */ }
    if (postTitle && preNavTitle && postTitle !== preNavTitle) {
      return {
        ok: false,
        reason: 'trigger_navigated',
        trigger_selector: trigger.selector,
        candidates: [`title_changed=${postTitle}`, `was=${preNavTitle}`],
        tried,
        trigger_score: trigger.score,
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

  // 5. Pick option by exact-ci → startsWith → contains → fuzzy. The
  //    candidate set is whatever was tagged with `data-sb-opt-candidate`
  //    by the open-and-tag step above, which includes both standard
  //    role-based options and DOM-diff "newly visible option-shaped"
  //    elements. If the target isn't among them, the within-listbox
  //    scroll loop below re-tags after each step.
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
  };
}
