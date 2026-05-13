"""Click tools — DOM-index, vision-bbox (V_n), and rect probe.

`BrowserClickTool` (DOM index), `BrowserClickAtTool` (vision-bbox / coords),
`BrowserGetRectTool` (read-only rect probe).
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
from ._click_core import (
    lookup_postcondition,
    maybe_scroll_bbox_into_view,
    run_click_with_ladder,
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
            log_target = f"V{vision_index}({x0},{y0}→{x1},{y1})"
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
                # Refresh log_target so post-dispatch messages reflect
                # the actual click coords.
                log_target = (
                    f"V{vision_index}("
                    f"{new_bbox['x0']},{new_bbox['y0']}"
                    f"→{new_bbox['x1']},{new_bbox['y1']})"
                )

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
            if err_dbg or label_mismatch or snapped is False:
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
                print(
                    f"  [click_ok: snapped={snapped} "
                    f"mutation_delta={mutation_delta} "
                    f"target={snap_dbg.get('target', '?')[:60]}]"
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
            snap_note = (
                f" snapped→({snap.get('x')},{snap.get('y')}) {snap.get('target','')}".strip()
                if snap.get("snapped") else " (raw bbox center; no interactive element matched)"
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
                snap_note += (
                    f" [WARN:iframe_cross_origin{host_hint} — outer-doc"
                    " click cannot reach inner content (cross-origin"
                    " SOP). Re-screenshot so vision emits a V_n inside"
                    " the iframe, then browser_click_at on that V_n."
                )
            elif warn == "target_in_iframe_miss":
                snap_note += (
                    f" [WARN:iframe_miss{host_hint} — descent ran but"
                    " no clickable was found inside the bbox region."
                    " Vision bbox may be loose. Re-screenshot to get a"
                    " tighter bbox, then browser_click_at on the new V_n."
                )
            elif warn == "target_in_iframe":
                snap_note += (
                    f" [WARN:target_in_iframe{host_hint} — click landed"
                    " on the <iframe> host, NOT inner content. Re-"
                    "screenshot so vision emits a V_n inside the iframe,"
                    " then browser_click_at on that V_n."
                )
            elif warn == "pointer_events_none_ancestor":
                snap_note += (
                    " [WARN:pointer_events_none_ancestor — an "
                    "ancestor has pointer-events:none, the click may "
                    "have passed through to a layer behind.]"
                )
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
