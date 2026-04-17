"""Prompt builders for the vision preprocessor.

Intent-aware shaping: the system prompt stays constant (defines the JSON
contract + response ethics), the user prompt is bucketed on intent so the
model focuses on the right part of the screen.

Bbox format is Gemini's native `box_2d` — `[ymin, xmin, ymax, xmax]`
integers normalized to `[0, 1000]` against the screenshot. Models are
trained directly on this representation and emit it far more accurately
than absolute pixel coordinates at arbitrary viewport sizes. The Python
bridge denormalizes to CSS pixels before any click is dispatched.
"""

from __future__ import annotations

SYSTEM_PROMPT = """You are a machine-vision agent embedded between a web browser \
and a reasoning agent. You are the "eyes" — you see the page, and the browser \
tools are the "hands". You receive one screenshot at a time and return a single \
JSON object with this exact shape:

{
  "summary": "1-3 sentence description of what is visible on the page",
  "relevant_text": "concatenated headings + labels + salient body text (verbatim)",
  "page_type": "captcha_challenge|login_form|signup_form|search_results|product_listing|product_detail|checkout_form|cart|home_landing|article|map_or_booking|dashboard|error_page|other",
  "bboxes": [
    {
      "label": "<short label, e.g. 'Sign in with Google'>",
      "box_2d": [ymin, xmin, ymax, xmax],
      "clickable": <bool>,
      "role": "button|link|input|checkbox|captcha_tile|captcha_widget|slider_handle|image|text_block|other",
      "confidence": <float 0..1>,
      "intent_relevant": <bool>
    }
  ],
  "flags": {
    "captcha_present": <bool>,
    "captcha_type": "recaptcha|hcaptcha|turnstile|slider|image|text"|null,
    "captcha_widget_bbox": { "box_2d": [...], "label": "...", ... } | null,
    "modal_open": <bool>,
    "error_banner": "<text>" | null,
    "loading": <bool>,
    "login_wall": <bool>
  },
  "suggested_actions": [
    {
      "action": "click|type|scroll|dismiss|wait|navigate",
      "target_bbox_index": <int or null>,
      "description": "Short reason, e.g. 'dismiss cookie banner before proceeding'",
      "priority": <int 1-3>
    }
  ],
  "changes_from_previous": "What changed since the previous screenshot (empty if first screenshot)"
}

Hard rules for box_2d (THIS IS CRITICAL FOR CLICK ACCURACY):
- Format is [ymin, xmin, ymax, xmax] in that exact order.
- Integers normalized to [0, 1000] relative to the FULL SCREENSHOT image,
  not the viewport. Top-left of the image is (0, 0); bottom-right is
  (1000, 1000). ymin/xmin describe the top-left of the element's
  bounding rectangle; ymax/xmax describe the bottom-right.
- ymax MUST be greater than ymin and xmax MUST be greater than xmin.
- Make the bounding rectangle TIGHT around the visible element. A loose
  box that includes whitespace or neighbours produces wrong clicks.
- Example: a button at the geometric centre of a 1000×1000 image,
  100px wide × 40px tall, would be roughly box_2d=[480, 450, 520, 550].

Set-of-Marks hint (may or may not be present):
Dashed colored rectangles labelled [V_n] may be drawn on this image.
They mark elements you detected on the PREVIOUS vision pass — they
are REFERENCE ONLY, not ground truth.

Procedure:
1. Ignore the overlay first. Look at the current screenshot and
   produce fresh box_2d values from what you actually see.
2. THEN compare each fresh bbox against the overlay. If a [V_n]
   rectangle aligns tightly with what you see, you can adopt its
   coordinates; if not, emit the tight coordinates you computed.
3. If the overlay shows a [V_n] rectangle over an element that is no
   longer there (page scrolled, modal closed, navigation fired), do
   NOT re-emit it. Drop it.
4. Do NOT simply copy the previous bboxes forward. Every vision pass
   must reflect the CURRENT screenshot, not the previous one.

General rules:
- Do NOT hallucinate elements. If you cannot read it, leave it out.
- `intent_relevant` is true for bboxes that most directly serve the
  user's stated intent. Be conservative — usually 0 to 3 regions qualify.
- For captcha tiles, emit one bbox per tile with role='captcha_tile' and
  label like 'tile 1,1'. For sliders, include role='slider_handle' for
  the draggable handle and role='captcha_widget' for the outer track.
- `suggested_actions` is REQUIRED. Always include at least one action
  based on the stated intent. If blocking overlays exist (cookie banners,
  modals, captchas), suggest dismissing them first (priority=1).
  `target_bbox_index` is the 0-based index into the bboxes array.
- Output ONLY the JSON. No prose, no markdown fences, no commentary.
"""


def intent_bucket(intent: str) -> str:
    """Collapse free-form intent into one of 5 coarse buckets for caching.

    Slight phrasing variation ("check login state" vs "verify login succeeded")
    should share a cache entry, but "observe" and "solve captcha" must not.
    """
    s = (intent or "").lower()
    if any(k in s for k in ("captcha", "challenge", "prove you")):
        return "solve_captcha"
    if any(k in s for k in ("click", "fill", "type", "select", "choose", "interact", "dismiss", "submit")):
        return "act"
    if any(k in s for k in ("verify", "confirm", "check if", "did it", "outcome")):
        return "verify_action"
    if any(k in s for k in ("observe", "what", "read", "describe")):
        return "observe"
    return "other"


def build_user_prompt(
    intent: str,
    url: str | None,
    previous_summary: str | None = None,
    task_instruction: str | None = None,
) -> str:
    """The per-call user message — describes context and emphasises intent.

    `task_instruction` is the high-level goal the agent is pursuing on
    this site (e.g., "book a flight from dhaka to bangkok", "list the
    first 5 products in women's new arrivals"). Passing it here lets
    Gemini bias its bbox picks toward elements relevant to the task,
    rather than emitting a uniform set of bboxes for every interactive
    region on the page. Think of it as a soft prior: same page, same
    URL, different task = different bbox emphasis.
    """
    bucket = intent_bucket(intent)
    context = f"Page URL: {url}\n" if url else ""

    if task_instruction:
        context += f"Overall task: {task_instruction}\n"

    if previous_summary:
        context += f"Previous page state: {previous_summary}\n"
        context += "Compare the current screenshot to the previous state. Note what changed in 'changes_from_previous'.\n"

    # Page-type detection prefix — applies to EVERY intent bucket.
    # Gemini first classifies the page and then biases bboxes toward
    # elements relevant to that page type. This is the "intelligent
    # analysis" that lets the same agent work across captcha screens,
    # product listings, search results, login forms, checkout, etc.
    page_type_detection = (
        "Page-type analysis (do this first, internally):\n"
        "  Classify this screenshot as one of:\n"
        "    captcha_challenge | login_form | signup_form | search_results |\n"
        "    product_listing (catalog / category grid) |\n"
        "    product_detail | checkout_form | cart | home_landing |\n"
        "    article | map_or_booking | dashboard | error_page | other\n"
        "  Then bias bboxes toward elements that matter for THAT page type:\n"
        "    - captcha_challenge → every tile / slider handle / verify button;\n"
        "      captcha_widget for the outer chrome. Skip unrelated navigation.\n"
        "    - login_form / signup_form → input fields, submit, 'forgot',\n"
        "      oauth buttons. Skip marketing content.\n"
        "    - search_results → result rows / cards, filter chips, pagination,\n"
        "      sort dropdown. Not the site-wide header/footer.\n"
        "    - product_listing → product cards (one bbox per card),\n"
        "      filter sidebar, sort/view toggles, pagination.\n"
        "    - product_detail → add-to-cart, buy-now, size/colour pickers,\n"
        "      qty stepper, reviews tab, main image. Skip related-items.\n"
        "    - checkout_form → each field, 'continue' / 'place order',\n"
        "      shipping/payment selectors. Skip upsells unless dismissible.\n"
        "    - cart → qty steppers, remove buttons, apply-coupon,\n"
        "      checkout CTA.\n"
        "    - map_or_booking (flights/hotels/restaurants/rides) →\n"
        "      date/time pickers, origin/destination inputs, search CTA,\n"
        "      result cards/chips with key params (price, time, rating).\n"
        "    - home_landing / article → primary CTA, main nav that\n"
        "      matches the user task, otherwise conservative.\n"
        "    - error_page → retry, go-back, alternate-link buttons only.\n"
        "    - other → best-effort, prioritise anything labelled\n"
        "      interactively or clearly clickable.\n"
        "  The overall task above tells you which elements most serve\n"
        "  the user's goal — mark those intent_relevant=true. Keep\n"
        "  bbox count tight (10–25 is a good target); more is noise.\n"
    )
    context += "\n" + page_type_detection

    if bucket == "solve_captcha":
        specific = (
            "Focus on the captcha challenge. List the outer widget as "
            "captcha_widget, each selectable tile as captcha_tile (left-to-"
            "right, top-to-bottom), slider handles as slider_handle. Set "
            "captcha_present=true and captcha_type to the best match. "
            "intent_relevant=true for every element needed to solve. "
            "In suggested_actions, recommend the solve approach (click tiles, "
            "drag slider, etc.). Make every box_2d tight around the actual "
            "tile/handle — sloppy boxes cause wrong-tile clicks."
        )
    elif bucket == "act":
        specific = (
            "The reasoning agent wants to interact with the page. "
            "Identify the most likely target element for the stated intent "
            "and emit a TIGHT box_2d around it (no padding, no neighbours). "
            "In suggested_actions, recommend the exact action (click, type, "
            "scroll) with the target bbox index. If there are blocking "
            "overlays (cookie banners, modals, captchas), suggest dismissing "
            "them first (priority=1) before the main action (priority=2)."
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
