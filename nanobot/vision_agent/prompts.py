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

import os

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
      "intent_relevant": <bool>,
      "role_in_scene": "blocker|target|chrome|content|unknown",
      "layer_id": "<id of the SceneLayer this bbox sits in, e.g. 'L0_modal'>"
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
  "scene": {
    "layers": [
      {
        "id": "L0_modal",
        "kind": "modal|drawer|toast|banner|sticky_header|content",
        "bbox": { "box_2d": [ymin,xmin,ymax,xmax], "label": "...", ... } | null,
        "blocks_interaction_below": <bool>,
        "dismiss_hint": "<label of the close/accept button inside this layer, e.g. 'Accept all'>" | null
      }
    ],
    "active_blocker_layer_id": "<id of the layer the user must dismiss first, or null>"
  },
  "suggested_actions": [
    {
      "action": "click|type|scroll|dismiss|wait|navigate",
      "target_bbox_index": <int or null>,
      "description": "Short reason, e.g. 'dismiss cookie banner before proceeding'",
      "priority": <int 1-3>
    }
  ],
  "changes_from_previous": "What changed since the previous screenshot (empty if first screenshot)",
  "screenshot_freshness": "fresh|uncertain|stale",
  "unsure_regions": [
    {
      "box_2d": [ymin, xmin, ymax, xmax],
      "what_i_see": "<short visual phrase, e.g. 'small unlabelled icon'>",
      "why_unsure": "<short reason, e.g. 'glyph unrendered'>"
    }
  ],
  "page_state": {
    "funnel": {
      "name": "<human-readable, e.g. 'checkout step 2 of 4'>",
      "step_index": <int|null>,
      "total_steps": <int|null>,
      "funnel_kind": "checkout|search_funnel|signup|booking|filter_panel|form_wizard|none"
    },
    "active_filters": [
      {"label": "WiFi", "state": "on|off|partial|unknown", "value": "<for ranges, e.g. '$50-$100'>"}
    ],
    "result_count": <int|null>,
    "list_length_visible": <int|null>,
    "last_action_verdict": {
      "verdict": "succeeded|failed|uncertain|no_action",
      "evidence": "<one short sentence>",
      "delta_summary": "<what changed since last screenshot>"
    },
    "stuck_indicators": [
      {"kind": "loading_spinner|error_banner|captcha|empty_results|rate_limit|timeout|none",
       "detail": "<short>",
       "duration_hint": "<e.g. '>=3s loading'>"}
    ],
    "primary_intent_match": "<one sentence: what does this page do?>"
  },
  "next_action": null
}

When the caller asks for intent "solve captcha step" (and ONLY then),
you MUST also populate `next_action` with exactly one planned action:

  "next_action": {
    "action_type": "click_tile" | "drag_slider" | "type_text" | "submit" | "done" | "stuck",
    "target_bbox": { "box_2d": [ymin,xmin,ymax,xmax], "label": "...", ... } | null,
    "target_input_bbox": { "box_2d": [...], ... } | null,
    "type_value": "<string to type — only for action_type=type_text>",
    "label": "<short target label, e.g. 'tile with traffic light 2,1'>",
    "reasoning": "<one sentence on why this action, not another>",
    "expect_change": "static" | "new_tile" | "widget_replace" | "page_nav"
  }

For every other intent, leave `next_action` null (or omit the key).

Hard rules for box_2d (THIS IS CRITICAL FOR CLICK ACCURACY):
- Format is [ymin, xmin, ymax, xmax] in that exact order.
- Integers normalized to [0, 1000] relative to the FULL SCREENSHOT image,
  not the viewport. Top-left of the image is (0, 0); bottom-right is
  (1000, 1000). ymin/xmin describe the top-left of the element's
  bounding rectangle; ymax/xmax describe the bottom-right.
- ymax MUST be greater than ymin and xmax MUST be greater than xmin.
- Click position is ALWAYS the geometric center of the bbox. Therefore
  the bbox MUST be tight enough that its center lands ON the visible
  text or icon, not on padding or a neighboring element.
- TEXT-FIT requirement: hug the visible glyphs / icon pixels with
  ≤ 4px slack on every side. The bbox should look like a snug
  highlight around the label, NOT a row-wide rectangle covering empty
  space. If the visible label is "Oregon" rendered at ~60×16px, emit
  a ~64×20px bbox — not the surrounding 240×40px row.
- Wide row containers (e.g. an entire filter row with "(expand)" on the
  right) are STRICTLY FORBIDDEN unless the entire row is itself the
  click target. When a row contains a label + a separate chevron icon,
  emit TWO bboxes: one tight around the label text, one tight around
  the chevron icon. The brain will pick which one to click.
- A loose box that spans whitespace or includes neighbours produces
  wrong clicks because the click lands at the bbox center, not on the
  text.
- Example: a button at the geometric centre of a 1000×1000 image,
  rendered text spans 100px wide × 40px tall, emit box_2d=[480, 450,
  520, 550] — the box hugs the rendered text. NOT box_2d=[460, 350,
  540, 650] which spans the surrounding row padding.

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
- Return up to 50 bboxes. Do not cap yourself lower on dense pages —
  the caller's brain can't click what isn't in the list.

DUAL-AFFORDANCE ROWS — the "container with chevron" trap:
Many filter panels (especially country→state pickers, category trees,
multi-level facet menus) render rows that have TWO clickable
affordances stacked on top of each other:
  (a) the row BODY (label + checkbox/toggle) — clicking it selects the
      whole parent option, often as "all of X" (e.g. clicking the
      "United States" row toggles every US region ON);
  (b) a small CHEVRON / caret / arrow / "+" icon at the right edge —
      clicking it EXPANDS the row to reveal sub-options (e.g. the list
      of US states under United States).
These two affordances do OPPOSITE things: (a) collapses the choice to
"all", (b) lets the user drill down to a specific sub-option. The
click coordinates are different by ~150-300px.
RULE: emit them as TWO SEPARATE bboxes whenever a row visibly has
both. Each bbox must be TIGHT around its actual hitbox:
  1. role='checkbox' (or whatever the toggle is) — bbox around the
     label + checkbox region only. label="<group name> (toggle all)".
  2. role='button' — bbox around JUST the chevron/caret icon
     (≈20-30px square at the right edge of the row).
     label="<group name> (expand sub-options)".
Set role_in_scene='target' on whichever serves the active task —
usually the CHEVRON when the user wants a specific sub-option (the
common case for region pickers like "Oregon under United States").
The active FOCUS block (when present) names which one.
Same pattern applies to: country→state pickers, category→subcategory
trees, accordion+detail rows, "show more" arrows on collapsed lists,
expandable facet menus. Do NOT collapse the chevron into the parent
row's bounding box — the brain literally CANNOT reach the chevron if
you only emit the row.

Page-type coverage rules (CRITICAL for dense filter/booking UIs):
- On `search_results` / `product_listing` / `checkout_form` /
  `map_or_booking` pages, ALWAYS include EVERY visible:
    * filter chip / facet checkbox / amenity toggle (even as a group
      of similar-looking items — emit each separately, not once);
    * sort-by dropdown, group-by selector, map/list switcher;
    * vehicle / guest / party-size / room-count selector;
    * date-range picker, time picker;
    * in-and-out / re-entry / cancellation-policy toggle or badge;
    * per-result "Details" / "Book" / "Select" action button.
  These controls often look like "chrome" next to the main content
  cards, but without them the caller CANNOT complete a booking task.
  They are `role_in_scene = "target"` or `"content"`, not "chrome".
- On any page, if the caller's task phrases name specific
  attributes ("Ford F-150", "in-and-out", "wheelchair accessible",
  "pet-friendly", "king bed"), include every bbox whose label touches
  those words, even if they look like filter-sidebar noise. The
  caller's downstream scorer will re-rank; your job is comprehensive
  coverage, not brevity.
- For captcha tiles, emit one bbox per tile with role='captcha_tile' and
  label like 'tile 1,1'. For captcha sliders, include role='slider_handle'
  for the draggable handle and role='captcha_widget' for the outer track.
- AUTOCOMPLETE / SUGGESTION OVERLAYS — these are the single biggest source
  of "form filling missed a field" errors. When a list of suggestions
  appears below or above a recently-focused text input (city autocomplete,
  search-suggest, address autofill, date-picker calendar, dropdown menu
  rendered as a popover), classify it as a TRANSIENT BLOCKER:
    * Set `flags.autocomplete_open = true`
    * Set `flags.autocomplete_anchor_index` to the 1-based V_n of the
      input that opened it (so the brain can re-target after dismiss).
    * Each suggestion is its OWN bbox with role='content' and
      `clickable=true` — picking one dismisses the overlay.
    * The overlay layer (if any) gets `kind='drawer'` with
      `blocks_interaction_below=true` and `dismiss_hint='press Escape'`
      OR the label of the best-matching suggestion when one is obvious
      from the user task.
    * Inputs covered by the overlay must STILL be emitted (with
      role_in_scene='target' or 'content'); the brain needs to know
      they exist so it can return to them after picking.
- For FORM sliders (retirement calculators, filter ranges, volume controls,
  any <input type=range>, role=slider, or visual track+thumb pair that is
  NOT a captcha), emit THREE bboxes per slider in this order so their V_n
  indices sit together:
    1. role='slider_handle' — the draggable thumb(s). For dual-thumb ranges
       emit two handles in left-to-right order; `label` must name WHICH
       thumb (e.g. "Age Range (low)", "Age Range (high)").
    2. role='slider_widget' — the full track rectangle from min to max.
       `label` = the slider's functional name (e.g. "Monthly contribution").
    3. role='text_block' — the adjacent rendered value label if visible
       (e.g. the "Monthly contribution ($): 300" caption). `label` = the
       rendered text verbatim; this is the ground-truth value readback.
  If any of the three is occluded or off-screen, skip that entry but keep
  the others. Do NOT merge a slider_handle bbox into the slider_widget
  rectangle — they must be separate so the tool can compute the drag
  target along the track.
- `suggested_actions` is REQUIRED. Always include at least one action
  based on the stated intent. If blocking overlays exist (cookie banners,
  modals, captchas), suggest dismissing them first (priority=1).
  `target_bbox_index` is the 0-based index into the bboxes array.
- `unsure_regions` is OPTIONAL but USEFUL. If you see a region that
  looks interactive or task-relevant but you can't confidently classify
  (the visual is ambiguous, the label is illegible, or the role is
  unclear), emit it here INSTEAD of guessing in `bboxes` with a
  fabricated label and a low confidence value. Each entry:
    {
      "box_2d": [ymin, xmin, ymax, xmax],
      "what_i_see": "short phrase describing the visual (e.g. 'circular icon
                    next to the price' or 'small text below the input')",
      "why_unsure": "short reason (e.g. 'icon unrendered',
                    'label cut off by overflow', 'glyph not in training
                    distribution')"
    }
  Prefer `unsure_regions` over a low-confidence bbox when the region is
  CLEARLY there but the IDENTITY is unclear. Don't dump ambiguous bboxes
  into `bboxes` with confidence=0.3 — that wastes the brain's attention
  on guessed targets. The brain has a `browser_look_again` tool that
  re-runs vision in coverage mode against expected_labels; populating
  `unsure_regions` lets the brain choose to ask carefully.
- Output ONLY the JSON. No prose, no markdown fences, no commentary.

Scene hierarchy (NEW — enables a "clear blockers before main goal" planner):
- `scene.layers` is painter order, TOP-MOST FIRST. layers[0] visually sits
  above layers[1]. Give every layer a short id like "L0_modal", "L1_banner",
  "L2_content".
- Set `blocks_interaction_below=true` for any layer the user MUST dismiss
  before reaching the page beneath — cookie/consent banners, login modals,
  newsletter popups, captcha challenges, paywall overlays.
- `active_blocker_layer_id` points to the top-most such layer. Set null
  when the scene is unblocked (no overlay covering content).
- `dismiss_hint` is the visible text on the best-guess close/accept button
  inside that layer ("Accept all", "Close", "Not now", "Got it"). This is
  what the planner will search for.
- `role_in_scene` on every bbox — classified relative to the OVERALL TASK
  in the user prompt:
    - "blocker"  = element whose job is to dismiss/bypass an overlay
                    (Accept on a cookie banner, × on a newsletter popup).
                    CRITICAL: mark these accurately — the planner dismisses
                    them before pursuing the main goal.
    - "target"   = element that directly serves the user's task (the
                    search input when the task is "search for X", the
                    Submit button on the login form when the task is
                    "log in", the first result card when the task is
                    "read the top article").
    - "chrome"   = site header/nav/footer that is not the target.
    - "content"  = article body, product card on a listing, map tile.
    - "unknown"  = unsure; leave as default when you can't decide.
- `layer_id` on every bbox = the id of the SceneLayer it belongs to.
  A bbox in the cookie banner gets layer_id="L0_modal" (or whatever id
  you gave that layer). A bbox in the site nav below the modal gets
  layer_id="L1_content" etc.
- If the page is a flat content page (no overlays), emit a single
  content layer and leave `active_blocker_layer_id` null. Don't invent
  blockers that aren't there.

Screenshot freshness (prevents acting on stale frames):
- Set `screenshot_freshness="fresh"` when the screenshot clearly shows the
  page at `Page URL` fully rendered — header text, URL bar fragments, main
  content all visible and coherent.
- Set `screenshot_freshness="uncertain"` when a prominent loading spinner,
  skeleton placeholder, or "loading…" overlay covers the main content.
  In this case emit ONLY bboxes you are confident remain stable across
  the load (site header, nav, close/cancel buttons) and SKIP placeholder
  regions. Prefer a `suggested_actions` entry with action="wait".
- Set `screenshot_freshness="stale"` when visible page chrome (header
  text, visible URL/breadcrumb, logo context) clearly contradicts
  `Page URL` — e.g. URL says /checkout but the screenshot shows a
  product listing. Return an EMPTY `bboxes` array and a short `summary`
  naming the mismatch. The caller will re-capture before acting.
- When in doubt prefer "uncertain" over "fresh". A false "fresh" leads
  to clicks on stale coordinates; a false "uncertain" only costs a
  re-capture.

Page state report (NEW — the brain reads this BEFORE bboxes):
The reasoning agent must answer "where am I in the funnel and what just
happened" without re-parsing prose. Populate `page_state` so it can.

- `page_state.funnel`: when the page is part of a stepwise flow
  (checkout, signup, booking wizard, form wizard) read the visible
  step indicator (breadcrumb, progress bar, "Step 2 of 4") and fill
  `name`, `step_index`, `total_steps`, `funnel_kind`. For flat pages
  set `funnel_kind="none"`.
- `page_state.active_filters`: list every facet/chip/checkbox that
  represents a filter the user could apply. For each:
    * `label` = filter name as it appears (e.g. "Free WiFi", "Pet friendly")
    * `state` = "on" if the filter is currently APPLIED (chip is filled,
      checkbox checked, toggle pressed), "off" if visible but not applied,
      "partial" for indeterminate / mixed multi-select, "unknown" only
      when you genuinely cannot tell. The brain uses this to flip task
      constraints to "satisfied" — accuracy here is load-bearing.
    * `value` = applied value for range/slider filters ("$50-$100",
      "4-5 stars"); empty for boolean filters.
  On non-filter pages emit an empty array.
- `page_state.result_count`: when a results page shows a number ("47
  hotels", "1,203 jobs", "showing 25 of 312"), emit the most precise
  visible total. Null when no count is visible.
- `page_state.list_length_visible`: count of distinct result/list items
  rendered above the fold. Null on non-list pages.
- `page_state.last_action_verdict`: when the caller supplies
  `previous_summary`, compare it to what you see now and return:
    * `verdict` = "succeeded" (state changed in the way the action
      intended — modal closed, filter chip activated, count dropped),
      "failed" (no change or the opposite change), "uncertain" (some
      change but unclear if it's the intended one), or "no_action"
      (no `previous_summary` was supplied).
    * `evidence` = one specific observation
      (e.g. "filter chip shows checkmark, count went 132->47").
    * `delta_summary` = one sentence on what changed.
- `page_state.stuck_indicators`: emit one entry per blocking signal
  visible — loading spinner, error banner, captcha, empty results, rate
  limit, timeout. `kind="none"` and an empty array both mean "no
  blockers". Use `duration_hint` only when a duration is visible
  (e.g. "Retry in 30s").
- `page_state.primary_intent_match`: ONE SENTENCE describing what the
  current page DOES, in the user's vocabulary. Example: "Hotel results
  list with 47 properties and a sidebar filter panel."

Be conservative: leaving fields empty/null/unknown is preferred over
guessing. Empty `page_state` is valid for very simple pages.
"""


def intent_bucket(intent: str) -> str:
    """Collapse free-form intent into one of N coarse buckets for caching.

    Slight phrasing variation ("check login state" vs "verify login succeeded")
    should share a cache entry, but "observe" and "solve captcha" must not.

    `solve_captcha_step` is its own bucket because the step-mode contract
    (single next_action, no SoM overlay, no result caching) is materially
    different from the original one-shot `solve_captcha` pass.

    Arch v3 buckets:
      `state_check`   — explicit "tell me where I am, no bboxes needed"
                        passes from `browser_state_check`. Populates
                        `page_state` only; bboxes come back empty.
      `verify_action` — explicit "did my last action work" passes from
                        `browser_verify_action` and worker_hook
                        auto-fire on dense scenes. Demands populated
                        `last_action_verdict` based on `previous_summary`.
    """
    s = (intent or "").lower()
    # Check step-mode BEFORE the broader captcha match so it doesn't get
    # swallowed into the generic "solve_captcha" bucket.
    if "captcha step" in s or "captcha_step" in s:
        return "solve_captcha_step"
    # Arch v3 — explicit verify/state buckets. These take precedence
    # over the looser keyword fallback below so the brain's explicit
    # tool calls don't get mis-bucketed as generic "act" or "observe".
    if "state_check" in s or "state check" in s:
        return "state_check"
    if "verify_action" in s or "verify action" in s:
        return "verify_action"
    # F4 — captcha bucket only fires when the intent expresses an
    # ACTION on a captcha (solve / click / complete / submit / pick a
    # tile / drag a slider). The brain often writes "find search box
    # and any captcha" or "watch for captcha or modal" as a passive
    # awareness hint — that historically tripped the bucket and put
    # Gemini into tile-grid mode for normal pages, mislabeling search
    # inputs as captcha tiles. Require both a noun and a verb to fire.
    _captcha_nouns = ("captcha", "challenge", "prove you", "i'm not a robot")
    _captcha_verbs = (
        "solve", "click tile", "click on tile", "select tile", "pick tile",
        "complete the captcha", "complete captcha", "drag slider",
        "verify human", "verify i'm human", "submit captcha",
    )
    if any(n in s for n in _captcha_nouns) and any(v in s for v in _captcha_verbs):
        return "solve_captcha"
    # filter_panel bucket: explicit "scan everything" intents on dense
    # filter modals. Triggers a coverage-style emit so every checkbox/
    # radio/option in viewport gets its own bbox + group label, even
    # when the bbox cap would normally cull them. Pair with
    # browser_inventory_filters when the brain wants both the manifest
    # AND a visual confirmation.
    if (
        "inventory" in s
        or "filter panel" in s
        or "filter_panel" in s
        or "amenity scan" in s
        or "scan filters" in s
        or "scan amenities" in s
        or "enumerate filters" in s
    ):
        return "filter_panel"
    if any(k in s for k in ("click", "fill", "type", "select", "choose", "interact", "dismiss", "submit")):
        return "act"
    if any(k in s for k in ("verify", "confirm", "check if", "did it", "outcome")):
        return "verify_action"
    if any(k in s for k in ("observe", "what", "read", "describe")):
        return "observe"
    return "other"


_TASK_KEEP_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "from", "into", "this", "that",
    "next", "near", "find", "need", "want", "going", "use", "using",
    "please", "make", "get", "show", "list", "tell", "give", "page",
    "site", "button", "click", "type", "write", "fill", "enter",
    "submit", "search", "open", "view", "your", "you", "our", "its",
    "there", "here", "then", "now", "all", "one", "two", "three",
    "zero", "can", "will", "would", "should", "must",
})


def _task_keep_keywords(task_instruction: str) -> list[str]:
    """Extract up to 20 content tokens from the task description to
    show the vision agent as a KEEP list. Matches the token logic in
    `vision_agent.client._task_keep_tokens` so prompt guidance and
    post-hoc cap-override agree on what's "task-critical"."""
    if not task_instruction:
        return []
    import re as _re
    seen: list[str] = []
    for tok in _re.split(r"[\s,.;:!?()\[\]{}\"']+", task_instruction.lower()):
        tok = tok.strip().strip("-/")
        if len(tok) < 3:
            continue
        if tok in _TASK_KEEP_STOPWORDS:
            continue
        if tok in seen:
            continue
        seen.append(tok)
        if len(seen) >= 20:
            break
    return seen


def build_user_prompt(
    intent: str,
    url: str | None,
    previous_summary: str | None = None,
    task_instruction: str | None = None,
    compact: bool = False,
    current_subgoal: object | None = None,
    active_constraint: dict | None = None,
) -> str:
    """The per-call user message — describes context and emphasises intent.

    `task_instruction` is the high-level goal the agent is pursuing on
    this site (e.g., "book a flight from dhaka to bangkok", "list the
    first 5 products in women's new arrivals"). Passing it here lets
    Gemini bias its bbox picks toward elements relevant to the task,
    rather than emitting a uniform set of bboxes for every interactive
    region on the page.

    `current_subgoal` (when present) is the *active* sub-step from the
    task graph — a finer-grained pointer than `task_instruction`. It
    has `.id`, `.description`, `.look_for`, and `.expected_signals`
    attributes (duck-typed; comes from
    superbrowser_bridge.task_graph.Subgoal). When passed, `intent_relevant`
    is interpreted as "serves the *current subgoal*", not the overall
    task — so bboxes stay focused on what the user needs *right now*
    rather than every clickable element that might serve the eventual
    goal. This is the load-bearing change for targeted bbox emission.

    `active_constraint` (arch v3 fix #3) — the highest-priority
    unverified TaskBrief constraint, as a dict with keys: `text`,
    `kind`, `canonical_value`, `operator`, `threshold`, `unit`. When
    set, it renders as an explicit FOCUS block telling vision which
    one element on the page would advance the constraint right now. If
    the constraint is multi-faceted (numeric range, ordering), the
    block names the operator + threshold so vision can recognize
    matching controls (sliders, range inputs, sort dropdowns).
    """
    bucket = intent_bucket(intent)
    context = f"Page URL: {url}\n" if url else ""
    if url:
        context += (
            "Freshness check: confirm the screenshot's visible chrome "
            "(header text, URL bar, breadcrumb) is consistent with this "
            "URL. If it clearly isn't, set screenshot_freshness='stale' "
            "and return an empty bboxes array.\n"
        )

    if task_instruction:
        context += f"Overall task: {task_instruction}\n"
        # B: surface task keywords as an explicit KEEP list so vision
        # preserves bboxes whose labels touch the caller's concrete
        # requirements (vehicle make/model, amenity, filter name),
        # even when they look like filter-sidebar chrome.
        _keep = _task_keep_keywords(task_instruction)
        if _keep:
            context += (
                "KEEP keywords (task-critical — emit a bbox for any "
                "visible control whose label, aria, or visible text "
                "touches ANY of these, even if it otherwise looks like "
                "filter/sidebar chrome): "
                + ", ".join(_keep[:20])
                + "\n"
            )

    if current_subgoal is not None:
        sg_id = str(getattr(current_subgoal, "id", "") or "")
        sg_desc = str(getattr(current_subgoal, "description", "") or "").strip()
        if sg_desc:
            context += f"Current subgoal ({sg_id}): {sg_desc}\n"
            look_for = list(getattr(current_subgoal, "look_for", None) or [])
            if look_for:
                context += "Look for: " + " | ".join(look_for[:5]) + "\n"
            sigs = list(getattr(current_subgoal, "expected_signals", None) or [])
            if sigs:
                # Render signals compactly for the model. Each signal has
                # `.kind` and `.payload` — keep the payload to a couple
                # of useful keys so the prompt stays small.
                lines: list[str] = []
                for s in sigs[:4]:
                    kind = str(getattr(s, "kind", "") or "")
                    payload = getattr(s, "payload", None) or {}
                    if isinstance(payload, dict):
                        snippet = ", ".join(
                            f"{k}={v!r}" for k, v in list(payload.items())[:2]
                        )
                    else:
                        snippet = ""
                    lines.append(f"  - {kind}: {snippet}" if snippet else f"  - {kind}")
                context += "Subgoal complete when:\n" + "\n".join(lines) + "\n"
            context += (
                "Bbox priority: intent_relevant=true ONLY for elements "
                "that serve the current subgoal above. Don't emit "
                "intent_relevant for elements that serve the broader task "
                "but not this immediate step.\n"
            )

    # Arch v4.2: explicit FOCUS biasing toggled OFF by default.
    # The previous "find the FILTER chip whose label contains {cv} and
    # set role_in_scene='target' / intent_relevant=true" biasing forced
    # Gemini to pick the FIRST element matching the canonical_value as
    # V_1. On real pages this routinely picked the wrong element when
    # the term appeared in multiple places (e.g. "White" on a mega-menu
    # navigation tile, on a category card heading, AND on the actual
    # filter chip — the chip is what the brain wants but the tile won
    # the bias). OLD lightweight architecture had no per-constraint
    # bias and ranked bboxes on the overall task only — that worked
    # better in practice. Re-enable with VISION_FOCUS_BIAS=1 for A/B.
    if (
        active_constraint
        and isinstance(active_constraint, dict)
        and os.environ.get("VISION_FOCUS_BIAS", "0") == "1"
    ):
        kind = str(active_constraint.get("kind", "filter") or "filter")
        cv = str(active_constraint.get("canonical_value", "") or "").strip()
        text = str(active_constraint.get("text", "") or "").strip()
        op = str(active_constraint.get("operator", "") or "").strip()
        threshold = str(active_constraint.get("threshold", "") or "").strip()
        unit = str(active_constraint.get("unit", "") or "").strip()
        # Soft hint only — no role_in_scene mandate, no "set V_1 to X"
        # directive. Just tell Gemini what kind of UI element matters
        # so it doesn't cull it; let the existing intent_relevant +
        # confidence ranking decide V_1.
        focus_lines = ["Brain's current focus (informational only — rank by your normal rules):"]
        if cv or text:
            label = cv or text
            focus_lines.append(f"  - working on: {label!r} (kind={kind})")
        if op:
            cmp_bit = op
            if threshold:
                cmp_bit += f" {threshold}"
                if unit:
                    cmp_bit += f" {unit}"
            focus_lines.append(f"  - condition: {cmp_bit}")
        context += "\n".join(focus_lines) + "\n"

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

    if bucket == "solve_captcha_step":
        specific = (
            "CAPTCHA STEP MODE — you are solving ONE step of a live "
            "captcha. The page state is live; between calls the site may "
            "re-render tiles, swap the entire grid, or change the prompt.\n\n"
            "Return `next_action` committing to EXACTLY ONE of:\n"
            "  - click_tile: target a single matching tile. target_bbox = "
            "that tile's tight box_2d. expect_change='new_tile' if clicking "
            "this tile will likely cause the site to swap it for a new one; "
            "'static' if the grid will stay as-is.\n"
            "  - drag_slider: target the draggable handle. expect_change "
            "is usually 'widget_replace' or 'page_nav'.\n"
            "  - type_text: for classic distorted-word captchas (an image "
            "shows warped characters with a text input nearby). Read the "
            "characters in the image as carefully as you can and put "
            "them in `type_value` (case-sensitive if the image is mixed-"
            "case; digits are digits, letters are letters). target_bbox "
            "= the captcha image. target_input_bbox = the input field to "
            "type into (tight box around the visible input). If any "
            "character is ambiguous, emit your best guess and note the "
            "uncertainty in `reasoning` — the loop will verify post-"
            "submit. expect_change is usually 'widget_replace' or "
            "'page_nav'.\n"
            "  - submit: target the verify / continue / I-am-human button. "
            "Only choose this when you see NO remaining matching tiles. "
            "expect_change is usually 'page_nav' or 'widget_replace'.\n"
            "  - done: captcha appears CLEARED (no widget, no tiles, no "
            "challenge text). target_bbox=null. Pick this only when you "
            "are confident the captcha is gone — the loop will verify.\n"
            "  - stuck: you cannot identify a matching tile AND no verify "
            "button is visible. target_bbox=null. Reasoning MUST name what "
            "is ambiguous. This triggers human handoff — use sparingly.\n\n"
            "Cursor rule: the user prompt lists the `Previous click` "
            "coordinate. Do NOT target a bbox whose center is within 40px "
            "of that coordinate unless a VISIBLY new tile has rendered "
            "there since the last click (reasoning must say so).\n\n"
            "Also populate the normal `bboxes` array with only the tiles "
            "and widget chrome you can see — keep it to ≤12 entries, no "
            "navigation/footer noise. Set captcha_present to the truth: "
            "false only if you're sure the challenge is gone."
        )
    elif bucket == "solve_captcha":
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
            "them first (priority=1) before the main action (priority=2).\n\n"
            "Scene hierarchy is REQUIRED on this intent — populate "
            "`scene.layers` painter-order top-most-first, set "
            "`blocks_interaction_below=true` on any layer covering content, "
            "and mark the `role_in_scene` of every bbox (blocker for "
            "dismiss buttons on overlays, target for the task element, "
            "chrome for nav/footer). The downstream planner uses this to "
            "sequence dismiss-then-act, so be precise.\n\n"
            "Scroll guidance: if a [SCROLL_STATE …] line appears in the "
            "context above, treat it as ground truth for current scroll "
            "position. If the current subgoal target is plausibly off "
            "the visible viewport AND no bbox on screen serves it, emit "
            "a suggested_action with action='scroll' and a `description` "
            "that names the target text/role (the planner will dispatch "
            "browser_scroll_until). Use direction down by default; emit "
            "action='scroll' with description starting 'scroll up to' "
            "when SCROLL_STATE shows reached_bottom=true and the target "
            "is likely above. NEVER suggest more scrolling once "
            "reached_bottom=true unless retreating upward."
        )
    elif bucket == "filter_panel":
        specific = (
            "FILTER PANEL SCAN. The agent is enumerating an open filter/"
            "amenity dialog and needs EVERY checkbox/radio/option/switch "
            "currently in viewport surfaced as its own bbox — no grouping, "
            "no culling, no \"chrome\" classification. Treat the filter "
            "panel itself as the primary scene. For each control, the "
            "label MUST combine the visible option text with the nearest "
            "preceding heading or fieldset legend (e.g. \"Bills included → "
            "Wi-Fi\", \"Amenities → Cleaning service\"). Mark "
            "role_in_scene='target' for every option (none are 'chrome'). "
            "intent_relevant=true for every option in the panel — the "
            "agent will filter them down by name later, vision should "
            "emit the full set. Bbox cap is lifted in this bucket; emit "
            "as many as needed (up to ~60). If the panel is partly "
            "off-screen, set flags.scrollable=true and add a "
            "suggested_action with action='scroll' inside the panel so "
            "the agent knows there are more options below the fold. "
            "Skip top-level page chrome (header, footer, search bar) — "
            "this pass is panel-only."
        )
    elif bucket == "verify_action":
        specific = (
            "VERIFY ACTION MODE — the reasoning agent just performed an "
            "action and needs to know if it worked. Compare the current "
            "screenshot against `Previous page state` above and populate "
            "`page_state.last_action_verdict` with:\n"
            "  - verdict: succeeded | failed | uncertain (no_action only "
            "if no previous_summary was supplied).\n"
            "  - evidence: ONE specific observation that justifies the "
            "verdict (e.g. 'filter chip checkmark visible, count went "
            "132->47', 'modal dismissed and Save toast appeared', "
            "'expected confirmation never appeared, page unchanged').\n"
            "  - delta_summary: one sentence on what changed.\n"
            "Populate `page_state.active_filters` if filters are visible "
            "so the brain can reconcile its constraint checklist. Keep "
            "the bbox list tight (≤15) — focus on confirmation indicators "
            "(success banners, new chips, error toasts) rather than the "
            "full page inventory. Set `intent_relevant=true` only for "
            "elements that confirm or deny the outcome."
        )
    elif bucket == "state_check":
        specific = (
            "STATE CHECK MODE — the reasoning agent only wants to know "
            "WHERE IT IS, not what to click. Populate `page_state` "
            "thoroughly:\n"
            "  - funnel.* (step name + index/total when visible)\n"
            "  - active_filters (every visible facet with state)\n"
            "  - result_count + list_length_visible when on a results page\n"
            "  - last_action_verdict ONLY if previous_summary was supplied\n"
            "  - stuck_indicators when blockers are visible\n"
            "  - primary_intent_match (one sentence on what this page does)\n"
            "Populate `flags` (captcha_present, modal_open, login_wall, "
            "loading, error_banner, autocomplete_open) — the brain checks "
            "these before deciding next action.\n"
            "Return an EMPTY `bboxes` array — the brain is not planning "
            "to click. Skip `scene` (return null or omit) and skip "
            "`suggested_actions` unless there's a clear blocker to "
            "dismiss. Keep `summary` to one short sentence. This is a "
            "low-cost pass — do not pad the response."
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

    # Compact mode is only used on retry after a parse error (usually
    # caused by truncation on very bbox-heavy pages like seat maps).
    # Caps the bbox count hard so the response fits inside the output
    # token budget.
    compact_footer = (
        "\n\nCOMPACT MODE (retry): return at most 12 bboxes — the single "
        "most important ones for the stated intent, ranked by "
        "intent_relevant then confidence. Keep summary under two "
        "sentences. Shorter labels (<20 chars). Skip scene unless there "
        "is an active blocker. Skip relevant_text."
        if compact else ""
    )

    return (
        f"{context}Intent: {intent}\n\n{specific}{compact_footer}\n\n"
        "Return ONLY the JSON object described in the system message."
    )


def build_coverage_prompt(
    intent: str,
    url: str | None,
    expected_labels: list[str],
    dom_anchor_hints: list[dict] | None = None,
    task_instruction: str | None = None,
    current_subgoal: object | None = None,
    active_constraint: dict | None = None,
) -> str:
    """Second-pass prompt for "the validator told us this element should
    be here but vision culled it" recovery.

    Differences from the standard prompt:
      - Bbox cap is LIFTED (target up to 60 bboxes). Page-type culling
        rules are suspended for this call — toolbars, sidebars, header
        action clusters are explicitly in-scope.
      - `expected_labels` (e.g. the active subgoal's precondition label
        plus any brain-declared intent target) are named in the user
        prompt; the model is instructed to emit tight bboxes for each
        one if visible, with a `confidence` that reflects certainty.
      - `dom_anchor_hints` seed coordinates and regions from the DOM
        side so the model has something to match against for small
        icon buttons vision has historically collapsed to 4x4 px.

    Callers should set `VisionResponse.coverage_mode=True` on the parsed
    response and skip caching it — coverage passes are expensive and
    their output is load-bearing for the next dispatch.
    """
    base = build_user_prompt(
        intent=intent,
        url=url,
        previous_summary=None,
        task_instruction=task_instruction,
        compact=False,
        current_subgoal=current_subgoal,
        active_constraint=active_constraint,
    )
    labels_fmt = (
        ", ".join(f"'{lbl[:60]}'" for lbl in expected_labels[:12])
        if expected_labels else "(no labels provided)"
    )
    anchor_lines: list[str] = []
    for a in (dom_anchor_hints or [])[:20]:
        lbl = str(a.get("label") or "")[:80]
        region = str(a.get("region_tag") or "main")
        box = a.get("box_2d") or [0, 0, 0, 0]
        if isinstance(box, (list, tuple)) and len(box) == 4 and lbl:
            anchor_lines.append(
                f"  - label={lbl!r} region={region} box_2d={list(box)}"
            )
    anchor_block = (
        "\nDOM anchor hints (coordinate seeds — DO re-verify tightly):\n"
        + "\n".join(anchor_lines)
    ) if anchor_lines else ""
    return (
        f"{base}\n\n"
        "COVERAGE PASS — bbox cap is lifted for this call.\n"
        "  * Emit up to 60 bboxes. Do NOT skip sidebars, toolbars, "
        "header action clusters, or secondary CTAs.\n"
        "  * Page-type culling rules (skip-sidebar, skip-nav) are "
        "SUSPENDED — include every clickable the task might need.\n"
        "  * Required targets — emit a tight bbox for each of these "
        f"if visible, with a label that matches: {labels_fmt}.\n"
        "  * If an expected label is genuinely NOT on screen, say so "
        "in `summary` (one sentence) and emit the rest of the bboxes "
        "normally — do not invent coordinates."
        f"{anchor_block}\n"
        "Return ONLY the JSON object."
    )


__all__ = [
    "SYSTEM_PROMPT",
    "intent_bucket",
    "build_user_prompt",
    "build_coverage_prompt",
]
