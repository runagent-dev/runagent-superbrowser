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

from ..effects import _ATOMIC_FIX_TEXT_JS, _diff_text, render_atomic_text_js
from ._click_core import resolve_epoch_target
from ..feedback import _feedback_gate
from ..formatting import _fetch_elements
from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState
from ..vision_pipeline import _append_fresh_vision, _schedule_vision_prefetch


async def _clear_via_keys_escalation(
    session_id: str, target_x: float, target_y: float,
) -> dict | None:
    """Fallback clear when the atomic native-setter empty was reverted by a
    controlled component. The field is already focused (the atomic JS ran
    el.focus()); press Ctrl+A then Delete, reset the React ``_valueTracker``,
    and re-probe. Returns a synthetic atomic-result dict ``{ok, before, after,
    changed}`` or None if the escalation errored. Works on both tiers (/keys +
    /evaluate exist on t1 and t3). One shot — the caller does not retry.
    """
    try:
        for combo in ("Control+a", "Delete"):
            await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/keys",
                json={"keys": combo},
                timeout=10.0,
            )
        probe_js = (
            "(() => { const el = document.elementFromPoint("
            f"{float(target_x)}, {float(target_y)});"
            " if (!el) return {ok:false, reason:'no_element'};"
            " try { const t = el._valueTracker;"
            " if (t && typeof t.setValue === 'function') t.setValue(''); } catch(e){}"
            " const v = ('value' in el && el.value !== undefined)"
            " ? (el.value || '') : (el.innerText || '');"
            " return {ok: v === '', before: '', after: v, changed: true,"
            " method: 'keys_clear'}; })()"
        )
        ev = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": probe_js},
            timeout=10.0,
        )
        body = ev.json()
        res = body.get("result") if isinstance(body, dict) else None
        if isinstance(res, dict):
            return res
    except Exception as exc:
        print(f"  [clear keys-escalation failed: {exc}]")
    return None


async def _insert_text_escalation(
    session_id: str, target_x: float, target_y: float, text: str,
) -> dict | None:
    """Rich-text editor escalation when the atomic execCommand write was
    reverted (``is_editable`` and not ``ok``). Focus via a real (trusted)
    click → Ctrl+A to select all → clear (Delete for empty, else CDP
    Input.insertText replaces the selection) → re-probe. Returns a synthetic
    atomic-result dict or None. Both tiers: /click, /keys, /insert-text exist
    on t1 and t3. One shot — the caller does not retry.
    """
    try:
        await _request_with_backoff(
            "POST", f"{SUPERBROWSER_URL}/session/{session_id}/click",
            json={"x": float(target_x), "y": float(target_y)}, timeout=10.0,
        )
        await _request_with_backoff(
            "POST", f"{SUPERBROWSER_URL}/session/{session_id}/keys",
            json={"keys": "Control+a"}, timeout=10.0,
        )
        if text == "":
            await _request_with_backoff(
                "POST", f"{SUPERBROWSER_URL}/session/{session_id}/keys",
                json={"keys": "Delete"}, timeout=10.0,
            )
        else:
            await _request_with_backoff(
                "POST", f"{SUPERBROWSER_URL}/session/{session_id}/insert-text",
                json={"text": text}, timeout=10.0,
            )
        probe_js = (
            "(() => { const el = document.elementFromPoint("
            f"{float(target_x)}, {float(target_y)});"
            " if (!el) return {ok:false, reason:'no_element'};"
            " const v = ('value' in el && el.value !== undefined)"
            " ? (el.value || '') : (el.innerText || '');"
            " const n = (s) => (s||'').replace(/\\u200b/g,'').replace(/\\s+/g,' ').trim();"
            f" return {{ok: n(v) === n({_json_top.dumps(text)}), before: '',"
            " after: v, changed: true, method: 'cdp_insert_text'}; })()"
        )
        ev = await _request_with_backoff(
            "POST", f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": probe_js}, timeout=10.0,
        )
        body = ev.json()
        res = body.get("result") if isinstance(body, dict) else None
        if isinstance(res, dict):
            return res
    except Exception as exc:
        print(f"  [insert_text escalation failed: {exc}]")
    return None


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
        w: Math.round(r.width),
        h: Math.round(r.height),
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
                "true (default): REPLACE the field's existing value (React/Vue-"
                "safe overwrite). false: APPEND text to the end of the current "
                "value instead. Pass text=\"\" with clear=true to EMPTY the "
                "field (delete everything)."
            ),
            default=True,
        ),
        required=["session_id", "text"],
    )
)
class BrowserTypeAtTool(Tool):
    """Type at a vision bbox (V_n) or (x, y) coordinate. The bbox analogue
    of `browser_type(index, text)`.

    Checks the field's current value before typing — outcomes the LLM sees
    in the return:
      - `skip_match`: field already contains the target text; no change.
      - `cleared_and_typed`: field had different content, replaced it.
      - `typed_into_empty`: field was empty, typed directly.
      - `appended`: clear=false, text added to the end of the existing value.
      - `cleared_to_empty`: text="", the field was emptied.

    Prefer this over `browser_click_at(V_n)` + `browser_keys([...])`,
    which appends at the cursor and turns `old|` + typing `new` into
    `oldnew` instead of `new`.
    """

    name = "browser_type_at"
    description = (
        "Type text into the input at a vision bbox (vision_index=V_n) or "
        "(x, y) coords. clear=true (default) REPLACES the field; clear=false "
        "APPENDS; text=\"\" empties it. Probes the current value first and "
        "writes React-safe. Replaces click_at + keys for bbox-targeted "
        "typing — no more concatenation bugs."
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
            # Shared epoch resolver: no_vision / bad_index / age gate /
            # image-dims / to_pixels(center) / per-epoch scroll-anchor gate.
            # The scroll gate is the fix for the /evaluate-typed path, which
            # has no TS-side viewport gate — a scroll between screenshot and
            # type_at would otherwise write at the pre-scroll coordinates.
            resolved = await resolve_epoch_target(
                self.s, session_id, int(vision_index),
                fail_prefix="type_at_failed",
                verb="typing",
                extra_hint=(
                    "NOTE: if a sibling browser_type_at in this same turn "
                    "returned [not_input], this V_n is likely also a date/time "
                    "picker trigger or value-bearing button — use "
                    f"browser_click_at(vision_index={vision_index}) to open the "
                    "popup, then screenshot. See SOUL.md \"Date & time pickers\"."
                ),
            )
            if isinstance(resolved, str):
                return resolved
            target_x, target_y, _bbox, _resp = resolved
            label = f"V{vision_index}"
            print(f"\n>> browser_type_at(V{vision_index}, text={text[:30]!r})")
        elif x is not None and y is not None:
            target_x = float(x)
            target_y = float(y)
            label = f"({int(target_x)},{int(target_y)})"
            print(f"\n>> browser_type_at(({x},{y}), text={text[:30]!r})")
        else:
            return "[type_at_failed:bad_args] Provide either vision_index or both x and y."

        # Route through /evaluate (works on both t1 and t3) rather than
        # through a dedicated /type-at endpoint (t3-only). Mechanism is
        # identical to browser_fix_text_at: atomic probe → native-setter
        # write → dispatched input/change events → confirm-read.
        #   clear=True  (default) → replace the field's value (overwrite).
        #   clear=False           → append to the existing value (no more
        #                           silent concatenation surprises — the caller
        #                           opted in). The final value is computed from
        #                           the live `before` inside the same JS tick.
        _mode = "replace" if clear else "append"
        atomic_js = render_atomic_text_js(
            target_x, target_y, text, mode=_mode,
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
                vi_str = (
                    str(vision_index)
                    if vision_index is not None else "<V_n>"
                )
                return (
                    f"[type_at_failed:not_input tag={tag}] at {label}. "
                    f"V_n is not a text input — the element under the "
                    f"cursor is a <{tag}>. Call browser_click_at("
                    f"vision_index={vi_str}) "
                    f"instead — calendar cells, time options, buttons, "
                    f"and gridcells all dispatch via a CDP click. Do "
                    f"not retry browser_type_at on this target.\n"
                    f"DATE / TIME PICKER PATTERN: if the field's visible "
                    f"label reads like a VALUE (e.g. 'May 24, 2026', "
                    f"'1:00 PM', 'Today, 10:00 AM'), it is almost "
                    f"certainly a picker trigger that opens a calendar "
                    f"or time popup — NOT a text input. Workflow: "
                    f"(1) browser_click_at(vision_index={vi_str}) to "
                    f"open the popup, (2) browser_screenshot so vision "
                    f"labels the calendar grid + month arrows + time "
                    f"options as fresh V_n, (3) click next/prev month "
                    f"to reach the target month (NEVER click 'Previous "
                    f"month' when target is in the future), (4) click "
                    f"the day cell, (5) click the time option if a "
                    f"separate one exists. Do NOT browser_run_script to "
                    f"set React state — pickers keep separate state "
                    f"that ignores DOM writes. See SOUL.md \"Date & "
                    f"time pickers\" for the full pattern."
                )
            return f"[type_at_failed:{reason}] at {label}. detail={result}"

        before = str(result.get("before", "") or "")
        after = str(result.get("after", "") or "")
        changed = bool(result.get("changed"))
        is_clear = (text == "" and clear)
        is_append = (not clear and text != "")

        if not changed:
            caption = (
                f"Field at {label} was already empty — no change needed."
                if is_clear else
                f"Field at {label} already contained {text!r} — no typing "
                f"needed. Proceed to next action."
            )
        elif is_clear:
            caption = f"Cleared field at {label} (was {before!r})."
        elif is_append:
            caption = f'Appended "{text}" at {label} (now: {after!r}).'
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
            (
                "skip_match" if not changed else
                "cleared_to_empty" if is_clear else
                "appended" if is_append else
                ("cleared_and_typed" if before else "typed_into_empty")
            ),
        )
        synthetic_data = {
            "success": True,
            "before": before,
            "after": after,
            "changed": changed,
        }
        # Post-type semantic verification. Returns a caption suffix and
        # may have already corrected the field in place. Skipped on a pure
        # clear-to-empty (nothing to correct toward).
        if changed and not is_clear:
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

        # Post-type autocomplete scan. Surfaces any visible suggestion
        # list inline + sets last_type_at so the dead-type guard can
        # catch a re-type into the same field. Skipped on a pure clear.
        scan: dict = {"suggestions": [], "detected": False}
        if changed and not is_clear:
            scan = await _scan_autocomplete_suggestions(session_id)
        suggestions: list[dict] = scan.get("suggestions") or []
        detected: bool = bool(scan.get("detected"))
        if suggestions or detected:
            count_str = str(len(suggestions)) if suggestions else "?"
            sample = "; ".join(
                ((s.get("text") or "")[:80]) for s in suggestions[:5]
            )
            caption += (
                f"\n\n[AUTOCOMPLETE_OPEN suggestions={count_str}] A "
                f"suggestion dropdown is open"
                + (f". Visible items: {sample}." if sample else ".")
                + " Call browser_screenshot, then "
                f"browser_click_at(vision_index=V_n) on the matching V_n."
            )
            self.s.last_type_at = time.time()

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
        "stale field values without multi-step click + clear + retype. "
        "Pass text=\"\" to EMPTY a field (delete all its content) — this is "
        "the canonical way to clear an input; it dispatches a React/Vue-safe "
        "clear and, if a controlled component re-hydrates, escalates to "
        "Ctrl+A+Delete automatically."
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
        # Age the vision epoch across this mutation (mirrors type_at).
        self.s._brain_turn_counter += 1
        if text is None:
            text = ""

        # Resolve target point via the shared epoch resolver (adds the same
        # age gate + scroll-anchor gate the other bbox tools use).
        if vision_index is not None:
            resolved = await resolve_epoch_target(
                self.s, session_id, int(vision_index),
                fail_prefix="fix_text_at_failed",
                verb="setting the value",
            )
            if isinstance(resolved, str):
                return resolved
            target_x, target_y, _bbox, _resp = resolved
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
        # fix_text_at always REPLACES (writes the exact target value in one
        # React/Vue-safe op). Pass text="" to empty the field.
        atomic_js = render_atomic_text_js(
            target_x, target_y, text, mode="replace",
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
            # Clear-to-empty (text="") that the native setter couldn't make
            # stick — some controlled components re-hydrate the old value.
            # Escalate ONCE via focus→Ctrl+A→Delete→tracker-reset, then re-probe.
            if text == "" and str(result.get("after", "") or "") != "":
                escalated = await _clear_via_keys_escalation(
                    session_id, target_x, target_y,
                )
                if isinstance(escalated, dict):
                    result = escalated
            # Rich-text editor that reverted even the execCommand write
            # (canvas / model-backed editors): escalate ONCE to a CDP-trusted
            # Input.insertText via /insert-text.
            if not result.get("ok") and result.get("is_editable"):
                esc2 = await _insert_text_escalation(
                    session_id, target_x, target_y, text,
                )
                if isinstance(esc2, dict):
                    result = esc2
            if not result.get("ok"):
                return (
                    f"[fix_text_at_failed:{result.get('reason','unknown')}] at "
                    f"{label}. detail={result}"
                )

        before = str(result.get("before", "") or "")
        after = str(result.get("after", "") or "")
        changed = bool(result.get("changed"))
        diff = _diff_text(before, after) if changed else "no change"
        is_clear = text == ""

        if not changed:
            caption = (
                f"Field at {label} was already empty — no change needed."
                if is_clear else
                f"Field at {label} already contained {text!r} — no change "
                f"needed. Proceed."
            )
        elif is_clear:
            caption = f"Cleared field at {label} (was {before!r})."
        else:
            caption = (
                f"Fixed {label}: {before!r} → {after!r}\n"
                f"Edit: {diff}"
            )

        self.s.record_step(
            "browser_fix_text_at",
            f"{label}, target={text[:30]!r}",
            "cleared_to_empty" if (is_clear and changed) else diff,
        )
        # Wrap result in the same shape build_text_only expects.
        synthetic_data = {
            "success": True,
            "before": before,
            "after": after,
            "changed": changed,
            "diff": diff,
        }
        # Skip the post-type verify/correct pass when clearing to empty —
        # there is nothing to "correct" toward, and the corrector would try
        # to re-type the (empty) target.
        if changed and not is_clear:
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
        op=StringSchema(
            "Edit operation: 'delete_tail' removes the last `count` characters "
            "from the field; 'append' adds `text` to the end."
        ),
        vision_index=IntegerSchema(
            description="1-based vision bbox V_n of the field.", nullable=True,
        ),
        x=NumberSchema(
            description="X (CSS px). Ignored when vision_index is set.",
            nullable=True,
        ),
        y=NumberSchema(
            description="Y (CSS px). Ignored when vision_index is set.",
            nullable=True,
        ),
        count=IntegerSchema(
            description="delete_tail: number of trailing characters to delete "
            "(default 1).",
            nullable=True,
        ),
        text=StringSchema(
            "append: the text to add to the end of the field.", nullable=True,
        ),
        required=["session_id", "op"],
    )
)
class BrowserEditTextAtTool(Tool):
    """Positional text edit at a vision bbox (V_n) or (x, y): delete the last
    N characters, or append to the end — WITHOUT overwriting the whole field.

    The final value is computed from the field's live value inside one atomic
    JS tick (via the shared _ATOMIC_FIX_TEXT_JS template), so a read-then-write
    can't race a debounced re-render. Works on both t1 and t3 (routes through
    /evaluate). For a full overwrite use browser_fix_text_at; to empty a field
    use browser_fix_text_at(text="").
    """

    name = "browser_edit_text_at"
    description = (
        "Edit text at a field without replacing all of it. op='delete_tail' "
        "deletes the last `count` characters; op='append' adds `text` to the "
        "end. Use browser_fix_text_at(text=...) to replace the whole value, "
        "or browser_fix_text_at(text=\"\") to empty it."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        op: str,
        vision_index: int | None = None,
        x: float | None = None,
        y: float | None = None,
        count: int | None = 1,
        text: str | None = None,
        **kw: Any,
    ) -> Any:
        sync_block = await self.s.ensure_vision_synced(reason="browser_edit_text_at")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1

        op_norm = (op or "").strip().lower()
        if op_norm not in ("delete_tail", "append"):
            return (
                "[edit_text_at_failed:bad_op] op must be 'delete_tail' or "
                "'append'. To replace the whole value use browser_fix_text_at."
            )
        if op_norm == "append" and not text:
            return "[edit_text_at_failed:bad_args] op='append' needs a non-empty text."
        try:
            n = int(count) if count is not None else 1
        except (TypeError, ValueError):
            n = 1
        if op_norm == "delete_tail" and n <= 0:
            return "[edit_text_at_failed:bad_args] delete_tail needs count >= 1."

        # Resolve target point (shared epoch resolver → age + scroll gates).
        if vision_index is not None:
            resolved = await resolve_epoch_target(
                self.s, session_id, int(vision_index),
                fail_prefix="edit_text_at_failed", verb="editing",
            )
            if isinstance(resolved, str):
                return resolved
            target_x, target_y, _bbox, _resp = resolved
            label = f"V{vision_index}"
        elif x is not None and y is not None:
            target_x, target_y = float(x), float(y)
            label = f"({int(target_x)},{int(target_y)})"
        else:
            return "[edit_text_at_failed:bad_args] Provide vision_index or both x and y."

        print(f"\n>> browser_edit_text_at({label}, op={op_norm}, count={n})")
        atomic_js = render_atomic_text_js(
            target_x, target_y, text or "", mode=op_norm, count=n,
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
        if not isinstance(result, dict) or not result.get("ok"):
            reason = (
                (result or {}).get("reason", "unknown")
                if isinstance(result, dict) else "bad_shape"
            )
            return f"[edit_text_at_failed:{reason}] at {label}. detail={result}"

        before = str(result.get("before", "") or "")
        after = str(result.get("after", "") or "")
        changed = bool(result.get("changed"))
        if op_norm == "delete_tail":
            caption = (
                f"Deleted last {n} char(s) at {label}: {before!r} → {after!r}."
                if changed else
                f"Field at {label} unchanged (nothing left to delete)."
            )
        else:
            caption = (
                f'Appended "{text}" at {label} (now: {after!r}).'
                if changed else f"Field at {label} unchanged."
            )
        self.s.record_step(
            "browser_edit_text_at",
            f"{label}, op={op_norm} count={n}",
            "changed" if changed else "no_change",
        )
        synthetic_data = {
            "success": True, "before": before, "after": after, "changed": changed,
        }
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
    description = (
        "Type text into an input field by its [index] number. "
        "Note: [index] refers to elements in the TOP-LEVEL document only. "
        "For inputs inside an <iframe> (quizzes, calculators, embedded "
        "forms), use browser_type_at(vision_index=V_n) — its atomic "
        "JS descends into same-origin iframes automatically."
    )

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
            sample = "; ".join(
                ((s.get("text") or "")[:80]) for s in suggestions[:5]
            )
            caption += (
                f"\n\n[AUTOCOMPLETE_OPEN suggestions={count_str}] A "
                f"suggestion dropdown is open"
                + (f". Visible items: {sample}." if sample else ".")
                + " Call browser_screenshot, then "
                f"browser_click_at(vision_index=V_n) on the matching V_n."
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
        # Keys can submit / navigate / edit — age the vision epoch across it.
        self.s._brain_turn_counter += 1
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
