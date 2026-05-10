"""Click tools — DOM-index, vision-bbox (V_n), CSS-selector, and rect probe.

`BrowserClickTool` (DOM index), `BrowserClickAtTool` (vision-bbox / coords),
`BrowserGetRectTool` (read-only rect probe), `BrowserClickSelectorTool`
(CSS-selector fast path).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)

from ..effects import _maybe_no_effect_prefix
from ..feedback import _feedback_gate
from ..formatting import _fetch_elements, _vision_alternatives_hint
from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState
from ..vision_pipeline import _append_fresh_vision, _schedule_vision_prefetch


# Dynamic-ID detector for `browser_click_selector`.
#
# React 18's `useId()` emits IDs like `:r13:` (a leading colon + base-32
# counter + trailing colon). Radix wraps it as `radix-:r13:`, Headless
# UI uses `headlessui-*-NN`, and some libraries stamp `__id_NNNN`. All
# four rotate between renders — the brain captures the ID once, the
# page re-mounts, and the selector is stale.
#
# `re.search` (not `re.match`) so compound selectors like
# `.modal #radix-:r13:` are caught too. Idempotent w.r.t. escaped
# colons (`\\?` accepts the optional backslash).
_DYNAMIC_ID_RE = re.compile(
    r"#(?:"
    r"(?:[a-zA-Z_][\w-]*)?\\?:r[a-z0-9]+"
    r"|headlessui-"
    r"|__id_"
    r")"
)


# Playwright / jQuery extension pseudo-selectors. The brain knows these
# from training data and assumes they're CSS — but `document.querySelector`
# throws SyntaxError on them, which the TS getRects path swallows. The
# brain then sees "selector not found or zero-size" (indistinguishable
# from a real missing element) and goes on a 5-tool fishing trip.
#
# Catching upfront with a regex turns 8 turns into 1: the brain gets a
# clear advisory pointing it to `browser_click_at(vision_index=...)`.
#
# Matches:
#   :has-text("X"), :contains("X")        — text matching
#   :visible, :hidden                     — jQuery visibility
#   :eq(N), :first, :last, :odd, :even    — jQuery indexing
#   :button, :input, :checkbox, :radio,
#   :submit, :selected, :enabled,
#   :disabled, :file, :image, :password,
#   :reset, :text                         — jQuery form filters
#                                            (note: `:enabled`/`:disabled`
#                                            ARE valid CSS but emitted
#                                            often in jQuery patterns
#                                            with `:button` etc.)
#   text=, role=, xpath=                  — Playwright engine prefixes
#   >> (chain operator)                   — Playwright selector chain
#
# `:not(...)`, `:is(...)`, `:has(...)`, `:where(...)`, `:nth-child(...)`,
# `:first-child`, `:first-of-type`, `:last-child`, etc. and other
# STANDARD CSS pseudos are explicitly NOT in this set — they parse
# fine in querySelector. The negative lookahead `(?![-\w])` after each
# bare jQuery keyword rejects the standard-CSS hyphenated forms (e.g.
# `:first-child` is CSS, `:first` alone is jQuery).
_PLAYWRIGHT_PSEUDO_RE = re.compile(
    r"(?:"
    r":has-text\("
    r"|:contains\("
    r"|:visible(?![-\w])"
    r"|:hidden(?![-\w])"
    r"|:eq\("
    r"|:first(?![-\w])"
    r"|:last(?![-\w])"
    r"|:odd(?![-\w])"
    r"|:even(?![-\w])"
    r"|:button(?![-\w])"
    r"|:input(?![-\w])"
    r"|:checkbox(?![-\w])"
    r"|:radio(?![-\w])"
    r"|:submit(?![-\w])"
    r"|:selected(?![-\w])"
    r"|:file(?![-\w])"
    r"|:image(?![-\w])"
    r"|:password(?![-\w])"
    r"|:reset(?![-\w])"
    r"|>>\s*text="
    r"|>>\s*role="
    r"|>>\s*xpath="
    r"|(?:^|\s)text="
    r"|(?:^|\s)role="
    r"|(?:^|\s)xpath="
    r")",
    re.IGNORECASE,
)


def _click_pending_screenshot_block(state: BrowserSessionState) -> str | None:
    """Refuse a click when an autocomplete dropdown is open and the
    brain hasn't taken a fresh screenshot since typing — the V_n / CSS
    selector the brain is about to act on is anchored to the
    pre-typing page state. Without this gate the click lands on
    whatever happens to be at that bbox in the OLD vision response,
    which is "the click had no effect" from the brain's perspective.

    Mirrors `_autocomplete_pending_block` in input_text.py but applies
    to clicks instead of types. Returns the refusal string when the
    guard fires, else None.
    """
    if not getattr(state, "last_type_had_suggestions", False):
        return None
    last_type_at = float(getattr(state, "last_type_at", 0.0) or 0.0)
    last_shot_at = float(getattr(state, "last_screenshot_at", 0.0) or 0.0)
    if last_shot_at >= last_type_at and last_type_at > 0.0:
        # A screenshot was taken AFTER the type opened the dropdown —
        # the brain has fresh vision; let the click proceed.
        return None
    anchor = getattr(state, "last_type_anchor_label", "") or "the input"
    return (
        f"[click_pending_screenshot] You typed into {anchor} and an "
        f"autocomplete dropdown opened, but you have NOT taken a "
        f"screenshot since. The V_n indices and CSS selectors you "
        f"have right now are anchored to the page BEFORE the dropdown "
        f"appeared, so this click would land on whatever element was "
        f"at that bbox in the pre-dropdown page (typically a no-op "
        f"or the wrong element). Call browser_screenshot first; the "
        f"dropdown items will then be labelled as V_n bboxes you can "
        f"click via browser_click_at(vision_index=V_n)."
    )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index"),
        button=StringSchema("Mouse button: left, right, middle", nullable=True),
        required=["session_id", "index"],
    )
)
class BrowserClickTool(Tool):
    name = "browser_click"
    description = "Click an interactive element by its [index] number."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, button: str | None = None, **kw: Any) -> Any:
        print(f"\n>> browser_click([{index}])")
        gate = await _feedback_gate("browser_click")
        if gate:
            return gate
        # Phase 1.1: hard sync gate. Wait for any in-flight vision
        # prefetch from the previous action before dispatching.
        sync_block = await self.s.ensure_vision_synced(reason="browser_click")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        # Cross-index flail guard. If the last two clicks timed out,
        # force a re-screenshot before dispatching another HTTP click —
        # the backend is hung (blocker, loader, nav in flight) and
        # walking [N±1] just wastes the iteration budget.
        if self.s.consecutive_click_timeouts >= self.s.MAX_CONSECUTIVE_CLICK_TIMEOUTS:
            alts = _vision_alternatives_hint(self.s, limit=3)
            self.s.log_activity(
                f"click([{index}])(LOOP_BLOCKED)",
                f"timeouts={self.s.consecutive_click_timeouts}",
            )
            return (
                f"[click_loop_detected] {self.s.consecutive_click_timeouts} "
                f"consecutive click timeouts. The page is likely blocked "
                f"(loader, modal, or a pending navigation). Call "
                f"browser_screenshot to refresh vision before any further "
                f"click."
                + (f"\n{alts}" if alts else "")
            )
        target_key = f"click[{index}]"
        # Cross-tool same-element guard: if the previous click (via any
        # tool) resolved to DOM index `index`, refuse this click unless
        # the page has been re-observed (fresh screenshot clears
        # last_click_dom_index). Catches the bbox-then-DOM cascade where
        # browser_click_at(V_n) toggled a checkbox ON and the brain
        # then clicks [N] (the same checkbox by index) and un-toggles it.
        dead = self.s.check_dead_click(target_key, dom_index=int(index))
        if dead:
            self.s.log_activity(f"click([{index}])(DEAD_CLICK_BLOCKED)", "")
            return dead
        self.s.register_click_attempt(target_key, target_dom_index=int(index))
        # Surgical undo: open a pending entry. We don't know pre_active
        # for DOM-index clicks (the dead-click guard's bbox path is the
        # only place is_active is read at click time). The label safety
        # net + url_changed demotion in finalize_click_record will still
        # classify it correctly.
        self.s.begin_click_record(
            tool="browser_click",
            target_key=target_key,
            vision_index=None,
            label=f"index={index}",
            box_2d=None,
            pre_active=None,
            expected_url_change=False,
            is_form_submit=False,
        )
        self.s.consecutive_click_calls += 1
        # Refuse a stale click after type opened a dropdown. The brain
        # MUST take a screenshot first so V_n / DOM-index resolves
        # against the post-type page state.
        pending = _click_pending_screenshot_block(self.s)
        if pending:
            return pending
        # Releases the autocomplete-pending guard set by browser_type /
        # browser_type_at. Clicking commits a suggestion (or moves on
        # intentionally), so type tools may proceed afterward.
        self.s.last_type_had_suggestions = False
        self.s.last_type_anchor_label = ""
        self.s.last_autocomplete_suggestions = []
        payload: dict[str, Any] = {"index": index}
        if button:
            payload["button"] = button
        # Send the fingerprint the LLM was targeting. If the DOM shifted,
        # the TS side returns 409 + stale_index with a suggested new index.
        cached_fp = self.s.element_fingerprints.get(index)
        if cached_fp:
            payload["expected_fingerprint"] = cached_fp
        elif self.s.element_fingerprints:
            # The cache has entries, just not for this index — the brain
            # is addressing an index that wasn't in the last state
            # response. Almost always means stale. Surface fast instead
            # of letting the TS click fail obscurely.
            await _fetch_elements(session_id, self.s)
            if index not in self.s.element_fingerprints:
                return (
                    f"[click_failed:unknown_index] [{index}] is not in "
                    f"the current selectorMap (fingerprints={len(self.s.element_fingerprints)} "
                    f"indices). Re-read the elements list and pick a "
                    f"valid index, or use browser_click_at(V_n) with a "
                    f"vision bbox."
                )
            cached_fp = self.s.element_fingerprints.get(index)
            if cached_fp:
                payload["expected_fingerprint"] = cached_fp

        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/click",
                json=payload,
                timeout=30.0,
            )
            # 409 = stale-index guard fired. Surface the suggested
            # index (if any) so the LLM retargets instead of blindly
            # retrying or falling back to click_at coords.
            if r.status_code == 409:
                info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                stale_msg = info.get("error", "Stale index")
                suggested = info.get("suggested_index")
                current = info.get("current_element", "")
                hint = f" Try [{suggested}]." if suggested is not None else " Re-read elements list and pick again."
                result = f"[stale_index] {stale_msg} Current [{index}] is {current}.{hint}"
                self.s.log_activity(f"click([{index}])(STALE)", f"suggested={suggested}")
                await _fetch_elements(session_id, self.s)
                return result
            # 400 = structured TS-side failure (element not found,
            # not visible, disabled, etc.). Parse and return an
            # actionable message to the LLM.
            if r.status_code == 400:
                info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                reason = info.get("reason", "unknown")
                err = info.get("error", f"click [{index}] failed")
                alternatives = info.get("alternatives") or []
                await _fetch_elements(session_id, self.s)
                self.s.log_activity(f"click([{index}])({reason})", err[:60])
                alt_lines = "\n".join(f"  - {a}" for a in alternatives[:3]) if alternatives else ""
                fresh_hint = "\nElements have been re-read above — pick a current [index]."
                # Phase 3.1: cursor failure ledger.
                self.s.record_cursor_failure(
                    strategy="click",
                    target=f"[{index}]",
                    reason=f"{reason}: {err[:80]}",
                )
                return (
                    f"[click_failed:{reason}] {err}"
                    + (f"\nAlternatives:\n{alt_lines}" if alt_lines else "")
                    + fresh_hint
                )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            # Opaque 4xx/5xx (not 400/409). Usually network-layer.
            self.s.log_activity(f"click([{index}])(HTTP{e.response.status_code})", str(e)[:60])
            return (
                f"[click_failed:http_{e.response.status_code}] {e.response.text[:200] if e.response.text else str(e)[:200]}"
            )
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout) as e:
            # Click dispatched but the backend never responded — almost
            # always means the page is blocked (a pending navigation, a
            # loader still running, or an overlay intercepting events).
            # Count it so the flail guard above trips on the next call.
            self.s.consecutive_click_timeouts += 1
            self.s.log_activity(
                f"click([{index}])(TIMEOUT)",
                f"count={self.s.consecutive_click_timeouts}",
            )
            alts = _vision_alternatives_hint(
                self.s, exclude_index=None, limit=3,
            )
            return (
                f"[click_failed:timeout] The backend didn't respond to "
                f"click([{index}]) within the HTTP timeout. The page is "
                f"likely waiting on navigation or blocked by a loader. "
                f"Call browser_screenshot to re-vision before retrying."
                + (f"\n{alts}" if alts else "")
            )
        except Exception as e:
            # True transport error (connection refused, etc.). Server down.
            self.s.log_activity(f"click([{index}])(TRANSPORT)", str(e)[:60])
            return f"[click_failed:transport] {str(e)[:200]} — browser service unreachable. Retry in a few seconds."

        # Successful HTTP response — clear the timeout counter so the
        # flail guard doesn't trip on a future unrelated hiccup.
        self.s.consecutive_click_timeouts = 0
        # Element-mismatch escape (mirrors BrowserClickAtTool). The
        # backend's clickInBbox Phase 2 grid-scan refused to dispatch
        # because the element at index [N]'s live bounds is now a
        # different label than expected — page shifted, list re-
        # ordered, etc. Without this check, a 200-OK with
        # error=element_mismatch was being treated as success and the
        # brain reported "Clicked [N]" while the page hadn't actually
        # changed (the long-standing "burst but no nav" symptom on
        # DOM-index click). Surface as a structured failure so the
        # brain re-screenshots and picks again.
        if isinstance(data, dict) and data.get("error") == "element_mismatch":
            found = data.get("found", {}) or {}
            self.s.snap_miss_count += 1
            self.s.record_cursor_failure(
                strategy="click",
                target=f"[{index}]",
                reason=(
                    f"element_mismatch tag={(found.get('tag') or '?').lower()} "
                    f"text={(found.get('text') or '')[:60]!r}"
                ),
            )
            self.s.log_activity(
                f"click([{index}])(ELEM_MISMATCH)",
                f"found={found.get('tag','?')}",
            )
            await _fetch_elements(session_id, self.s)
            return (
                f"[click_failed:element_mismatch] DOM index [{index}] "
                f"resolves to a <{(found.get('tag') or '?').lower()} "
                f"role='{found.get('role','')}'> with text='"
                f"{(found.get('text') or '')[:80]}', which doesn't "
                f"match the expected label for [{index}]. The page "
                f"likely re-rendered (filter applied, list re-sorted) "
                f"and [{index}] now points at a different element. "
                f"Re-read the elements list above and pick a current "
                f"[index], or call browser_screenshot to refresh "
                f"vision before retrying."
            )
        # Surgical undo: finalize the pending entry with the response.
        # `pre_url` / `pre_dom_hash` were already captured at
        # begin_click_record time, so finalize doesn't need them passed
        # in. Demotes toggle→nav when effect.url_changed is true.
        self.s.finalize_click_record(response=data)
        # Auto-refresh element_fingerprints so the next click ships a
        # current expected_fingerprint for any [N] in the cache. (B6)
        _fp_map = data.get("fingerprints") if isinstance(data, dict) else None
        if isinstance(_fp_map, dict):
            self.s.element_fingerprints = {
                int(k): v for k, v in _fp_map.items() if isinstance(v, str)
            }
        # v6 F1 — invalidate the frozen vision epoch when this click
        # produced a significant DOM mutation. The brain's V_n indices
        # captured before this click point at PRE-shift coords; if we
        # let the next click_at resolve against the frozen epoch it
        # will land on the wrong element. Forcing the next click to
        # require a fresh screenshot (or letting the in-flight
        # prefetch settle) catches the page-shift misclick cascade.
        verify_note = ""
        try:
            effect = (data or {}).get("effect") or {}
            mutation_delta = int(effect.get("mutation_delta") or 0)
            url_changed_eff = bool(effect.get("url_changed"))
            try:
                # Default 4 (was 8): a small filter that hides 1-2
                # checkboxes produces ~5-8 mutations, just above the
                # old threshold. 4 was too aggressive — every accordion
                # toggle / tab switch / chevron expand sits at 4-8
                # mutations and invalidating the vision epoch on those
                # surfaces [epoch_invalidated] on the brain's next
                # legitimate click, which conditioned the brain to
                # distrust clicks and drift toward eval/run_script.
                # Middle ground 6 covers filter applies (>=7 mutations)
                # but rides over light UI toggles. Override via env
                # MUTATION_DIRTY_THRESHOLD if a specific site needs it.
                threshold = int(
                    os.environ.get("MUTATION_DIRTY_THRESHOLD") or "6"
                )
            except ValueError:
                threshold = 6
            if mutation_delta > threshold:
                self.s._vision_epoch_response = None
                self.s.log_activity(
                    f"click([{index}])(EPOCH_DIRTY)",
                    f"mutation_delta={mutation_delta} > {threshold}",
                )
            # Silent-click detection — same shape as BrowserClickAtTool's
            # post-click verify, but in-band: zero mutations + no URL
            # change is the canonical "click went out, page didn't react"
            # signal. Surface as [click_silent] so the brain re-screen-
            # shots / tries a different target instead of believing
            # `Clicked [N]` succeeded. Cheap (no extra HTTP — the effect
            # block is already in the response).
            if (
                mutation_delta == 0
                and not url_changed_eff
                and os.environ.get("VERIFY_AFTER_CLICK", "1") != "0"
            ):
                verify_note = (
                    f"\n[click_silent reason=no_effect] Click [{index}] "
                    f"dispatched but produced zero DOM mutations and no "
                    f"URL change. Target may be non-interactive, covered "
                    f"by an overlay, or the page is mid-load. Call "
                    f"browser_screenshot to re-vision before retrying."
                )
                self.s.log_activity(f"click([{index}])(SILENT)", "no_effect")
        except Exception:
            pass
        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.record_url(actual_url)
        # Snap telemetry (P3.12).
        snap = data.get("snap") if isinstance(data, dict) else None
        if isinstance(snap, dict) and snap.get("snapped") is False:
            self.s.snap_miss_count += 1
        self.s.log_activity(f"click([{index}])", f"url={actual_url[:50] if actual_url else '?'}")
        self.s.record_step("browser_click", f"index={index}", f"url={actual_url[:60] if actual_url else '?'}")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, f"Clicked [{index}]") + verify_note,
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index (the V_n the vision agent "
                "labelled this element). When set, the server snaps to "
                "the interactive element inside that bbox — far more "
                "accurate than clicking a guessed (x,y)."
            ),
            nullable=True,
        ),
        x=NumberSchema(description="X coordinate (CSS pixel). Ignored when vision_index is set.", nullable=True),
        y=NumberSchema(description="Y coordinate (CSS pixel). Ignored when vision_index is set.", nullable=True),
        required=["session_id"],
    )
)
class BrowserClickAtTool(Tool):
    name = "browser_click_at"
    description = (
        "Click using a vision bbox (vision_index=V_n) or raw (x,y) "
        "coordinates. Prefer vision_index whenever the vision agent "
        "labelled the target — the server snaps to the actual interactive "
        "element inside the bbox, eliminating off-by-pixel misses.\n\n"
        "TOGGLE SEMANTICS: when V_n shows `active=true` (a filter chip, "
        "checkbox, or radio that's already ON), clicking it AGAIN will "
        "UN-toggle it (un-apply the filter, uncheck the box, deselect "
        "the radio). This is the natural undo for filter mistakes — "
        "if you accidentally applied the wrong filter, just re-click "
        "the same V_n. Do NOT use browser_navigate or "
        "browser_rewind_to_checkpoint to undo a filter; those reload "
        "the page and lose other filtering progress.\n\n"
        "JUST-TOGGLED MARKER: after a click, the next vision response "
        "may show `just_toggled=on` or `just_toggled=off` next to "
        "`active=` — that means YOUR last click flipped this control. "
        "If `just_toggled=on` appeared on a control whose label "
        "doesn't match the task, re-click the same V_n to reverse it."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        vision_index: int | None = None,
        x: float | None = None,
        y: float | None = None,
        **kw: Any,
    ) -> Any:
        # Phase 1.1: hard sync gate. Block until the in-flight vision
        # prefetch from the previous action lands — without this the
        # brain's V_n resolves against a frozen epoch but the freshness
        # gate has no fresh post-action vision to validate against.
        sync_block = await self.s.ensure_vision_synced(reason="browser_click_at")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        self.s.click_at_count += 1
        self.s.consecutive_click_calls += 1
        # Refuse stale click after type opened a dropdown — see helper.
        pending = _click_pending_screenshot_block(self.s)
        if pending:
            return pending
        # Release the autocomplete-pending guard (see BrowserClickTool).
        self.s.last_type_had_suggestions = False
        self.s.last_type_anchor_label = ""
        self.s.last_autocomplete_suggestions = []
        if self.s.click_at_count > self.s.MAX_CLICK_AT:
            return (
                f"[BLOCKED] browser_click_at used "
                f"{self.s.click_at_count} times in this session. The "
                f"task is looping on clicks — call browser_screenshot "
                f"to re-observe, then try browser_click_selector with "
                f"a stable CSS hook, or browser_rewind_to_checkpoint "
                f"if the page is stuck. Do NOT attempt "
                f"browser_run_script to click — JS clicks are "
                f"isTrusted=false and bot-detected."
            )

        # Build the target key BEFORE resolving the bbox, so the guard
        # fires on intent (vision_index=V3) not on resolved coords (which
        # could shift slightly between calls due to anti-aliasing).
        if vision_index is not None:
            target_key = f"click_at(V{int(vision_index)})"
        elif x is not None and y is not None:
            # Round to a 5px grid — micro-jitter shouldn't escape the guard.
            target_key = f"click_at({round(float(x)/5)*5},{round(float(y)/5)*5})"
        else:
            target_key = "click_at(?)"
        # For the (x, y) path we don't have a bbox to read is_active
        # from, so run the dead-click guard with no toggle context here.
        # The vision_index path defers the check until AFTER bbox
        # resolution (line ~XYZ below) so it can pass the bbox's
        # current is_active and trigger the toggle-aware exemption.
        if vision_index is None:
            dead = self.s.check_dead_click(target_key)
            if dead:
                self.s.log_activity(f"click_at{target_key}(DEAD_CLICK_BLOCKED)", "")
                return dead
            self.s.register_click_attempt(target_key)
            # Surgical undo: open a pending entry for raw-coord clicks.
            # No bbox/label info — pre_active is None, classification
            # falls through to "toggle" then demotes to "nav" if the
            # click ends up navigating.
            self.s.begin_click_record(
                tool="browser_click_at",
                target_key=target_key,
                vision_index=None,
                label=f"({x},{y})",
                box_2d=None,
                pre_active=None,
                expected_url_change=False,
                is_form_submit=False,
            )

        payload: dict[str, Any]
        log_target: str
        if vision_index is not None:
            # HARD epoch-invalidated gate. When a previous click's
            # mutation_delta exceeded MUTATION_DIRTY_THRESHOLD,
            # `_vision_epoch_response` was reset to None to keep V_n
            # indices from resolving against shifted positions. But
            # without this gate, `vision_for_target_resolution` falls
            # back to `_last_vision_response`, which the background
            # prefetch may have just refreshed with NEW bboxes the
            # brain HAS NOT SEEN — so V_n (picked from the OLD bbox
            # list in the brain's mental model) silently resolves to
            # a different element in the NEW bbox list.
            #
            # `_vision_epoch_id > 0` distinguishes "epoch was
            # invalidated mid-session" (refuse, force re-screenshot)
            # from "first turn ever / mock-test no screenshot taken
            # yet" (let it fall through, original behaviour). The
            # epoch_id increments on every freeze_vision_epoch().
            if (
                self.s._vision_epoch_response is None
                and getattr(self.s, "_vision_epoch_id", 0) > 0
            ):
                # HARD reject. Auto-recovery (refreshing vision + re-
                # resolving V_n against the new response) is NOT safe
                # here: V_n is a POSITIONAL index into a ranked bbox
                # list, and re-ranking on a mutated page puts a
                # different element at position N. A "successful"
                # recovery would silently click the wrong element.
                # The only correct path is to force the brain to call
                # `browser_screenshot`, see the new bbox numbering,
                # and re-pick V_n — even though that costs one extra
                # turn.
                alts = _vision_alternatives_hint(
                    self.s, exclude_index=int(vision_index), limit=3,
                )
                self.s.log_activity(
                    f"click_at(V{vision_index})(EPOCH_INVALIDATED)",
                    f"epoch_id={getattr(self.s, '_vision_epoch_id', 0)}",
                )
                return (
                    f"[click_at_failed:epoch_invalidated] V"
                    f"{vision_index} resolves against a vision "
                    f"snapshot that has been invalidated since you "
                    f"saw it (a previous click changed the page "
                    f"meaningfully — the bbox indices no longer "
                    f"line up with the current page state). Call "
                    f"browser_screenshot to refresh vision before "
                    f"clicking."
                    + (f"\n{alts}" if alts else "")
                )

            # Prefer the frozen epoch (what the brain SAW on its last
            # screenshot), fall back to the live response only when no
            # epoch is set yet (pre-first-screenshot path / tests).
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return (
                    "[click_at_failed:no_vision] No recent vision response "
                    "to resolve vision_index against. Re-fetch state to "
                    "trigger a fresh vision pass, or pass raw (x, y)."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                return (
                    f"[click_at_failed:bad_vision_index] V{vision_index} "
                    f"is out of range (only {len(resp.bboxes)} bboxes in "
                    "the last vision response)."
                )
            # Freshness gate — refuse to click when the last vision pass
            # flagged the screenshot as stale or uncertain. The planner
            # should re-screenshot before committing a click on a frame
            # the model itself said it couldn't trust.
            freshness = getattr(resp, "screenshot_freshness", "fresh")
            if freshness != "fresh":
                self.s.record_cursor_failure(
                    strategy="click_at",
                    target=f"V{vision_index}",
                    reason=f"stale_vision freshness={freshness}",
                )
                alts = _vision_alternatives_hint(
                    self.s, exclude_index=int(vision_index), limit=3,
                )
                return (
                    f"[click_at_failed:stale_vision freshness={freshness}] "
                    "Vision flagged the last screenshot as not fresh "
                    "(URL/page mismatch or loading overlay). Call "
                    "browser_screenshot to refresh vision before clicking."
                    + (f"\n{alts}" if alts else "")
                )
            # Phase 1.3 turn-based age gate. Beyond
            # VISION_MAX_AGE_TURNS mutating actions since the last
            # screenshot, the V_n indices the brain captured no longer
            # reliably point at the elements they did when the
            # screenshot was taken. The brain MUST re-screenshot. Wall-
            # clock isn't a useful proxy because a long thinking pause
            # doesn't mutate the page; the right unit is "actions
            # taken between epoch and now". _brain_turn_counter was
            # bumped by ensure_vision_synced for THIS click already, so
            # subtract 1 to count actions BEFORE this one.
            try:
                max_age_turns = int(
                    os.environ.get("VISION_MAX_AGE_TURNS") or "1"
                )
            except ValueError:
                max_age_turns = 1
            if max_age_turns > 0:
                age_turns = max(
                    0,
                    self.s._brain_turn_counter - 1
                    - self.s._vision_epoch_turn,
                )
                if age_turns > max_age_turns:
                    alts = _vision_alternatives_hint(
                        self.s, exclude_index=int(vision_index), limit=3,
                    )
                    return (
                        f"[click_at_failed:epoch_too_old age_turns="
                        f"{age_turns} max={max_age_turns}] V"
                        f"{vision_index} resolves against a vision "
                        f"snapshot taken {age_turns} actions ago — the "
                        f"page state may have shifted. Call "
                        f"browser_screenshot to refresh the V_n "
                        f"indices before clicking."
                        + (f"\n{alts}" if alts else "")
                    )
            # Blocker gate — if the scene has an active blocker layer
            # (cookie banner, modal, consent dialog) and this bbox lives
            # in a different layer, refuse. The planner must dismiss
            # the blocker before acting on content beneath it.
            scene = getattr(resp, "scene", None)
            active_blocker = (
                getattr(scene, "active_blocker_layer_id", None)
                if scene is not None else None
            )
            if active_blocker:
                bbox_layer = getattr(bbox, "layer_id", None)
                if bbox_layer and bbox_layer != active_blocker:
                    # Find the dismiss hint from the blocker layer so
                    # the brain has a concrete target to click first.
                    dismiss_hint = ""
                    try:
                        for layer in (getattr(scene, "layers", []) or []):
                            if getattr(layer, "id", None) == active_blocker:
                                dismiss_hint = (
                                    getattr(layer, "dismiss_hint", "") or ""
                                )
                                break
                    except Exception:
                        dismiss_hint = ""
                    hint = f" Dismiss '{dismiss_hint}' first." if dismiss_hint else ""
                    return (
                        f"[click_at_failed:blocker_active layer={active_blocker}] "
                        f"A blocker layer ({active_blocker}) is on top of "
                        f"content, and V{vision_index} sits in a different "
                        f"layer ({bbox_layer}).{hint} Then re-screenshot."
                    )
            # Confidence gate — a low-confidence bbox is Gemini's way of
            # saying "I'm not sure this is really here". Clicking it
            # lands on the wrong target more often than not. Threshold
            # is tuned via VISION_MIN_CLICK_CONFIDENCE (default 0.45).
            try:
                min_conf = float(
                    os.environ.get("VISION_MIN_CLICK_CONFIDENCE") or "0.45"
                )
            except ValueError:
                min_conf = 0.45
            if getattr(bbox, "confidence", 0.5) < min_conf:
                alts = _vision_alternatives_hint(
                    self.s, exclude_index=int(vision_index), limit=3,
                )
                return (
                    f"[click_at_failed:low_confidence V{vision_index}] "
                    f"bbox confidence={bbox.confidence:.2f} < "
                    f"{min_conf:.2f}. Call browser_screenshot to re-run "
                    "vision, then retry with a higher-confidence target."
                    + (f"\n{alts}" if alts else "")
                )
            # B5: precondition gate. When this bbox has a parent
            # expand-button (resolved via aria-controls during DOM
            # enrichment) AND that parent is currently collapsed
            # (aria_expanded='false'), refuse — clicking the child
            # would land on something not yet rendered or already
            # selected at group-level. Brain has to expand first.
            if os.environ.get(
                "BBOX_PRECONDITION_GATE", "1"
            ) not in ("0", "false", "no"):
                parent_v = getattr(bbox, "parent_expand_v", None)
                if isinstance(parent_v, int) and parent_v > 0:
                    parent_bbox = resp.get_bbox(parent_v)
                    parent_expanded = (
                        getattr(parent_bbox, "aria_expanded", None)
                        if parent_bbox is not None
                        else None
                    )
                    if parent_expanded == "false":
                        parent_label = (
                            getattr(parent_bbox, "label", "")
                            if parent_bbox is not None
                            else ""
                        )
                        my_label = (
                            getattr(bbox, "label", "") or ""
                        ).strip()
                        self.s.record_cursor_failure(
                            strategy="click_at",
                            target=f"V{vision_index}",
                            reason=(
                                f"expand_parent_first V{parent_v} "
                                f"(parent={parent_label!r})"
                            ),
                        )
                        return (
                            f"[click_at_blocked:expand_parent_first "
                            f"V{parent_v}] V{vision_index} "
                            f"({my_label!r}) is a child of a "
                            f"COLLAPSED group {parent_label!r} "
                            f"(V{parent_v}, aria_expanded=false). "
                            f"Clicking V{vision_index} now would land "
                            f"on a hidden / not-yet-rendered element. "
                            f"Click V{parent_v} FIRST to expand the "
                            f"group, then re-screenshot and retry "
                            f"V{vision_index}."
                        )
            # v4 C3 — bbox-aware dead-click guard. Pass the bbox's
            # CURRENT is_active so the guard recognizes a re-click as
            # a legitimate filter toggle (rather than flailing) when
            # the state has flipped since the last click on this V_n.
            bbox_active_now = bool(getattr(bbox, "is_active", False))
            # Cross-tool: vision_pipeline's DOM enrichment populates
            # bbox.dom_index when it can match a vision bbox to a
            # selectorMap entry. Pass it to the dead-click guard so a
            # bbox click on the same DOM element a previous DOM-index
            # click toggled (less common direction, but valid) is also
            # caught. The TS-resolved snap.dom_index in the response
            # below will refine this if it differs.
            bbox_dom_index = getattr(bbox, "dom_index", None)
            dead = self.s.check_dead_click(
                target_key,
                current_active_state=bbox_active_now,
                dom_index=(
                    int(bbox_dom_index)
                    if isinstance(bbox_dom_index, int) and bbox_dom_index >= 0
                    else None
                ),
            )
            if dead:
                self.s.log_activity(
                    f"click_at{target_key}(DEAD_CLICK_BLOCKED)", "",
                )
                return dead
            # v4 C6 — record the bbox's label, current active state,
            # and box_2d so the post-click vision pass can match the
            # SAME bbox in the new response and stamp `just_toggled`
            # if its is_active flipped.
            bbox_label_for_state = (
                getattr(bbox, "label", "") or ""
            )[:120]
            box_2d_copy = list(getattr(bbox, "box_2d", []) or []) or None
            self.s.register_click_attempt(
                target_key,
                target_label=bbox_label_for_state,
                target_active_state=bbox_active_now,
                target_box_2d=box_2d_copy,
                target_dom_index=(
                    int(bbox_dom_index)
                    if isinstance(bbox_dom_index, int) and bbox_dom_index >= 0
                    else None
                ),
            )
            # Surgical undo: open a pending entry now we have full bbox
            # context. is_form_submit heuristic on the bbox label catches
            # destructive primary buttons; the safety-net regex inside
            # begin_click_record catches the rest.
            _is_submitlike = bool(
                bbox_label_for_state
                and re.search(
                    r"(?i)\b(submit|search|sign\s*in|sign\s*up|register"
                    r"|continue|next|place\s+order|buy|pay|checkout)\b",
                    bbox_label_for_state,
                )
            )
            self.s.begin_click_record(
                tool="browser_click_at",
                target_key=target_key,
                vision_index=int(vision_index),
                label=bbox_label_for_state,
                box_2d=box_2d_copy,
                pre_active=bbox_active_now,
                expected_url_change=False,
                is_form_submit=_is_submitlike,
            )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return (
                    "[click_at_failed:no_image_dims] Last vision response "
                    "has no source image dimensions; cannot denormalize "
                    "box_2d. Re-fetch state."
                )
            # CDP/JS expects CSS pixels; on retina/HiDPI viewports the
            # screenshot is physical-pixel-sized so we divide by DPR.
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
            payload = {"bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}
            # Carry the vision label into the click payload so the T3
            # backend can run a post-snap semantic match check. Empty
            # label → the check is skipped on the backend, which is
            # fine for raw-coord clicks further below.
            bbox_label = (getattr(bbox, "label", "") or "").strip()
            if bbox_label:
                payload["expected_label"] = bbox_label[:120]
                payload["label"] = bbox_label[:120]
            log_target = f"V{vision_index}({x0},{y0}→{x1},{y1})"
            print(f"\n>> browser_click_at(V{vision_index}) → bbox=({x0},{y0},{x1},{y1})")
        else:
            if x is None or y is None:
                return "[click_at_failed:bad_args] Provide either vision_index or both x and y."
            payload = {"x": float(x), "y": float(y)}
            log_target = f"({x},{y})"
            print(f"\n>> browser_click_at({x}, {y})")

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/click",
            json=payload,
            timeout=30.0,
        )
        # 409 = reward-band reject. Historical data says this zone
        # doesn't respond to clicks on this host; surface the hint
        # so the LLM re-reads elements instead of trying another
        # nearby coord.
        if r.status_code == 409:
            info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            err = info.get("error") or "click_at rejected: low-reward zone"
            self.s.log_activity(f"click_at{log_target}(BAND_REJECT)", f"band={info.get('band')}")
            return f"[low_reward_band] {err}"
        r.raise_for_status()
        data = r.json()
        # Viewport-shift guard. The TS handler compared the page's
        # current scrollY/scrollHeight/viewport dims to what they were
        # when the brain last received a screenshot. A shift means
        # the V_n bbox is in a stale reference frame — clicking it
        # would land on whatever happens to be at those CSS coords
        # NOW, which is a different absolute element than the brain
        # picked. Hard-invalidate the frozen epoch and surface a
        # structured signal so the brain re-screenshots before
        # retrying. This catches the case where the page reflowed
        # globally (lazy-load, banner, modal) and labels alone can't
        # disambiguate same-kind neighbours under the bbox.
        if isinstance(data, dict) and data.get("error") == "viewport_shifted":
            self.s._vision_epoch_response = None
            delta = data.get("delta", {}) or {}
            reason = data.get("reason", "shift")
            dy = delta.get("scrollY", 0)
            dh = delta.get("scrollHeight", 0)
            dvh = delta.get("viewportHeight", 0)
            self.s.log_activity(
                f"click_at{log_target}(VIEWPORT_SHIFTED)",
                f"reason={reason} dy={dy} dh={dh} dvh={dvh}",
            )
            return (
                f"[click_at_failed:viewport_shifted reason={reason}] The "
                f"page has shifted since your last screenshot "
                f"(scrollY delta={dy}px, scrollHeight delta={dh}px"
                + (f", viewportHeight delta={dvh}px" if dvh else "")
                + "). Your V_n bboxes are anchored to a stale "
                f"reference frame, so clicking one would land on "
                f"whatever element happens to be at those CSS coords "
                f"now (likely a same-kind neighbour). Call "
                f"browser_screenshot to refresh vision before clicking."
            )
        # Element-mismatch guard (P1.4). The T3 backend compared the
        # element at the click target to the vision label we sent and
        # decided they don't match. Don't dispatch — return an
        # observation so the brain can re-screenshot and pick again.
        if isinstance(data, dict) and data.get("error") == "element_mismatch":
            found = data.get("found", {}) or {}
            alts = _vision_alternatives_hint(
                self.s, exclude_index=vision_index, limit=3,
            )
            self.s.log_activity(
                f"click_at{log_target}(ELEM_MISMATCH)",
                f"found={found.get('tag','?')}",
            )
            return (
                f"[click_at_failed:element_mismatch] Vision said this "
                f"target was '{data.get('expected_label','')}' but the "
                f"element at ({data.get('coords', {}).get('x','?')},"
                f"{data.get('coords', {}).get('y','?')}) is "
                f"<{(found.get('tag') or '?').lower()} "
                f"role='{found.get('role','')}'> text='"
                f"{(found.get('text') or '')[:80]}'. Call "
                f"browser_screenshot to refresh vision."
                + (f"\n{alts}" if alts else "")
            )
        # v6 F1 — invalidate frozen vision epoch on significant
        # mutation. After a filter apply / list re-render, the brain's
        # V_n coords from before the click now point at shifted
        # positions; resolving the next click_at against the frozen
        # epoch lands on the wrong element. Drop the epoch so the
        # next click_at must wait for the fresh prefetch (already
        # scheduled below) or call browser_screenshot. Threshold 8 ≈
        # filter applies / list re-renders / modal opens; ignores
        # focus-shift-only changes (1-3 mutations).
        try:
            effect = (data or {}).get("effect") or {}
            mutation_delta = int(effect.get("mutation_delta") or 0)
            try:
                # See browser_click for the 4-vs-6-vs-8 rationale.
                # Light toggles (tabs, chevrons, accordions) sit at
                # 4-6 mutations; 6 covers filter re-renders (>=7)
                # without surfacing [epoch_invalidated] on legitimate
                # UI state changes.
                threshold = int(
                    os.environ.get("MUTATION_DIRTY_THRESHOLD") or "6"
                )
            except ValueError:
                threshold = 6
            if mutation_delta > threshold:
                self.s._vision_epoch_response = None
                self.s.log_activity(
                    f"click_at{log_target}(EPOCH_DIRTY)",
                    f"mutation_delta={mutation_delta} > {threshold}",
                )
        except Exception:
            pass
        # Surgical undo: finalize the pending entry before record_url
        # rewrites self.current_url. begin_click_record already stamped
        # pre_url at dispatch time so this is just folding the response.
        self.s.finalize_click_record(response=data)
        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.record_url(actual_url)
        # Cross-tool: refine last_click_dom_index from the TS-side
        # `snap.dom_index` (post-resolve, more accurate than the
        # vision_pipeline's pre-click DOM enrichment which can drift
        # when the page was animating mid-click).
        _resolved_dom_idx = (
            data.get("snap", {}).get("dom_index")
            if isinstance(data, dict) else None
        )
        if isinstance(_resolved_dom_idx, int) and _resolved_dom_idx >= 0:
            self.s.last_click_dom_index = _resolved_dom_idx
        # Auto-refresh element_fingerprints from the click response.
        # The TS bridge now ships `fingerprints` with every click reply
        # so the Python cache stays in sync after a re-render. Without
        # this, a follow-up DOM-index click sends a STALE fingerprint
        # that may collide with the new occupant of [N], silently
        # misclicking. (B6 in the plan.)
        _fp_map = data.get("fingerprints") if isinstance(data, dict) else None
        if isinstance(_fp_map, dict):
            self.s.element_fingerprints = {
                int(k): v for k, v in _fp_map.items() if isinstance(v, str)
            }
        snap = data.get("snap")  # {x, y, snapped: bool, target?: str, warning?: str}
        if snap:
            snap_note = (
                f" snapped→({snap.get('x')},{snap.get('y')}) {snap.get('target','')}".strip()
                if snap.get("snapped") else " (raw bbox center; no interactive element matched)"
            )
            # A2: surface clickInBbox warnings so the brain can react.
            # The click still dispatched; this is advisory — but on
            # 'target_in_iframe' the click reliably hits the iframe
            # host instead of inner content, so the brain should
            # plan around it (e.g., switch to selector inside the
            # iframe document).
            warn = snap.get("warning")
            if warn == "target_in_iframe":
                snap_note += (
                    " [WARN:target_in_iframe — click landed on the "
                    "<iframe> host, NOT inner content. Inner-doc "
                    "selectors require iframe-scoped tooling.]"
                )
            elif warn == "pointer_events_none_ancestor":
                snap_note += (
                    " [WARN:pointer_events_none_ancestor — an "
                    "ancestor has pointer-events:none, the click may "
                    "have passed through to a layer behind.]"
                )
        else:
            snap_note = ""

        # Post-click verification — look up the postcondition the planner
        # attached to this target (by vision_index or by coord match)
        # and run it via verify_action. Runs on BOTH t1 and t3 sessions
        # when VERIFY_AFTER_CLICK is enabled (default on). The t3-only
        # gate that used to live here was the root cause of t1's
        # "phantom click" symptom — t1 silently treated dispatched but
        # ineffective clicks as success. verify_action.py routes the
        # state read through HTTP /state for t1 sessions and through
        # T3SessionManager for t3 sessions, so the same check works on
        # both tiers. The js/keyboard escalation ladder below stays t3-
        # native (mgr.click_at(strategy=...)) but a parallel HTTP-routed
        # branch handles t1 escalation against the same /click endpoint.
        verify_note = ""
        if os.environ.get("VERIFY_AFTER_CLICK", "1") != "0":
            postcond = self._lookup_postcondition(vision_index, x, y)
            if postcond is not None:
                try:
                    from superbrowser_bridge.antibot import interactive_session as _t3mgr
                    from superbrowser_bridge.verify_action import verify_after, PreState
                    mgr = _t3mgr.default() if session_id.startswith("t3-") else None
                    pre_state = PreState(
                        url=self.s.current_url or "",
                        dom_hash=self.s._last_dom_hash or "",
                    )
                    vr = await verify_after(
                        mgr, session_id, postcond,
                        pre_state=pre_state,
                        state=self.s,
                    )
                    if not vr.verified:
                        # Default postcondition (dom_mutated) failing means
                        # the click went out but NOTHING changed — page,
                        # DOM, URL all identical. Before bothering the
                        # brain, ESCALATE through the click ladder —
                        # many pages reject "primary" bezier clicks but
                        # respond to a direct `el.click()` (JS) dispatch
                        # or to keyboard Enter. Silent failure most
                        # often means the site's click handler has a
                        # guard our primary click tripped (0-dwell, CSS
                        # pointer-events masking, framework re-render).
                        is_silent_default = (
                            postcond.get("kind") == "dom_mutated"
                            and not getattr(
                                self.s._last_action_queue, "actions", None,
                            )
                        )
                        escalated = False
                        if is_silent_default and \
                                os.environ.get("CLICK_LADDER_AUTO", "1") != "0" and \
                                payload.get("bbox"):
                            alt_bbox = payload.get("bbox")
                            alt_x = (alt_bbox["x0"] + alt_bbox["x1"]) / 2
                            alt_y = (alt_bbox["y0"] + alt_bbox["y1"]) / 2
                            for alt_strategy in ("js", "keyboard"):
                                try:
                                    if session_id.startswith("t3-"):
                                        # T3 path: in-process manager.
                                        from superbrowser_bridge.antibot import (
                                            interactive_session as _t3mgr2,
                                        )
                                        mgr2 = _t3mgr2.default()
                                        alt_resp = await mgr2.click_at(
                                            session_id, alt_x, alt_y,
                                            bbox=alt_bbox,
                                            strategy=alt_strategy,
                                        )
                                    else:
                                        # T1 path: TS /click endpoint with
                                        # `strategy` field. Mirrors the t3
                                        # ladder so brain sees the same
                                        # `[click_escalated]` advisory on
                                        # both tiers.
                                        alt_payload: dict[str, Any] = {
                                            "x": alt_x, "y": alt_y,
                                            "bbox": alt_bbox,
                                            "strategy": alt_strategy,
                                        }
                                        ar = await _request_with_backoff(
                                            "POST",
                                            f"{SUPERBROWSER_URL}/session/{session_id}/click",
                                            json=alt_payload,
                                            timeout=10.0,
                                        )
                                        if ar.status_code != 200:
                                            continue
                                        alt_body = ar.json() or {}
                                        # Treat a 200 with an `error` field
                                        # (e.g. element_mismatch) as a
                                        # non-success — escalation didn't
                                        # land, try next strategy.
                                        if alt_body.get("error"):
                                            continue
                                        alt_resp = {"success": True, **alt_body}
                                    if not isinstance(alt_resp, dict) or \
                                            not alt_resp.get("success"):
                                        continue
                                    # Re-verify after the escalated strategy.
                                    vr2 = await verify_after(
                                        mgr, session_id, postcond,
                                        pre_state=pre_state,
                                        state=self.s,
                                    )
                                    if vr2.verified:
                                        escalated = True
                                        verify_note = (
                                            f"\n[click_escalated strategy={alt_strategy}] "
                                            f"Primary click was silent; "
                                            f"{alt_strategy} strategy landed the "
                                            f"action."
                                        )
                                        break
                                except Exception as exc:
                                    print(
                                        f"  [click ladder ({alt_strategy}) "
                                        f"failed: {exc}]"
                                    )
                                    continue
                        if not escalated:
                            if is_silent_default:
                                verify_note = (
                                    f"\n[click_silent reason={vr.reason}] "
                                    f"Primary + escalated (js/keyboard) "
                                    f"clicks all landed no DOM change. "
                                    f"Target likely non-interactive, "
                                    f"covered by an overlay, or waiting "
                                    f"on an async load. Call "
                                    f"browser_screenshot to re-vision, "
                                    f"dismiss any active blocker, or try "
                                    f"a different target."
                                )
                            else:
                                verify_note = (
                                    f"\n[VERIFY_MISS kind={vr.kind} reason={vr.reason}] "
                                    f"The click dispatched but the expected effect "
                                    f"({postcond.get('kind')}) didn't land. Consider "
                                    f"browser_plan_next_steps to re-sequence, or try "
                                    f"a different target."
                                )
                    elif os.environ.get("VERIFY_DEBUG") == "1":
                        verify_note = f"\n[verify_ok kind={vr.kind}]"
                except Exception as exc:
                    print(f"  [verify_action: skipped — {exc}]")

        self.s.record_step(
            "browser_click_at",
            log_target,
            f"url={actual_url[:60] if actual_url else '?'}{snap_note}",
        )
        # Phase 3.3 click-hit verification: capture pre-click signals
        # so the post-click vision pass can flag a no-op click that
        # left the labeled target still visible.
        _expected_label = ""
        if vision_index is not None:
            try:
                _expected_label = (
                    payload.get("expected_label")
                    or payload.get("label")
                    or ""
                )
            except Exception:
                _expected_label = ""
        _pre_url = self.s.current_url or ""
        _pre_dom_hash = self.s._last_dom_hash or ""
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, f"Clicked {log_target}{snap_note}") + verify_note,
            expected_label=_expected_label or None,
            pre_url=_pre_url,
            pre_dom_hash=_pre_dom_hash,
            state=self.s,
        )

    def _lookup_postcondition(
        self,
        vision_index: int | None,
        x: float | None,
        y: float | None,
    ) -> dict | None:
        """Match the current click against the top planned action and return
        its postcondition, or fall through to a weakest-possible
        default that only catches "click dispatched but page didn't
        change at all" (the canonical silent-miss signal).

        A planner match is: the click's vision_index equals the top
        action's target_vision_index, OR the click's (x, y) falls
        inside the top action's target bbox (± 10 px slack).

        The default (dom_mutated) runs when no planner postcondition
        applies. Set VERIFY_DEFAULT=0 to disable and preserve the old
        "no postcondition, no verification" behaviour.
        """
        queue = self.s._last_action_queue
        if queue is not None and getattr(queue, "actions", None):
            top = queue.actions[0]
            # vision_index match (preferred)
            if vision_index is not None and top.target_vision_index is not None:
                if int(vision_index) == int(top.target_vision_index):
                    return top.postcondition.to_dict()
            # coord match (fallback)
            if x is not None and y is not None and top.target_bbox_pixels:
                x0, y0, x1, y1 = top.target_bbox_pixels
                if (x0 - 10) <= float(x) <= (x1 + 10) and \
                        (y0 - 10) <= float(y) <= (y1 + 10):
                    return top.postcondition.to_dict()
        # Default: "did anything change?" — dom_mutated catches the
        # "click silently missed" case even when the planner didn't
        # attach an explicit postcondition.
        if os.environ.get("VERIFY_DEFAULT", "1") != "0":
            return {"kind": "dom_mutated"}
        return None


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        selectors_json=StringSchema(
            "JSON-encoded array of CSS selectors, e.g. "
            '\'["button.submit", "#email"]\'. Selectors ride as a string '
            "because this layer doesn't expose ArraySchema."
        ),
        ensure_visible=BooleanSchema(
            description=(
                "If true, scroll each element into view before measuring. "
                "Default false — pure read-only probe."
            ),
            nullable=True,
        ),
        required=["session_id", "selectors_json"],
    )
)
class BrowserGetRectTool(Tool):
    name = "browser_get_rect"
    description = (
        "Return getBoundingClientRect() for one or more CSS selectors. "
        "Pixel-exact, zero vision cost. Use to derive coordinates before "
        "calling browser_click_selector / browser_drag_selectors. "
        "Selectors ride as a JSON string (no ArraySchema in this layer)."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        selectors_json: str,
        ensure_visible: bool | None = None,
        **kw: Any,
    ) -> str:
        try:
            selectors = json.loads(selectors_json)
        except (TypeError, ValueError) as exc:
            return f"[get_rect_failed] selectors_json is not valid JSON: {exc}"
        if not isinstance(selectors, list) or not all(isinstance(s, str) for s in selectors):
            return "[get_rect_failed] selectors_json must decode to a list of strings."

        print(f"\n>> browser_get_rect({len(selectors)} selectors)")
        payload = {"selectors": selectors, "ensureVisible": bool(ensure_visible)}
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/rect",
            json=payload,
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        rects = data.get("rects") or []
        lines = ["Selector rects:"]
        for sel, rect in zip(selectors, rects):
            if rect is None:
                lines.append(f"  {sel} → NOT FOUND")
                continue
            lines.append(
                f"  {sel} → cx={rect['cx']:.1f} cy={rect['cy']:.1f} "
                f"w={rect['w']:.1f} h={rect['h']:.1f} "
                f"visible={rect['visible']} inViewport={rect['inViewport']}"
            )
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        selector=StringSchema("CSS selector of the element to click"),
        button=StringSchema("Mouse button: left|right|middle", nullable=True),
        click_count=IntegerSchema("Number of clicks (1 for single, 2 for double)", nullable=True),
        linear=BooleanSchema(
            description=(
                "If true (default), use deterministic teleport click (pixel-exact). "
                "Set false for stealth-critical contexts (captchas) that need Bezier humanisation."
            ),
            nullable=True,
        ),
        required=["session_id", "selector"],
    )
)
class BrowserClickSelectorTool(Tool):
    name = "browser_click_selector"
    description = (
        "Click the centre of a DOM element by CSS selector. Pixel-exact, "
        "zero Gemini cost. PREFER OVER browser_click_at(vision_index=...) "
        "whenever the target has a stable hook — chess squares "
        "(.square-54), form fields (#email), buttons with data-test-id, "
        "captcha handles. Fails fast if the selector is missing or zero-size."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        selector: str,
        button: str | None = None,
        click_count: int | None = None,
        linear: bool | None = None,
        **kw: Any,
    ) -> str:
        print(f"\n>> browser_click_selector({selector!r})")
        # Phase 1.1: hard sync gate.
        sync_block = await self.s.ensure_vision_synced(reason="browser_click_selector")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls += 1
        # Refuse stale click after type opened a dropdown — see helper.
        pending = _click_pending_screenshot_block(self.s)
        if pending:
            return pending
        # Release the autocomplete-pending guard (see BrowserClickTool).
        self.s.last_type_had_suggestions = False
        self.s.last_type_anchor_label = ""
        self.s.last_autocomplete_suggestions = []

        # Phase 1.2: dynamic-ID guard. React's `useId()` and friends
        # generate IDs that rotate between renders (`:r13:` → `:r14:`).
        # Selector dispatch on these silently fails or hits the wrong
        # element by the time the click lands. Reject upstream so the
        # brain routes to `browser_click_at(vision_index=...)` on the
        # first call instead of wasting a round-trip on a stale ID.
        # Note: `begin_click_record` hasn't fired yet, so no undo entry
        # needs cleanup.
        if _DYNAMIC_ID_RE.search(selector):
            self.s.record_cursor_failure(
                strategy="click_selector",
                target=selector,
                reason="dynamic_id_pattern",
            )
            self.s.log_activity(
                f"click_selector({selector})(DYNAMIC_ID_REJECTED)", "",
            )
            return (
                f"[click_selector_rejected:dynamic_id] Selector "
                f"{selector!r} uses a React-generated dynamic ID "
                f"(useId() / radix-:rN: / headlessui-* / __id_*) that "
                f"changes between renders. Call "
                f"browser_click_at(vision_index=V_n) instead — the "
                f"vision bbox is stable across re-renders."
            )

        # Phase 1.3: Playwright/jQuery pseudo-selector guard. The brain
        # knows `:has-text("X")`, `:contains("X")`, `text=X`, `:visible`
        # etc. from Playwright/jQuery training data and assumes they're
        # CSS. `document.querySelector` throws SyntaxError on them, the
        # TS getRects wrapper swallows it, and the brain sees "selector
        # not found" — indistinguishable from a missing element. The
        # brain then wastes 5+ turns on `browser_eval`/markdown lookups
        # before falling through to raw coords.
        #
        # Reject with an explicit advisory that names what the brain
        # tried and where to route instead.
        if _PLAYWRIGHT_PSEUDO_RE.search(selector):
            self.s.record_cursor_failure(
                strategy="click_selector",
                target=selector,
                reason="playwright_pseudo_pattern",
            )
            self.s.log_activity(
                f"click_selector({selector})(PLAYWRIGHT_PSEUDO_REJECTED)", "",
            )
            return (
                f"[click_selector_rejected:playwright_pseudo] Selector "
                f"{selector!r} uses Playwright/jQuery extension syntax "
                f"(:has-text, :contains, :visible, :hidden, :eq, :first, "
                f":button, text=, role=, xpath=, >> chain, etc.) — these "
                f"are NOT standard CSS and document.querySelector throws "
                f"SyntaxError on them. To click an element BY ITS TEXT, "
                f"call browser_click_at(vision_index=V_n) — vision "
                f"already labels each visible button by its text. To "
                f"click by stable hook, use a real CSS selector "
                f"(`.square-54`, `#email`, `[data-testid=submit]`)."
            )

        payload: dict[str, Any] = {"selector": selector, "ensureVisible": True}
        if button is not None:
            payload["button"] = button
        if click_count is not None:
            payload["clickCount"] = click_count
        if linear is not None:
            payload["linear"] = linear

        # Surgical undo: open a pending entry. pre_active is None for
        # selector clicks (we don't probe aria state on this path); the
        # label safety-net regex still catches destructive selectors
        # like 'button.delete' / '#submit' and classifies them
        # irreversible. url_changed demotion in finalize handles nav.
        self.s.begin_click_record(
            tool="browser_click_selector",
            target_key=f"click_selector({selector})",
            vision_index=None,
            label=selector,
            box_2d=None,
            pre_active=None,
            expected_url_change=False,
            is_form_submit=False,
        )

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/click-selector",
            json=payload,
            timeout=15.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            # Phase 3.1: record cursor failure so the script lockout
            # gate counts this as a tried-and-failed cursor strategy.
            self.s.record_cursor_failure(
                strategy="click_selector",
                target=selector,
                reason=str(err)[:120],
            )
            # Drop the pending undo entry — the click never landed.
            self.s._pending_undo_entry = None
            return f"[click_selector_failed] {err}"
        data = r.json()
        self.s.finalize_click_record(response=data)
        # Auto-refresh element_fingerprints from the click response. (B6)
        _fp_map = data.get("fingerprints") if isinstance(data, dict) else None
        if isinstance(_fp_map, dict):
            self.s.element_fingerprints = {
                int(k): v for k, v in _fp_map.items() if isinstance(v, str)
            }
        clicked = data.get("clicked", {})
        self.s.record_step(
            "browser_click_selector",
            f"{selector} @ ({clicked.get('x','?')},{clicked.get('y','?')})",
            data.get("url", ""),
        )
        # Anti-pattern: if `selector` matches `#<id>` and <id> was
        # stamped onto an element via browser_eval / browser_run_script
        # within the last few turns, surface a corrective advisory.
        # See state.check_anti_pattern_selector for the heuristic.
        anti_pattern = self.s.check_anti_pattern_selector(selector)
        if anti_pattern:
            self.s.log_activity(
                f"click_selector({selector})(ANTI_PATTERN)",
                "stamped-id-then-click_selector",
            )
        # click_selector is a mutation — advance the observation token
        # and schedule a vision prefetch so the next screenshot is warm.
        self.s.advance_observation_token("click_selector")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        caption = (
            f"Clicked {selector} at "
            f"({clicked.get('x','?')},{clicked.get('y','?')})"
        )
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        if anti_pattern:
            caption += anti_pattern
        return await _append_fresh_vision(
            _vision_task,
            _maybe_no_effect_prefix(
                data, "browser_click_selector", caption,
                session_state=self.s,
            ),
            state=self.s,
        )
