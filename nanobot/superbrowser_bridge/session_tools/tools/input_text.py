"""Text input tools — DOM index, vision-bbox, atomic field correction, key events.

`BrowserTypeTool` (DOM index), `BrowserTypeAtTool` (vision-bbox / coords),
`BrowserFixTextAtTool` (atomic probe-write-verify cycle),
`BrowserKeysTool` (raw key send).
"""

from __future__ import annotations

import json as _json_top
import time
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)

from ..effects import _ATOMIC_FIX_TEXT_JS, _diff_text
from ..feedback import _feedback_gate
from ..formatting import _fetch_elements
from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState
from ..vision_pipeline import _append_fresh_vision, _schedule_vision_prefetch


_AUTOCOMPLETE_SCAN_JS = """
(async () => {
  // Debounce wait — many autocomplete widgets fetch suggestions on a
  // 100-300ms debounce, so an immediate scan misses them.
  await new Promise(r => requestAnimationFrame(() => r()));
  await new Promise(r => setTimeout(r, 300));

  const seen = new Set();
  const out = [];
  const selectors = [
    // Standard ARIA listbox / option
    '[role="listbox"] [role="option"]',
    '[role="combobox"] + * li',
    '[role="combobox"] + * [role="option"]',
    '[role="option"]:not([aria-hidden="true"])',
    '[aria-selected]:not([aria-hidden="true"])',
    // Generic class-name patterns
    '.autocomplete-suggestions li, .autocomplete li',
    'ul.suggestions li, .suggestions li',
    '.MuiAutocomplete-listbox li',
    '[aria-live] li',
    '.dropdown-menu.show li, .dropdown-menu[style*="display: block"] li',
    '.ui-autocomplete li',
    '[class*="autocomplete"][class*="option"]',
    '[class*="suggestion"] li, [class*="suggestions"] li',
    // Algolia InstantSearch / Autocomplete
    '.ais-Hits-list .ais-Hits-item',
    '[class*="ais-Hits-item"]',
    '[class*="aa-Item"]',
    '[class*="aa-Suggestion"]',
    // Downshift
    '[id^="downshift"] [role="option"]',
    '[id^="downshift"] li',
    // React Select / similar
    '[class*="select__option"]',
    '[id*="-option-"]',
    // Reach UI
    '[data-reach-combobox-option]',
    // Headless UI
    '[id^="headlessui-listbox-option-"]',
    '[id^="headlessui-combobox-option-"]',
  ];
  for (const sel of selectors) {
    let nodes;
    try { nodes = document.querySelectorAll(sel); } catch { continue; }
    nodes.forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.width < 30 || r.height < 10) return;
      if (r.top > window.innerHeight * 1.5) return;
      const cs = window.getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return;
      const txt = (el.innerText || el.textContent || '').trim();
      if (!txt || txt.length > 120 || seen.has(txt)) return;
      seen.add(txt);
      out.push({
        text: txt,
        x: Math.round(r.left + r.width / 2),
        y: Math.round(r.top + r.height / 2),
      });
    });
  }

  // ARIA-based detection on the focused/typed-into input. Catches
  // widgets whose listbox DOM doesn't match any of our selectors —
  // we still know an autocomplete is wired up, even if we can't
  // enumerate the items.
  const el = document.activeElement;
  let isAutocompleteInput = false;
  let popupId = null;
  if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) {
    const role = (el.getAttribute('role') || '').toLowerCase();
    const ariaAutocomplete = (el.getAttribute('aria-autocomplete') || '').toLowerCase();
    const ariaHaspopup = (el.getAttribute('aria-haspopup') || '').toLowerCase();
    const ariaExpanded = (el.getAttribute('aria-expanded') || '').toLowerCase();
    const ariaControls = el.getAttribute('aria-controls') || '';
    isAutocompleteInput = (
      role === 'combobox' || role === 'searchbox' ||
      ariaAutocomplete === 'list' || ariaAutocomplete === 'both' ||
      ariaAutocomplete === 'inline' ||
      ariaHaspopup === 'listbox' || ariaHaspopup === 'menu' ||
      ariaHaspopup === 'grid' || ariaHaspopup === 'tree' ||
      ariaHaspopup === 'dialog' ||
      ariaExpanded === 'true' ||
      !!ariaControls
    );
    popupId = ariaControls || null;
  }

  // Popup-via-aria-controls visibility check. If the input points at
  // an explicit popup id, see if that popup is on-screen with content.
  let popupVisible = false;
  if (popupId) {
    const popup = document.getElementById(popupId);
    if (popup) {
      const r = popup.getBoundingClientRect();
      const cs = window.getComputedStyle(popup);
      popupVisible = (
        r.width > 0 && r.height > 0 &&
        cs.display !== 'none' && cs.visibility !== 'hidden'
      );
    }
  }

  const detected = out.length > 0 || isAutocompleteInput || popupVisible;
  return {
    suggestions: out.slice(0, 8),
    detected,
    is_autocomplete_input: isAutocompleteInput,
    popup_visible: popupVisible,
  };
})();
"""


async def _scan_autocomplete_suggestions(session_id: str) -> dict:
    """Probe the page for autocomplete state. Returns:
        {
          'suggestions': list[dict] (visible options with center coords),
          'detected': bool (True if suggestions found OR ARIA says combobox),
          'is_autocomplete_input': bool,
          'popup_visible': bool,
        }
    Best-effort — returns an empty dict on probe error.
    """
    empty: dict = {
        "suggestions": [],
        "detected": False,
        "is_autocomplete_input": False,
        "popup_visible": False,
    }
    try:
        sr = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": _AUTOCOMPLETE_SCAN_JS},
            timeout=5.0,
        )
        if sr.status_code != 200:
            return empty
        body = sr.json()
        got = body.get("result") if isinstance(body, dict) else None
        if not isinstance(got, dict):
            return empty
        suggestions = got.get("suggestions") or []
        if not isinstance(suggestions, list):
            suggestions = []
        suggestions = [s for s in suggestions if isinstance(s, dict) and s.get("text")]
        out = {
            "suggestions": suggestions,
            "detected": bool(got.get("detected")),
            "is_autocomplete_input": bool(got.get("is_autocomplete_input")),
            "popup_visible": bool(got.get("popup_visible")),
        }
        print(
            f"  [autocomplete scan: suggestions={len(suggestions)} "
            f"aria_input={out['is_autocomplete_input']} "
            f"popup_visible={out['popup_visible']} "
            f"detected={out['detected']}]"
        )
        return out
    except Exception as exc:
        print(f"  [autocomplete scan failed: {exc}]")
    return empty


def _autocomplete_pending_block(
    state: BrowserSessionState,
    current_target_label: str,
) -> str | None:
    """Refuse typing into a DIFFERENT field while a previous type's
    autocomplete dropdown stands open and unpicked. Returns the
    refusal string when the guard fires, else None.
    """
    if not state.last_type_had_suggestions:
        return None
    if not state.last_type_anchor_label:
        return None
    if state.last_type_anchor_label == current_target_label:
        return None
    elapsed = time.time() - state.last_type_at
    if elapsed >= 30.0:
        # Stale; auto-clear and pass through.
        state.last_type_had_suggestions = False
        state.last_type_anchor_label = ""
        return None
    return (
        f"[autocomplete_pending_block] Field {state.last_type_anchor_label} "
        f"has an open autocomplete dropdown that you opened "
        f"{int(elapsed)}s ago by typing — and you never picked a "
        f"suggestion. Refusing to type into {current_target_label} "
        f"(a DIFFERENT field) while the first one is unresolved.\n"
        f"PRIMARY FIX: take a browser_screenshot, then "
        f"browser_click_at(vision_index=V_n) on the suggestion bbox "
        f"that matches the value you typed. ONLY THEN type into the "
        f"next field.\n"
        f"If you actually want to abandon the first field: take a "
        f"browser_screenshot first (clears this guard), then retry."
    )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index (the V_n the vision agent "
                "labelled this input). Preferred over (x, y) whenever "
                "the vision agent has pointed at the field."
            ),
            nullable=True,
        ),
        x=NumberSchema(
            description="X coordinate (CSS pixel). Ignored when vision_index is set.",
            nullable=True,
        ),
        y=NumberSchema(
            description="Y coordinate (CSS pixel). Ignored when vision_index is set.",
            nullable=True,
        ),
        text=StringSchema("Text to type into the field at that point."),
        clear=BooleanSchema(
            description=(
                "Clear the field's existing value before typing (default: true). "
                "Uses React/Vue-aware clear so controlled components replace "
                "properly instead of appending."
            ),
            default=True,
        ),
        required=["session_id", "text"],
    )
)
class BrowserTypeAtTool(Tool):
    """Type at a vision bbox (V_n) or (x, y) coordinate. The bbox analogue
    of `browser_type(index, text)`.

    Checks the field's current value before typing — three outcomes the
    LLM sees in the return:
      - `skip_match`: field already contains the target text; no change.
      - `cleared_and_typed`: field had different content, cleared + typed.
      - `typed_into_empty`: field was empty, typed directly.

    Prefer this over `browser_click_at(V_n)` + `browser_keys([...])`,
    which appends at the cursor and turns `old|` + typing `new` into
    `oldnew` instead of `new`.
    """

    name = "browser_type_at"
    description = (
        "Type text into the input at a vision bbox (vision_index=V_n) or "
        "(x, y) coords. Probes the field's current value first and clears "
        "it (React-safe) before typing. Replaces click_at + keys for "
        "bbox-targeted typing — no more concatenation bugs."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        text: str,
        vision_index: int | None = None,
        x: float | None = None,
        y: float | None = None,
        clear: bool = True,
        **kw: Any,
    ) -> Any:
        # Phase 1.1: hard sync gate before mutation.
        sync_block = await self.s.ensure_vision_synced(reason="browser_type_at")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        if text is None:
            text = ""

        # Resolve target point: vision_index first, then (x, y).
        target_x: float
        target_y: float
        label: str
        if vision_index is not None:
            current_label = f"V{vision_index}"
            block = _autocomplete_pending_block(self.s, current_label)
            if block:
                return block
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return (
                    "[type_at_failed:no_vision] No recent vision response "
                    "to resolve vision_index against. Take a screenshot "
                    "first, or pass raw (x, y)."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                return (
                    f"[type_at_failed:bad_vision_index] V{vision_index} "
                    f"is out of range (only {len(resp.bboxes)} bboxes in "
                    "the last vision response)."
                )
            # Phase 1.3 turn-based age gate (mirrors BrowserClickAtTool).
            try:
                import os as _os_local
                _max_age = int(
                    _os_local.environ.get("VISION_MAX_AGE_TURNS") or "1"
                )
            except ValueError:
                _max_age = 1
            if _max_age > 0:
                _age = max(
                    0,
                    self.s._brain_turn_counter - 1
                    - self.s._vision_epoch_turn,
                )
                if _age > _max_age:
                    return (
                        f"[type_at_failed:epoch_too_old age_turns={_age} "
                        f"max={_max_age}] V{vision_index} resolves "
                        f"against a vision snapshot taken {_age} actions "
                        f"ago. Call browser_screenshot to refresh before "
                        f"typing."
                    )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return (
                    "[type_at_failed:no_image_dims] Last vision response "
                    "has no source image dimensions; cannot denormalize "
                    "box_2d. Take a fresh screenshot."
                )
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
            target_x = (x0 + x1) / 2
            target_y = (y0 + y1) / 2
            label = f"V{vision_index}"
            print(f"\n>> browser_type_at(V{vision_index}, text={text[:30]!r})")
        elif x is not None and y is not None:
            target_x = float(x)
            target_y = float(y)
            label = f"({int(target_x)},{int(target_y)})"
            block = _autocomplete_pending_block(self.s, label)
            if block:
                return block
            print(f"\n>> browser_type_at(({x},{y}), text={text[:30]!r})")
        else:
            return "[type_at_failed:bad_args] Provide either vision_index or both x and y."

        # Route through /evaluate (works on both t1 and t3) rather than
        # through a dedicated /type-at endpoint (t3-only). Mechanism is
        # identical to browser_fix_text_at: atomic probe → native-setter
        # write → dispatched input/change events → confirm-read.
        import json as _json
        atomic_js = _ATOMIC_FIX_TEXT_JS.replace(
            "__TARGET_X__", str(float(target_x))
        ).replace(
            "__TARGET_Y__", str(float(target_y))
        ).replace(
            "__TARGET_TEXT__", _json.dumps(text)
        )
        ev = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": atomic_js},
            timeout=30.0,
        )
        ev.raise_for_status()
        payload_body = ev.json()
        result = (
            payload_body.get("result") if isinstance(payload_body, dict) else None
        ) or {}
        if not isinstance(result, dict) or not result.get("ok"):
            reason = (result or {}).get("reason", "unknown") if isinstance(result, dict) else "bad_shape"
            # Educational redirect for the most common LLM hallucination:
            # typing into a non-input target. The atomic JS already detected
            # this and surfaced `tag` (button, td, div, …) — name it and
            # point at the right tool so the brain doesn't escalate to
            # browser_run_script.
            if reason == "not_input" and isinstance(result, dict):
                tag = str(result.get("tag", "") or "?")
                return (
                    f"[type_at_failed:not_input tag={tag}] at {label}. "
                    f"V_n is not a text input — the element under the "
                    f"cursor is a <{tag}>. If this is a calendar/date "
                    f"cell, call browser_pick_date(date='YYYY-MM-DD'). "
                    f"If it's any other clickable target (button, "
                    f"option, gridcell), call browser_click_at("
                    f"vision_index={vision_index if vision_index is not None else '<V_n>'}). "
                    f"Do not retry browser_type_at on this target."
                )
            return f"[type_at_failed:{reason}] at {label}. detail={result}"

        before = str(result.get("before", "") or "")
        after = str(result.get("after", "") or "")
        changed = bool(result.get("changed"))

        if not changed:
            caption = (
                f"Field at {label} already contained {text!r} — no typing "
                f"needed. Proceed to next action."
            )
        elif before:
            caption = (
                f'Typed "{text}" at {label} (replaced existing '
                f'{before!r}).'
            )
        else:
            caption = f'Typed "{text}" at {label}.'

        self.s.record_step(
            "browser_type_at",
            f"{label}, text={text[:30]!r}",
            "skip_match" if not changed else ("cleared_and_typed" if before else "typed_into_empty"),
        )
        synthetic_data = {
            "success": True,
            "before": before,
            "after": after,
            "changed": changed,
        }
        # Post-type semantic verification. Returns a caption suffix and
        # may have already corrected the field in place.
        if changed:
            from ...type_verify import verify_and_correct
            field_meta = {
                "label": str(result.get("label", "") or ""),
                "name": str(result.get("name", "") or ""),
                "autocomplete": str(result.get("autocomplete", "") or ""),
                "input_type": str(result.get("input_type", "") or ""),
            }
            outcome = await verify_and_correct(
                self.s, session_id,
                target_x=target_x, target_y=target_y,
                typed_text=text, label=label,
                page_url=self.s.current_url,
                field_meta=field_meta,
            )
            if outcome.kind == "corrected" and outcome.corrected_to:
                synthetic_data["after"] = outcome.after or outcome.corrected_to
                synthetic_data["auto_corrected"] = True
                synthetic_data["corrected_to"] = outcome.corrected_to
            caption += outcome.caption_suffix

        # Post-type autocomplete scan (mirrors browser_type). Surfaces
        # any visible suggestion list inline + sets the pending guard
        # so a follow-up type into a DIFFERENT field is refused until
        # the brain commits via browser_click_at(V_n).
        scan: dict = {"suggestions": [], "detected": False}
        if changed:
            scan = await _scan_autocomplete_suggestions(session_id)
        suggestions: list[dict] = scan.get("suggestions") or []
        detected: bool = bool(scan.get("detected"))
        if suggestions or detected:
            count_str = str(len(suggestions)) if suggestions else "?"
            caption += (
                f"\n\n[AUTOCOMPLETE_OPEN suggestions={count_str}] A "
                f"suggestion dropdown is now visible. NEXT TURN: take "
                f"a browser_screenshot — the dropdown items will be "
                f"labelled as V_n bboxes; click the one you want via "
                f"browser_click_at(vision_index=V_n). Any click you "
                f"attempt before that screenshot is REFUSED (the "
                f"current V_n / selectors are anchored to the "
                f"pre-dropdown page). Do NOT browser_run_script / "
                f"browser_eval to enumerate suggestions; the "
                f"screenshot will show them. Do NOT use Arrow+Enter; "
                f"many sites won't commit the value via keyboard."
            )
        if suggestions or detected:
            self.s.last_type_had_suggestions = True
            self.s.last_type_anchor_label = label
            self.s.last_type_at = time.time()
            self.s.last_autocomplete_suggestions = list(suggestions)
        else:
            # Clean type — clear any stale pending flag.
            self.s.last_type_had_suggestions = False
            self.s.last_type_anchor_label = ""
            self.s.last_autocomplete_suggestions = []

        # Phase 2.1: notify the active form_session that this field was
        # typed into. Promotes its FieldStatus to FILLED (or
        # AWAIT_AUTOCOMPLETE if declared with autocomplete=true at
        # form_begin). The worker hook reads the updated state on the
        # next iteration so the brain sees a refreshed checklist.
        if self.s.form_session is not None:
            try:
                if vision_index is not None:
                    self.s.form_session.mark_typed(
                        label_or_index=int(vision_index),
                        value_typed=text,
                        turn=self.s._brain_turn_counter,
                    )
                if label:
                    self.s.form_session.mark_typed(
                        label_or_index=label,
                        value_typed=text,
                        turn=self.s._brain_turn_counter,
                    )
            except Exception:
                pass
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(synthetic_data, caption),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index for the input to correct. "
                "Preferred over (x, y) when vision labelled the field."
            ),
            nullable=True,
        ),
        x=NumberSchema(description="X coord; used only when vision_index absent.", nullable=True),
        y=NumberSchema(description="Y coord; used only when vision_index absent.", nullable=True),
        text=StringSchema(
            "The EXACT final text the field should contain after the fix. "
            "This is the target state, not a diff or an instruction — give "
            "the corrected spelling / value verbatim."
        ),
        required=["session_id", "text"],
    )
)
class BrowserFixTextAtTool(Tool):
    """Set a text field to an exact target value in one atomic step.

    Human-like correction pathway: when you've noticed a typo or stale
    content ('dahka', 'old search', leftover default), call this with the
    CORRECT final text. The tool reads the current value, computes the
    minimal diff for logging, then writes the target with the React/Vue
    safe native-setter + input/change events — no intermediate empty
    state where a race could concatenate.

    Prefer this over click_at → clear → type_at when fixing a typo:
    surgical, single-call, deterministic.
    """

    name = "browser_fix_text_at"
    description = (
        "Atomically set an input / textarea / contenteditable to a target "
        "text value. Reads the current content, reports the diff, writes "
        "the correction in one step. Use this to fix typos or replace "
        "stale field values without multi-step click + clear + retype."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        text: str,
        vision_index: int | None = None,
        x: float | None = None,
        y: float | None = None,
        **kw: Any,
    ) -> Any:
        if text is None:
            text = ""

        # Resolve target point.
        if vision_index is not None:
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return (
                    "[fix_text_at_failed:no_vision] No recent vision response "
                    "to resolve vision_index against. Take a screenshot first "
                    "or pass raw (x, y)."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                return (
                    f"[fix_text_at_failed:bad_vision_index] V{vision_index} "
                    f"out of range (only {len(resp.bboxes)} bboxes)."
                )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return "[fix_text_at_failed:no_image_dims] take a fresh screenshot."
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
            target_x = (x0 + x1) / 2
            target_y = (y0 + y1) / 2
            label = f"V{vision_index}"
        elif x is not None and y is not None:
            target_x = float(x)
            target_y = float(y)
            label = f"({int(target_x)},{int(target_y)})"
        else:
            return "[fix_text_at_failed:bad_args] Provide vision_index or (x, y)."

        print(f"\n>> browser_fix_text_at({label}, target={text[:40]!r})")

        # Run the whole probe-write-verify cycle inside ONE /evaluate
        # call. /evaluate works on both t1 (TS server) and t3 (patchright
        # intercept), whereas a dedicated /fix-text-at endpoint only
        # exists on t3. Doing the full op in a single evaluate is also
        # race-free: elementFromPoint → native setter → confirm-read all
        # happen within one synchronous JS tick.
        import json as _json
        atomic_js = _ATOMIC_FIX_TEXT_JS.replace(
            "__TARGET_X__", str(float(target_x))
        ).replace(
            "__TARGET_Y__", str(float(target_y))
        ).replace(
            "__TARGET_TEXT__", _json.dumps(text)
        )
        ev = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": atomic_js},
            timeout=20.0,
        )
        ev.raise_for_status()
        payload = ev.json()
        result = (
            payload.get("result") if isinstance(payload, dict) else None
        ) or {}
        if not isinstance(result, dict):
            return f"[fix_text_at_failed] unexpected evaluate shape: {type(result).__name__}"

        if not result.get("ok"):
            return (
                f"[fix_text_at_failed:{result.get('reason','unknown')}] at "
                f"{label}. detail={result}"
            )

        before = str(result.get("before", "") or "")
        after = str(result.get("after", "") or "")
        changed = bool(result.get("changed"))
        diff = _diff_text(before, after) if changed else "no change"

        if not changed:
            caption = (
                f"Field at {label} already contained {text!r} — no change "
                f"needed. Proceed."
            )
        else:
            caption = (
                f"Fixed {label}: {before!r} → {after!r}\n"
                f"Edit: {diff}"
            )

        self.s.record_step(
            "browser_fix_text_at",
            f"{label}, target={text[:30]!r}",
            diff,
        )
        # Wrap result in the same shape build_text_only expects.
        synthetic_data = {
            "success": True,
            "before": before,
            "after": after,
            "changed": changed,
            "diff": diff,
        }
        if changed:
            from ...type_verify import verify_and_correct
            field_meta = {
                "label": str(result.get("label", "") or ""),
                "name": str(result.get("name", "") or ""),
                "autocomplete": str(result.get("autocomplete", "") or ""),
                "input_type": str(result.get("input_type", "") or ""),
            }
            outcome = await verify_and_correct(
                self.s, session_id,
                target_x=target_x, target_y=target_y,
                typed_text=text, label=label,
                page_url=self.s.current_url,
                field_meta=field_meta,
            )
            if outcome.kind == "corrected" and outcome.corrected_to:
                synthetic_data["after"] = outcome.after or outcome.corrected_to
                synthetic_data["auto_corrected"] = True
                synthetic_data["corrected_to"] = outcome.corrected_to
            caption += outcome.caption_suffix
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(synthetic_data, caption),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index"),
        text=StringSchema("Text to type"),
        clear=BooleanSchema(description="Clear field first (default: true)", default=True),
        required=["session_id", "index", "text"],
    )
)
class BrowserTypeTool(Tool):
    name = "browser_type"
    description = "Type text into an input field by its [index] number."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, text: str, clear: bool = True, **kw: Any) -> Any:
        print(f'\n>> browser_type([{index}], "{text}")')
        gate = await _feedback_gate("browser_type")
        if gate:
            return gate
        # Phase 1.1: hard sync gate.
        sync_block = await self.s.ensure_vision_synced(reason="browser_type")
        if sync_block:
            return sync_block
        # Autocomplete-pending guard (Phase 4): block a new field while
        # the prior type's suggestion dropdown is still unresolved.
        block = _autocomplete_pending_block(self.s, f"[{index}]")
        if block:
            return block
        self.s._brain_turn_counter += 1

        # --- Dead-type guard --------------------------------------------
        # The LLM's most destructive misread: type "khulna" → autocomplete
        # dropdown appears → LLM doesn't notice → retypes "khulna,
        # Bangladesh" → field now reads "khulnakhulna, Bangladesh". Catch
        # the second identical-ish type and force the LLM to inspect the
        # dropdown before retyping.
        now_ts = time.time()
        if (
            index == self.s.last_type_index
            and self.s.last_type_text
            and (now_ts - self.s.last_type_at) < 12.0
        ):
            last_lower = self.s.last_type_text.lower()
            cur_lower = text.lower()
            # Consider it a dead-type if: the new text starts with the old
            # text, OR the new text is a superset of the old (contains it),
            # OR it's exactly the same.
            duplicative = (
                cur_lower == last_lower
                or cur_lower.startswith(last_lower)
                or last_lower in cur_lower
            )
            if duplicative:
                self.s.record_step(
                    "browser_type",
                    f"index={index}, text={text[:30]!r}",
                    "DEAD_TYPE: refused (autocomplete likely)",
                )
                return (
                    f"[DEAD_TYPE_REJECTED] Refused to re-type into [{index}]. "
                    f"You already typed {self.s.last_type_text!r} into this "
                    f"field seconds ago. Typing again WILL concatenate "
                    f"(producing garbage like \"{self.s.last_type_text}{text}\"). "
                    f"An autocomplete dropdown almost certainly opened.\n"
                    f"PRIMARY FIX: take a browser_screenshot, then "
                    f"browser_click_at(vision_index=V_n) on the matching "
                    f"suggestion bbox. Bbox clicks land precisely on the "
                    f"suggestion text and commit the value.\n"
                    f"FALLBACK only if no suggestion bbox appears in vision: "
                    f"browser_keys ArrowDown+Enter (less reliable — some "
                    f"sites need a real click). Only retype if you pass "
                    f"clear=true AND the field is empty."
                )

        self.s.consecutive_click_calls += 1  # type is also step-by-step
        payload: dict[str, Any] = {"index": index, "text": text, "clear": clear}
        cached_fp = self.s.element_fingerprints.get(index)
        if cached_fp:
            payload["expected_fingerprint"] = cached_fp
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/type",
            json=payload,
            timeout=30.0,
        )
        if r.status_code == 409:
            info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            suggested = info.get("suggested_index")
            current = info.get("current_element", "")
            hint = f" Try [{suggested}]." if suggested is not None else " Re-read elements list and pick again."
            await _fetch_elements(session_id, self.s)
            return f"[stale_index] Element [{index}] is now {current}.{hint}"
        # Same structured-400 handling as BrowserClickTool — avoid
        # surfacing raw 'Client error 400' which empties Gemini's
        # next turn.
        if r.status_code == 400:
            info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            reason = info.get("reason", "unknown")
            err = info.get("error", f"type [{index}] failed")
            alternatives = info.get("alternatives") or []
            await _fetch_elements(session_id, self.s)
            self.s.log_activity(f"type([{index}])({reason})", err[:60])
            alt_lines = "\n".join(f"  - {a}" for a in alternatives[:3]) if alternatives else ""
            return (
                f"[type_failed:{reason}] {err}"
                + (f"\nAlternatives:\n{alt_lines}" if alt_lines else "")
                + "\nElements have been re-read above — pick a current [index]."
            )
        r.raise_for_status()
        data = r.json()

        # Record last-type state so the dead-type guard fires next time.
        self.s.last_type_index = index
        self.s.last_type_text = text
        self.s.last_type_at = time.time()

        # --- Post-type autocomplete dropdown scan -----------------------
        # Probe the page for newly-appeared autocomplete suggestions. If
        # we find any, surface them inline so the LLM picks one instead
        # of re-typing the full phrase.
        scan: dict = await _scan_autocomplete_suggestions(session_id)
        suggestions: list[dict] = scan.get("suggestions") or []
        detected: bool = bool(scan.get("detected"))
        if suggestions or detected:
            self.s.last_type_had_suggestions = True
            self.s.last_type_anchor_label = f"[{index}]"
            self.s.last_autocomplete_suggestions = list(suggestions)
        else:
            self.s.last_type_had_suggestions = False
            self.s.last_type_anchor_label = ""
            self.s.last_autocomplete_suggestions = []

        self.s.record_step(
            "browser_type",
            f'index={index}, text="{text[:30]}"',
            f"ok ({len(suggestions)} suggestions, detected={detected})" if (suggestions or detected) else "ok",
        )

        # Surface pre-type inspection info so the LLM knows whether we
        # actually changed the field. `pretype_action` is one of
        # `typed_into_empty` (field was empty), `cleared_and_typed`
        # (existing value replaced), or `skip_match` (field already
        # contained target text — no change).
        pre_action = data.get("pretype_action") if isinstance(data, dict) else None
        pre_value = data.get("pretype_value") if isinstance(data, dict) else None
        if pre_action == "skip_match":
            caption = (
                f'Field [{index}] already contained {text!r} — no typing '
                f'needed. Proceed to next action.'
            )
        elif pre_action == "cleared_and_typed":
            caption = (
                f'Typed "{text}" into [{index}] '
                f'(cleared existing {pre_value!r} first)'
            )
        else:
            caption = f'Typed "{text}" into [{index}]'
        if suggestions or detected:
            count_str = str(len(suggestions)) if suggestions else "?"
            caption += (
                f"\n\n[AUTOCOMPLETE_OPEN suggestions={count_str}] A "
                f"suggestion dropdown is now visible. NEXT TURN: take "
                f"a browser_screenshot — the dropdown items will be "
                f"labelled as V_n bboxes; click the one you want via "
                f"browser_click_at(vision_index=V_n). Any click you "
                f"attempt before that screenshot is REFUSED (the "
                f"current V_n / selectors are anchored to the "
                f"pre-dropdown page). Do NOT browser_run_script / "
                f"browser_eval to enumerate suggestions; the "
                f"screenshot will show them. Do NOT use Arrow+Enter; "
                f"many sites won't commit the value via keyboard."
            )

        # Post-type semantic verification (index-addressed variant).
        # Skip when the tool no-op'd (field already matched).
        if pre_action != "skip_match":
            from ...type_verify import verify_and_correct_by_index
            outcome = await verify_and_correct_by_index(
                self.s, session_id,
                dom_index=index, typed_text=text,
                page_url=self.s.current_url,
                field_meta={},
            )
            if outcome.kind == "corrected" and outcome.corrected_to:
                if isinstance(data, dict):
                    data["auto_corrected"] = True
                    data["corrected_to"] = outcome.corrected_to
            caption += outcome.caption_suffix

        # Prefetch vision so next screenshot call finds bboxes cached.
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, caption),
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        keys=StringSchema("Keys to send (e.g. Enter, ArrowDown, Tab)"),
        required=["session_id", "keys"],
    )
)
class BrowserKeysTool(Tool):
    name = "browser_keys"
    description = (
        "Send keyboard keys or shortcuts (Enter, Tab, Escape, "
        "Control+A, etc.). For autocomplete suggestions, prefer "
        "browser_click_at(vision_index=V_n) on the suggestion bbox "
        "— bbox clicks commit the value reliably across more sites; "
        "ArrowDown+Enter is a fallback only when no suggestion bbox "
        "is emitted."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, keys: str, **kw: Any) -> Any:
        print(f"\n>> browser_keys({keys})")
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/keys",
            json={"keys": keys},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        # Fetch updated elements after key press (e.g., Enter may submit form)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, f"Sent keys: {keys}"),
        )
