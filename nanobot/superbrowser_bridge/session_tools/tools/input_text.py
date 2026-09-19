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


# Selector families for visible suggestion items. Kept in one place so the
# pre-type signature and the post-type poll harvest the same set — the
# staleness test compares them, so any divergence would be a false signal.
_SUGGESTION_SELECTORS_JS = r"""
  const SELECTORS = [
    '[role="listbox"] [role="option"]',
    '[role="combobox"] + * li',
    '[role="combobox"] + * [role="option"]',
    '[role="option"]:not([aria-hidden="true"])',
    '[aria-selected]:not([aria-hidden="true"])',
    '.autocomplete-suggestions li, .autocomplete li',
    'ul.suggestions li, .suggestions li',
    '.MuiAutocomplete-listbox li',
    '[aria-live] li',
    '.dropdown-menu.show li, .dropdown-menu[style*="display: block"] li',
    '.ui-autocomplete li',
    '[class*="autocomplete"][class*="option"]',
    '[class*="suggestion"] li, [class*="suggestions"] li',
    '.ais-Hits-list .ais-Hits-item',
    '[class*="ais-Hits-item"]',
    '[class*="aa-Item"]',
    '[class*="aa-Suggestion"]',
    '[id^="downshift"] [role="option"]',
    '[id^="downshift"] li',
    '[class*="select__option"]',
    '[id*="-option-"]',
    '[data-reach-combobox-option]',
    '[id^="headlessui-listbox-option-"]',
    '[id^="headlessui-combobox-option-"]',
  ];
  function harvestSuggestions() {
    const seen = new Set();
    const out = [];
    for (const sel of SELECTORS) {
      let nodes;
      try { nodes = document.querySelectorAll(sel); } catch (e) { continue; }
      nodes.forEach(function (el) {
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
    return out;
  }
  function signatureOf(items) {
    return items.map(function (i) { return i.text; }).join('␟');
  }
"""

# Signature of what is on screen BEFORE a keystroke lands. The post-type
# poll needs it to answer the only question that matters: did this list
# respond to what I just typed, or is it left over from a previous query?
_AUTOCOMPLETE_PRESCAN_JS = "(() => {" + _SUGGESTION_SELECTORS_JS + """
  return signatureOf(harvestSuggestions());
})()
"""

# Post-type poll. Replaces a flat 300ms sleep, which was shorter than a
# network round trip and so routinely sampled the gap before results
# arrived — reporting "no suggestions" on a widget that was still
# fetching, or the PREVIOUS query's results on one that had not cleared.
_AUTOCOMPLETE_SCAN_TEMPLATE = "(async () => {" + _SUGGESTION_SELECTORS_JS + r"""
  const PREV = __PREV_SIG__;
  const TYPED = (__TYPED__ || '').trim().toLowerCase();
  const BUDGET = __BUDGET__;
  // The list must hold still this long before it is judged.
  const QUIET_MS = 500;

  const active = document.activeElement;
  const isTextEntry = !!active && (
    active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable
  );

  function attr(el, n) { return ((el && el.getAttribute(n)) || '').toLowerCase(); }

  // Does the widget advertise itself as an autocomplete? This describes
  // what the field IS, never whether a list is currently open, and must
  // not be allowed to stand in for the latter.
  let isAutocompleteInput = false;
  let popupId = null;
  if (isTextEntry) {
    const role = attr(active, 'role');
    const ac = attr(active, 'aria-autocomplete');
    const hp = attr(active, 'aria-haspopup');
    popupId = active.getAttribute('aria-controls') || null;
    isAutocompleteInput = (
      role === 'combobox' || role === 'searchbox' ||
      ac === 'list' || ac === 'both' || ac === 'inline' ||
      hp === 'listbox' || hp === 'menu' || hp === 'grid' ||
      hp === 'tree' || hp === 'dialog' ||
      attr(active, 'aria-expanded') === 'true' || !!popupId
    );
  }

  function popupEl() { return popupId ? document.getElementById(popupId) : null; }

  function popupVisibleNow() {
    const p = popupEl();
    if (!p) return false;
    const r = p.getBoundingClientRect();
    const cs = window.getComputedStyle(p);
    return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
  }

  // Is the widget still fetching? Without this the poll cannot tell "no
  // matches exist" from "the request has not come back yet", and those
  // two need opposite advice.
  function busyNow() {
    const scopes = [active, popupEl()].filter(Boolean);
    for (const el of scopes) {
      if (attr(el, 'aria-busy') === 'true') return true;
    }
    for (const el of scopes) {
      try {
        if (el.querySelector('[role="progressbar"], [aria-busy="true"], [class*="spinner"], [class*="loading"], [class*="Spinner"], [class*="Loading"]')) return true;
      } catch (e) { /* ignore */ }
    }
    if (document.documentElement.getAttribute('aria-busy') === 'true') return true;
    return false;
  }

  // Does any item plausibly answer what was typed? Soft evidence only:
  // a query like "JFK" legitimately returns "John F. Kennedy Airport".
  // Used to rescue the case where the same text is typed twice and the
  // list correctly does not change.
  function correspondsTo(items) {
    if (!TYPED) return false;
    const head = TYPED.split(/\s+/)[0];
    return items.some(function (i) {
      const t = (i.text || '').toLowerCase();
      return t.indexOf(TYPED) !== -1 || (head.length >= 3 && t.indexOf(head) !== -1);
    });
  }

  // Wait for the list to STOP changing rather than exiting on the first
  // non-empty read. Measured on booking.com: focusing the field paints a
  // "popular destinations" list within a few ms, and the real query
  // results replace it a few hundred ms later. An early exit therefore
  // reported the popular list as the answer to the query — and since one
  // of its entries happened to be "New York", even a correspondence check
  // was satisfied by it. Only a settled list can be judged.
  const start = Date.now();
  let items = [], sig = '', busy = false, timedOut = false;
  let lastSig = null, quietSince = 0;

  for (;;) {
    items = harvestSuggestions();
    sig = signatureOf(items);
    busy = busyNow();
    const elapsed = Date.now() - start;

    if (sig !== lastSig) { lastSig = sig; quietSince = elapsed; }
    if (!busy && (elapsed - quietSince) >= QUIET_MS) break;
    if (elapsed >= BUDGET) { timedOut = true; break; }
    await new Promise(function (r) { setTimeout(r, 100); });
  }

  const changed = sig !== PREV;
  const corresponds = correspondsTo(items);
  let state;
  if (items.length > 0) {
    // Correspondence is the only signal that survives a site which
    // leaves a previous query's results on screen — "changed" does not,
    // change. Two real shapes land in 'unrelated': booking.com leaving
    // the previous query's cities on screen, and booking.com answering
    // nonsense with a fuzzy fallback ("zzqqxxwwvv" -> "Mzuzu (ZZU)").
    // Neither is a match for what was typed. A legitimate match sharing
    // no substring (JFK -> "John F. Kennedy Airport") lands here too,
    // and its advice is to verify before clicking rather than assume —
    // the safe direction: it never asserts a match nobody checked.
    state = corresponds ? 'open' : 'unrelated';
  } else if (busy || timedOut) {
    state = 'pending';
  } else {
    state = 'empty';
  }

  return {
    suggestions: items.slice(0, 8),
    state: state,
    signature: sig,
    changed: changed,
    corresponds: corresponds,
    busy: busy,
    timed_out: timedOut,
    elapsed_ms: Date.now() - start,
    is_autocomplete_input: isAutocompleteInput,
    popup_visible: popupVisibleNow(),
  };
})()
"""


async def _prescan_suggestion_signature(session_id: str) -> str:
    """Signature of the suggestion list as it stands BEFORE a keystroke.

    Cheap (one evaluate, no waiting) and load-bearing: without it the
    post-type poll cannot distinguish a list that answered this keystroke
    from one left over from the previous query.
    """
    try:
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": _AUTOCOMPLETE_PRESCAN_JS},
            timeout=5.0,
        )
        if r.status_code != 200:
            return ""
        got = r.json().get("result")
        return got if isinstance(got, str) else ""
    except Exception:
        # No signature just means the staleness test abstains.
        return ""


_AUTOCOMPLETE_BUDGET_MS = 3000


async def _scan_autocomplete_suggestions(
    session_id: str,
    *,
    prev_signature: str = "",
    typed_text: str = "",
    budget_ms: int = _AUTOCOMPLETE_BUDGET_MS,
) -> dict:
    """Probe autocomplete state after typing.

    Returns a `state` that the caller must respect:

      open    — a list is on screen AND it answered this keystroke.
      unrelated — a list is on screen but nothing in it matches what was
                typed: the previous query's results left rendered, or a
                fuzzy fallback for a query with no real match.
      empty   — no list, and the widget is not fetching. There genuinely
                are no suggestions.
      pending — still fetching when the budget ran out. Nothing is
                settled; the caller must not describe the page yet.

    `detected` is retained for back-compat and now means exactly
    "state == 'open'". It used to be true whenever the focused field
    merely LOOKED like a combobox, which made the caller announce an open
    dropdown over a closed one.
    """
    empty: dict = {
        "suggestions": [], "state": "empty", "detected": False,
        "signature": "", "changed": False, "corresponds": False,
        "busy": False, "timed_out": False, "elapsed_ms": 0,
        "is_autocomplete_input": False, "popup_visible": False,
    }
    script = (
        _AUTOCOMPLETE_SCAN_TEMPLATE
        .replace("__PREV_SIG__", _json_top.dumps(prev_signature or ""))
        .replace("__TYPED__", _json_top.dumps(typed_text or ""))
        .replace("__BUDGET__", str(int(budget_ms)))
    )
    try:
        sr = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": script},
            # The probe polls for up to budget_ms inside the page, so the
            # HTTP timeout has to clear it with room to spare.
            timeout=(budget_ms / 1000.0) + 6.0,
        )
        if sr.status_code != 200:
            return empty
        got = sr.json().get("result")
        if not isinstance(got, dict):
            return empty
        suggestions = [
            x for x in (got.get("suggestions") or [])
            if isinstance(x, dict) and x.get("text")
        ]
        state = str(got.get("state") or "empty")
        if state not in ("open", "unrelated", "empty", "pending"):
            state = "empty"
        out = {
            "suggestions": suggestions,
            "state": state,
            "detected": state == "open",
            "signature": str(got.get("signature") or ""),
            "changed": bool(got.get("changed")),
            "corresponds": bool(got.get("corresponds")),
            "busy": bool(got.get("busy")),
            "timed_out": bool(got.get("timed_out")),
            "elapsed_ms": int(got.get("elapsed_ms") or 0),
            "is_autocomplete_input": bool(got.get("is_autocomplete_input")),
            "popup_visible": bool(got.get("popup_visible")),
        }
        print(
            f"  [autocomplete scan: state={out['state']} "
            f"suggestions={len(suggestions)} changed={out['changed']} "
            f"corresponds={out['corresponds']} busy={out['busy']} "
            f"waited={out['elapsed_ms']}ms]"
        )
        return out
    except Exception as exc:
        print(f"  [autocomplete scan failed: {exc}]")
    return empty


def _release_stuck_autocomplete_field(
    state: "BrowserSessionState",
    label: str | None,
    index: int | None,
    attempts: int,
    typed_text: str,
) -> None:
    """Let a form field out of AWAIT_AUTOCOMPLETE when no list ever came.

    Without this the release in the caption is advice the worker cannot
    act on: `browser_form_commit` refuses to submit while any field is
    pending, and the worker hook re-injects the checklist every single
    iteration. Telling the brain to move on while the form machinery
    keeps saying the field is unfinished is how a worker burns its whole
    budget on one input.
    """
    if attempts < getattr(state, "AUTOCOMPLETE_GIVE_UP_AFTER", 2):
        return
    sess = getattr(state, "form_session", None)
    if sess is None:
        return
    try:
        key = label or ""
        if not key and index is not None:
            fs = sess._match_field(index)
            key = fs.label if fs is not None else ""
        if key:
            sess.mark_autocomplete_unavailable(key, observed_value=typed_text)
    except Exception:
        pass


def _autocomplete_caption(scan: dict, typed_text: str, attempts: int = 0) -> str:
    """Model-facing text for an autocomplete probe.

    The rule this enforces: never assert that a dropdown is open unless
    items were actually seen on screen, and never present items as
    matches for `typed_text` unless they answered it. The previous
    version said "A suggestion dropdown is open" whenever the focused
    field looked like a combobox — including with the listbox display:none
    and zero options — and then told the model to click "the matching
    V_n". Being ordered to pick from a list that does not exist is what
    the confabulated selections were.
    """
    state = scan.get("state") or "empty"
    items: list[dict] = scan.get("suggestions") or []
    sample = "; ".join((s.get("text") or "")[:80] for s in items[:5])
    waited = scan.get("elapsed_ms") or 0
    quoted = f'"{typed_text}"' if typed_text else "the text"

    # Past the give-up point the advice has to invert. Every other branch
    # below tells the worker how to get a suggestion list; repeated
    # unchanged, that is what a 20-iteration retype/eval/screenshot loop
    # is made of. The worker also has a form checklist re-injected every
    # iteration and a commit gate that blocks on pending fields, so
    # nothing else in the system will tell it to stop.
    give_up = attempts >= 2 and state != "open"

    def _release(msg: str) -> str:
        if not give_up:
            return msg
        return msg + (
            f" \n[AUTOCOMPLETE_GIVE_UP] You have now typed into this field "
            f"{attempts} times without a usable suggestion list, and the probe "
            "waits for the list to settle before reporting — so it is not "
            "arriving. STOP retyping this field and stop hunting for the "
            "dropdown with eval/markdown/region crops. The typed value is on "
            "the page. Move on: submit or press the site's search control with "
            "the value as typed, or reach the result another way (a direct URL "
            "with query parameters). If a form checklist still lists this "
            "field, that is expected — proceed anyway."
        )

    if state == "open":
        return (
            f"\n\n[AUTOCOMPLETE_OPEN suggestions={len(items)}] A suggestion "
            f"dropdown is open"
            + (f". Visible items: {sample}." if sample else ".")
            + " Call browser_screenshot, then "
            "browser_click_at(vision_index=V_n) on the matching V_n."
        )

    if state == "unrelated":
        return _release(
            f"\n\n[AUTOCOMPLETE_UNRELATED suggestions={len(items)}] A dropdown is "
            f"on screen, but none of its items match {quoted}"
            + ("" if scan.get("changed") else " and it did not change when you typed")
            + (f". It shows: {sample}." if sample else ".")
            + " Three things cause this: the list is left over from an "
            "earlier query, the site answered with a fuzzy fallback because "
            "nothing really matched, or its results for your query are still "
            "on their way and what you see is what was there before. In none "
            f"of those cases are these confirmed matches for {quoted}, so do "
            "NOT click one on the assumption that it is. Confirm the field "
            "really contains what you meant, give the list another moment "
            "with browser_wait_for on the item you expect, and only click an "
            "item that genuinely corresponds."
        )

    if state == "pending":
        return _release(
            f"\n\n[AUTOCOMPLETE_PENDING] The suggestion list was still loading "
            f"{waited}ms after {quoted} was entered, so nothing is settled "
            "yet. Do NOT describe or click suggestions from this turn. Use "
            "browser_wait_for for the item you expect, or take a fresh "
            "browser_screenshot before deciding."
        )

    # empty
    note = (
        f"\n\n[AUTOCOMPLETE_EMPTY] No suggestion list appeared for {quoted} "
        f"(waited {waited}ms and the field is not still loading)."
    )
    if scan.get("is_autocomplete_input"):
        note += (
            " The field IS an autocomplete, so the usual cause is that the "
            "query matched nothing — a typo, or text that landed differently "
            "than intended."
        )
    note += (
        " There is nothing on screen to pick. Do NOT invent a suggestion or "
        "click where one would have been. Confirm the field's real value "
        "(browser_get_markdown or browser_screenshot), correct it if wrong, "
        "or proceed without the dropdown."
    )
    return _release(note)


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
        # What is on screen BEFORE the keystroke. The post-type probe
        # compares against this to tell a list that answered this input
        # from one left over from a previous query.
        pre_sig = await _prescan_suggestion_signature(session_id)
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
        scan: dict = {"suggestions": [], "state": "empty", "detected": False}
        if changed and not is_clear:
            scan = await _scan_autocomplete_suggestions(
                session_id,
                prev_signature=pre_sig,
                typed_text=text,
            )
            self.s.record_suggestion_signature(scan.get("signature") or "")
        suggestions: list[dict] = scan.get("suggestions") or []
        ac_state = str(scan.get("state") or "empty")
        field_key = (
            f"v{vision_index}" if vision_index is not None
            else f"{int(target_x)},{int(target_y)}"
        )
        attempts = self.s.note_autocomplete_attempt(
            field_key, usable=(ac_state == "open"),
        )
        if ac_state in ("open", "unrelated", "pending") or suggestions:
            caption += _autocomplete_caption(scan, text, attempts=attempts)
            self.s.last_type_at = time.time()
        _release_stuck_autocomplete_field(self.s, label, vision_index, attempts, text)

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
        # Pre-keystroke suggestion signature — see browser_type_at.
        pre_sig = await _prescan_suggestion_signature(session_id)
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
        scan: dict = await _scan_autocomplete_suggestions(
            session_id, prev_signature=pre_sig, typed_text=text,
        )
        self.s.record_suggestion_signature(scan.get("signature") or "")
        suggestions: list[dict] = scan.get("suggestions") or []
        state: str = str(scan.get("state") or "empty")

        self.s.record_step(
            "browser_type",
            f'index={index}, text="{text[:30]}"',
            f"ok (autocomplete {state}, {len(suggestions)} suggestions)",
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
        attempts = self.s.note_autocomplete_attempt(
            f"i{index}", usable=(state == "open"),
        )
        if state in ("open", "unrelated", "pending") or suggestions:
            caption += _autocomplete_caption(scan, text, attempts=attempts)
        _release_stuck_autocomplete_field(self.s, None, index, attempts, text)

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
