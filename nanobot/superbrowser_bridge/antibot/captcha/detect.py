"""Captcha detection on a patchright page.

Walks the DOM for known captcha signatures (reCAPTCHA v2, hCaptcha,
Cloudflare Turnstile, slider widgets, generic "verify you are human"
challenges). Pattern ported from `src/browser/captcha/registry.ts` +
`src/browser/captcha/orchestrator.ts`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Optional

logger = logging.getLogger(__name__)

CaptchaType = Literal[
    "recaptcha-v2", "hcaptcha", "turnstile",
    "slider", "grid", "text_captcha", "cf_interstitial",
    "generic", "none",
]


@dataclass
class CaptchaInfo:
    type: CaptchaType = "none"
    present: bool = False
    site_key: str = ""
    widget_selector: str = ""
    widget_bbox: Optional[list[float]] = None  # [x0, y0, x1, y1]
    frame_url: str = ""
    notes: list[str] = field(default_factory=list)
    # For text_captcha: the input element the user must type into.
    # Pixel rect [x0, y0, x1, y1] or None. Vision can re-detect but
    # surfacing it from the DOM gives the solver a reliable anchor.
    input_bbox: Optional[list[float]] = None


_DETECT_JS = """
() => {
  const out = {type: 'none', present: false, site_key: '', widget_selector: '',
              widget_bbox: null, input_bbox: null, frame_url: '', notes: []};

  const pickBbox = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return null;
    return [Math.round(r.left), Math.round(r.top),
            Math.round(r.right), Math.round(r.bottom)];
  };

  // --- reCAPTCHA v2 ---------------------------------------------------
  const rc = document.querySelector('.g-recaptcha, iframe[src*="recaptcha/api2/anchor"]');
  if (rc) {
    out.type = 'recaptcha-v2';
    out.present = true;
    const gr = document.querySelector('.g-recaptcha');
    out.site_key = (gr && (gr.getAttribute('data-sitekey') || '')) || '';
    out.widget_selector = '.g-recaptcha, iframe[src*="recaptcha/api2"]';
    out.widget_bbox = pickBbox(gr || rc);
    out.frame_url = (rc.tagName === 'IFRAME' ? rc.src : '');
  }

  // --- hCaptcha -------------------------------------------------------
  if (!out.present) {
    const hc = document.querySelector('.h-captcha, iframe[src*="hcaptcha.com"]');
    if (hc) {
      out.type = 'hcaptcha';
      out.present = true;
      const hh = document.querySelector('.h-captcha');
      out.site_key = (hh && (hh.getAttribute('data-sitekey') || '')) || '';
      out.widget_selector = '.h-captcha, iframe[src*="hcaptcha.com"]';
      out.widget_bbox = pickBbox(hh || hc);
      out.frame_url = (hc.tagName === 'IFRAME' ? hc.src : '');
    }
  }

  // --- Cloudflare Turnstile -------------------------------------------
  if (!out.present) {
    const ts = document.querySelector('.cf-turnstile, iframe[src*="challenges.cloudflare.com/turnstile"]');
    if (ts) {
      out.type = 'turnstile';
      out.present = true;
      const tt = document.querySelector('.cf-turnstile');
      out.site_key = (tt && (tt.getAttribute('data-sitekey') || '')) || '';
      out.widget_selector = '.cf-turnstile, iframe[src*="challenges.cloudflare.com/turnstile"]';
      out.widget_bbox = pickBbox(tt || ts);
      out.frame_url = (ts.tagName === 'IFRAME' ? ts.src : '');
    }
  }

  // --- Slider puzzle --------------------------------------------------
  if (!out.present) {
    const slider = document.querySelector(
      '[class*="slider"][class*="captcha"], [class*="slide"][class*="verify"], ' +
      '[id*="slide-captcha"], [aria-label*="slider"][role="slider"]'
    );
    if (slider) {
      out.type = 'slider';
      out.present = true;
      out.widget_selector = slider.className ? '.' + slider.className.split(/\\s+/)[0] : '';
      out.widget_bbox = pickBbox(slider);
    }
  }

  // --- Grid (tile-selection) captchas ---------------------------------
  if (!out.present) {
    const tileGrid = document.querySelector(
      '[class*="tile-grid"], [class*="image-grid"][class*="captcha"], ' +
      '[class*="select-all"][class*="image"]'
    );
    if (tileGrid) {
      out.type = 'grid';
      out.present = true;
      out.widget_bbox = pickBbox(tileGrid);
    }
  }

  // --- Text-from-image captchas (classic "type the distorted word") ---
  // Heuristic: an IMG whose src / alt / id / name contains captcha-ish
  // keywords, paired with a nearby text input. Keep the image size gate
  // generous (distorted-word captchas run 60-300px wide, 20-90px tall).
  if (!out.present) {
    const capRe = /captcha|verify|security|challenge|securecode|securimage|verif(?:ication)?/i;
    const imgs = document.querySelectorAll('img');
    let bestPair = null;
    let bestScore = -1;
    for (const img of imgs) {
      const sig = [img.src || '', img.alt || '', img.id || '',
                   img.name || '', img.className || ''].join(' ');
      if (!capRe.test(sig)) continue;
      const ir = img.getBoundingClientRect();
      if (ir.width < 40 || ir.height < 15 || ir.width > 600) continue;
      // Find the nearest visible text input — not hidden, not submit,
      // not password, reasonable size.
      const inputs = document.querySelectorAll(
        'input[type="text"], input:not([type]), input[type="tel"], ' +
        'input[type="number"]'
      );
      let bestInput = null;
      let bestDist = Infinity;
      for (const inp of inputs) {
        if (inp.disabled || inp.readOnly) continue;
        const r = inp.getBoundingClientRect();
        if (r.width < 30 || r.height < 14) continue;
        const dx = (r.left + r.width/2) - (ir.left + ir.width/2);
        const dy = (r.top + r.height/2) - (ir.top + ir.height/2);
        const dist = Math.sqrt(dx*dx + dy*dy);
        if (dist < bestDist && dist < 400) {
          bestDist = dist;
          bestInput = inp;
        }
      }
      if (!bestInput) continue;
      // Score: prefer close inputs + strong keyword presence on the img.
      const kwStrength = (sig.match(capRe) || []).length;
      const score = kwStrength * 100 - bestDist;
      if (score > bestScore) {
        bestScore = score;
        bestPair = {img, input: bestInput};
      }
    }
    if (bestPair) {
      const ir = bestPair.img.getBoundingClientRect();
      const tr = bestPair.input.getBoundingClientRect();
      out.type = 'text_captcha';
      out.present = true;
      out.widget_selector = 'img[src*="captcha"], img[alt*="captcha"]';
      out.widget_bbox = [
        Math.round(Math.min(ir.left, tr.left)),
        Math.round(Math.min(ir.top, tr.top)),
        Math.round(Math.max(ir.right, tr.right)),
        Math.round(Math.max(ir.bottom, tr.bottom)),
      ];
      out.input_bbox = [
        Math.round(tr.left), Math.round(tr.top),
        Math.round(tr.right), Math.round(tr.bottom),
      ];
      out.notes.push('text_captcha:image+input_pair');
    }
  }

  // --- Cloudflare Managed Challenge interstitial ----------------------
  // The whole page IS the challenge (not a widget embed like Turnstile).
  // Multi-signal match: structural elements + challenge-platform script +
  // title/body strings. Any two of these three signals = strong match.
  if (!out.present) {
    const hasStructural = !!document.querySelector(
      '#challenge-running, #challenge-form, #challenge-error-title, ' +
      '#challenge-stage, .main-wrapper #cf-wrapper'
    );
    const hasPlatformScript = !!document.querySelector(
      'script[src*="/cdn-cgi/challenge-platform/"], ' +
      'script[src*="challenges.cloudflare.com/cdn-cgi/"]'
    );
    const title = ((document.title || '') + '').toLowerCase();
    const body = ((document.body && document.body.innerText) || '').toLowerCase();
    const titleMatch = (
      title.indexOf('just a moment') !== -1
      || title.indexOf('attention required') !== -1
      || title.indexOf('one moment') !== -1
    );
    const bodyMatch = (
      body.indexOf('performing security verification') !== -1
      || body.indexOf('checking your browser') !== -1
      || body.indexOf('verify you are human') !== -1
      || body.indexOf('verifying you are human') !== -1
    );
    let signals = 0;
    if (hasStructural) signals++;
    if (hasPlatformScript) signals++;
    if (titleMatch) signals++;
    if (bodyMatch) signals++;
    if (signals >= 2) {
      out.type = 'cf_interstitial';
      out.present = true;
      out.widget_selector = '#challenge-running, #cf-wrapper, body';
      // The whole viewport is the challenge — bbox covers the visible
      // area so a solver that needs a widget bbox has something to read.
      out.widget_bbox = [
        0, 0,
        Math.round(window.innerWidth || 1024),
        Math.round(window.innerHeight || 768),
      ];
      if (hasStructural) out.notes.push('cf_signal:structural');
      if (hasPlatformScript) out.notes.push('cf_signal:challenge-platform');
      if (titleMatch) out.notes.push('cf_signal:title');
      if (bodyMatch) out.notes.push('cf_signal:body');
    }
  }

  // --- Generic text signals (fallback) --------------------------------
  if (!out.present) {
    const bodyText = (document.body ? document.body.innerText : '') || '';
    const needles = [
      'verify you are human', 'checking your browser', 'just a moment',
      'complete the captcha', 'i\\'m not a robot', 'prove you are not a robot',
    ];
    for (const n of needles) {
      if (bodyText.toLowerCase().indexOf(n) !== -1) {
        out.type = 'generic';
        out.present = true;
        out.notes.push('text_signal:' + n);
        break;
      }
    }
  }

  return out;
}
"""


async def detect(t3manager, session_id: str) -> CaptchaInfo:
    """Run the detection script against a t3 page."""
    try:
        raw = await t3manager.evaluate(session_id, _DETECT_JS)
    except Exception as exc:
        logger.debug("detect eval failed: %s", exc)
        return CaptchaInfo()
    if not isinstance(raw, dict):
        return CaptchaInfo()
    return CaptchaInfo(
        type=raw.get("type", "none"),  # type: ignore[arg-type]
        present=bool(raw.get("present", False)),
        site_key=str(raw.get("site_key", "") or ""),
        widget_selector=str(raw.get("widget_selector", "") or ""),
        widget_bbox=raw.get("widget_bbox") or None,
        input_bbox=raw.get("input_bbox") or None,
        frame_url=str(raw.get("frame_url", "") or ""),
        notes=list(raw.get("notes") or []),
    )
