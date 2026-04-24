/**
 * JavaScript code injected into pages for DOM analysis.
 *
 * Walks the DOM, identifies interactive elements, assigns numeric indices,
 * and returns a serialized tree structure.
 *
 * Detection pipeline ported from browser-use
 * (browser_use/dom/serializer/clickable_elements.py and serializer.py):
 *
 *   1. MULTI-TIER INTERACTIVE DETECTION — tag, ARIA role, event attrs,
 *      cursor:pointer, contenteditable, search-indicator heuristics,
 *      icon-size rule, shadow DOM form-control piercing, iframes >100×100.
 *   2. PAINT-ORDER FILTER — drop elements fully occluded at their center
 *      point by another candidate. Eliminates ghost indices behind modals.
 *   3. CONTAINMENT FILTER — drop child candidates whose bounds are fully
 *      inside an interactive parent's bounds (parent is the real click
 *      target; children are propagating wrappers).
 *   4. INDEX ASSIGNMENT — number visible interactive elements 0..N.
 *
 * Called from dom.ts buildDomTree() which pairs this with a prior
 * selectorMap to mark newly-appeared elements with `*[n]` in the LLM view.
 */

export function getBuildDomTreeScript(): string {
  return `
(function buildDomTree(viewportExpansion) {
  viewportExpansion = viewportExpansion || 0;

  // --- Canonical lists (from browser-use clickable_elements.py:139-226) ---

  const INTERACTIVE_TAGS = new Set([
    'a', 'button', 'input', 'select', 'textarea',
    'details', 'summary', 'label', 'option',
  ]);
  const INTERACTIVE_ROLES = new Set([
    'button', 'link', 'tab', 'tabpanel', 'menuitem', 'menuitemcheckbox',
    'menuitemradio', 'option', 'switch', 'checkbox', 'radio',
    'combobox', 'textbox', 'searchbox', 'slider', 'spinbutton',
    'treeitem', 'listbox', 'scrollbar', 'separator',
  ]);
  const EVENT_ATTRS = [
    'onclick', 'onmousedown', 'onmouseup', 'onkeydown', 'onkeyup',
    'ontouchstart', 'ontouchend', 'onpointerdown', 'onpointerup',
  ];
  const INCLUDE_ATTRIBUTES = [
    'id', 'type', 'name', 'placeholder', 'value', 'href', 'src', 'alt',
    'title', 'role', 'aria-label', 'aria-expanded', 'aria-checked',
    'aria-selected', 'aria-disabled', 'data-testid', 'for',
    'action', 'method', 'target', 'rel',
    // Slider metadata — lets the LLM see current/min/max without a
    // separate probe before picking a value to pass browser_set_slider.
    'min', 'max', 'step',
    'aria-valuenow', 'aria-valuemin', 'aria-valuemax', 'aria-orientation',
  ];
  // Heuristic: class/id substrings that strongly imply interactivity.
  // Used as a tiebreaker when other signals are absent — e.g., a <div> with
  // class="search-btn" is a button even without role attribute.
  const SEARCH_INDICATORS = [
    'button', 'btn', 'clickable', 'clicker', 'selectable',
    'menu', 'nav-item', 'nav-link', 'tab', 'pill', 'chip',
    'close', 'dismiss', 'toggle', 'submit', 'search',
    'action', 'control', 'handle', 'trigger',
  ];

  // --- Interactive-detection core ---

  function isElementDisabled(el) {
    if (el.hasAttribute('disabled')) return true;
    if (el.getAttribute('aria-disabled') === 'true') return true;
    if (el.getAttribute('aria-hidden') === 'true') return true;
    // Note: inert only disables descendants; we let visibility-pass handle that.
    return false;
  }

  function hasShadowFormDescendant(root, depth) {
    // browser-use: descend up to 2 levels through shadow roots looking for
    // form controls. Catches custom elements that wrap inputs/buttons.
    depth = depth || 0;
    if (depth > 2) return false;
    const children = root.children || [];
    for (let i = 0; i < children.length; i++) {
      const c = children[i];
      const tag = c.tagName && c.tagName.toLowerCase();
      if (tag && INTERACTIVE_TAGS.has(tag)) return true;
      if (c.shadowRoot && hasShadowFormDescendant(c.shadowRoot, depth + 1)) {
        return true;
      }
      if (c.children && c.children.length && hasShadowFormDescendant(c, depth + 1)) {
        return true;
      }
    }
    return false;
  }

  function classOrIdMatchesSearch(el) {
    const parts = ((el.className && typeof el.className === 'string' ? el.className : '') + ' ' + (el.id || '')).toLowerCase();
    if (!parts.trim()) return false;
    for (let i = 0; i < SEARCH_INDICATORS.length; i++) {
      if (parts.indexOf(SEARCH_INDICATORS[i]) !== -1) return true;
    }
    return false;
  }

  function isInteractive(el) {
    if (isElementDisabled(el)) return false;

    const tag = el.tagName.toLowerCase();
    // Tier 1: form tags
    if (INTERACTIVE_TAGS.has(tag)) return true;

    // Tier 2: ARIA roles
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (role && INTERACTIVE_ROLES.has(role)) return true;

    // Tier 3: event attrs
    for (let i = 0; i < EVENT_ATTRS.length; i++) {
      if (el.hasAttribute(EVENT_ATTRS[i])) return true;
    }

    // Tier 4: contenteditable
    const ce = el.getAttribute('contenteditable');
    if (ce && ce !== 'false') return true;

    // Tier 5: tabindex + cursor:pointer (explicit keyboard focus + visual affordance)
    const style = window.getComputedStyle(el);
    const cursor = style.cursor;
    if (el.hasAttribute('tabindex') && el.getAttribute('tabindex') !== '-1') {
      if (cursor === 'pointer') return true;
    }

    // Tier 6: cursor:pointer + class/id indicator or icon-size element
    //   - A <div> with cursor:pointer and "btn" in its class is a button
    //   - A small element (10-50px wide/tall) with cursor:pointer is an icon button
    if (cursor === 'pointer') {
      if (classOrIdMatchesSearch(el)) return true;
      const r = el.getBoundingClientRect();
      if (r.width >= 10 && r.width <= 50 && r.height >= 10 && r.height <= 50) return true;
      // Large div with cursor:pointer is usually a link-like element
      if (r.width >= 24 && r.height >= 20) return true;
    }

    // Tier 7: iframes larger than 100×100 — typically embedded UIs
    if (tag === 'iframe') {
      const r = el.getBoundingClientRect();
      if (r.width > 100 && r.height > 100) return true;
    }

    // Tier 8: shadow DOM custom elements with form-control descendants
    if (el.shadowRoot && hasShadowFormDescendant(el.shadowRoot)) return true;

    return false;
  }

  function isVisible(el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const opacity = parseFloat(style.opacity || '1');
    // File inputs are commonly hidden via opacity:0 overlays — keep them.
    const tag = el.tagName.toLowerCase();
    if (opacity === 0 && tag !== 'input' && tag !== 'label') return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    return true;
  }

  function isInViewport(el, expansion) {
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth || document.documentElement.clientWidth;
    const vh = window.innerHeight || document.documentElement.clientHeight;
    return (
      r.bottom >= -expansion &&
      r.right >= -expansion &&
      r.top <= vh + expansion &&
      r.left <= vw + expansion
    );
  }

  function getXPath(el) {
    const parts = [];
    let current = el;
    while (current && current.nodeType === Node.ELEMENT_NODE) {
      let index = 1;
      let sibling = current.previousSibling;
      while (sibling) {
        if (sibling.nodeType === Node.ELEMENT_NODE && sibling.tagName === current.tagName) {
          index++;
        }
        sibling = sibling.previousSibling;
      }
      const tag = current.tagName.toLowerCase();
      parts.unshift(tag + '[' + index + ']');
      current = current.parentElement;
    }
    return '/' + parts.join('/');
  }

  function getTextContent(el) {
    let text = '';
    for (const child of el.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        const t = child.textContent.trim();
        if (t) text += (text ? ' ' : '') + t;
      }
    }
    return text.substring(0, 200);
  }

  // --- Candidate collection pass ---
  //
  // Before assigning indices we collect all candidates, then apply the
  // paint-order + containment filters, then number what remains. This
  // mirrors browser-use's 4-stage pipeline and matters because the filters
  // need to compare candidates pairwise.

  const candidates = [];  // { el, nodeRef, bounds, tag, interactive, inViewport }

  // Regions that help Python-side perception fusion seed "tools section"
  // coverage passes. Walk up ancestors to find the nearest semantic region
  // landmark (ARIA role or HTML5 section tag) and tag the element with it.
  // Maps a raw role/tag to the compact token set Python expects.
  function computeRegionTag(el) {
    let cur = el;
    let depth = 0;
    while (cur && cur.nodeType === Node.ELEMENT_NODE && depth < 12) {
      const role = (cur.getAttribute && cur.getAttribute('role') || '').toLowerCase();
      const tag = cur.tagName && cur.tagName.toLowerCase();
      if (role === 'toolbar') return 'toolbar';
      if (role === 'navigation') return 'sidebar';
      if (role === 'banner') return 'header';
      if (role === 'contentinfo') return 'footer';
      if (role === 'complementary') return 'sidebar';
      if (role === 'main') return 'main';
      if (tag === 'nav') return 'sidebar';
      if (tag === 'aside') return 'sidebar';
      if (tag === 'header') return 'header';
      if (tag === 'footer') return 'footer';
      if (tag === 'main') return 'main';
      cur = cur.parentElement;
      depth++;
    }
    return 'main';
  }

  function buildNodeRef(el, interactive, visible, inViewport, isFromShadow) {
    const tag = el.tagName.toLowerCase();
    const attributes = {};
    for (const attr of INCLUDE_ATTRIBUTES) {
      const v = el.getAttribute(attr);
      if (v !== null && v !== '') attributes[attr] = v.substring(0, 100);
    }
    let bounds = null;
    let regionTag = null;
    if (interactive) {
      const r = el.getBoundingClientRect();
      // Viewport dims ride with every bounds payload so the Python
      // bridge can normalize to vision's 0-1000 box_2d space without
      // needing a separate probe.
      bounds = {
        x: Math.round(r.left),
        y: Math.round(r.top),
        width: Math.round(r.width),
        height: Math.round(r.height),
        vw: Math.round(window.innerWidth || document.documentElement.clientWidth || 0),
        vh: Math.round(window.innerHeight || document.documentElement.clientHeight || 0),
      };
      regionTag = computeRegionTag(el);
    }
    return {
      type: 'ELEMENT',
      tagName: tag,
      xpath: getXPath(el),
      attributes: attributes,
      text: getTextContent(el),
      isInteractive: interactive,
      isVisible: visible,
      isInViewport: inViewport,
      isFromShadow: !!isFromShadow,
      highlightIndex: null,
      children: [],
      bounds: bounds,
      regionTag: regionTag,
    };
  }

  function processNode(node, isFromShadow) {
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent.trim();
      if (!text) return null;
      return { type: 'TEXT', text: text.substring(0, 200), isVisible: true };
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return null;

    const el = node;
    const tag = el.tagName.toLowerCase();
    if (['script', 'style', 'noscript', 'svg', 'path', 'link', 'meta', 'head'].includes(tag)) {
      return null;
    }

    const visible = isVisible(el);
    if (!visible) return null;

    const interactive = isInteractive(el);
    const inViewport = isInViewport(el, viewportExpansion);

    const ref = buildNodeRef(el, interactive, visible, inViewport, isFromShadow);

    // Traverse light DOM
    for (const child of el.childNodes) {
      const processed = processNode(child, isFromShadow);
      if (processed) ref.children.push(processed);
    }

    // Traverse shadow DOM (open only — closed shadow roots are invisible
    // without CDP). For our purposes this is fine; closed shadow DOM
    // mostly appears in browser-internal UI we don't need to interact with.
    if (el.shadowRoot) {
      for (const child of el.shadowRoot.childNodes) {
        const processed = processNode(child, true);
        if (processed) ref.children.push(processed);
      }
    }

    if (interactive && (inViewport || viewportExpansion > 0)) {
      // Register as candidate; index assigned later after filters.
      candidates.push({ ref: ref, el: el, bounds: ref.bounds });
    }

    return ref;
  }

  const tree = processNode(document.body, false);

  // --- Filter pass 1: PAINT-ORDER (remove occluded elements) ---
  //
  // For each candidate, check document.elementFromPoint(center). If the
  // top-painted element is neither the candidate itself nor a descendant,
  // the candidate is occluded — skip indexing it.
  //
  // Skip this check for elements that are likely to be covered for UX
  // reasons (e.g., dropdown items under a modal that the user can still
  // reach by closing the modal). Heuristic: only drop if the occluder is
  // both a sibling-ish peer AND has role=dialog/modal/overlay.
  const occluded = new Set();
  for (let i = 0; i < candidates.length; i++) {
    const c = candidates[i];
    if (!c.bounds || c.bounds.width < 4 || c.bounds.height < 4) continue;
    const cx = c.bounds.x + c.bounds.width / 2;
    const cy = c.bounds.y + c.bounds.height / 2;
    // elementFromPoint can throw or return null on cross-origin iframes.
    let hit = null;
    try { hit = document.elementFromPoint(cx, cy); } catch (e) { hit = null; }
    if (!hit) continue;
    if (hit === c.el || c.el.contains(hit) || hit.contains(c.el)) continue;
    // The candidate's center is covered by an unrelated element. Only
    // treat it as occluded if the covering node looks like a modal/overlay
    // — otherwise we'd drop half of every menu.
    const hitRole = (hit.getAttribute && hit.getAttribute('role') || '').toLowerCase();
    const hitCls = (hit.className && typeof hit.className === 'string' ? hit.className : '').toLowerCase();
    if (hitRole === 'dialog' || hitRole === 'alertdialog' ||
        hitCls.indexOf('modal') !== -1 || hitCls.indexOf('overlay') !== -1 ||
        hitCls.indexOf('backdrop') !== -1) {
      occluded.add(c.ref);
    }
  }

  // --- Filter pass 2: CONTAINMENT (remove children fully inside a clickable parent) ---
  //
  // If candidate A's bounds are fully inside candidate B's bounds AND B is
  // an ancestor of A in the DOM, then A is probably a propagating wrapper
  // (button text span, icon, etc.) — clicking A and B do the same thing.
  // Keep the OUTER (the one the user visually targets).
  function contains(outer, inner) {
    return outer.bounds &&
           inner.bounds &&
           outer.bounds.x <= inner.bounds.x &&
           outer.bounds.y <= inner.bounds.y &&
           outer.bounds.x + outer.bounds.width  >= inner.bounds.x + inner.bounds.width &&
           outer.bounds.y + outer.bounds.height >= inner.bounds.y + inner.bounds.height;
  }
  const shadowed = new Set();
  for (let i = 0; i < candidates.length; i++) {
    const child = candidates[i];
    if (occluded.has(child.ref)) continue;
    for (let j = 0; j < candidates.length; j++) {
      if (i === j) continue;
      const parent = candidates[j];
      if (occluded.has(parent.ref)) continue;
      if (!parent.el.contains(child.el)) continue;
      if (parent.el === child.el) continue;
      if (contains(parent, child)) {
        // Prefer the parent unless the child carries semantic info the
        // parent doesn't — e.g., an <input> inside a styled <label>.
        const childTag = child.ref.tagName;
        const parentTag = parent.ref.tagName;
        if (childTag === 'input' || childTag === 'textarea' || childTag === 'select') break;
        if (parentTag === 'a' || parentTag === 'button') {
          shadowed.add(child.ref);
          break;
        }
        // For div-on-div containment, also prefer parent.
        shadowed.add(child.ref);
        break;
      }
    }
  }

  // --- Index assignment ---
  let highlightIndex = 0;
  const selectorEntries = [];
  for (let i = 0; i < candidates.length; i++) {
    const c = candidates[i];
    if (occluded.has(c.ref) || shadowed.has(c.ref)) continue;
    c.ref.highlightIndex = highlightIndex++;
    selectorEntries.push({
      index: c.ref.highlightIndex,
      xpath: c.ref.xpath,
      tagName: c.ref.tagName,
      attributes: c.ref.attributes,
      text: c.ref.text,
      bounds: c.ref.bounds,
      regionTag: c.ref.regionTag,
      role: c.ref.attributes && c.ref.attributes.role,
    });
  }

  return {
    tree: tree,
    selectorEntries: selectorEntries,
    url: window.location.href,
    title: document.title,
    scrollY: Math.round(window.scrollY),
    scrollHeight: Math.round(document.documentElement.scrollHeight),
    viewportHeight: Math.round(window.innerHeight),
    detectorStats: {
      candidates: candidates.length,
      occluded: occluded.size,
      shadowed: shadowed.size,
      indexed: selectorEntries.length,
    },
  };
})
`;
}
