"""Module-level constants for the session-tools package.

Extracted from the legacy session_tools.py monolith. These values are
imported by every other submodule (and by external callers via the
package __init__ re-export).
"""

from __future__ import annotations

import os

SUPERBROWSER_URL = "http://localhost:3100"
SCREENSHOT_DIR = os.environ.get("SUPERBROWSER_SCREENSHOT_DIR", "/tmp/superbrowser/screenshots")
_ATOMIC_FIX_TEXT_JS = """
(() => {
  const x = __TARGET_X__, y = __TARGET_Y__, target = __TARGET_TEXT__;
  const el = document.elementFromPoint(x, y);
  if (!el) return {ok: false, reason: 'no_element'};
  const tag = el.tagName.toLowerCase();
  const isInput = tag === 'input' || tag === 'textarea';
  const isEditable = !!el.isContentEditable;
  if (!isInput && !isEditable) {
    return {ok: false, reason: 'not_input', tag};
  }
  const attrLabel = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || '';
  const attrName = el.name || '';
  const attrAutocomplete = el.getAttribute('autocomplete') || '';
  const attrInputType = isInput ? (el.getAttribute('type') || 'text').toLowerCase() : '';
  if (isInput) {
    if (['file','checkbox','radio','hidden','submit','button',
         'image','reset','range','color'].includes(attrInputType)) {
      return {ok: false, reason: 'non_text_input', tag, input_type: attrInputType};
    }
  }
  const before = isInput ? (el.value || '') : (el.innerText || '');
  if (before === target) {
    return {ok: true, before, after: target, changed: false, tag,
            label: attrLabel, name: attrName, autocomplete: attrAutocomplete,
            input_type: attrInputType};
  }
  try { el.focus(); } catch (_) {}
  try {
    if (isInput) {
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype
                                       : HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && desc.set) {
        desc.set.call(el, target);
        el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: target}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      } else {
        el.value = target;
      }
    } else if (isEditable) {
      el.innerText = target;
      el.dispatchEvent(new InputEvent('input', {bubbles: true}));
    }
  } catch (e) {
    return {ok: false, reason: 'exception', error: String(e).slice(0, 120), before, tag};
  }
  const after = isInput ? (el.value || '') : (el.innerText || '');
  return {ok: after === target, before, after, changed: before !== after, tag,
          label: attrLabel, name: attrName, autocomplete: attrAutocomplete,
          input_type: attrInputType};
})()
"""

BLOCKED_BROWSER_OPEN_HARD_STOP = 3
_CURSOR_TOOL_NAMES = frozenset({
    "browser_click",
    "browser_click_at",
    "browser_click_selector",
    "browser_type",
    "browser_type_at",
    "browser_fix_text_at",
    "browser_keys",
    "browser_drag",
    "browser_drag_slider_until",
    "browser_select",
    "browser_semantic_click",
    "browser_semantic_type",
})

_CAPTCHA_KEYWORDS = (
    "captcha", "recaptcha", "hcaptcha", "turnstile", "cloudflare",
    "verify you are human", "prove you are not a robot", "slider puzzle",
    "click all images", "select all", "drag the", "i'm not a robot",
)
_HARD_DOMAINS = (
    "apartments.com", "zillow.com", "ticketmaster.com", "nytimes.com",
    "linkedin.com", "instagram.com", "facebook.com",
)
_CONTENT_HASH_LEN = 500
_SUBMIT_KEYWORDS = ("verify", "submit", "next", "continue", "check", "done", "i'm done")
RESUMPTION_PATH = "/tmp/superbrowser/resumption.json"
RESUMPTION_TTL_SEC = 300
