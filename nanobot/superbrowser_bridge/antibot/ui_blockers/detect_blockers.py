"""DOM-side blocker detection (cookie consent, newsletter popups, generic modals).

Shape mirrors `antibot/captcha/detect.py` deliberately: one JS blob that
walks the DOM once and returns a ranked list of hits, plus a thin async
wrapper that the bridge can call during screenshot preprocessing.

This is the "DOM-derived" half of the two-signal blocker pipeline — the
other half is the vision agent's `scene.layers`. Having both lets the
planner cross-validate before committing a dismiss click: if vision says
"blocker" but DOM doesn't, or vice versa, the planner trusts the
agreement case and falls back conservatively when they disagree.

Non-goals:
    * Captcha detection — already covered by `antibot/captcha/detect.py`;
      this module skips any element inside a known captcha signature.
    * Paywalls / age gates — deferred per the approved plan.
    * Destructive removal — we report, we do NOT delete. The read-only
      tier-3 fetch path in `fetch_undetected.py` keeps its `kill()`-based
      overlay stripper because the fetch never interacts; interactive
      sessions must dismiss via clicks so the site's analytics/consent
      state stays coherent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Optional

logger = logging.getLogger(__name__)


BlockerType = Literal["cookie", "newsletter", "generic_modal", "none"]
BlockerSeverity = Literal["hard", "soft"]


@dataclass
class BlockerInfo:
    """One detected blocker. Pixel coords are CSS pixels in viewport space."""

    type: BlockerType = "none"
    severity: BlockerSeverity = "soft"
    widget_bbox: Optional[list[float]] = None      # [x0, y0, x1, y1]
    dismiss_bbox: Optional[list[float]] = None     # best-guess close/accept btn
    dismiss_label: str = ""
    dismiss_selector: str = ""                      # css selector for the dismiss el
    confidence: float = 0.0                         # 0.0..1.0
    notes: list[str] = field(default_factory=list)


# Single JS blob — one DOM walk, returns ranked list. Keeping everything
# in one eval limits cross-context round-trips (patchright evaluate is
# the dominant cost).
_DETECT_JS = """
() => {
  const OUT = [];
  const VIEW_W = window.innerWidth || 1024;
  const VIEW_H = window.innerHeight || 768;
  const VIEW_AREA = VIEW_W * VIEW_H;

  const pickBbox = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return null;
    return [Math.round(r.left), Math.round(r.top),
            Math.round(r.right), Math.round(r.bottom)];
  };

  const cssPath = (el) => {
    if (!el) return '';
    if (el.id) return '#' + el.id;
    const cls = (el.className && typeof el.className === 'string')
      ? el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
    return (el.tagName || '').toLowerCase() + (cls ? '.' + cls : '');
  };

  const visible = (el) => {
    if (!el) return false;
    const cs = getComputedStyle(el);
    if (!cs) return false;
    if (cs.visibility === 'hidden' || cs.display === 'none') return false;
    if (parseFloat(cs.opacity || '1') < 0.1) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 20 && r.height >= 20;
  };

  // Is this element inside a known captcha widget? If yes we skip it so
  // the captcha pipeline stays authoritative.
  const CAPTCHA_SELS = [
    '.g-recaptcha', '.h-captcha', '.cf-turnstile',
    'iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]',
    'iframe[src*="turnstile"]', 'iframe[src*="challenges.cloudflare.com"]',
    '[class*="captcha" i]',
  ];
  const insideCaptcha = (el) => {
    let n = el;
    while (n && n !== document.body) {
      for (const sel of CAPTCHA_SELS) {
        try { if (n.matches && n.matches(sel)) return true; } catch {}
      }
      n = n.parentElement;
    }
    return false;
  };

  const DISMISS_RE = /accept|agree|consent|got it|ok(ay)?|allow|continue|i understand/i;
  const REJECT_RE  = /reject|decline|deny|refuse|opt.?out/i;
  const CLOSE_RE   = /close|dismiss|no thanks|not now|maybe later|×|✕|✖/i;

  // Prefer Accept over Reject over Close. Order the candidates so the
  // same walk handles all three.
  const findDismissBtn = (root) => {
    const btns = root.querySelectorAll(
      'button, a, [role=button], input[type=button], input[type=submit]'
    );
    let best = null;
    let bestScore = -1;
    for (const b of btns) {
      if (!visible(b)) continue;
      // Consider ALL label sources, not just the first truthy one —
      // a button with text "×" and aria-label="close" needs the aria
      // to classify as CLOSE. Pre-concat, score against the union,
      // display back the most human-readable single source.
      const srcs = [
        (b.innerText || '').trim(),
        (b.textContent || '').trim(),
        (b.value || '').trim(),
        (b.getAttribute('aria-label') || '').trim(),
        (b.getAttribute('title') || '').trim(),
      ].filter(Boolean);
      if (!srcs.length) continue;
      const unioned = srcs.join(' ').slice(0, 160);
      let score = 0;
      if (DISMISS_RE.test(unioned)) score = 3;
      else if (REJECT_RE.test(unioned)) score = 2;
      else if (CLOSE_RE.test(unioned)) score = 1;
      else continue;
      // Human label: prefer the aria-label/title (more descriptive)
      // when the visible text is a bare glyph or very short.
      const visibleText = srcs[0] || '';
      let short = visibleText;
      if (visibleText.length <= 2) {
        const aria = b.getAttribute('aria-label') || b.getAttribute('title') || '';
        if (aria && aria.length > visibleText.length) short = aria;
      }
      short = short.slice(0, 60) || unioned.slice(0, 60);
      if (score > bestScore) {
        bestScore = score;
        best = {el: b, label: short};
      }
    }
    return best;
  };

  const pushHit = (el, type, severity, confidence, notes) => {
    if (!visible(el)) return;
    if (insideCaptcha(el)) return;
    const wb = pickBbox(el);
    if (!wb) return;
    const d = findDismissBtn(el);
    OUT.push({
      type, severity, confidence,
      widget_bbox: wb,
      dismiss_bbox: d ? pickBbox(d.el) : null,
      dismiss_label: d ? d.label : '',
      dismiss_selector: d ? cssPath(d.el) : '',
      notes: notes || [],
    });
  };

  // --- 1. Cookie / GDPR / consent banners ---------------------------
  // Port seed list from antibot/fetch_undetected.py:38-45, extended with
  // the OneTrust / Cookiebot / TrustArc selectors that cover ~80% of
  // real-world banners.
  const COOKIE_SELECTORS = [
    '#onetrust-banner-sdk', '#onetrust-consent-sdk',
    '[id^="onetrust-"]',
    '#CybotCookiebotDialog', '[id^="CybotCookiebot"]',
    '.cky-consent-container', '[class^="cky-"]',
    '#cmpwrapper', '#cmpcontainer',
    '#truste-consent-track', '#truste-consent-content',
    '[id*="cookie-banner" i]', '[class*="cookie-banner" i]',
    '[id*="consent-banner" i]', '[class*="consent-banner" i]',
    '[id*="gdpr" i]', '[class*="gdpr" i]',
    '[aria-label*="cookie" i]', '[aria-label*="consent" i]',
    '[role="dialog"][aria-label*="cookie" i]',
    '[role="dialog"][aria-label*="consent" i]',
  ];
  const cookieSeen = new Set();
  for (const sel of COOKIE_SELECTORS) {
    let nodes;
    try { nodes = document.querySelectorAll(sel); }
    catch (e) { continue; }
    for (const el of nodes) {
      if (cookieSeen.has(el)) continue;
      cookieSeen.add(el);
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      // Reject tiny link-style "manage cookies" footers — they don't block.
      if (r.width * r.height < VIEW_AREA * 0.03) continue;
      // A hard cookie wall is a fixed/sticky overlay OR lives on
      // body with overflow locked. Soft = the banner at page bottom
      // that doesn't dim content.
      const isFixed = (cs.position === 'fixed' || cs.position === 'sticky');
      const bodyLocked = getComputedStyle(document.body).overflow === 'hidden';
      const severity = (isFixed && (bodyLocked || r.height > VIEW_H * 0.3))
        ? 'hard' : 'soft';
      pushHit(el, 'cookie', severity, 0.9, ['match:' + sel.slice(0, 40)]);
    }
  }

  // --- 2. Newsletter / email capture popups -------------------------
  // Heuristic: a fixed-position container with an email input and a
  // visible close icon, covering ≥25% of viewport.
  const NEWSLETTER_INPUT_SELS = [
    'input[type=email]',
    'input[name*="email" i]',
    'input[placeholder*="email" i]',
  ];
  const newsletterSeen = new Set();
  for (const sel of NEWSLETTER_INPUT_SELS) {
    let inputs;
    try { inputs = document.querySelectorAll(sel); }
    catch (e) { continue; }
    for (const inp of inputs) {
      if (!visible(inp)) continue;
      // Walk up to find the fixed/sticky container.
      let container = inp;
      let hops = 0;
      while (container && container !== document.body && hops < 8) {
        const cs = getComputedStyle(container);
        if (cs && (cs.position === 'fixed' || cs.position === 'sticky')) {
          break;
        }
        container = container.parentElement;
        hops++;
      }
      if (!container || container === document.body) continue;
      if (newsletterSeen.has(container)) continue;
      newsletterSeen.add(container);
      const r = container.getBoundingClientRect();
      // Cover ≥20% of viewport in EITHER dimension to count as a popup.
      if (r.width < VIEW_W * 0.4 || r.height < VIEW_H * 0.15) continue;
      // Avoid matching on site-wide footers that happen to have an
      // email input — reject if the container is already a known cookie
      // banner.
      if (cookieSeen.has(container)) continue;
      pushHit(container, 'newsletter', 'soft', 0.7,
              ['input_sel:' + sel]);
    }
  }

  // --- 3. Generic modal fallback -----------------------------------
  // Any fixed/absolute element at z>1000 covering ≥30% viewport with a
  // visible close button. Deliberately last so specific hits above win.
  const seen = new Set([...cookieSeen, ...newsletterSeen]);
  const allFixed = document.querySelectorAll(
    '[role="dialog"], [aria-modal="true"], [role="alertdialog"], div, section'
  );
  for (const el of allFixed) {
    if (seen.has(el)) continue;
    const cs = getComputedStyle(el);
    if (!cs) continue;
    const pos = cs.position;
    if (pos !== 'fixed' && pos !== 'absolute') continue;
    const z = parseInt(cs.zIndex, 10);
    if (!(z > 1000)) continue;
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    const area = r.width * r.height;
    if (area < VIEW_AREA * 0.3) continue;
    if (area > VIEW_AREA * 0.98) continue;  // probably a layout wrapper
    const d = findDismissBtn(el);
    if (!d) continue;  // no way to dismiss -> not actionable as a blocker
    seen.add(el);
    const bodyLocked = getComputedStyle(document.body).overflow === 'hidden';
    const severity = bodyLocked ? 'hard' : 'soft';
    pushHit(el, 'generic_modal', severity, 0.5,
            ['z:' + z, 'area_frac:' + (area / VIEW_AREA).toFixed(2)]);
  }

  // Rank: hard before soft, cookies before newsletters before generic,
  // higher confidence first.
  const typeRank = {cookie: 0, newsletter: 1, generic_modal: 2, none: 3};
  OUT.sort((a, b) => {
    if ((a.severity === 'hard') !== (b.severity === 'hard')) {
      return a.severity === 'hard' ? -1 : 1;
    }
    if (typeRank[a.type] !== typeRank[b.type]) {
      return typeRank[a.type] - typeRank[b.type];
    }
    return b.confidence - a.confidence;
  });
  // Cap at 6 — never going to dismiss more than a handful before the
  // LLM re-plans, and unbounded lists bloat caption text.
  return OUT.slice(0, 6);
}
"""


async def detect(t3manager, session_id: str) -> list[BlockerInfo]:
    """Run the blocker walk against a t3 page. Returns ranked hits (top-most first).

    Empty list when no blocker passes the heuristics. Errors (evaluate
    failures, page closed mid-call) degrade to an empty list rather than
    raising — this runs in the hot path of `browser_screenshot` and a
    transient failure must never block the screenshot.
    """
    try:
        raw = await t3manager.evaluate(session_id, _DETECT_JS)
    except Exception as exc:
        logger.debug("blocker detect eval failed: %s", exc)
        return []
    if not isinstance(raw, list):
        return []
    out: list[BlockerInfo] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t not in ("cookie", "newsletter", "generic_modal"):
            continue
        sev = item.get("severity")
        if sev not in ("hard", "soft"):
            sev = "soft"
        try:
            conf = float(item.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        out.append(BlockerInfo(
            type=t,  # type: ignore[arg-type]
            severity=sev,  # type: ignore[arg-type]
            widget_bbox=item.get("widget_bbox") or None,
            dismiss_bbox=item.get("dismiss_bbox") or None,
            dismiss_label=str(item.get("dismiss_label", "") or "")[:120],
            dismiss_selector=str(item.get("dismiss_selector", "") or "")[:200],
            confidence=max(0.0, min(1.0, conf)),
            notes=list(item.get("notes") or []),
        ))
    return out
