"""<select> + label-anchored option pickers + tracked form-fill orchestration.

`BrowserSelectTool` (DOM-index <select>), `BrowserSelectOptionTool` (label
+ value picker for any dropdown widget), `BrowserFormPlanTool` (one-shot
cascading filter execution), and the trio that drives a tracked form-fill
session: `BrowserFormBeginTool`, `BrowserFormStatusTool`,
`BrowserFormCommitTool`.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from ..formatting import _fetch_elements
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState
from ..vision_pipeline import _append_fresh_vision, _schedule_vision_prefetch


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(description="Element index of the select/dropdown"),
        value=StringSchema("Option value or visible text to select"),
        required=["session_id", "index", "value"],
    )
)
class BrowserSelectTool(Tool):
    name = "browser_select"
    description = "Select an option in a dropdown by value."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, index: int, value: str, **kw: Any) -> Any:
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/select",
            json={"index": index, "value": value},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        # Fetch updated elements after selection (may trigger form changes)
        if not data.get("elements"):
            elements = await _fetch_elements(session_id, self.s)
            if elements:
                data["elements"] = elements
        return self.s.build_text_only(data, f'Selected "{value}" in [{index}]')


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        label=StringSchema(
            "Human-readable label of the dropdown trigger (e.g. 'Brand', "
            "'Processor Brand', 'Year of Release'). Matched via accessible-name "
            "/ <label for=> / aria-labelledby / visible text. Optional when "
            "vision_index is provided — the bbox replaces label-based picking."
        ),
        value=StringSchema(
            "Visible text or value of the option to pick (e.g. 'Dell', 'Intel', "
            "'2017'). Matching priority: exact-ci → startsWith-with-word-boundary "
            "→ contains-with-word-boundary → fuzzy ≥0.85."
        ),
        fuzzy=BooleanSchema(
            description="Allow fuzzy match (Levenshtein ≥0.85). Default true.",
            default=True,
        ),
        timeout=IntegerSchema(
            description="Max ms to wait for listbox/options to render (default 6000).",
            nullable=True,
        ),
        extra_option_selectors=ArraySchema(
            description=(
                "Optional CSS selectors to add to the option-discovery list, "
                "for bespoke widgets that don't expose [role=option]."
            ),
            items=StringSchema(""),
            nullable=True,
        ),
        vision_index=IntegerSchema(
            description=(
                "Optional 1-based vision bbox index (V_n). When set, the "
                "dropdown trigger is resolved by bbox geometry instead of "
                "label-text matching — use this when label resolution returns "
                "ambiguous_trigger. Honors the same vision-freshness / "
                "epoch-age / blocker gates as browser_click_at."
            ),
            nullable=True,
        ),
        in_iframe=StringSchema(
            description=(
                "CSS selector of an <iframe> host. When provided, the "
                "<select> is resolved INSIDE the iframe's contentFrame "
                "instead of the top-level document. Use this when the "
                "target <select> lives inside an embedded frame (quizzes, "
                "calculators, embedded forms). v1 supports NATIVE <select> "
                "only — ARIA combobox/listbox dropdowns inside iframes "
                "should be driven via browser_click_at(vision_index=V_n) "
                "on the trigger; after the menu opens, take a "
                "browser_screenshot so vision labels each menu item and "
                "click the V_n you want."
            ),
            nullable=True,
        ),
        required=["session_id", "value"],
    )
)
class BrowserSelectOptionTool(Tool):
    """Pick a dropdown option by *label* + *value*, hiding DOM-index churn.

    Use this for ANY dropdown/listbox/combobox — native <select>, ARIA
    combobox+listbox, Headless-UI Listbox, etc. You never pass an index or
    vision V-index, so re-renders between cascade steps don't matter.

    On ambiguity (no exact/fuzzy match) the tool returns the candidate list
    instead of guessing — retry with a corrected `value`. For ≥2 dependent
    dropdowns prefer `browser_form_plan` so progress is tracked structurally.
    """

    name = "browser_select_option"
    description = (
        "Pick a dropdown option by label+value. Works on native <select> "
        "AND custom listbox/combobox widgets. Returns {ok, picked_text, "
        "verified, candidates?} — on ambiguity, retry with one of the "
        "candidates instead of clicking blindly. For dropdowns inside "
        "an <iframe>, pass in_iframe='iframe#host' — native <select>s "
        "inside frames work directly (CDP click on a native <select> "
        "does NOT open the dropdown in headless Chromium; this tool "
        "sets the value programmatically + fires `change`)."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        value: str,
        label: str = "",
        fuzzy: bool = True,
        timeout: int | None = None,
        extra_option_selectors: list[str] | None = None,
        vision_index: int | None = None,
        in_iframe: str | None = None,
        **kw: Any,
    ) -> str:
        iframe_note = f" in_iframe={in_iframe!r}" if in_iframe else ""
        if vision_index is not None:
            print(
                f"\n>> browser_select_option(V{vision_index}, "
                f"value={value!r}, label={label!r}{iframe_note})"
            )
        else:
            print(
                f"\n>> browser_select_option(label={label!r}, "
                f"value={value!r}{iframe_note})"
            )
        payload: dict[str, Any] = {
            "label": label or "",
            "value": value,
            "fuzzy": bool(fuzzy),
        }
        if timeout is not None:
            payload["timeout"] = int(timeout)
        if extra_option_selectors:
            payload["extra_option_selectors"] = list(extra_option_selectors)
        if in_iframe:
            payload["in_iframe"] = in_iframe

        # Vision-bbox path. Resolve V_n against the frozen vision epoch
        # (same path browser_click_at uses), apply the same freshness +
        # age + blocker gates, and pass `bbox` + `expected_label` to the
        # TS server. Sidesteps DOM-text ambiguity entirely.
        if vision_index is not None:
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return (
                    "[select_option_failed:no_vision] No recent vision "
                    "response to resolve vision_index against. Take a "
                    "browser_screenshot first, then retry. Or omit "
                    "vision_index and pass `label` for label-based picking."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                return (
                    f"[select_option_failed:bad_vision_index] V{vision_index} "
                    f"is out of range (only {len(resp.bboxes)} bboxes in "
                    f"the last vision response). Re-screenshot before retry."
                )
            freshness = getattr(resp, "screenshot_freshness", "fresh")
            if freshness != "fresh":
                return (
                    f"[select_option_failed:stale_vision freshness="
                    f"{freshness}] Vision flagged the last screenshot as "
                    f"not fresh. Call browser_screenshot to refresh and "
                    f"retry."
                )
            try:
                max_age_turns = int(
                    os.environ.get("VISION_MAX_AGE_TURNS") or "1"
                )
            except ValueError:
                max_age_turns = 1
            if max_age_turns > 0:
                age_turns = max(
                    0,
                    self.s._brain_turn_counter - self.s._vision_epoch_turn,
                )
                if age_turns > max_age_turns:
                    return (
                        f"[select_option_failed:epoch_too_old "
                        f"age_turns={age_turns} max={max_age_turns}] "
                        f"V{vision_index} resolves against a vision "
                        f"snapshot older than the safe age window. "
                        f"browser_screenshot, then retry."
                    )
            # Blocker gate.
            scene = getattr(resp, "scene", None)
            active_blocker = (
                getattr(scene, "active_blocker_layer_id", None)
                if scene is not None else None
            )
            if active_blocker:
                bbox_layer = getattr(bbox, "layer_id", None)
                if bbox_layer and bbox_layer != active_blocker:
                    return (
                        f"[select_option_failed:blocker_active "
                        f"layer={active_blocker}] A blocker layer is on "
                        f"top of content; dismiss it before targeting "
                        f"V{vision_index}."
                    )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return (
                    "[select_option_failed:no_image_dims] Vision "
                    "response has no image dimensions; cannot "
                    "denormalize bbox. Re-screenshot."
                )
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
            payload["bbox"] = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
            bbox_label = (getattr(bbox, "label", "") or "").strip()
            if bbox_label:
                payload["expected_label"] = bbox_label[:120]
                # Backfill `label` when caller omitted it — useful for
                # diagnostics.
                if not payload["label"]:
                    payload["label"] = bbox_label[:120]

        if not payload["label"] and "bbox" not in payload:
            return (
                "[select_option_failed:bad_args] Provide either `label` "
                "or `vision_index`."
            )

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/select_option",
            json=payload,
            timeout=20.0,
        )
        try:
            r.raise_for_status()
        except Exception as e:
            self.s.record_step("browser_select_option", f"{label}={value}", f"HTTP error: {e}")
            return f"[select_option_http_error] {e}"
        data = r.json() or {}
        # Auto-refresh element_fingerprints from the response. select_option
        # often mutates the DOM (filter applied, listbox re-rendered),
        # invalidating the Python-side cache. Without this, a follow-up
        # DOM-index click sends a stale fingerprint that may collide. (B6)
        _fp_map = data.get("fingerprints") if isinstance(data, dict) else None
        if isinstance(_fp_map, dict):
            self.s.element_fingerprints = {
                int(k): v for k, v in _fp_map.items() if isinstance(v, str)
            }
        ok = bool(data.get("ok"))
        picked = data.get("picked_text") or value
        verified = bool(data.get("verified"))
        reason = data.get("reason")
        candidates = data.get("candidates") or []
        tried = data.get("tried") or []
        dom_changed = bool(data.get("dom_changed"))
        new_classes = data.get("new_classes") or []
        trigger_score = data.get("trigger_score")

        # Cursor strategy ledger — counts as a real cursor attempt for the
        # cursor-first lockout in browser_run_script.
        try:
            self.s.cursor_failure_strategies  # type: ignore[attr-defined]
        except Exception:
            pass
        else:
            if not ok and reason:
                self.s.cursor_failure_strategies.add(f"select_option:{reason}")

        if ok:
            note = f"Picked '{picked}' for '{label}'" + ("" if verified else " (verify pending)")
            print(f"   [select_option] ok -> {picked!r} (verified={verified})")
            self.s.log_activity(f"select_option({label})", picked[:40])
            self.s.record_step("browser_select_option", f"{label}={picked}", "ok")
            return self.s.build_text_only(data, note)

        # Ambiguity / failure path — surface candidates so the LLM corrects
        # the value rather than re-screenshotting and click-looping.
        cand_preview = (
            (" candidates=" + str([c[:30] for c in candidates[:6]]))
            if candidates else ""
        )
        print(f"   [select_option] FAIL reason={reason or '?'}{cand_preview}")

        msg_parts = [f"[select_option_failed] reason={reason or 'unknown'} label={label!r} value={value!r}"]
        _ambiguous_needs_vision = False
        if reason == "ambiguous_trigger":
            # Multiple candidates with similar scores. The TS picker
            # surfaces them as `candidates` (shape: tag<role>['text']
            # score=N). Brain narrows the label or passes vision_index.
            msg_parts.append(
                "Multiple candidates matched the label with similar "
                "scores — refusing to guess. Either (a) pass a more "
                "specific `label` (e.g. 'Sort by' instead of 'Sort'), "
                "or (b) take a fresh browser_screenshot and call "
                f"browser_select_option(vision_index=V_n, value={value!r}) "
                "with the V_n of the dropdown trigger you actually want."
            )
        elif reason == "ambiguous_option":
            shown = ", ".join(repr(c) for c in candidates[:5])
            # Literal duplicates ('HP', 'HP') mean the dropdown DOM has
            # multiple identically-labelled options (e.g. a 'Popular'
            # section + alphabetical section both containing the value).
            # Text matching can't disambiguate — defer to vision +
            # visual position via V_n. Handled by the deterministic
            # recovery path below.
            has_literal_duplicates = bool(
                candidates
                and len(candidates) > 1
                and len({
                    c.strip().lower() for c in candidates
                    if isinstance(c, str)
                }) < len(candidates)
            )
            if has_literal_duplicates:
                msg_parts.append(
                    f"Multiple options match {value!r} EXACTLY "
                    f"(candidates: {shown}). The dropdown has duplicate "
                    f"option labels — text-matching can't distinguish "
                    f"them. Refreshing vision so you can pick by "
                    f"VISUAL POSITION via V_n; see [matching_v_n] "
                    f"below."
                )
                # Mark for vision-refresh + V_n surfacing at end of
                # failure path. Avoids restructuring the whole
                # msg_parts builder.
                _ambiguous_needs_vision = True
            else:
                msg_parts.append(
                    f"Multiple options match {value!r} at the same "
                    f"tier. Top candidates: {shown}. Try a more "
                    f"specific value, or call "
                    f"browser_scroll_within(target_text={value!r}) "
                    f"if the right option is below the visible fold of "
                    f"the dropdown, then browser_screenshot and "
                    f"browser_click_at(vision_index=V_n) on the right "
                    f"one."
                )
                _ambiguous_needs_vision = False
        elif reason == "popup_on_navigated_page":
            msg_parts.append(
                "The trigger opened a listbox but the page also "
                "navigated underneath it — the option click would be "
                "applied on the navigated page, not the original one. "
                "Re-screenshot and continue from the new page state, "
                "or use browser_rewind_to_checkpoint to back out."
            )
        elif reason == "bbox_not_a_dropdown":
            msg_parts.append(
                "The bbox you passed via vision_index is not a dropdown "
                "trigger (no aria-haspopup, role=combobox/listbox, "
                "<select>, datalist, or chevron-styled button). Use "
                "browser_click_at(vision_index=V_n) on this bbox "
                "instead, or pass the V_n of a real dropdown trigger."
            )
        elif reason == "option_navigated":
            msg_parts.append(
                "The option click navigated the page. The selection "
                "was NOT applied to the original page state. "
                "Re-screenshot to inspect the new page, or "
                "browser_rewind_to_checkpoint if this was unintentional."
            )
        elif reason == "trigger_disappeared":
            msg_parts.append(
                "The trigger element vanished after the option click "
                "without a navigation event. The page may have "
                "re-rendered with a different layout. Re-screenshot "
                "and re-target."
            )
        elif reason == "trigger_not_found":
            msg_parts.append(
                "The label was not found on this page. Two common causes:\n"
                "  (a) the cascading dropdown stage is over and the page "
                "transitioned to a results grid / model picker — in which "
                "case STOP using browser_form_plan / browser_select_option "
                "and instead browser_get_markdown to inspect the result list, "
                "then browser_click on the matching item.\n"
                "  (b) the label text in the page is different from what "
                "you passed — call browser_screenshot to read actual labels, "
                "then retry with the exact text. Or pass vision_index=V_n "
                "to bypass label resolution entirely."
            )
        elif reason == "trigger_navigated":
            # The trigger turned out to be a link / nav action, not a
            # dropdown opener. Best Buy trade-in 'Brand' picker is the
            # canonical case — each brand is a clickable card that
            # navigates to the next step. Tell the brain to switch
            # tools, don't keep retrying select_option.
            nav_to = ""
            for c in candidates:
                if isinstance(c, str) and c.startswith("navigated_to="):
                    nav_to = c.split("=", 1)[1]
                    break
            msg_parts.append(
                f"The trigger NAVIGATED ({label!r} click changed the "
                f"page to: {nav_to or '?'}). This isn't a dropdown — "
                f"it's a link or card-grid item. STOP retrying "
                f"select_option/form_plan for this label. The page "
                f"is now on a new step. Take a fresh screenshot, "
                f"then click the next target with `browser_click_at` "
                f"(by V_n). For "
                f"card-grid pickers (Brand → Model → ...), each step "
                f"is a navigation, not a dropdown — `browser_click_at` "
                f"on the visible card is the right tool."
            )
        elif reason == "no_popup_detected":
            msg_parts.append(
                f"Click on {label!r} did NOT open any popup/listbox/menu "
                f"(no [role=listbox|menu|dialog] or [aria-expanded=true] "
                f"appeared). The trigger is probably not a dropdown — "
                f"it might be a nav button, a card, or a label without "
                f"an interactive child. Try: (a) `browser_click_at` on "
                f"the visible target, (b) `browser_get_markdown` to "
                f"inspect the page shape, or (c) re-screenshot — the "
                f"page may have transitioned to a different stage."
            )
        if candidates and reason not in ("trigger_navigated",) and not _ambiguous_needs_vision:
            shown = ", ".join(repr(c) for c in candidates[:15])
            msg_parts.append(f"candidates: {shown}")
            msg_parts.append(
                "Retry browser_select_option with one of the candidates above. "
                "Do NOT fall back to raw clicking — DOM indices change after "
                "each pick; this tool re-anchors on the label."
            )
        elif (
            reason
            and reason not in ("trigger_not_found", "trigger_navigated", "no_popup_detected")
            and not _ambiguous_needs_vision
        ):
            msg_parts.append(
                "No options were collected. The listbox may use a non-ARIA "
                "pattern — retry with a more specific label, or pass "
                "extra_option_selectors=[...] (e.g. ['li.option', '.dropdown-item'])."
            )

        # Phase D diagnostics — append on EVERY failure path so the
        # brain can pick a different tool instead of looping. Includes
        # which open-strategies were tried and what the DOM did.
        if tried:
            msg_parts.append(f"open_strategies_tried: {', '.join(str(t) for t in tried)}")
        if reason in ("options_did_not_render", "no_popup_detected"):
            if dom_changed and new_classes:
                # Page DID change — but not into a dropdown. Show top
                # new classes so the LLM can recognize "oh that's a
                # card grid / a wizard step / a modal".
                msg_parts.append(
                    "dom_changed=true. New visible classes (top-5): "
                    + ", ".join(str(c) for c in new_classes[:5])
                )
                msg_parts.append(
                    "→ The trigger likely opened/navigated to a "
                    "non-dropdown UI. Re-screenshot and use "
                    "`browser_click_at` on the visible target instead "
                    "of `browser_select_option`."
                )
            elif not dom_changed:
                msg_parts.append(
                    "dom_changed=false — the trigger click did NOTHING. "
                    "Either the picked element wasn't actually the "
                    "dropdown trigger (label-text matched a heading or "
                    "wrapper near the real trigger), OR the trigger is "
                    "disabled. Re-screenshot, then use `browser_click_at` "
                    "on the visible dropdown chevron / control."
                )
        if trigger_score is not None and float(trigger_score) <= 0:
            # Low-confidence trigger pick — flag it. With a 4-phase
            # picker scoring positive on real dropdowns, a non-positive
            # score is itself a smell.
            msg_parts.append(
                f"trigger_score={trigger_score} (low) — the picked "
                "element may not be a real dropdown trigger. Pass a "
                "more specific label (e.g. 'Processor Brand' instead "
                "of 'Brand') or use `browser_click_at` directly."
            )
        self.s.log_activity(f"select_option({label}, FAIL)", (reason or "")[:60])
        self.s.record_step("browser_select_option", f"{label}={value}", f"FAIL:{reason or '?'}")

        # Deterministic ambiguous_option recovery. The TS matcher found
        # multiple options with identical labels (e.g. 'HP' under
        # "Popular brands" + 'HP' in the alphabetical list) — but the
        # candidates may not all be visible in the current viewport of
        # the dropdown popup.
        #
        # Strategy: PIXEL-SCROLL the popup in steps of ~half its
        # viewport, refreshing vision after each step and scanning the
        # bboxes for matching labels. Stop as soon as one or more
        # matches appear in vision (or after MAX_ITERS exhausts the
        # popup). Pixel scrolling is more reliable than text-based
        # scroll-until: it doesn't depend on the TS matcher correctly
        # identifying when the target text is VISUALLY visible (which
        # can false-positive when the text is in DOM but outside the
        # popup's clipped scroll viewport).
        if _ambiguous_needs_vision:
            MAX_ITERS = 5
            matching: list[tuple[int, str, list[int] | None]] = []
            needle = value.strip().lower()
            try:
                for iteration in range(MAX_ITERS + 1):
                    # Refresh vision and scan current viewport for the
                    # value label. First iteration scans the initial
                    # screenshot (no scroll yet) — handles the case
                    # where all matches are already visible.
                    try:
                        _vision_task = _schedule_vision_prefetch(self.s, session_id)
                        _ = await _append_fresh_vision(
                            _vision_task, "", state=self.s,
                        )
                    except Exception as ve:
                        print(f"   [select_option:vision_refresh iter={iteration} failed] {ve}")

                    resp = self.s.vision_for_target_resolution()
                    if resp is not None:
                        for v_n, bb in enumerate(
                            getattr(resp, "bboxes", []) or [], 1,
                        ):
                            lbl = (getattr(bb, "label", "") or "").strip()
                            if not lbl:
                                continue
                            ll = lbl.lower()
                            if ll == needle or ll.startswith(needle) or needle in ll:
                                matching.append((
                                    v_n, lbl[:60],
                                    list(getattr(bb, "box_2d", []) or []) or None,
                                ))
                            if len(matching) >= 12:
                                break

                    # Stop as soon as vision sees the value somewhere.
                    # The brain picks visually from the displayed V_n.
                    if matching:
                        break
                    if iteration >= MAX_ITERS:
                        break

                    # Pixel-scroll the open popup. amount='page' ≈ 85%
                    # of the container's clientHeight — bigger steps so
                    # we cover the full popup in fewer iterations. The
                    # smaller MAX_ITERS=5 budget * 0.85 ratio reaches
                    # ~4.25 viewports of total scroll, enough for
                    # 200-300 row brand pickers. Auto-detects the popup
                    # container via the expanded signal stack in
                    # page.ts:scrollWithin.
                    try:
                        sw_payload = {
                            "direction": "down",
                            "amount": "page",
                        }
                        sw_r = await _request_with_backoff(
                            "POST",
                            f"{SUPERBROWSER_URL}/session/{session_id}/scroll-within",
                            json=sw_payload,
                            timeout=10.0,
                        )
                        if sw_r.status_code == 200:
                            sw_body = sw_r.json() or {}
                            sw_outcome = (sw_body.get("outcome") or {})
                            sw_px = sw_outcome.get("scrolledPx", 0)
                            sw_reason = sw_outcome.get("reason", "")
                            print(
                                f"   [select_option:pixel_scroll iter={iteration + 1}]"
                                f" scrolledPx={sw_px} reason={sw_reason}"
                            )
                            # If the popup couldn't be found or it hit
                            # its bottom, no point scrolling further.
                            if sw_reason in ("no_container", "page_end"):
                                break
                            if sw_px == 0:
                                # Didn't move — likely hit the end even
                                # if the reason wasn't reported.
                                break
                    except Exception as sw_exc:
                        print(
                            f"   [select_option:pixel_scroll iter={iteration + 1}"
                            f" failed] {sw_exc}"
                        )
                        break

                # DOM-DIRECT FALLBACK. If vision saw 0 matches across
                # all scroll iterations, fall back to a direct DOM
                # query for option elements whose text contains the
                # value. The DOM is authoritative — vision can miss
                # rows (small font, low contrast, beyond Gemini's
                # labeling budget), but DOM doesn't. After finding the
                # elements, scrollIntoView the FIRST one and run vision
                # one more time so the brain still gets a V_n it can
                # click. If vision STILL misses, surface the DOM-direct
                # bounds so the user sees exactly what's there.
                if not matching:
                    try:
                        eval_script = (
                            "(() => {"
                            " const v = " + repr(value.strip().lower()) + ";"
                            " const sels = ['[role=\"option\"]', "
                            "'[role=\"menuitem\"]', "
                            "'[role=\"menuitemcheckbox\"]', "
                            "'[role=\"menuitemradio\"]'];"
                            " const hits = [];"
                            " const seen = new Set();"
                            " for (const sel of sels) {"
                            "  for (const el of document.querySelectorAll(sel)) {"
                            "   if (seen.has(el)) continue; seen.add(el);"
                            "   const t = (el.innerText || el.textContent || '').trim();"
                            "   if (!t) continue;"
                            "   const tl = t.toLowerCase();"
                            "   if (tl === v || tl.startsWith(v) || tl.includes(v)) {"
                            "    const r = el.getBoundingClientRect();"
                            "    const cs = window.getComputedStyle(el);"
                            "    const visible = r.width > 0 && r.height > 0"
                            "      && cs.visibility !== 'hidden'"
                            "      && cs.display !== 'none';"
                            "    hits.push({"
                            "     text: t.slice(0, 80),"
                            "     x: Math.round(r.left + r.width/2),"
                            "     y: Math.round(r.top + r.height/2),"
                            "     w: Math.round(r.width),"
                            "     h: Math.round(r.height),"
                            "     visible: visible,"
                            "     in_viewport: r.top >= 0 && r.bottom <= window.innerHeight,"
                            "    });"
                            "   }"
                            "   if (hits.length >= 10) break;"
                            "  }"
                            "  if (hits.length >= 10) break;"
                            " }"
                            " return hits;"
                            "})()"
                        )
                        ev_r = await _request_with_backoff(
                            "POST",
                            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                            json={"script": eval_script},
                            timeout=8.0,
                        )
                        if ev_r.status_code == 200:
                            ev_body = ev_r.json() or {}
                            dom_hits = ev_body.get("result") or []
                            if isinstance(dom_hits, list) and dom_hits:
                                print(
                                    f"   [select_option:dom_fallback] found "
                                    f"{len(dom_hits)} DOM-direct matches "
                                    f"({sum(1 for h in dom_hits if h.get('in_viewport'))} "
                                    f"in viewport)"
                                )
                                # Step A: scroll the first NON-visible match
                                # into view. block:'center' so vision has
                                # context above and below.
                                first_off_screen = next(
                                    (h for h in dom_hits if not h.get("in_viewport")),
                                    None,
                                )
                                if first_off_screen:
                                    scroll_script = (
                                        "(() => {"
                                        " const v = " + repr(value.strip().lower()) + ";"
                                        " const sels = ['[role=\"option\"]', "
                                        "'[role=\"menuitem\"]', "
                                        "'[role=\"menuitemcheckbox\"]', "
                                        "'[role=\"menuitemradio\"]'];"
                                        " for (const sel of sels) {"
                                        "  for (const el of document.querySelectorAll(sel)) {"
                                        "   const t = (el.innerText || el.textContent || '').trim().toLowerCase();"
                                        "   if (t === v || t.startsWith(v) || t.includes(v)) {"
                                        "    try { el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'}); } catch(e) {}"
                                        "    return true;"
                                        "   }"
                                        "  }"
                                        " }"
                                        " return false;"
                                        "})()"
                                    )
                                    await _request_with_backoff(
                                        "POST",
                                        f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                                        json={"script": scroll_script},
                                        timeout=5.0,
                                    )
                                    # Settle then re-vision
                                    await asyncio.sleep(0.3)
                                    try:
                                        _vt2 = _schedule_vision_prefetch(self.s, session_id)
                                        _ = await _append_fresh_vision(_vt2, "", state=self.s)
                                    except Exception:
                                        pass
                                    resp2 = self.s.vision_for_target_resolution()
                                    if resp2 is not None:
                                        for v_n, bb in enumerate(
                                            getattr(resp2, "bboxes", []) or [], 1,
                                        ):
                                            lbl = (getattr(bb, "label", "") or "").strip()
                                            if not lbl:
                                                continue
                                            ll = lbl.lower()
                                            if ll == needle or ll.startswith(needle) or needle in ll:
                                                matching.append((
                                                    v_n, lbl[:60],
                                                    list(getattr(bb, "box_2d", []) or []) or None,
                                                ))
                                            if len(matching) >= 12:
                                                break
                                # If even after scrollIntoView vision missed
                                # the matches, at least surface the DOM
                                # truth so the brain has a concrete target.
                                if not matching:
                                    rows = []
                                    for h in dom_hits[:8]:
                                        vis = "visible" if h.get("in_viewport") else "scrolled-off"
                                        rows.append(
                                            f"  '{h.get('text','')}'"
                                            f" at ({h.get('x',0)},{h.get('y',0)})"
                                            f" size={h.get('w',0)}x{h.get('h',0)}"
                                            f" [{vis}]"
                                        )
                                    msg_parts.append(
                                        f"[matching_dom count={len(dom_hits)}] "
                                        f"DOM-direct query found these option "
                                        f"elements with text containing "
                                        f"{value!r} (vision didn't label them "
                                        f"as V_n):\n" + "\n".join(rows)
                                        + "\nThe first match was scrolled into "
                                        "view; call browser_screenshot to let "
                                        "vision retry labelling it, then "
                                        "browser_click_at(vision_index=V_n)."
                                    )
                    except Exception as exc:
                        print(f"   [select_option:dom_fallback failed] {exc}")

                if matching:
                    # Sort by visual position (top-to-bottom by ymin,
                    # then left-to-right by xmin).
                    def _pos(item: tuple[int, str, list[int] | None]) -> tuple[int, int]:
                        b = item[2] or [0, 0, 0, 0]
                        return (int(b[0] or 0), int(b[1] or 0))
                    matching.sort(key=_pos)
                    rows = []
                    for v_n, lbl, _ in matching:
                        rows.append(f"  V{v_n} {lbl!r}")
                    msg_parts.append(
                        f"[matching_v_n count={len(matching)}] "
                        f"vision labelled these as candidates after "
                        f"pixel-scrolling the popup (top-to-bottom):\n"
                        + "\n".join(rows)
                        + "\nCall browser_click_at(vision_index=V_n) "
                        "on the one whose visual position / section "
                        "header matches your intent."
                    )
                elif not any("[matching_dom" in p for p in msg_parts):
                    # Neither vision NOR DOM-direct found matches.
                    msg_parts.append(
                        "[matching_v_n count=0] Pixel-scrolled the popup "
                        f"{MAX_ITERS}× and ran a DOM-direct query — "
                        f"neither vision nor DOM saw an option matching "
                        f"{value!r}. Possible causes: dropdown closed "
                        "mid-flight, popup container not auto-detected, "
                        "or option text differs from the value you "
                        "passed (try a more specific value or take "
                        "browser_screenshot + browser_get_markdown to "
                        "inspect the dropdown options)."
                    )
            except Exception as exc:
                msg_parts.append(
                    f"[matching_v_n_error] pixel-scroll recovery failed: "
                    f"{exc}. Call browser_screenshot then "
                    f"browser_click_at(vision_index=V_n) manually."
                )

        return "\n".join(msg_parts)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "Short description of what this filter form is for "
            "(e.g. 'Best Buy laptop trade-in valuation')."
        ),
        fields=ArraySchema(
            description=(
                "Ordered list of dropdowns to fill. Each entry: "
                "{label, value, kind?}. Order is the cascade order — later "
                "fields are filled only after earlier ones succeed. Use "
                "the *visible label text* (e.g. 'Brand', 'Processor Brand') "
                "and the *visible option text* (e.g. 'Dell', 'Intel')."
            ),
            items=ObjectSchema(
                label=StringSchema("Visible label text of the dropdown"),
                value=StringSchema("Visible option text to pick"),
                kind=StringSchema(
                    "Optional: 'select' | 'cascade_select' (default).",
                    nullable=True,
                ),
                required=["label", "value"],
            ),
        ),
        per_step_timeout=IntegerSchema(
            description="Per-field listbox-render timeout (ms, default 4000).",
            nullable=True,
        ),
        stop_on_failure=BooleanSchema(
            description=(
                "If true (default), stop and return on first failed field "
                "with the candidate list. If false, continue past failures "
                "to fill what's possible."
            ),
            default=True,
        ),
        required=["session_id", "intent", "fields"],
    )
)
class BrowserFormPlanTool(Tool):
    """Plan + execute a cascading filter form in one tool call.

    The LLM declares the *whole* form once — Brand=Dell, Processor=Intel,
    RAM=8GB, Year=2017, etc. — and the runtime fills each field by
    label-anchored selection (browser_select_option), settling between
    steps so the next dropdown's options can populate. Removes the
    "stale V-index, regress, retry" loop on multi-step filter forms.

    Returns a structured progress string. On per-field failure (no match,
    listbox didn't render) the tool surfaces the candidate list so the
    LLM can retry with a corrected value.
    """

    name = "browser_form_plan"
    description = (
        "Fill a cascading filter form (≥2 dependent dropdowns) in one "
        "call. Pass an ordered list of {label, value} pairs. The runtime "
        "label-anchors each pick (no DOM index, no V-index) and settles "
        "between steps. Strongly preferred over manual click-loops on "
        "trade-in / search-filter / quote forms."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        intent: str,
        fields: list[dict[str, Any]],
        per_step_timeout: int | None = None,
        stop_on_failure: bool = True,
        **kw: Any,
    ) -> str:
        if not isinstance(fields, list) or not fields:
            return "[form_plan_failed] `fields` must be a non-empty list."
        # Defensive: coerce to plain dicts; reject malformed entries early.
        clean: list[dict[str, Any]] = []
        for i, f in enumerate(fields):
            if not isinstance(f, dict):
                return f"[form_plan_failed] fields[{i}] is not a dict."
            label = (f.get("label") or "").strip()
            value = (f.get("value") or "").strip()
            if not label or not value:
                return (
                    f"[form_plan_failed] fields[{i}] missing label or value: "
                    f"{f!r}"
                )
            clean.append({"label": label, "value": value, "kind": (f.get("kind") or "cascade_select")})

        try:
            from superbrowser_bridge.form_session import FormFillSession, FieldStatus
        except ImportError as exc:
            return f"[form_plan_failed:import] {exc}"

        sess = FormFillSession.begin_cascade(
            intent=intent, fields=clean,
            started_at_turn=self.s._brain_turn_counter,
        )
        self.s.form_session = sess

        print(f"\n>> browser_form_plan({len(clean)} fields)")
        progress: list[str] = [
            f"[form_plan] intent={intent!r} planning {len(clean)} fields"
        ]
        failures: list[str] = []

        # Best-effort: close any open dropdown / modal before we start.
        # If the previous tool call left a listbox/menu open, the next
        # trigger lookup will land inside the overlay rather than on the
        # field's own combobox button.
        try:
            await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/keys",
                json={"keys": "Escape"},
                timeout=5.0,
            )
            await asyncio.sleep(0.2)
        except Exception:
            pass

        for entry in clean:
            label = entry["label"]
            value = entry["value"]
            payload = {"label": label, "value": value, "fuzzy": True}
            if per_step_timeout is not None:
                payload["timeout"] = int(per_step_timeout)
            print(f"   [form_plan] -> {label}={value!r}")
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/select_option",
                json=payload,
                timeout=20.0,
            )
            try:
                r.raise_for_status()
            except Exception as e:
                failures.append(f"{label}={value} → HTTP error: {e}")
                if stop_on_failure:
                    break
                continue
            data = r.json() or {}
            ok = bool(data.get("ok"))
            picked = data.get("picked_text") or value
            reason = data.get("reason")
            candidates = data.get("candidates") or []

            if ok:
                sess.mark_picked(label, picked)
                progress.append(f"  [+] {label} = {picked!r}")
                print(f"   [form_plan]   ok -> picked {picked!r}")
                # Settle so dependent dropdown's options can populate
                # before the next iteration. 350ms covers most React
                # state-update + listbox-render flows; tune via per_step_timeout.
                await asyncio.sleep(0.35)
                # And close any lingering listbox before the next trigger lookup.
                try:
                    await _request_with_backoff(
                        "POST",
                        f"{SUPERBROWSER_URL}/session/{session_id}/keys",
                        json={"keys": "Escape"},
                        timeout=5.0,
                    )
                    await asyncio.sleep(0.15)
                except Exception:
                    pass
            else:
                cand_str = ""
                if candidates:
                    cand_str = (
                        " candidates=" + ", ".join(repr(c) for c in candidates[:10])
                    )
                msg = f"{label}={value} → {reason or 'no_match'}{cand_str}"
                failures.append(msg)
                progress.append(f"  [!] {msg}")
                print(f"   [form_plan]   FAIL -> {reason or '?'} {('cands=' + str(candidates[:6])) if candidates else ''}")
                if stop_on_failure:
                    break

        # Final progress summary
        progress.append("")
        progress.append(sess.cascade_progress())
        if failures and stop_on_failure:
            progress.append(
                "Stopped on first failure. Retry browser_form_plan with "
                "corrected values for the remaining fields, OR retry just "
                "the failed field with browser_select_option using one of "
                "the listed candidates."
            )
        elif failures:
            progress.append(
                f"Continued past {len(failures)} failure(s). Use "
                "browser_select_option to retry each."
            )

        self.s.log_activity(
            f"form_plan({intent[:30]})",
            f"verified {sum(1 for fs in sess.fields.values() if fs.status == FieldStatus.VERIFIED)}/{len(sess.fields)}",
        )
        return "\n".join(progress)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        intent=StringSchema(
            "What this form does — e.g. 'apartment search filters', "
            "'flight booking', 'signup'. Used by the worker hook to "
            "phrase the per-turn checklist."
        ),
        fields=ArraySchema(
            description=(
                "Ordered list of fields to fill. Each entry is an object "
                "with `label` (human-readable name to match against vision "
                "bboxes), `value` (target text to type), and optional "
                "`autocomplete` (true if this field opens a suggestions "
                "overlay that must be picked from)."
            ),
            items=ObjectSchema(
                label=StringSchema("Field name shown to the user"),
                value=StringSchema("Value to type"),
                autocomplete=BooleanSchema(
                    description="Whether this field opens an autocomplete dropdown",
                    nullable=True,
                ),
                required=["label", "value"],
            ),
        ),
        submit_label=StringSchema(
            "Optional label of the submit button (e.g. 'Search'). "
            "If provided, browser_form_commit will look for it in vision.",
            nullable=True,
        ),
        required=["session_id", "intent", "fields"],
    )
)
class BrowserFormBeginTool(Tool):
    """Phase 2.1: open a form-fill session.

    Tracks pending/filled/verified state for each declared field. While
    a session is active the worker hook injects a remaining-fields
    checklist into every tool result, and browser_form_commit refuses
    to dispatch the submit click until every field's typed value is
    visible on the page.

    Use this on dense filter/booking/signup forms where the brain
    routinely loses track of fields below an autocomplete dropdown.
    """

    name = "browser_form_begin"
    description = (
        "Open a tracked form-fill session. After calling, fill each "
        "field with browser_type_at — the session tracks progress and "
        "warns when a field is missed. Conclude with browser_form_commit "
        "to verify all values stuck before submitting."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        intent: str,
        fields: list[dict[str, Any]],
        submit_label: str | None = None,
        **kw: Any,
    ) -> str:
        if os.environ.get("FORM_SESSION_ENABLED", "1") in ("0", "false", "no"):
            return (
                "[form_begin_disabled] FORM_SESSION_ENABLED=0 — fall "
                "back to ad-hoc filling. Track remaining fields yourself."
            )
        if not isinstance(fields, list) or not fields:
            return "[form_begin_failed] `fields` must be a non-empty list."
        try:
            from superbrowser_bridge.form_session import FormFillSession
        except ImportError as exc:
            return f"[form_begin_failed:import] {exc}"
        sess = FormFillSession.begin(
            intent=intent,
            fields=fields,
            started_at_turn=self.s._brain_turn_counter,
            submit_label=submit_label,
        )
        self.s.form_session = sess
        labels = ", ".join(fs.label for fs in sess.fields.values())
        return (
            f"[form_begin] intent={intent!r} fields=[{labels}]\n"
            f"Now: call browser_screenshot once to anchor every field's "
            f"bbox, then for each field call browser_type_at(vision_index="
            f"V_n, text=...). After typing into a field that opens "
            f"autocomplete, click the matching suggestion (or press "
            f"Escape) BEFORE moving on. When every field is filled, "
            f"call browser_form_commit to verify."
        )


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserFormStatusTool(Tool):
    """Phase 2.1: report the current form-fill checklist."""

    name = "browser_form_status"
    description = (
        "Report status of the active form-fill session: which fields are "
        "still pending, which are filled, which need autocomplete picks. "
        "Cheap / no screenshot. Returns [no_form_session] if none active."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        sess = self.s.form_session
        if sess is None:
            return (
                "[no_form_session] No form-fill session active. Call "
                "browser_form_begin to start tracking a multi-field form."
            )
        return sess.remaining_checklist(max_lines=20)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        force=BooleanSchema(
            description=(
                "Skip the verify-all-fields check and close the session anyway. "
                "Use only when you intentionally want to submit a partial form."
            ),
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserFormCommitTool(Tool):
    """Phase 2.1: verify and close a form-fill session.

    Forces a fresh screenshot, then for each tracked field checks that
    the typed value appears in the page's relevant_text. Returns a
    structured pass/fail report — the brain decides whether to refill
    mismatched fields or submit.
    """

    name = "browser_form_commit"
    description = (
        "Verify every tracked field's typed value appears on screen, "
        "then close the form-fill session. Returns the per-field "
        "verdict so the brain can refill any mismatches before "
        "clicking submit."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        force: bool = False,
        **kw: Any,
    ) -> str:
        sess = self.s.form_session
        if sess is None:
            return (
                "[form_commit_failed:no_session] No form session is "
                "active. Call browser_form_begin first."
            )
        # Refresh vision so we verify against the latest screenshot.
        resp = self.s._last_vision_response
        text_hay = ""
        if resp is not None:
            text_hay = (getattr(resp, "relevant_text", "") or "").lower()
        for fs in sess.fields.values():
            if fs.status == FieldStatus.SKIPPED:
                continue
            target_lower = (fs.target_value or "").strip().lower()
            if not target_lower:
                continue
            if target_lower in text_hay:
                sess.mark_verified(fs.label, fs.target_value)
            else:
                # Don't overwrite VERIFIED set during typing flow.
                if fs.status not in (FieldStatus.VERIFIED,):
                    fs.status = FieldStatus.MISMATCH
        summary = sess.commit_summary()
        if not force and not sess.is_complete():
            return (
                f"[form_commit_incomplete] {summary}\n"
                f"{sess.remaining_checklist(max_lines=10)}\n"
                f"Refill the mismatched / pending fields, then call "
                f"browser_form_commit again. Use force=true ONLY if you "
                f"intentionally want to submit a partial form."
            )
        # Success — clear the session so the next form starts clean.
        result = (
            f"[form_commit_ok] {summary}\n"
            f"You may now click the submit button "
            + (f"('{sess.submit_label}') " if sess.submit_label else "")
            + "via browser_click_at(vision_index=V_n)."
        )
        self.s.form_session = None
        return result


# Re-export FieldStatus into module scope so the commit tool can refer to
# it without importing locally on every call.
try:
    from superbrowser_bridge.form_session import FieldStatus  # noqa: F401
except ImportError:
    class FieldStatus:  # type: ignore[no-redef]
        VERIFIED = "verified"
        SKIPPED = "skipped"
        MISMATCH = "mismatch"
