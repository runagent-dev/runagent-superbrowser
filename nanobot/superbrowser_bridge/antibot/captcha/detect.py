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
    "slider", "grid", "generic", "none",
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


_DETECT_JS = """
() => {
  const out = {type: 'none', present: false, site_key: '', widget_selector: '',
              widget_bbox: null, frame_url: '', notes: []};

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
        frame_url=str(raw.get("frame_url", "") or ""),
        notes=list(raw.get("notes") or []),
    )
