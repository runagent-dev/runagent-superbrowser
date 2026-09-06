// Click recorder shared by every fixture page. Records the deepest element
// that received each click (capture phase, so handlers cannot swallow it) —
// the runner compares the recorded id with the item's intended target.
(function () {
  window.__clicks = [];
  function idOf(el) {
    while (el && el !== document.documentElement) {
      if (el.id) return el.id;
      el = el.parentElement;
    }
    return '';
  }
  document.addEventListener('click', function (ev) {
    var t = ev.target;
    window.__clicks.push({
      id: idOf(t),
      tag: (t && t.tagName || '').toLowerCase(),
      text: ((t && t.textContent) || '').trim().slice(0, 40),
      x: ev.clientX, y: ev.clientY, isTrusted: ev.isTrusted, t: Date.now(),
    });
  }, true);
  // Every intended control does something visible so a human can verify the
  // fixtures by hand too.
  document.addEventListener('click', function (ev) {
    var el = ev.target.closest('[data-toggle]');
    if (el) {
      var target = document.getElementById(el.getAttribute('data-toggle'));
      if (target) target.hidden = !target.hidden;
      var exp = el.getAttribute('aria-expanded');
      if (exp !== null) el.setAttribute('aria-expanded', exp === 'true' ? 'false' : 'true');
    }
  });
})();
