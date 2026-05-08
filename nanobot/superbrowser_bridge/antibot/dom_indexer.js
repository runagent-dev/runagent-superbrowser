// Enumerate clickable elements + nearby inputs into a stable index list.
// Loaded into every patchright page; evaluated by `T3SessionManager._index_elements`.
//
// Output: Array<{index, tag, text, attrs, bbox: [x0,y0,x1,y1],
//                selector, role, visible, select_pos?}>
//
// Matches the shape the TS side emits so `browser_click(index=N)`,
// `browser_type(index=N)`, etc., work identically on either backend.

(() => {
  const INTERACTIVE_TAGS = new Set([
    'a', 'button', 'input', 'select', 'textarea', 'label',
    'summary', 'details', 'option',
  ]);
  const INTERACTIVE_ROLES = new Set([
    'button', 'link', 'checkbox', 'menuitem', 'tab', 'textbox',
    'combobox', 'switch', 'radio', 'searchbox',
  ]);

  const out = [];
  const seen = new WeakSet();
  let selectCounter = 0;

  function isVisible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    if (parseFloat(s.opacity || '1') < 0.05) return false;
    // Inside viewport? We still want above/below to be indexable for
    // "scroll then click" patterns, so allow offscreen as long as it's
    // within ±2 viewport heights of the current scroll position.
    const vh = window.innerHeight;
    if (r.bottom < -vh * 2 || r.top > vh * 3) return false;
    return true;
  }

  function getText(el) {
    // Prefer accessible name when available; fall back to textContent.
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    const title = (el.getAttribute('title') || '').trim();
    if (title) return title;
    const alt = (el.getAttribute('alt') || '').trim();
    if (alt) return alt;
    const placeholder = (el.getAttribute('placeholder') || '').trim();
    if (placeholder) return placeholder;
    const t = (el.innerText || el.textContent || '').trim();
    return t.replace(/\s+/g, ' ').slice(0, 120);
  }

  function buildSelector(el) {
    // Unique-ish CSS selector: id > data-testid > tag + class chain.
    if (el.id && !/[^\w-]/.test(el.id)) return `#${el.id}`;
    const testId = el.getAttribute('data-testid');
    if (testId) return `[data-testid="${testId}"]`;
    // Fallback: tag + classes + nth-of-type within parent.
    let sel = el.tagName.toLowerCase();
    const cls = (el.className || '').toString().trim().split(/\s+/).filter(Boolean);
    if (cls.length) sel += '.' + cls.slice(0, 2).join('.');
    const parent = el.parentElement;
    if (parent) {
      const same = Array.from(parent.children).filter(c => c.tagName === el.tagName);
      if (same.length > 1) {
        sel += `:nth-of-type(${same.indexOf(el) + 1})`;
      }
    }
    return sel;
  }

  function attrs(el) {
    const keep = ['type', 'name', 'href', 'role', 'aria-label', 'placeholder', 'value'];
    const out = {};
    for (const k of keep) {
      const v = el.getAttribute(k);
      if (v != null && v !== '') out[k] = String(v).slice(0, 80);
    }
    return out;
  }

  function pushElement(el) {
    if (seen.has(el)) return;
    seen.add(el);
    if (!isVisible(el)) return;
    const r = el.getBoundingClientRect();
    const entry = {
      index: out.length + 1,
      tag: el.tagName.toLowerCase(),
      text: getText(el),
      attrs: attrs(el),
      bbox: [Math.round(r.left), Math.round(r.top),
             Math.round(r.right), Math.round(r.bottom)],
      selector: buildSelector(el),
      role: el.getAttribute('role') || '',
      visible: true,
    };
    if (entry.tag === 'select') {
      entry.select_pos = selectCounter++;
    }
    out.push(entry);
  }

  // 1. Standard interactive tags.
  document.querySelectorAll('a, button, input, select, textarea, label, summary, details, option')
    .forEach(pushElement);

  // 2. Elements with an interactive ARIA role.
  document.querySelectorAll('[role]').forEach(el => {
    if (INTERACTIVE_ROLES.has((el.getAttribute('role') || '').toLowerCase())) {
      pushElement(el);
    }
  });

  // 3. Elements with onclick handlers (via event listeners is not introspectable,
  //    but onclick attribute is a strong hint).
  document.querySelectorAll('[onclick], [tabindex]:not([tabindex="-1"])').forEach(pushElement);

  return out;
})();
