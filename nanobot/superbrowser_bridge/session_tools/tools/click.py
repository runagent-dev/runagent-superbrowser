"""Click tools — DOM-index, vision-bbox (V_n), CSS-selector, and rect probe.

`BrowserClickTool` (DOM index), `BrowserClickAtTool` (vision-bbox / coords),
`BrowserGetRectTool` (read-only rect probe), `BrowserClickSelectorTool`
(CSS-selector fast path, supports `in_iframe` for iframe-scoped clicks).
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

from .._label import clean_label
from ..effects import _maybe_no_effect_prefix
from ..feedback import _feedback_gate
from ..formatting import _fetch_elements, _vision_alternatives_hint
from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState
from ..vision_pipeline import _append_fresh_vision, _schedule_vision_prefetch
from ._click_core import (
    lookup_postcondition,
    maybe_scroll_bbox_into_view,
    run_click_with_ladder,
)


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
#   :submit, :selected, :file, :image,
#   :password, :reset                     — jQuery form filters
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


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index"),
        button=StringSchema("Mouse button: left, right, middle", nullable=True),
        expected_label=StringSchema(
            description=(
                "The text/aria-label you saw on element [index] when you "
                "read the elements list. Backend cross-checks this against "
                "the actual element at [index]; if they don't match, the "
                "click is rejected as element_mismatch — catches the case "
                "where you confused indices in your reading of the list. "
                "Optional but strongly recommended; passing the label you "
                "saw makes wrong-element clicks fail loudly instead of "
                "silently landing on an unintended sibling."
            ),
            nullable=True,
        ),
        required=["session_id", "index"],
    )
)
class BrowserClickTool(Tool):
    name = "browser_click"
    description = (
        "Click an interactive element by its [index] number. Pass "
        "expected_label with the text/aria-label you saw at [index] so "
        "the backend can cross-check intent vs. the actual element."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        index: int,
        button: str | None = None,
        expected_label: str | None = None,
        **kw: Any,
    ) -> Any:
        print(f"\n>> browser_click([{index}])")
        gate = await _feedback_gate("browser_click")
        if gate:
            return gate
        # Phase 1.1: hard sync gate. Wait for any in-flight vision
        # prefetch from the previous action before dispatching.
        sync_block = await self.s.ensure_vision_synced(reason="browser_click")
        if sync_block:
            return sync_block
        # Popup-scroll guard. After any popup-internal scroll (via
        # browser_scroll_within or the pixel-scroll loop inside
        # browser_select_option's auto-recovery), DOM indices for
        # popup items are stale — the option list moved but the
        # brain's cached [N] mapping didn't. Refuse the click and
        # redirect to the deterministic bbox path: screenshot →
        # fresh V_n → browser_click_at. Cleared the moment a
        # screenshot lands; auto-expires after POPUP_SCROLL_EXPIRY_SECONDS
        # so a brain pivoting to an unrelated task isn't permanently
        # blocked.
        if self.s.popup_scroll_guard_active():
            self.s.log_activity(
                f"click([{index}])(POPUP_SCROLL_BLOCKED)",
                f"pending_since={self.s.popup_scroll_at:.1f}",
            )
            return (
                f"[click_blocked:popup_scrolled_no_rebbox] A popup was "
                f"just scrolled and no fresh screenshot has landed yet — "
                f"DOM indices for popup options are STALE. The element "
                f"at [{index}] has likely moved to a different row, or "
                f"a different element occupies that index now. Required "
                f"next steps:\n"
                f"  1. browser_screenshot — vision relabels the popup "
                f"with fresh V_n.\n"
                f"  2. browser_click_at(vision_index=V_n) on the option "
                f"you want.\n"
                f"Do NOT retry browser_click([N]) on popup items — the "
                f"index map only updates after a fresh vision pass."
            )
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
        payload: dict[str, Any] = {"index": index}
        if button:
            payload["button"] = button
        # Brain's expected_label: catches case where the brain misread
        # the elements list and picked the wrong [N]. Without this, the
        # backend computes the label from the element AT [N] and
        # validates against itself — a tautology that always passes.
        # With it, clickInBbox's Phase 1 label-match guard compares the
        # element under [N] against what the brain *intended*, surfacing
        # element_mismatch when the brain's reading and the page's reality
        # diverge. Length-capped to mirror the TS-side label slice and
        # keep the click payload bounded.
        if expected_label:
            _label = expected_label.strip()[:80]
            if _label:
                payload["expected_label"] = _label
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
        except Exception:
            pass
        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.record_url(actual_url)
        # Snap telemetry (P3.12).
        snap = data.get("snap") if isinstance(data, dict) else None
        if isinstance(snap, dict) and snap.get("snapped") is False:
            self.s.snap_miss_count += 1
        # Shared click ladder — verify_after + js/keyboard escalation.
        # DOM-index clicks previously only emitted [click_silent] on
        # no-op; they now auto-recover via the same ladder bbox clicks
        # use. Escalation uses snap.x / snap.y (resolved click coords).
        snap_x = snap.get("x") if isinstance(snap, dict) else None
        snap_y = snap.get("y") if isinstance(snap, dict) else None
        try:
            alt_x = float(snap_x) if snap_x is not None else 0.0
            alt_y = float(snap_y) if snap_y is not None else 0.0
        except (TypeError, ValueError):
            alt_x = 0.0
            alt_y = 0.0
        postcond = lookup_postcondition(self.s, vision_index=None, x=alt_x, y=alt_y)
        verify_note = ""
        if alt_x > 0 and alt_y > 0:
            verify_note = await run_click_with_ladder(
                self.s, session_id,
                log_target=f"[{index}]",
                primary_response=data,
                alt_x=alt_x,
                alt_y=alt_y,
                alt_bbox=None,
                postcondition=postcond,
            )
        # Mirror of the no-effect / label-mismatch surfacing in
        # browser_click_at — DOM-index clicks share the same silent-miss
        # failure mode (label_mismatch=True / snapped=False / no DOM
        # mutation) but the diagnostic was previously stderr-only. See
        # browser_click_at for the rationale.
        no_effect_caption = f"Clicked [{index}]"
        record_suffix = ""
        _escalation_succeeded = "[click_escalated strategy=" in (verify_note or "")
        if not _escalation_succeeded:
            _label_mismatch_flag = bool(snap.get("labelMismatch")) if isinstance(snap, dict) else False
            _snapped_flag = snap.get("snapped") if isinstance(snap, dict) else None
            no_effect_caption = _maybe_no_effect_prefix(
                data, "browser_click", no_effect_caption,
                session_state=self.s,
            )
            _tagged_no_effect = no_effect_caption.startswith("[no_effect:")
            # See browser_click_at: labelMismatch is advisory only after
            # the page.ts:1574 fix; the secondary tag fires only on
            # genuine Phase 3 hard fallback (snapped=False) combined
            # with zero page inertia.
            _effect_dbg = (data or {}).get("effect") or {}
            _mutation_delta_dbg = int(_effect_dbg.get("mutation_delta") or 0)
            _url_changed_dbg = bool(_effect_dbg.get("url_changed"))
            if (not _tagged_no_effect
                    and _snapped_flag is False
                    and not _label_mismatch_flag
                    and _mutation_delta_dbg == 0
                    and not _url_changed_dbg):
                no_effect_caption = (
                    f"[no_effect:browser_click] click on [{index}] "
                    f"resolved to no interactive element (snapped=False) "
                    f"and had no effect. The DOM index may be stale "
                    f"(page re-rendered, list re-sorted). Call "
                    f"browser_screenshot or browser_list_elements to "
                    f"refresh; DO NOT retry [{index}].\n"
                    f"{no_effect_caption}"
                )
                _tagged_no_effect = True
            if _tagged_no_effect:
                record_suffix = " [no_effect]"
                # Phase 3.1: feed the cursor-failure ledger (mirrors
                # browser_click_at) so a silent DOM-index click counts
                # toward lifting the run_script lockout. No expected_label
                # dedup needed here — vision_pipeline.py only records the
                # click_at path.
                self.s.record_cursor_failure(
                    strategy="click",
                    target=f"[{index}]",
                    reason="no_effect",
                )
                try:
                    from nanobot.vision_agent.client import get_vision_agent
                    await get_vision_agent()._cache.bust_session(session_id)
                except Exception:
                    pass

        self.s.log_activity(f"click([{index}])", f"url={actual_url[:50] if actual_url else '?'}")
        self.s.record_step(
            "browser_click",
            f"index={index}",
            f"url={actual_url[:60] if actual_url else '?'}{record_suffix}",
        )
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, no_effect_caption) + verify_note,
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index (the V_n the vision agent "
                "labelled this element). REQUIRED — every bbox click "
                "must anchor to a labelled element. The server snaps to "
                "the interactive element inside the bbox, eliminating "
                "off-by-pixel misses."
            ),
        ),
        required=["session_id", "vision_index"],
    )
)
class BrowserClickAtTool(Tool):
    name = "browser_click_at"
    description = (
        "Click a vision bbox by its V_n. The server snaps to the "
        "actual interactive element inside the bbox, eliminating "
        "off-by-pixel misses. Synthetic V_n (V>=1000, injected by "
        "DOM scans after type/click) are clicked the same way.\n\n"
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
        "doesn't match the task, re-click the same V_n to reverse it.\n\n"
        "AUTO-SCROLL: when the bbox is below the fold (page or inner "
        "popup), the tool auto-scrolls the right container before "
        "dispatching. Do NOT pre-call browser_scroll_within for a "
        "popup option — this tool handles it."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        vision_index: int | None = None,
        **kw: Any,
    ) -> Any:
        # Coerce vision_index — some LLMs emit "2" (string) instead of 2
        # (int). The schema declares IntegerSchema; in practice the
        # validator lets the string through and we used to silently drop
        # the parameter. Cast once here so the rest of the function sees
        # an int.
        if vision_index is not None and not isinstance(vision_index, int):
            try:
                vision_index = int(str(vision_index).strip())
            except (TypeError, ValueError):
                vision_index = None
        if vision_index is None:
            print("\n>> browser_click_at(?) — rejected: no vision_index")
            return (
                "[click_at_failed:no_vision_index] vision_index is "
                "required. Pass the V_n (1-based) of the bbox the "
                "vision agent labelled for the target you want to "
                "click. Raw (x, y) coords are no longer accepted; "
                "every bbox click must anchor to a labelled element."
            )
        print(f"\n>> browser_click_at(V{vision_index})")
        # Phase 1.1: hard sync gate. Block until the in-flight vision
        # prefetch from the previous action lands — without this the
        # brain's V_n resolves against a frozen epoch but the freshness
        # gate has no fresh post-action vision to validate against.
        sync_block = await self.s.ensure_vision_synced(reason="browser_click_at")
        if sync_block:
            print("  [click_at rejected: sync_block]")
            return sync_block
        self.s._brain_turn_counter += 1
        self.s.consecutive_click_calls += 1
        # Snapshot modal_open BEFORE dispatch so the post-click scan
        # trigger can detect a fresh modal_cta_open transition (was off,
        # is now on) and not double-fire when a modal was already up.
        _pre_last = getattr(self.s, "_last_vision_response", None)
        _pre_flags = getattr(_pre_last, "flags", None) if _pre_last else None
        self._modal_open_before_click = bool(
            getattr(_pre_flags, "modal_open", False)
        ) if _pre_flags is not None else False
        # Build the target key on intent (vision_index=V3) so the
        # dead-click guard fires on what the brain asked for, not on
        # post-snap pixel coords which can drift between calls.
        target_key = f"click_at(V{int(vision_index)})"

        payload: dict[str, Any]
        log_target: str
        if True:
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                print("  [click_at rejected: no_vision]")
                return (
                    "[click_at_failed:no_vision] No recent vision "
                    "response to resolve vision_index against. "
                    "Re-fetch state to trigger a fresh vision pass, "
                    "or pass raw (x, y)."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                print(f"  [click_at rejected: bad_vision_index V{vision_index} (only {len(resp.bboxes)} bboxes)]")
                return (
                    f"[click_at_failed:bad_vision_index] V{vision_index} "
                    f"is out of range (only {len(resp.bboxes)} bboxes in "
                    "the last vision response)."
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
                    print(f"  [click_at rejected: blocker_active layer={active_blocker}]")
                    return (
                        f"[click_at_failed:blocker_active layer={active_blocker}] "
                        f"A blocker layer ({active_blocker}) is on top of "
                        f"content, and V{vision_index} sits in a different "
                        f"layer ({bbox_layer}).{hint} Then re-screenshot."
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
                print(f"  [click_at rejected: dead_click (V{vision_index} repeated with no effect)]")
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
            # Pass expected_label so the TS backend can compute a
            # labelMismatch flag in the response (logged as diagnostic).
            # The server no longer blocks on mismatch — the click
            # always dispatches and the brain reads the result.
            if bbox_label:
                payload["expected_label"] = bbox_label[:120]
                payload["label"] = bbox_label[:120]
            # log_target lands in StepOutcome.args → the ledger RECENT
            # block. Coords churn turn-to-turn (DPR, scroll, post-snap
            # reflow) so they're useless for re-identification; the
            # (V_n, label) pair is the determinism anchor.
            log_target = f'V{vision_index}|"{clean_label(bbox_label)}"'
            # Continuation of the top-of-function dispatch print —
            # adds the resolved bbox so the operator can see what
            # coordinates the cursor will actually go to.
            print(f"  → bbox=({x0},{y0},{x1},{y1})")

        # Auto-scroll the bbox into view if it's below the page or
        # popup fold. Cheap: server returns scrolled=false when the
        # bbox is already visible, so the common case adds one HTTP
        # round-trip (~5ms) but avoids the brain having to call
        # browser_scroll_within for dropdown options that sit below
        # the visible portion of the popup. New geometry is fed back
        # into the click payload so the TS server clicks the correct
        # post-scroll coords.
        if isinstance(payload, dict) and isinstance(payload.get("bbox"), dict):
            new_bbox = await maybe_scroll_bbox_into_view(
                self.s, session_id, payload["bbox"],
            )
            if isinstance(new_bbox, dict):
                payload["bbox"] = new_bbox
                # log_target stays label-anchored after auto-scroll —
                # coords would just churn here too.
                log_target = f'V{vision_index}|"{clean_label(bbox_label)}"'

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
        # Diagnostic: surface the TS-side outcome so cursor/action-not-
        # happening cases are debuggable without DevTools. Prints the
        # snap state (snapped/labelMismatch/element_mismatch) so we can
        # see when the click was SKIPPED server-side (no cursor emit,
        # no dispatch). Loud only on anomalies; silent on plain success.
        if isinstance(data, dict):
            snap_dbg = data.get("snap") or data.get("clicked") or {}
            err_dbg = data.get("error")
            # Both element_mismatch source paths in http.ts construct
            # the response from `snap.labelMismatch` — i.e. an
            # element_mismatch error IS a labelMismatch under the hood.
            label_mismatch = bool(snap_dbg.get("labelMismatch")) or (
                err_dbg == "element_mismatch"
            )
            snapped = snap_dbg.get("snapped")
            effect = (data.get("effect") or {})
            mutation_delta = effect.get("mutation_delta", "?")
            # Anomaly = real error, OR (snap-uncertainty AND the click
            # didn't move the page). When mutation_delta>0 or URL
            # changed, the click clearly landed — labelMismatch /
            # snapped=False is advisory noise and we don't shout it.
            _eff_delta_int = int(effect.get("mutation_delta") or 0)
            _click_had_effect_print = (
                _eff_delta_int > 0 or bool(effect.get("url_changed"))
            )
            _is_anomaly = bool(err_dbg) or (
                (label_mismatch or snapped is False)
                and not _click_had_effect_print
            )
            if _is_anomaly:
                expected_label_dbg = data.get("expected_label", "")
                found_dbg = data.get("found") or {}
                found_text = (found_dbg.get("text") or "")[:60]
                found_tag = found_dbg.get("tag", "")
                found_role = found_dbg.get("role", "")
                print(
                    f"  [click_response: err={err_dbg!r} "
                    f"label_mismatch={label_mismatch} "
                    f"snapped={snapped} mutation_delta={mutation_delta}]"
                )
                if err_dbg == "element_mismatch":
                    print(
                        f"    [mismatch_detail expected={expected_label_dbg!r} "
                        f"found_tag={found_tag!r} found_role={found_role!r} "
                        f"found_text={found_text!r}]"
                    )
            else:
                _lm_tail = (
                    f" label_mismatch=True (advisory; click had effect)"
                    if label_mismatch else ""
                )
                print(
                    f"  [click_ok: snapped={snapped} "
                    f"mutation_delta={mutation_delta} "
                    f"target={snap_dbg.get('target', '?')[:60]}{_lm_tail}]"
                )
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
            # Three snap states distinguished by `snapped` and `target`:
            #   - snapped=true                  → Phase 1/2 confident match.
            #   - snapped=false + target set    → Phase 2.5 labelMismatch
            #     (best-by-area found, label diverged). Common on value-
            #     bearing triggers (Chakra/MUI/AntD datetime, custom React
            #     rows showing displayed value where vision labels by
            #     function). The click DID land on `target.center`.
            #   - snapped=false + no target     → Phase 3 hard fallback
            #     (grid scan found NO clickable; clicked raw bbox center).
            #
            # The brain previously read "no interactive element matched"
            # for both snapped=false cases — wrong for the labelMismatch
            # path. When the page mutated or URL changed, the click
            # clearly worked, so even the labelMismatch advisory is
            # noise.
            _has_target = bool(snap.get("target"))
            _effect_outer = data.get("effect") or {}
            _click_had_effect = (
                int(_effect_outer.get("mutation_delta") or 0) > 0
                or bool(_effect_outer.get("url_changed"))
            )
            if snap.get("snapped") or _has_target:
                # Confident match OR labelMismatch-with-target. Both
                # clicked at `target.center`; same caption shape.
                snap_note = (
                    f" snapped→({snap.get('x')},{snap.get('y')}) "
                    f"{snap.get('target','')}"
                ).rstrip()
            elif _click_had_effect:
                # Phase 3 hard fallback BUT the page mutated — a same-
                # coord handler fired. Don't tell the brain "no
                # interactive element matched" because it'll retry the
                # click and miss the popup it just opened.
                snap_note = (
                    f" clicked→({snap.get('x')},{snap.get('y')}) "
                    f"(raw bbox center; same-coord handler fired)"
                )
            else:
                # True Phase 3 hard fallback with no effect — the
                # original message stays accurate here.
                snap_note = (
                    " (raw bbox center; no interactive element matched)"
                )
            # Surface clickInBbox warnings so the brain can react.
            # Variants (Phase A/B):
            #   - target_in_iframe_resolved: descent worked, click
            #     landed on the inner element. Short success note.
            #   - target_in_iframe_cross_origin: SOP blocked
            #     contentDocument access; Phase B Frame walk also
            #     failed. Brain should use in_iframe parameter.
            #   - target_in_iframe_miss: same-origin BUT neither
            #     pinpoint nor inner grid scan found a clickable in
            #     the bbox region. Vision bbox is probably loose or
            #     pointing at a non-interactive area.
            #   - target_in_iframe: legacy fallback (should not fire).
            #   - pointer_events_none_ancestor: existing pe:none case.
            #
            # iframe_host_selector (when set) is a stable CSS the
            # brain can pass back as in_iframe=<...> without first
            # running an inspection script. We always include it in
            # the WARN advisories so the recovery action is obvious.
            warn = snap.get("warning")
            chain = snap.get("iframe_chain") or []
            host_sel = snap.get("iframe_host_selector") or ""
            host_hint = (
                f" host_selector={host_sel!r}" if host_sel else ""
            )
            if warn == "target_in_iframe_resolved":
                hops = f" depth={len(chain)}" if chain else ""
                snap_note += f" [iframe_descent_ok{hops}]"
            elif warn == "target_in_iframe_cross_origin":
                # Primary advice: use the deterministic selector path.
                # `browser_click_selector(in_iframe=<host>)` routes through
                # the TS `clickSelectorInIframe()` which uses contentFrame()
                # + frame.evaluate() to find the element by CSS inside the
                # iframe and dispatches a CDP click at the translated
                # viewport coords — no need for the brain to author JS.
                snap_note += (
                    f" [WARN:iframe_cross_origin{host_hint} — outer-doc"
                    " click cannot reach inner content (cross-origin"
                    " SOP). Call browser_click_selector(selector="
                    f"<inner_css>, in_iframe={host_sel!r}) to target the"
                    " element directly inside the frame."
                    if host_sel
                    else " [WARN:iframe_cross_origin — outer-doc click"
                         " cannot reach inner content (cross-origin"
                         " SOP). Call browser_click_selector with"
                         " in_iframe=<host_css> to target inside the"
                         " frame.]"
                )
            elif warn == "target_in_iframe_miss":
                snap_note += (
                    f" [WARN:iframe_miss{host_hint} — descent ran but"
                    " no clickable was found inside the bbox region."
                    " Vision bbox may be loose. Re-screenshot to get a"
                    " tighter bbox, or call browser_click_selector("
                    f"selector=<inner_css>, in_iframe={host_sel!r})."
                    if host_sel
                    else " [WARN:iframe_miss — descent ran but no"
                         " clickable was found in the bbox region."
                         " Re-screenshot or call browser_click_selector"
                         " with in_iframe=<host_css>.]"
                )
            elif warn == "target_in_iframe":
                snap_note += (
                    f" [WARN:target_in_iframe{host_hint} — click landed"
                    " on the <iframe> host, NOT inner content. Call"
                    f" browser_click_selector(..., in_iframe={host_sel!r})"
                    " or re-screenshot so vision emits a V_n inside the"
                    " iframe."
                    if host_sel
                    else " [WARN:target_in_iframe — click landed on the"
                         " <iframe> host. Re-screenshot or call"
                         " browser_click_selector with"
                         " in_iframe=<host_css>.]"
                )
            elif warn == "pointer_events_none_ancestor":
                snap_note += (
                    " [WARN:pointer_events_none_ancestor — an "
                    "ancestor has pointer-events:none, the click may "
                    "have passed through to a layer behind.]"
                )

            # Iframe-miss escalation. After two misses on the SAME
            # (V_n, host) — i.e. the brain already tried the
            # browser_click_selector path advised in the WARN above and
            # it still didn't land — drop to browser_run_script as a
            # final escape. browser_click_selector is the deterministic
            # first step; run_script is only the last resort.
            _iframe_miss_warnings = (
                "target_in_iframe_cross_origin",
                "target_in_iframe_miss",
                "target_in_iframe",
            )
            if warn in _iframe_miss_warnings:
                miss_key = f"V{int(vision_index)}|{host_sel}"
                if self.s.iframe_miss_key == miss_key:
                    self.s.iframe_miss_count += 1
                else:
                    self.s.iframe_miss_key = miss_key
                    self.s.iframe_miss_count = 1
                if self.s.iframe_miss_count >= self.s.MAX_IFRAME_MISSES_BEFORE_NUDGE:
                    host_arg = (
                        f"const f = page.frames().find(fr => "
                        f"fr.url().includes('<substring_of_{host_sel}_url>'));"
                        if host_sel else
                        "const f = page.frames().find(fr => "
                        "fr.url().includes('<host_substring>'));"
                    )
                    snap_note += (
                        f" [ESCALATE:run_script iframe_misses="
                        f"{self.s.iframe_miss_count}] "
                        f"You've now missed V{int(vision_index)} inside"
                        f" this iframe {self.s.iframe_miss_count} times,"
                        f" including via browser_click_selector. Drop to"
                        f" browser_run_script(mutates=true) and dispatch"
                        f" the click via frame.evaluate so it runs in"
                        f" the iframe's own JS context. Example skeleton: "
                        f"{host_arg} await f.evaluate(() => "
                        f"document.querySelector('<inner_button>').click()). "
                        f"If you don't know the inner selector yet, first"
                        f" run browser_run_script(mutates=false) with"
                        f" f.evaluate(() => Array.from(document.querySelectorAll("
                        f"'button,a,[role=button]')).map(el => el.outerHTML.slice(0,200))) "
                        f"to enumerate clickable candidates."
                    )
            elif warn == "target_in_iframe_resolved" or warn is None:
                # Reset on success or on non-iframe warnings — the brain
                # has either landed the click or moved on to a different
                # target / class of problem.
                if self.s.iframe_miss_count:
                    self.s.iframe_miss_count = 0
                    self.s.iframe_miss_key = ""
            # Phase G: native <select> hint. Always-on (top-level or
            # inside an iframe). Native dropdowns don't open via CDP
            # Input.dispatchMouseEvent — the click here only focuses
            # the element. Brain should switch to browser_select_option
            # which sets .value + dispatches `change` programmatically.
            # Hint-only policy: the click still dispatched.
            if snap.get("native_select"):
                if host_sel:
                    snap_note += (
                        f" [hint:native_select host_selector={host_sel!r}"
                        " — click landed on a <select>; native dropdowns"
                        " don't open via CDP. Call"
                        " browser_select_option(label=<dropdown_label>,"
                        f" value=<option>, in_iframe={host_sel!r})"
                        " to set the value programmatically.]"
                    )
                else:
                    snap_note += (
                        " [hint:native_select — click landed on a"
                        " <select>; native dropdowns don't open via CDP."
                        " Call browser_select_option(label=<dropdown_label>,"
                        " value=<option>) to set the value"
                        " programmatically.]"
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
        # Shared verification ladder. The bbox dispatch sent {"bbox": ...};
        # extract its center for js/keyboard escalation if verify fails.
        alt_bbox = payload.get("bbox") if isinstance(payload, dict) else None
        if alt_bbox:
            alt_x = (alt_bbox["x0"] + alt_bbox["x1"]) / 2
            alt_y = (alt_bbox["y0"] + alt_bbox["y1"]) / 2
        else:
            alt_x = 0.0
            alt_y = 0.0
        postcond = lookup_postcondition(
            self.s, vision_index=vision_index, x=alt_x, y=alt_y,
        )
        verify_note = await run_click_with_ladder(
            self.s, session_id,
            log_target=log_target,
            primary_response=data,
            alt_x=alt_x,
            alt_y=alt_y,
            alt_bbox=alt_bbox,
            postcondition=postcond,
        )

        # Surface silent click failures to the brain. Without this, the
        # TS-side label_mismatch / snapped=False / mutation_delta=0
        # diagnostics only land in stderr — the brain reads "Clicked V_n"
        # plus an unchanged state block, concludes success, and moves to
        # the next planned action (typing / navigating / etc.) instead of
        # retrying with a fresh screenshot. _maybe_no_effect_prefix covers
        # the strict no-DOM/no-URL/no-focus case; we additionally tag
        # label_mismatch / snapped=False because a snap to the wrong
        # element can still register a focus change that masks the miss.
        # Skip both when the click ladder rescued the click via js/keyboard.
        no_effect_caption = f"Clicked {log_target}{snap_note}"
        record_suffix = ""
        _escalation_succeeded = "[click_escalated strategy=" in (verify_note or "")
        if not _escalation_succeeded:
            _snap_dbg = (
                (data.get("snap") or data.get("clicked") or {})
                if isinstance(data, dict) else {}
            )
            _label_mismatch_flag = bool(_snap_dbg.get("labelMismatch")) if isinstance(_snap_dbg, dict) else False
            _snapped_flag = _snap_dbg.get("snapped") if isinstance(_snap_dbg, dict) else None
            no_effect_caption = _maybe_no_effect_prefix(
                data, "browser_click_at", no_effect_caption,
                session_state=self.s,
            )
            _tagged_no_effect = no_effect_caption.startswith("[no_effect:")
            # labelMismatch is now ADVISORY ONLY (page.ts:1574 dropped
            # the silent-skip; grid-scan winner gets dispatched even on
            # low-confidence label match — value-bearing controls like
            # Chakra DateTimePicker triggers systematically have
            # role-vs-value label divergence). Tagging the brain off
            # labelMismatch alone would mis-inform it that the click
            # did nothing when in fact it did. Real silent misclicks
            # still surface via _maybe_no_effect_prefix above on true
            # zero url/DOM/focus delta.
            #
            # The only secondary case worth surfacing is Phase 3 hard
            # fallback (snapped=False because grid-scan found NO
            # interactive element at all — the bbox covered dead space)
            # combined with genuine page inertia.
            _effect_dbg = (data or {}).get("effect") or {}
            _mutation_delta_dbg = int(_effect_dbg.get("mutation_delta") or 0)
            _url_changed_dbg = bool(_effect_dbg.get("url_changed"))
            if (not _tagged_no_effect
                    and _snapped_flag is False
                    and not _label_mismatch_flag
                    and _mutation_delta_dbg == 0
                    and not _url_changed_dbg):
                no_effect_caption = (
                    f"[no_effect:browser_click_at] bbox at V{vision_index} "
                    f"covered no interactive element (Phase 3 hard fallback) "
                    f"and the click had no effect. Call browser_screenshot "
                    f"to refresh vision before clicking; DO NOT retry the "
                    f"same V_n.\n"
                    f"{no_effect_caption}"
                )
                _tagged_no_effect = True
            if _tagged_no_effect:
                record_suffix = " [no_effect]"
                # Phase 3.1: feed the cursor-failure ledger so the
                # run_script escape hatch (scripting.py) can eventually
                # open when cursor clicks silently no-op (snapped but zero
                # DOM/url/focus delta) — the exact deadlock shape on heavy
                # search pages. Skip when an expected_label was passed: the
                # post-click label_still_visible check in vision_pipeline.py
                # already records a click_at failure for that case, and we
                # want exactly ONE record per silent click (the total-failure
                # release threshold counts records).
                try:
                    _exp_label_nf = (
                        payload.get("expected_label")
                        or payload.get("label")
                        or ""
                    )
                except Exception:
                    _exp_label_nf = ""
                if not _exp_label_nf:
                    self.s.record_cursor_failure(
                        strategy="click_at",
                        target=(log_target or f"V{vision_index}")[:80],
                        reason="no_effect",
                    )
                # Bust vision cache for this session so the next prefetch
                # re-runs the model instead of returning identical V_n
                # bboxes that just missed (dom_hash unchanged → cache HIT
                # would otherwise serve the same stale labels).
                try:
                    from nanobot.vision_agent.client import get_vision_agent
                    await get_vision_agent()._cache.bust_session(session_id)
                except Exception:
                    pass

        self.s.record_step(
            "browser_click_at",
            log_target,
            f"url={actual_url[:60] if actual_url else '?'}{snap_note}{record_suffix}",
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
            self.s.build_text_only(data, no_effect_caption) + verify_note,
            expected_label=_expected_label or None,
            pre_url=_pre_url,
            pre_dom_hash=_pre_dom_hash,
            state=self.s,
        )



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
        "calling browser_drag_selectors. Selectors ride as a JSON string "
        "(no ArraySchema in this layer)."
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
        click_count=IntegerSchema(
            "Number of clicks (1 for single, 2 for double)",
            nullable=True,
        ),
        linear=BooleanSchema(
            description=(
                "If true (default), use deterministic teleport click "
                "(pixel-exact). Set false for stealth-critical contexts "
                "(captchas) that need Bezier humanisation."
            ),
            nullable=True,
        ),
        in_iframe=StringSchema(
            description=(
                "CSS selector of an <iframe> host. When provided, "
                "`selector` is resolved INSIDE the iframe's contentFrame "
                "instead of the top-level document. Use this when the "
                "target lives inside an embedded frame (e.g. quizzes, "
                "calculators, captcha widgets). Same-origin iframes work "
                "directly; cross-origin OOPIFs use Puppeteer's Frame API."
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
        "captcha handles. For elements inside an <iframe>, pass "
        "in_iframe=<host_css> to scope the selector to that frame — the "
        "server descends via contentFrame() + frame.evaluate() and "
        "dispatches a CDP click at the translated viewport coords. Fails "
        "fast if the selector is missing or zero-size."
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
        in_iframe: str | None = None,
        **kw: Any,
    ) -> str:
        scope_note = f" in_iframe={in_iframe!r}" if in_iframe else ""
        print(f"\n>> browser_click_selector({selector!r}{scope_note})")
        # Phase 1.1: hard sync gate. Wait for any in-flight vision
        # prefetch from the previous action before dispatching.
        sync_block = await self.s.ensure_vision_synced(
            reason="browser_click_selector",
        )
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls += 1

        # Phase 1.2: dynamic-ID guard. React's `useId()` and friends
        # generate IDs that rotate between renders (`:r13:` → `:r14:`).
        # Selector dispatch on these silently fails or hits the wrong
        # element by the time the click lands. Reject upstream so the
        # brain routes to `browser_click_at(vision_index=...)` on the
        # first call instead of wasting a round-trip on a stale ID.
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
        if _PLAYWRIGHT_PSEUDO_RE.search(selector):
            self.s.record_cursor_failure(
                strategy="click_selector",
                target=selector,
                reason="playwright_pseudo_pattern",
            )
            self.s.log_activity(
                f"click_selector({selector})(PLAYWRIGHT_PSEUDO_REJECTED)",
                "",
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

        payload: dict[str, Any] = {
            "selector": selector,
            "ensureVisible": True,
        }
        if button is not None:
            payload["button"] = button
        if click_count is not None:
            payload["clickCount"] = click_count
        if linear is not None:
            payload["linear"] = linear
        if in_iframe:
            payload["in_iframe"] = in_iframe

        # Surgical undo: open a pending entry. pre_active is None for
        # selector clicks (we don't probe aria state on this path); the
        # label safety-net in finalize_click_record still catches
        # destructive selectors like 'button.delete' / '#submit' and
        # classifies them irreversible. url_changed demotion in
        # finalize handles nav cases.
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
            # Record cursor failure so the script-lockout gate counts
            # this as a tried-and-failed cursor strategy.
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
        # Auto-refresh element_fingerprints from the click response so a
        # follow-up DOM-index click doesn't ship a stale fingerprint.
        _fp_map = data.get("fingerprints") if isinstance(data, dict) else None
        if isinstance(_fp_map, dict):
            self.s.element_fingerprints = {
                int(k): v for k, v in _fp_map.items() if isinstance(v, str)
            }
        clicked = data.get("clicked", {})
        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.record_url(actual_url)
        self.s.record_step(
            "browser_click_selector",
            f"{selector} @ ({clicked.get('x','?')},{clicked.get('y','?')})"
            + (f" in_iframe={in_iframe!r}" if in_iframe else ""),
            data.get("url", ""),
        )
        # click_selector is a mutation — advance the observation token
        # and schedule a vision prefetch so the next screenshot is warm.
        self.s.advance_observation_token("click_selector")
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        caption = (
            f"Clicked {selector} at "
            f"({clicked.get('x','?')},{clicked.get('y','?')})"
        )
        if in_iframe:
            caption += f" [iframe={in_iframe}]"
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        # A successful iframe-scoped click means the deterministic path
        # worked — clear the iframe-miss counter so the next miss starts
        # fresh from advisory-level escalation, not run_script.
        if in_iframe and self.s.iframe_miss_count:
            self.s.iframe_miss_count = 0
            self.s.iframe_miss_key = ""
        return await _append_fresh_vision(
            _vision_task,
            _maybe_no_effect_prefix(
                data, "browser_click_selector", caption,
                session_state=self.s,
            ),
            state=self.s,
        )
