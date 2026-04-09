/**
 * JavaScript code injected into pages for DOM analysis.
 *
 * Walks the DOM, identifies interactive elements, assigns numeric indices,
 * and returns a serialized tree structure.
 *
 * Pattern from nanobrowser (dom/clickable/service.ts), written from scratch.
 */

export function getBuildDomTreeScript(): string {
  return `
(function buildDomTree(viewportExpansion) {
  viewportExpansion = viewportExpansion || 0;

  const INTERACTIVE_TAGS = new Set([
    'a', 'button', 'input', 'select', 'textarea', 'details', 'summary',
  ]);
  const INTERACTIVE_ROLES = new Set([
    'button', 'link', 'tab', 'menuitem', 'menuitemcheckbox',
    'menuitemradio', 'option', 'switch', 'checkbox', 'radio',
    'combobox', 'textbox', 'searchbox', 'slider', 'spinbutton',
    'treeitem',
  ]);
  const INCLUDE_ATTRIBUTES = [
    'id', 'type', 'name', 'placeholder', 'value', 'href', 'src', 'alt',
    'title', 'role', 'aria-label', 'aria-expanded', 'aria-checked',
    'aria-selected', 'aria-disabled', 'data-testid', 'for',
    'action', 'method', 'target', 'rel',
  ];

  let highlightIndex = 0;

  function isVisible(el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    return true;
  }

  function isInViewport(el, expansion) {
    const rect = el.getBoundingClientRect();
    const vw = window.innerWidth || document.documentElement.clientWidth;
    const vh = window.innerHeight || document.documentElement.clientHeight;
    return (
      rect.bottom >= -expansion &&
      rect.right >= -expansion &&
      rect.top <= vh + expansion &&
      rect.left <= vw + expansion
    );
  }

  function isInteractive(el) {
    const tag = el.tagName.toLowerCase();
    if (INTERACTIVE_TAGS.has(tag)) return true;
    const role = el.getAttribute('role');
    if (role && INTERACTIVE_ROLES.has(role)) return true;
    if (el.hasAttribute('onclick') || el.hasAttribute('onmousedown') || el.hasAttribute('onmouseup')) return true;
    if (el.hasAttribute('contenteditable') && el.getAttribute('contenteditable') !== 'false') return true;
    if (el.hasAttribute('tabindex') && el.getAttribute('tabindex') !== '-1') {
      const style = window.getComputedStyle(el);
      if (style.cursor === 'pointer') return true;
    }
    return false;
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

  function processNode(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent.trim();
      if (!text) return null;
      return {
        type: 'TEXT',
        text: text.substring(0, 200),
        isVisible: true,
      };
    }

    if (node.nodeType !== Node.ELEMENT_NODE) return null;
    const el = node;
    const tag = el.tagName.toLowerCase();

    // Skip script, style, noscript, svg, etc.
    if (['script', 'style', 'noscript', 'svg', 'path', 'link', 'meta', 'head'].includes(tag)) {
      return null;
    }

    const visible = isVisible(el);
    if (!visible) return null;

    const interactive = isInteractive(el);
    const inViewport = isInViewport(el, viewportExpansion);

    // Collect attributes
    const attributes = {};
    for (const attr of INCLUDE_ATTRIBUTES) {
      const val = el.getAttribute(attr);
      if (val !== null && val !== '') {
        attributes[attr] = val.substring(0, 100);
      }
    }

    // Process children
    const children = [];
    for (const child of el.childNodes) {
      const processed = processNode(child);
      if (processed) children.push(processed);
    }

    // Also traverse shadow DOM
    if (el.shadowRoot) {
      for (const child of el.shadowRoot.childNodes) {
        const processed = processNode(child);
        if (processed) children.push(processed);
      }
    }

    const result = {
      type: 'ELEMENT',
      tagName: tag,
      xpath: getXPath(el),
      attributes: attributes,
      text: getTextContent(el),
      isInteractive: interactive,
      isVisible: visible,
      isInViewport: inViewport,
      highlightIndex: null,
      children: children,
    };

    // Assign index to interactive elements
    if (interactive && (inViewport || viewportExpansion > 0)) {
      result.highlightIndex = highlightIndex++;
    }

    return result;
  }

  // Build the tree from document.body
  const tree = processNode(document.body);

  // Build selector map (highlightIndex -> element info with CSS selector)
  const selectorEntries = [];
  function collectSelectors(node) {
    if (!node) return;
    if (node.highlightIndex !== null && node.highlightIndex !== undefined) {
      selectorEntries.push({
        index: node.highlightIndex,
        xpath: node.xpath,
        tagName: node.tagName,
        attributes: node.attributes,
        text: node.text,
      });
    }
    if (node.children) {
      for (const child of node.children) {
        collectSelectors(child);
      }
    }
  }
  collectSelectors(tree);

  return {
    tree: tree,
    selectorEntries: selectorEntries,
    url: window.location.href,
    title: document.title,
    scrollY: Math.round(window.scrollY),
    scrollHeight: Math.round(document.documentElement.scrollHeight),
    viewportHeight: Math.round(window.innerHeight),
  };
})
`;
}
