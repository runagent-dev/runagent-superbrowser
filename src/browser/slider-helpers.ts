// Shadow-DOM-piercing JS helpers used by setSlider, listSliderHandles,
// and dragSliderUntil in page.ts. Concatenated into every frame.evaluate()
// call so the helpers are in scope for the IIFE that follows.
//
// Closed shadow roots are inaccessible by spec — el.shadowRoot is null
// for those, so we only walk open roots. <slot>-reparenting is irrelevant
// here: slider thumbs live INSIDE a shadow root, not slotted from outside.

export const SHADOW_DOM_HELPERS_SRC = `
function __sb_queryDeep(root, sel) {
  if (!root) return null;
  var direct = (root.querySelector ? root.querySelector(sel) : null);
  if (direct) return direct;
  var queue = [root];
  while (queue.length) {
    var node = queue.shift();
    var children = (node.children || []);
    for (var i = 0; i < children.length; i++) {
      var c = children[i];
      if (c.shadowRoot) {
        var hit = c.shadowRoot.querySelector(sel);
        if (hit) return hit;
        queue.push(c.shadowRoot);
      }
      if (c.children && c.children.length) queue.push(c);
    }
  }
  return null;
}

function __sb_queryAllDeep(root, sel) {
  if (!root) return [];
  var out = [];
  var seen = (typeof Set === 'function') ? new Set() : null;
  function pushUnique(el) {
    if (seen) { if (seen.has(el)) return; seen.add(el); }
    out.push(el);
  }
  function collect(scope) {
    if (!scope || !scope.querySelectorAll) return;
    var hits = scope.querySelectorAll(sel);
    for (var i = 0; i < hits.length; i++) pushUnique(hits[i]);
  }
  collect(root);
  var queue = [root];
  while (queue.length) {
    var node = queue.shift();
    var children = (node.children || []);
    for (var i = 0; i < children.length; i++) {
      var c = children[i];
      if (c.shadowRoot) {
        collect(c.shadowRoot);
        queue.push(c.shadowRoot);
      }
      if (c.children && c.children.length) queue.push(c);
    }
  }
  return out;
}

function __sb_walkDeepElements(root, visit) {
  if (!root) return;
  var queue = [root];
  while (queue.length) {
    var node = queue.shift();
    var children = (node.children || []);
    for (var i = 0; i < children.length; i++) {
      var c = children[i];
      var stop = visit(c);
      if (stop === false) return;
      if (c.shadowRoot) queue.push(c.shadowRoot);
      if (c.children && c.children.length) queue.push(c);
    }
  }
}

function __sb_dispatchHostSignal(el, eventNames) {
  // After mutating a shadow-rooted form input, fire the same events on
  // the shadow host so React/Lit listeners on the custom element wrapper
  // see the change. Walks up nested shadow roots so deeply-nested inputs
  // signal every ancestor host.
  try {
    var current = el;
    var guard = 0;
    while (current && guard++ < 8) {
      var root = current.getRootNode ? current.getRootNode() : null;
      if (!root || root === document || !root.host) break;
      for (var i = 0; i < eventNames.length; i++) {
        try {
          root.host.dispatchEvent(new Event(eventNames[i], { bubbles: true }));
        } catch (e) {}
      }
      current = root.host;
    }
  } catch (e) {}
}
`;
