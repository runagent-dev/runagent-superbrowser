"""Effect classification, mutation gates, small constants.

Pure helpers used by the click/type/keys/scroll tool path to detect
silent no-ops and warn the brain off failing tactics. No I/O.

Several private symbols (`_classify_effect`, `_maybe_no_effect_prefix`,
`_ATOMIC_FIX_TEXT_JS`) are imported by external callers (tests/, the
type_verify module) — kept reachable from `session_tools.__init__`.
"""

from __future__ import annotations

from typing import Any


# After this many guard-refused browser_open calls in a single worker run, we
# stop being polite and abort the worker. The guard's text message is clearly
# not getting through to the LLM at this point and continuing would just
# drain the iteration budget on a no-op loop.
BLOCKED_BROWSER_OPEN_HARD_STOP = 3


# --- Atomic field-correction JS (tier-agnostic) -------------------------------
# Runs inside /evaluate on either t1 (TS server) or t3 (patchright) to do the
# full probe-write-verify cycle in a single synchronous tick. No intermediate
# empty state where a framework re-render could race. Placeholders
# __TARGET_X__ / __TARGET_Y__ / __TARGET_TEXT__ get string-replaced by the
# Python caller with JSON-literal values.
_ATOMIC_FIX_TEXT_JS = """
(() => {
  const x = __TARGET_X__, y = __TARGET_Y__, target = __TARGET_TEXT__;
  let el = document.elementFromPoint(x, y);
  if (!el) return {ok: false, reason: 'no_element'};

  // Phase H: same-origin iframe descent. When the top-level
  // elementFromPoint returns an <iframe>, the actual input we want to
  // type into lives inside that frame's contentDocument. Translate
  // (x, y) into frame-local coords and re-query inside the frame.
  // Mirrors the Phase A descent in clickInBbox (page.ts).
  //
  // Cross-origin iframes throw on contentDocument access — bail
  // silently and let the existing not_input path surface a clean
  // failure; brain can fall back to
  // browser_click_selector(in_iframe, ...) + browser_type sequence.
  //
  // Cap depth at 3 for iframe-in-iframe nests.
  let _frameDepth = 0;
  // Track the (x, y) offset accumulated as we descend so the
  // wrapper-descent below sees coords in the same frame as `el`.
  let _localX = x, _localY = y;
  while (el && el.tagName && el.tagName.toLowerCase() === 'iframe'
         && _frameDepth < 3) {
    const _ir = el.getBoundingClientRect();
    const _nextX = _localX - _ir.left;
    const _nextY = _localY - _ir.top;
    let _innerDoc = null;
    try { _innerDoc = el.contentDocument; } catch (_) {}
    if (!_innerDoc) break;
    const _inner = _innerDoc.elementFromPoint(_nextX, _nextY);
    if (!_inner || _inner === _innerDoc.documentElement
         || _inner === _innerDoc.body) break;
    el = _inner;
    _localX = _nextX;
    _localY = _nextY;
    _frameDepth += 1;
  }
  // Phase H uses _localX/_localY for the wrapper-descent geometry
  // check below — bbox containment must be in the same frame as `el`.
  const _checkX = _localX, _checkY = _localY;

  // Wrapper-descent: when elementFromPoint hits a styled wrapper rather
  // than the real <input> (petfinder's #findAPetLocation, Google Maps'
  // #searchboxinput, MUI Autocomplete, plenty of Tailwind designs),
  // descend into a usable descendant before bailing.
  //   Pass A — descendant whose bounding box CONTAINS (x, y); pick the
  //            smallest (innermost) one. Conservative — geometry-scoped.
  //   Pass B — single-input wrapper fallback (exactly one descendant,
  //            visible). Bounded — never picks from a list of options.
  const isUsable = (n) => {
    if (!n) return false;
    const t = n.tagName ? n.tagName.toLowerCase() : '';
    return t === 'input' || t === 'textarea' || !!n.isContentEditable;
  };
  if (!isUsable(el)) {
    const candidates = el.querySelectorAll(
      'input, textarea, [contenteditable=""], [contenteditable="true"]'
    );
    let best = null, bestArea = Infinity;
    for (const c of candidates) {
      const r = c.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      // Phase H: when we descended into an iframe, `el` and its
      // candidates report rects in iframe-local coords; the original
      // (x, y) is viewport-relative. Compare against `_checkX/_checkY`
      // which tracks the same frame as `el`. For top-level (no
      // descent), `_checkX === x` and `_checkY === y`, so behaviour
      // is unchanged.
      if (_checkX < r.left || _checkX > r.right
          || _checkY < r.top || _checkY > r.bottom) continue;
      const a = r.width * r.height;
      if (a < bestArea) { best = c; bestArea = a; }
    }
    if (!best && candidates.length === 1) {
      const r = candidates[0].getBoundingClientRect();
      if (r.width > 0 && r.height > 0) best = candidates[0];
    }
    if (best) el = best;
  }

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
  // Phase H: when `el` was found inside an iframe (descent above), its
  // prototype + event constructors come from the iframe's window, not
  // the main frame's. Using the main-frame `HTMLInputElement.prototype`
  // throws `TypeError: Illegal invocation` because the setter's
  // internal type-check rejects the cross-frame receiver. Same for
  // `new InputEvent(...)` constructed in the main frame and dispatched
  // on an iframe-owned element. Resolve everything against
  // `el.ownerDocument.defaultView` so cross-frame inputs work too.
  // Falls back to the main-frame globals when ownerView is unavailable
  // (top-level inputs behave identically to before).
  const _ownerWin = (el.ownerDocument && el.ownerDocument.defaultView) || window;
  const _InputProto = _ownerWin.HTMLInputElement
                       ? _ownerWin.HTMLInputElement.prototype
                       : HTMLInputElement.prototype;
  const _TextareaProto = _ownerWin.HTMLTextAreaElement
                          ? _ownerWin.HTMLTextAreaElement.prototype
                          : HTMLTextAreaElement.prototype;
  const _InputEventCtor = _ownerWin.InputEvent || InputEvent;
  const _EventCtor = _ownerWin.Event || Event;
  try {
    if (isInput) {
      const proto = tag === 'textarea' ? _TextareaProto : _InputProto;
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && desc.set) {
        // React 16+ caches the prior value in el._valueTracker. If the
        // tracker matches el.value at dispatch time, React short-circuits
        // its synthetic onChange and the framework never sees the typed
        // text — autocomplete/search loops on controlled inputs (e.g.
        // Google Maps' search box) silently break. Resetting the tracker
        // forces a non-match. No-op on non-React inputs.
        const tracker = el._valueTracker;
        if (tracker && typeof tracker.setValue === 'function') {
          try { tracker.setValue(''); } catch (_) {}
        }
        desc.set.call(el, target);
        el.dispatchEvent(new _InputEventCtor('input', {bubbles: true, inputType: 'insertText', data: target}));
        el.dispatchEvent(new _EventCtor('change', {bubbles: true}));
      } else {
        el.value = target;
      }
    } else if (isEditable) {
      el.innerText = target;
      el.dispatchEvent(new _InputEventCtor('input', {bubbles: true}));
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


def _diff_text(a: str, b: str) -> str:
    """Human-readable diff summary for a → b text change."""
    if a == b:
        return "no change"
    p = 0
    while p < len(a) and p < len(b) and a[p] == b[p]:
        p += 1
    suf = 0
    while (suf < len(a) - p and suf < len(b) - p
           and a[len(a) - 1 - suf] == b[len(b) - 1 - suf]):
        suf += 1
    old_mid = a[p:len(a) - suf]
    new_mid = b[p:len(b) - suf]
    if not old_mid and new_mid:
        return f"inserted {new_mid!r} at position {p}"
    if old_mid and not new_mid:
        return f"removed {old_mid!r} at position {p}"
    return f"replaced {old_mid!r} with {new_mid!r} at position {p}"


def _maybe_no_effect_prefix(
    data: Any, tool_name: str, base_caption: str,
    *, session_state: "BrowserSessionState | None" = None,
) -> str:
    """Wrap a mutation-tool caption with a `[no_effect:...]` header when
    the TS bridge reports zero url/DOM/focus delta. The base caption is
    preserved so vision prefetch, cached bboxes and elements text still
    reach the brain — the prefix is what the brain AND the worker hook
    read as a hard failure signal.

    Also records the failure against the per-domain tactic registry via
    `routing.record_tactic_failure` and (on effect) decays any prior
    penalty via `routing.decay_tactic_success`. The penalty data is what
    the next worker's delegation prompt reads to pre-select a better
    tactic on sites that systematically reject a given tool.
    """
    had_effect, reason = _classify_effect(data, tool_name)
    # Tactic-penalty bookkeeping — resolve domain from session state.
    domain = ""
    if session_state is not None:
        try:
            from urllib.parse import urlparse
            url = session_state.current_url or ""
            if url:
                host = (urlparse(url).hostname or "").lower()
                domain = host[4:] if host.startswith("www.") else host
        except Exception:
            domain = ""
    try:
        from superbrowser_bridge.routing import (
            record_tactic_failure, decay_tactic_success,
        )
        if domain:
            if had_effect:
                decay_tactic_success(domain, tool_name)
            else:
                record_tactic_failure(domain, tool_name)
    except Exception:
        pass

    if had_effect:
        # Positive reinforcement — when a cursor tool successfully
        # moves the page state, tag it so the brain's next turn sees
        # "this tactic worked" and stays on the cursor track instead
        # of pivoting to scripts. Also resets the script-usage
        # counter so a cursor-success cleanly breaks any recent
        # script streak.
        if tool_name in _CURSOR_TOOL_NAMES:
            if session_state is not None:
                try:
                    session_state.consecutive_script_calls = 0
                except Exception:
                    pass
            return f"[cursor_success:{tool_name}] {base_caption}"
        return base_caption
    hint = (
        f"[no_effect:{tool_name}] {reason}. The tool dispatched but the "
        f"page didn't respond — no DOM mutation, no URL change, no focus "
        f"change. Do NOT retry the same tool with the same target; try "
        f"ONE OF (in this preference order): "
        f"(a) **browser_screenshot** first — the page may have changed "
        f"under you; re-observe and click the fresh [V_n]; "
        f"(b) **browser_semantic_click(target='<label>')** — atomic "
        f"fresh vision + dispatch, works across React apps; "
        f"(c) browser_click_selector(<css>) — pixel-exact if the target "
        f"has a stable CSS hook; "
        f"(d) browser_rewind_to_checkpoint if the page appears frozen. "
        f"Do NOT synthesize clicks via browser_run_script — JS clicks are "
        f"isTrusted=false and bot-detected; the sandbox will reject them."
    )
    return f"{hint}\n{base_caption}"


# Names of the "cursor-based" interaction tools — click, type, drag
# etc. Used by `_maybe_no_effect_prefix` to tag successful actions
# with `[cursor_success:...]` as positive reinforcement. Excludes
# scripts/eval (which don't move the cursor) and observation tools
# like screenshot/get_markdown.
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


def _is_captcha_intent(intent: str) -> bool:
    """True when `intent`'s bucket is a captcha bucket — these intents
    switch the vision prompt into captcha-tile mode, so they should
    NEVER become sticky (would poison subsequent non-captcha tools).
    Falls back to a substring match when intent_bucket is unavailable."""
    if not intent:
        return False
    try:
        from vision_agent.prompts import intent_bucket as _bucket
        return _bucket(intent) in ("solve_captcha", "solve_captcha_step")
    except Exception:
        s = intent.lower()
        return "captcha" in s or "challenge" in s


def _last_vision_has_captcha_flag(state: "BrowserSessionState") -> bool:
    """True when the last vision response's flags.captcha_present is
    True. Used to decide whether a sticky captcha intent is still
    relevant — if the current page isn't flagged as a captcha, the
    intent is stale and should be dropped."""
    resp = getattr(state, "_last_vision_response", None)
    if resp is None:
        return False
    flags = getattr(resp, "flags", None)
    if flags is None:
        return False
    return bool(getattr(flags, "captcha_present", False))


def _maybe_script_usage_warning(state: "BrowserSessionState") -> str:
    """Return a `[script_warning] ...` string when the brain is
    over-using `browser_run_script` even though cursor alternatives
    are visible, else empty string.

    Trigger: 3+ consecutive script calls, AND the last vision pass
    emitted at least one clickable bbox. The warning lists the top 3
    labels so the brain has concrete semantic targets to reach for.
    """
    try:
        count = int(state.consecutive_script_calls or 0)
    except Exception:
        count = 0
    if count < 3:
        return ""
    resp = getattr(state, "_last_vision_response", None)
    bboxes = getattr(resp, "bboxes", None) if resp is not None else None
    if not bboxes:
        return ""
    labels: list[str] = []
    for b in bboxes[:20]:
        if not getattr(b, "clickable", False):
            continue
        lbl = (getattr(b, "label", "") or "").strip()
        if lbl and lbl not in labels:
            labels.append(lbl)
        if len(labels) >= 3:
            break
    if not labels:
        return ""
    rendered = ", ".join(f"'{lbl}'" for lbl in labels)
    return (
        f"[script_warning] {count} consecutive run_script calls. "
        f"Vision shows clickable bboxes ({rendered}, ...) — these can "
        f"be clicked atomically via `browser_semantic_click(target='<label>')` "
        f"without the WAF-block risk scripts carry. Reserve scripts "
        f"for actions no cursor tool can express."
    )


def _classify_effect(
    data: Any, tool_name: str,
) -> tuple[bool, str]:
    """Inspect a mutation tool's HTTP response for the TS `effect` field.

    Returns `(had_effect, no_effect_reason)`:
      * `had_effect=True, ""` when the TS bridge reports any of
        url_changed / mutation_delta > 0 / focused_changed.
      * `had_effect=False, <human_reason>` when all three are zero —
        the caller prefixes `[no_effect:<tool>] …` onto its return so
        the brain and the worker hook can distinguish "the tool fired
        but nothing happened" from a real success.
      * `had_effect=True, ""` when the `effect` field is missing —
        preserves legacy behavior against an older TS bridge that
        hasn't shipped the effect snapshot yet.

    Used by click / type / keys / drag / type_at / click_at / drag_slider
    at the moment they've got the HTTP response back but haven't built
    the brain-facing caption yet.
    """
    if not isinstance(data, dict):
        return True, ""
    effect = data.get("effect")
    if not isinstance(effect, dict):
        return True, ""  # TS side too old OR path didn't capture effect
    url_changed = bool(effect.get("url_changed"))
    try:
        mutation_delta = int(effect.get("mutation_delta") or 0)
    except (TypeError, ValueError):
        mutation_delta = 0
    focused_changed = bool(effect.get("focused_changed"))
    if url_changed or mutation_delta > 0 or focused_changed:
        return True, ""
    return False, (
        f"{tool_name}: url unchanged, DOM unchanged "
        f"(mutation_delta=0), focus unchanged"
    )


# After this many guard-refused browser_open calls in a single worker run, we
# stop being polite and abort the worker. The guard's text message is clearly
# not getting through to the LLM at this point and continuing would just
# drain the iteration budget on a no-op loop.



class WorkerMustExitError(RuntimeError):
    """Raised from a tool when the worker must terminate immediately.

    Bubbles up through nanobot's tool runner. Carries a reason string the
    orchestrator can surface to the user so the failure mode is observable
    (vs. a silent iteration drain).
    """


_CAPTCHA_KEYWORDS = (
    "captcha", "recaptcha", "hcaptcha", "turnstile", "cloudflare",
    "verify you are human", "prove you are not a robot", "slider puzzle",
    "click all images", "select all", "drag the", "i'm not a robot",
)
_HARD_DOMAINS = (
    "apartments.com", "zillow.com", "ticketmaster.com", "nytimes.com",
    "linkedin.com", "instagram.com", "facebook.com",
)
# Hash length used to dedupe screenshots when page content changes on same URL.
_CONTENT_HASH_LEN = 500
