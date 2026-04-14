"""Prompt builders for the vision preprocessor.

Intent-aware shaping: the system prompt stays constant (defines the JSON
contract + response ethics), the user prompt is bucketed on intent so the
model focuses on the right part of the screen.
"""

from __future__ import annotations

SYSTEM_PROMPT = """You are a machine-vision agent embedded between a web browser \
and a reasoning agent. You receive one screenshot at a time and return a single \
JSON object with this exact shape:

{
  "summary": "1-3 sentence description of what is visible on the page",
  "relevant_text": "concatenated headings + labels + salient body text (verbatim)",
  "bboxes": [
    {
      "label": "<short label, e.g. 'Sign in with Google'>",
      "x": <int>, "y": <int>, "w": <int>, "h": <int>,
      "clickable": <bool>,
      "role": "button|link|input|checkbox|captcha_tile|captcha_widget|slider_handle|image|text_block|other",
      "confidence": <float 0..1>,
      "intent_relevant": <bool>
    }
  ],
  "flags": {
    "captcha_present": <bool>,
    "captcha_type": "recaptcha|hcaptcha|turnstile|slider|image|text"|null,
    "captcha_widget_bbox": { ... } | null,
    "modal_open": <bool>,
    "error_banner": "<text>" | null,
    "loading": <bool>,
    "login_wall": <bool>
  }
}

Hard rules:
- Coordinates are CSS pixels of the rendered page (top-left origin).
  They are integers. Width and height are positive.
- Do NOT hallucinate elements. If you cannot read it, leave it out.
- `intent_relevant` is true for bboxes that most directly serve the user's
  stated intent. Be conservative — usually 0 to 3 regions qualify.
- For captcha tiles, emit one bbox per tile with role='captcha_tile' and
  label like 'tile 1,1'. For sliders, include role='slider_handle' for the
  draggable handle and role='captcha_widget' for the outer track.
- Output ONLY the JSON. No prose, no markdown fences, no commentary.
"""


def intent_bucket(intent: str) -> str:
    """Collapse free-form intent into one of 4 coarse buckets for caching.

    Slight phrasing variation ("check login state" vs "verify login succeeded")
    should share a cache entry, but "observe" and "solve captcha" must not.
    """
    s = (intent or "").lower()
    if any(k in s for k in ("captcha", "challenge", "prove you")):
        return "solve_captcha"
    if any(k in s for k in ("verify", "confirm", "check if", "did it", "outcome")):
        return "verify_action"
    if any(k in s for k in ("observe", "what", "read", "describe")):
        return "observe"
    return "other"


def build_user_prompt(intent: str, url: str | None) -> str:
    """The per-call user message — describes context and emphasises intent."""
    bucket = intent_bucket(intent)
    context = f"Page URL: {url}\n" if url else ""

    if bucket == "solve_captcha":
        specific = (
            "Focus on the captcha challenge. List the outer widget as "
            "captcha_widget, each selectable tile as captcha_tile (left-to-"
            "right, top-to-bottom), slider handles as slider_handle. Set "
            "captcha_present=true and captcha_type to the best match. "
            "intent_relevant=true for every element needed to solve."
        )
    elif bucket == "verify_action":
        specific = (
            "The reasoning agent just performed an action. Judge whether "
            "the expected outcome is visible. Populate flags carefully "
            "(modal_open, error_banner, loading, login_wall). "
            "intent_relevant=true for indicators that confirm or deny the "
            "outcome (success banner, error banner, new element, etc.)."
        )
    elif bucket == "observe":
        specific = (
            "Give a faithful description of the page. List interactive "
            "elements (buttons, links, inputs, checkboxes). intent_relevant "
            "is true only for elements central to the page's purpose."
        )
    else:
        specific = (
            "Describe what is visible and list interactive elements. "
            "Mark bboxes intent_relevant=true when they serve this intent: "
            f"\"{intent}\"."
        )

    return (
        f"{context}Intent: {intent}\n\n{specific}\n\n"
        "Return ONLY the JSON object described in the system message."
    )
