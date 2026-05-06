"""Cursor-action tools — vision-bbox click_at / type / keys / drag /
fix-text, plus the selector-based click fast path.

Click hierarchy exposed to the brain (see tool descriptions):
  1. ``browser_click_selector(<css>)`` — pixel-exact, zero vision cost,
     when the target has a stable hook (id, data-test-id, name).
  2. ``browser_click_at(vision_index=V_n)`` — vision bbox with
     server-side snap to the interactive element inside the bbox.
  3. ``browser_run_script`` — last resort, hard-gated until at least
     one cursor attempt was made on the current vision epoch.

The DOM-index ``browser_click(index)`` path stays removed (indices
drift between turns); selector + vision_bbox covers v2's ground."""

from __future__ import annotations

from ._common import *  # noqa: F401,F403


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        index=IntegerSchema(
            description=(
                "1-based DOM element index from the most recent state "
                "fetch (the [N] markers in the interactive element list). "
                "Use when vision returned a single bbox for a compound "
                "row but you need a specific sub-element (chevron in a "
                "Region row, the actual checkbox not its label), OR "
                "when `browser_click_at(V_n)` keeps snapping to the "
                "wrong child within a wide bbox."
            ),
        ),
        button=StringSchema(
            "Mouse button: left|right|middle. Default left.",
            nullable=True,
        ),
        required=["session_id", "index"],
    )
)
class BrowserClickTool(Tool):
    """Phase H: DOM-index click revival.

    Restored from v2 because vision sometimes returns a single bbox for
    a compound row (label + chevron + count badge), and the row's
    label dominates the snap algorithm — clicks land on the label,
    never the chevron. The DOM-index path bypasses vision entirely:
    the brain reads the [N] markers from the latest interactive
    element list and clicks by index. The TS server's `/click` endpoint
    has always supported `{index, expected_fingerprint}` (it's how
    `browser_type(index)` works); this tool just re-exposes it.

    Stale-index guard: every state fetch caches each index's
    fingerprint. The fingerprint is sent in the click payload; the
    server returns 409 + suggested_index if the fingerprint doesn't
    match the current DOM at that index. The brain re-targets without
    a re-screenshot.
    """

    name = "browser_click"
    description = (
        "Click an interactive element by its DOM `[index]` (the [N] "
        "marker in the most recent element list). **Use when** vision "
        "returns one bbox for a compound row and you need a specific "
        "sub-element (chevron, checkbox, count badge), OR when "
        "`browser_click_at(V_n)` keeps snapping to the wrong child. "
        "**Re-fetch state** right before calling — DOM indices DO "
        "change between turns; the fingerprint guard refuses stale "
        "indices and suggests the new index. **Hierarchy:** "
        "(1) `browser_click_selector(<css>)` for stable hooks; "
        "(2) `browser_click_at(vision_index=V_n)` for vision targets "
        "(or `region='right'` for a sub-control of a compound row); "
        "(3) `browser_click(index=N)` for DOM-index sub-elements; "
        "(4) `browser_run_script` last resort."
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
        **kw: Any,
    ) -> Any:
        print(f"\n>> browser_click([{index}])")
        gate = await _feedback_gate("browser_click")
        if gate:
            return gate
        if self.s._mutation_needs_observation:
            return (
                "[click_refused:observe_first] Your last action changed "
                "the page but you haven't observed what happened. Call "
                "browser_screenshot or browser_get_markdown BEFORE the "
                "next click."
            )
        if getattr(self.s, "_force_reobserve_pending", False):
            return (
                "[click_refused:cursor_cascade] 3+ recent cursor failures "
                "with a stale screenshot. Call browser_screenshot to "
                "refresh vision before any more clicks."
            )
        sync_block = await self.s.ensure_vision_synced(reason="browser_click")
        if sync_block:
            return sync_block

        self.s._brain_turn_counter += 1
        # Phase 4 hard gate: counts as a cursor interaction on this epoch.
        self.s.epoch_interact_attempts += 1
        self.s.consecutive_click_calls += 1
        self.s._scripts_since_observation = 0

        if self.s.consecutive_click_timeouts >= self.s.MAX_CONSECUTIVE_CLICK_TIMEOUTS:
            alts = _vision_alternatives_hint(self.s, limit=3)
            self.s.log_activity(
                f"click([{index}])(LOOP_BLOCKED)",
                f"timeouts={self.s.consecutive_click_timeouts}",
            )
            return (
                f"[click_loop_detected] {self.s.consecutive_click_timeouts} "
                "consecutive click timeouts. Page is likely blocked "
                "(loader, modal, pending nav). Call browser_screenshot."
                + (f"\n{alts}" if alts else "")
            )

        target_key = f"click[{index}]"
        dead = self.s.check_dead_click(target_key)
        if dead:
            self.s.log_activity(f"click([{index}])(DEAD_CLICK_BLOCKED)", "")
            return dead
        loose = self.s.check_dead_click_loose(target_key)
        if loose:
            self.s.log_activity(f"click([{index}])(LOOSE_DEAD_BLOCKED)", "")
            return loose
        self.s.register_click_attempt(target_key)
        self.s.capture_action_snapshot(target_index=index)
        await self.s.inter_action_pause()

        payload: dict[str, Any] = {"index": index}
        if button:
            payload["button"] = button
        cached_fp = self.s.element_fingerprints.get(index)
        if cached_fp:
            payload["expected_fingerprint"] = cached_fp
        elif self.s.element_fingerprints:
            await _fetch_elements(session_id, self.s)
            if index not in self.s.element_fingerprints:
                self.s.record_cursor_failure(
                    strategy="click",
                    target=f"[{index}]",
                    reason="unknown_index",
                )
                return (
                    f"[click_failed:unknown_index] [{index}] is not in "
                    f"the current selectorMap "
                    f"({len(self.s.element_fingerprints)} indices). "
                    "Re-read elements list and pick a valid index, OR "
                    "use browser_click_at(vision_index=V_n)."
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
            if r.status_code == 409:
                info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                stale_msg = info.get("error", "Stale index")
                suggested = info.get("suggested_index")
                current = info.get("current_element", "")
                hint = (
                    f" Try [{suggested}]." if suggested is not None
                    else " Re-read elements list and pick again."
                )
                self.s.log_activity(f"click([{index}])(STALE)", f"suggested={suggested}")
                self.s.record_cursor_failure(
                    strategy="click",
                    target=f"[{index}]",
                    reason="stale_index",
                )
                await _fetch_elements(session_id, self.s)
                return (
                    f"[click_failed:stale_index] {stale_msg} "
                    f"Current [{index}] is {current}.{hint}"
                )
            if r.status_code == 400:
                info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                reason = info.get("reason", "unknown")
                err = info.get("error", f"click [{index}] failed")
                alternatives = info.get("alternatives") or []
                await _fetch_elements(session_id, self.s)
                self.s.log_activity(f"click([{index}])({reason})", err[:60])
                self.s.record_cursor_failure(
                    strategy="click",
                    target=f"[{index}]",
                    reason=f"{reason}: {err[:80]}",
                )
                alt_lines = "\n".join(f"  - {a}" for a in alternatives[:3]) if alternatives else ""
                return (
                    f"[click_failed:{reason}] {err}"
                    + (f"\nAlternatives:\n{alt_lines}" if alt_lines else "")
                    + "\nElements have been re-read — pick a current [index]."
                )
            r.raise_for_status()
            data = r.json()
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout) as e:
            self.s.consecutive_click_timeouts += 1
            self.s.log_activity(
                f"click([{index}])(TIMEOUT)",
                f"count={self.s.consecutive_click_timeouts}",
            )
            return (
                f"[click_failed:timeout] Backend didn't respond to "
                f"click([{index}]) within timeout. Page likely waiting "
                "on navigation or blocked by a loader. Call "
                "browser_screenshot."
            )
        except httpx.HTTPStatusError as e:
            self.s.log_activity(f"click([{index}])(HTTP{e.response.status_code})", str(e)[:60])
            return (
                f"[click_failed:http_{e.response.status_code}] "
                f"{e.response.text[:200] if e.response.text else str(e)[:200]}"
            )
        except Exception as e:
            self.s.log_activity(f"click([{index}])(TRANSPORT)", str(e)[:60])
            return f"[click_failed:transport] {str(e)[:200]} — browser unreachable."

        self.s.consecutive_click_timeouts = 0
        actual_url = data.get("url", self.s.current_url) or ""
        if actual_url:
            self.s.record_url(actual_url)
        self.s.log_activity(f"click([{index}])", f"url={actual_url[:50] if actual_url else '?'}")
        self.s.record_step("browser_click", f"index={index}", f"url={actual_url[:60] if actual_url else '?'}")
        self.s._mutation_needs_observation = True
        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, f"Clicked [{index}]"),
            state=self.s,
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
        region=StringSchema(
            description=(
                "Phase G4: target a SUB-REGION of the bbox instead of "
                "the whole bbox. Useful when vision returned one bbox "
                "for a compound row and you need a specific control: "
                "`right` clicks the right ~30% (chevrons / toggles), "
                "`left` clicks the left ~30% (checkboxes / icons), "
                "`top`/`bottom` for vertical rows, `center` is the "
                "default. The chosen sub-region's centroid is sent to "
                "the snap algorithm — combined with G3 chevron-aware "
                "tiebreaker, this lets you hit specifically the "
                "expand arrow on a Region row instead of the row's "
                "label area."
            ),
            nullable=True,
        ),
        required=["session_id"],
    )
)
class BrowserClickAtTool(Tool):
    name = "browser_click_at"
    description = (
        "Click using a vision bbox (vision_index=V_n) or raw (x,y) "
        "coordinates. The server snaps to the interactive element inside "
        "the bbox, eliminating off-by-pixel misses. "
        "**Compound rows:** when one bbox covers a row with multiple "
        "controls (label + chevron, checkbox + label + count), pass "
        "`region='right'` (or 'left'/'top'/'bottom') to target the "
        "specific sub-control. "
        "**Hierarchy:** try `browser_click_selector(<css>)` FIRST when the "
        "target has a stable DOM hook (id, data-test-id, name attribute, "
        "ARIA role with text) — pixel-exact and zero Gemini cost. Fall "
        "back to this tool only when no DOM hook exists or the bbox is "
        "the only reliable handle. **Never** use `browser_run_script` to "
        "click — it bypasses anti-bot heuristics."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        vision_index: int | None = None,
        x: float | None = None,
        y: float | None = None,
        region: str | None = None,
        **kw: Any,
    ) -> Any:
        # Post-mutation observation gate: refuse if the brain hasn't
        # observed the result of its last action yet.
        if self.s._mutation_needs_observation:
            return (
                "[click_at_refused:observe_first] Your last action changed "
                "the page but you haven't observed what happened. Call "
                "browser_screenshot (to see the current state and get fresh "
                "V_n labels) or browser_get_markdown (if you only need text) "
                "BEFORE clicking again. Acting on stale assumptions causes "
                "misclicks and hallucinated interactions."
            )
        # Phase E1: cursor cascade gate. After 3+ cursor failures with
        # no fresh observation, refuse all mutating cursor work until a
        # browser_screenshot lands. Prevents the wineaccess.com loop
        # where the brain keeps clicking V_n on a stale screenshot.
        if getattr(self.s, "_force_reobserve_pending", False):
            return (
                "[click_at_refused:cursor_cascade] You have 3+ recent "
                "cursor failures and your last screenshot is stale. "
                "This loop is dead — call browser_screenshot to refresh "
                "vision before any more clicks. If you've been searching "
                "for a label that no V_n has matched, prefer "
                "browser_find_target(label=<what you want>) which "
                "locates targets across viewport + collapsed sections "
                "and reports a structured next-action hint."
            )
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
        self.s._scripts_since_observation = 0
        # Phase 4 hard gate: count this as a cursor interaction on the
        # current vision epoch — un-blocks run_script after this turn.
        self.s.epoch_interact_attempts += 1
        # click_at addresses by vision bbox or pixel coords, not DOM
        # index — pass None so target_disappeared isn't computed.
        self.s.capture_action_snapshot(target_index=None)
        await self.s.inter_action_pause()
        if self.s.click_at_count > self.s.MAX_CLICK_AT:
            return (
                f"[BLOCKED] browser_click_at used "
                f"{self.s.click_at_count} times in this session. The "
                f"task is looping on clicks — call browser_screenshot "
                f"to re-observe, then re-issue browser_click_at with a "
                f"fresh vision_index=V_n from the new screenshot, or "
                f"browser_rewind_to_checkpoint if the page is stuck. "
                f"Do NOT attempt browser_run_script to click — JS clicks "
                f"are isTrusted=false and bot-detected."
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
        dead = self.s.check_dead_click(target_key)
        if dead:
            self.s.log_activity(f"click_at{target_key}(DEAD_CLICK_BLOCKED)", "")
            return dead
        # Phase A4: looser guard runs after target_key resolution but
        # BEFORE coords are known. Coord-equivalence matching adds
        # value once we resolve the bbox; for the target_key path it
        # also fires earlier and shorts the click cascade. Pass coords
        # later via register_click_attempt.
        loose = self.s.check_dead_click_loose(target_key)
        if loose:
            self.s.log_activity(f"click_at{target_key}(LOOSE_DEAD_BLOCKED)", "")
            return loose
        self.s.register_click_attempt(target_key)

        payload: dict[str, Any]
        log_target: str
        if vision_index is not None:
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
                # Phase I2/I3: better message + record as cursor
                # failure so the cascade gate counts hallucinations
                # toward force-reobserve.
                _v_max = len(resp.bboxes)
                self.s.record_cursor_failure(
                    strategy="click_at",
                    target=f"V{int(vision_index)}",
                    reason=f"bad_vision_index (range V1..V{_v_max})",
                )
                return (
                    f"[click_at_failed:bad_vision_index] V{vision_index} "
                    f"is out of range. The current vision epoch has "
                    f"V1..V{_v_max}. V{vision_index} is either "
                    "(a) stale — left over from an older screenshot "
                    "that has since been evicted from your context, "
                    "or (b) hallucinated. Call browser_screenshot to "
                    f"refresh, then re-issue with a V_n in V1..V{_v_max}. "
                    "If you keep referencing V_n that don't exist, the "
                    "cursor-cascade gate will force-reobserve."
                )
            # Freshness gate — refuse to click when the last vision pass
            # flagged the screenshot as stale or uncertain. The planner
            # should re-screenshot before committing a click on a frame
            # the model itself said it couldn't trust.
            freshness = getattr(resp, "screenshot_freshness", "fresh")
            if freshness != "fresh":
                # Don't record this as a cursor failure — it's a system
                # pre-condition gate (vision stale), not a real click
                # attempt. Recording it lets the brain cheaply unlock
                # mutates=true scripts via the cursor-first lockout.
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
            # Phase A2: bbox age gate. Vision freshness is model-reported
            # ("did the screenshot look stale to me"); this gate is
            # wall-clock-measured ("how long since I captured the bbox
            # the brain is acting on?"). The wineaccess.com trace had
            # 30+ second windows between screenshot and click — vision
            # said "fresh" because the URL hadn't changed, but the page
            # had loaded a lot of dynamic content underneath. 8 seconds
            # is generous for normal interactions but catches stale
            # epochs. Tunable via BBOX_MAX_AGE_MS.
            try:
                _bbox_max_age_ms = int(
                    os.environ.get("BBOX_MAX_AGE_MS") or "8000"
                )
            except ValueError:
                _bbox_max_age_ms = 8000
            _epoch_at = float(getattr(self.s, "_vision_epoch_taken_at", 0.0) or 0.0)
            if _epoch_at > 0 and _bbox_max_age_ms > 0:
                _bbox_age_ms = int((time.time() - _epoch_at) * 1000)
                if _bbox_age_ms > _bbox_max_age_ms:
                    return (
                        f"[click_at_failed:bbox_too_old "
                        f"age={_bbox_age_ms}ms threshold={_bbox_max_age_ms}ms] "
                        f"The screenshot V{vision_index} resolves against "
                        "is more than 8 seconds old. The page has likely "
                        "moved underneath you (lazy-loaded content, async "
                        "renders, dynamic ads). Call browser_screenshot to "
                        "capture the settled state, then re-issue "
                        "browser_click_at with a fresh V_n."
                    )
            # Trust the bbox: vision returns coords, brain picks V_n,
            # we click the centre. The previous epoch_too_old /
            # blocker_active / low_confidence gates were heuristics
            # that refused legitimate clicks when vision was briefly
            # noisy and forced extra screenshot round-trips. The
            # ensure_vision_synced gate above already guarantees the
            # bboxes were produced for the current page state.
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
            # Phase G4: optional sub-region targeting. The brain passes
            # `region='right'` when it wants the chevron in a compound
            # row; we shrink the bbox to that sub-region BEFORE sending
            # to the snap algorithm so the grid scan only considers
            # interactive elements within that fraction of the row.
            region_norm = (region or "center").strip().lower()
            if region_norm in ("left", "right", "top", "bottom", "center"):
                w = x1 - x0
                h = y1 - y0
                # Sub-region width/height fraction. 35% wide on the
                # short axis, full on the long axis. Generous enough
                # that small chevrons / count badges fall inside.
                FRACT = 0.35
                if region_norm == "left":
                    x1 = x0 + max(1, int(round(w * FRACT)))
                elif region_norm == "right":
                    x0 = x1 - max(1, int(round(w * FRACT)))
                elif region_norm == "top":
                    y1 = y0 + max(1, int(round(h * FRACT)))
                elif region_norm == "bottom":
                    y0 = y1 - max(1, int(round(h * FRACT)))
                # 'center' = no shrink — equivalent to omitting region.
            payload = {"bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}
            # One-line debug so the user can spot coord-space bugs at a
            # glance: confirms image dims + DPR + the [0..1000] box_2d
            # the model produced. Disable with VISION_CLICK_DEBUG=0.
            if os.environ.get("VISION_CLICK_DEBUG", "1") not in ("0", "false", "no"):
                _region_dbg = f" region={region_norm}" if region_norm != "center" else ""
                print(
                    f"  [click_at_debug] V{vision_index} "
                    f"image=({iw}x{ih}) dpr={dpr_val} "
                    f"box_2d={list(bbox.box_2d)} → "
                    f"css=({x0},{y0},{x1},{y1}){_region_dbg}"
                )
            # Carry the vision label into the click payload so the T3
            # backend can run a post-snap semantic match check. Empty
            # label → the check is skipped on the backend, which is
            # fine for raw-coord clicks further below.
            bbox_label = (getattr(bbox, "label", "") or "").strip()
            if bbox_label:
                payload["expected_label"] = bbox_label[:120]
                payload["label"] = bbox_label[:120]
            log_target = (
                f"V{vision_index}"
                + (f"[{region_norm}]" if region_norm != "center" else "")
                + f"({x0},{y0}→{x1},{y1})"
            )
            print(f"\n>> browser_click_at(V{vision_index}{f' region={region_norm}' if region_norm != 'center' else ''}) → bbox=({x0},{y0},{x1},{y1})")
        else:
            if x is None or y is None:
                return "[click_at_failed:bad_args] Provide either vision_index or both x and y."
            # Raw (x, y) clicks are unconditionally blocked. The brain
            # must use vision_index=V_n from the last screenshot. Raw
            # pixel coordinates are brittle (DPR mismatch, layout shift,
            # guessed values) and produce "click hits nothing" failures.
            return (
                f"[click_at_refused:raw_coords] Raw (x={x}, y={y}) "
                f"coordinates are not supported. Use "
                f"vision_index=V_n from your last browser_screenshot "
                f"to click elements. If the target isn't in the V_n "
                f"list, call browser_screenshot(intent=\"<describe the "
                f"specific control you need>\") to surface it."
            )

        # Vision-trust mode: bbox coords from the most recent vision
        # pass are dispatched directly. No DOM↔vision IoU crosscheck,
        # no re-screenshot retry — the brain already gates on a fresh
        # vision via ensure_vision_synced() before this tool runs.

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
        # Phase A1: drift detection. The TS clickInBbox now re-reads
        # elementsFromPoint(snap.x, snap.y) just before dispatching the
        # CDP click. If the element materially shifted (different tag,
        # rect moved >10px, or text replaced) it returns drift=true
        # WITHOUT clicking. Force re-screenshot — the visual context
        # the brain was acting on no longer matches the page state.
        # This is the common silent-no-op cascade root cause: layout
        # shifted between vision pass and click dispatch (lazy-loaded
        # images, font swaps, animations).
        snap_obj = (data.get("snap") or {}) if isinstance(data, dict) else {}
        if isinstance(snap_obj, dict) and snap_obj.get("drift"):
            reason = snap_obj.get("driftReason") or "layout_shifted"
            self.s.log_activity(
                f"click_at{log_target}(DRIFT)",
                str(reason)[:80],
            )
            self.s.record_cursor_failure(
                strategy="click_at",
                target=log_target,
                reason=f"drift:{reason}"[:120],
            )
            # Force a fresh observation epoch — the next click_at on
            # this V_n would resolve against the same stale anchor and
            # drift again. Setting _mutation_needs_observation makes
            # ensure_vision_synced + the post-mutation gate require a
            # screenshot before the next mutation.
            self.s._mutation_needs_observation = True
            return (
                f"[click_at_failed:drift_detected reason={reason}] "
                "The element you targeted shifted between the vision "
                "pass and click dispatch (lazy-loaded image, font "
                "swap, animation, framework re-render). The click was "
                "REFUSED — firing it would have hit empty space or a "
                "different element. Call browser_screenshot to capture "
                "the settled page state, then re-issue browser_click_at "
                "with a fresh V_n. Do NOT retry the same V_n without "
                "re-screenshotting first."
            )
        # Phase J3: surface snap.method + edge_pick to the brain. When
        # the snap took the grid_scan path AND landed off-centre
        # (edgePick=true), the bbox was loose and the brain should
        # re-screenshot before the NEXT click on this V_n. We don't
        # block — the click already dispatched — but we attach a hint.
        snap_method_hint = ""
        if isinstance(snap_obj, dict):
            snap_method = snap_obj.get("method") or ""
            edge_pick = bool(snap_obj.get("edgePick") or snap_obj.get("edge_pick"))
            if snap_method == "grid_scan" and edge_pick:
                snap_method_hint = (
                    "\n[SNAP_LOOSE method=grid_scan edge_pick=true] "
                    "The bbox was loose and the snap chose an off-centre "
                    "interactive child. Subsequent clicks on this same V_n "
                    "may snap to a different child. Call browser_screenshot "
                    "before the next click on this target, OR use "
                    "browser_click_at(vision_index=V_n, region='right'/'left') "
                    "to narrow the snap region if you want a specific "
                    "sub-control."
                )
            elif snap_method == "raw_centre":
                snap_method_hint = (
                    "\n[SNAP_NO_INTERACTIVE method=raw_centre] "
                    "The bbox centre had no interactive element and grid "
                    "scan found none either. Click dispatched at raw "
                    "coords (snapped=false). If nothing changed, this "
                    "bbox is over an inert region — re-screenshot."
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
        actual_url = data.get("url", self.s.current_url)
        if actual_url:
            self.s.record_url(actual_url)
        snap = data.get("snap")  # {x, y, snapped: bool, target?: str}
        if snap:
            snap_note = (
                f" snapped→({snap.get('x')},{snap.get('y')}) {snap.get('target','')}".strip()
                if snap.get("snapped") else " (raw bbox center; no interactive element matched)"
            )
        else:
            snap_note = ""

        # Post-click verification — look up the postcondition the planner
        # attached to this target (by vision_index or by coord match)
        # and run it via verify_action. Runs only for t3 sessions and
        # when VERIFY_AFTER_CLICK is enabled (default on). A miss is
        # reported in the caption so the brain can decide to retry with
        # a different strategy or call browser_plan_next_steps.
        verify_note = ""
        if session_id.startswith("t3-") and \
                os.environ.get("VERIFY_AFTER_CLICK", "1") != "0":
            postcond = self._lookup_postcondition(vision_index, x, y)
            if postcond is not None:
                try:
                    from superbrowser_bridge.antibot import interactive_session as _t3mgr
                    from superbrowser_bridge.verify_action import verify_after, PreState
                    mgr = _t3mgr.default()
                    vr = await verify_after(
                        mgr, session_id, postcond,
                        pre_state=PreState(url=self.s.current_url or ""),
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
                            for alt_strategy in ("js", "keyboard"):
                                try:
                                    from superbrowser_bridge.antibot import (
                                        interactive_session as _t3mgr2,
                                    )
                                    mgr2 = _t3mgr2.default()
                                    alt_bbox = payload.get("bbox")
                                    alt_x = (alt_bbox["x0"] + alt_bbox["x1"]) / 2
                                    alt_y = (alt_bbox["y0"] + alt_bbox["y1"]) / 2
                                    alt_resp = await mgr2.click_at(
                                        session_id, alt_x, alt_y,
                                        bbox=alt_bbox,
                                        strategy=alt_strategy,
                                    )
                                    if not isinstance(alt_resp, dict) or \
                                            not alt_resp.get("success"):
                                        continue
                                    # Re-verify after the escalated strategy.
                                    vr2 = await verify_after(
                                        mgr, session_id, postcond,
                                        pre_state=PreState(
                                            url=self.s.current_url or "",
                                        ),
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
        # V_n priority nudge. Two triggers, either one fires the warn:
        #   (a) score-gap: brief's focus-bbox recommendation points at a
        #       V_m with significantly higher focus-match score than
        #       the chosen V_n.
        #   (b) index-gap: chosen V_n >= 3 — vision sorts V1, V2, ... V_N
        #       by importance, so anything V3+ is a priority skip
        #       regardless of focus match.
        # Soft only — never refuses; the brain has legitimate reasons
        # to skip V1/V2 (banner dismissal, sub-link).
        v_priority_note = ""
        try:
            brief = getattr(self.s, "task_brief", None)
            if vision_index is not None:
                vr_pre = getattr(self.s, "_last_vision_response", None)
                bboxes_pre = list(getattr(vr_pre, "bboxes", []) or []) if vr_pre else []
                chosen_v = int(vision_index)
                recs = brief.recommend_bboxes(vr_pre, top_k=3) if (brief is not None and vr_pre) else []
                chosen_score = (
                    next((r["score"] for r in recs if r["v_index"] == chosen_v), 0.0)
                    if recs else 0.0
                )
                top = recs[0] if recs else None
                score_gap = bool(
                    top and top["v_index"] != chosen_v
                    and (top["score"] - chosen_score) >= 0.3
                )
                index_gap = chosen_v >= 3
                if (score_gap or index_gap) and bboxes_pre:
                    focus = brief.next_focus() if brief else None
                    focus_str = (
                        f"#{focus.id} {focus.label!r}" if focus else "(none)"
                    )
                    v1_label = (
                        getattr(bboxes_pre[0], "label", "") or ""
                    ).strip()[:40]
                    v2_label = (
                        getattr(bboxes_pre[1], "label", "") or ""
                    ).strip()[:40] if len(bboxes_pre) >= 2 else ""
                    rec_str = (
                        f"V{top['v_index']} {top['label'][:40]!r} (match {top['score']}) is recommended for your focus."
                        if top and top["v_index"] != chosen_v else ""
                    )
                    v2_str = f", V2={v2_label!r}" if v2_label else ""
                    v_priority_note = (
                        f"\n[V_PRIORITY] focus={focus_str}. You picked V{chosen_v}. "
                        f"Vision sorts V1, V2, ... V_N by importance — V1={v1_label!r}{v2_str}"
                        " is the model's strongest recommendation. "
                        f"{rec_str} If V{chosen_v} was a deliberate choice "
                        "(e.g. dismissing a banner or following a sub-link), proceed; "
                        "otherwise re-screenshot and prefer V1 or V2."
                    )
        except Exception as exc:
            print(f"[v_priority_check_error] {exc}")
        # Mark that the brain must observe before its next mutation.
        self.s._mutation_needs_observation = True
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, f"Clicked {log_target}{snap_note}")
            + verify_note + v_priority_note + snap_method_hint,
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
        "bbox-targeted typing — no more concatenation bugs. "
        "**Hierarchy:** when the field has a stable selector "
        "(`#email`, `[name=\"...\"]`, `[data-test-id=...]`), prefer "
        "`browser_click_selector` followed by `browser_keys` or use the "
        "form-fill orchestration tools — this avoids the vision round-trip."
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
        # Phase 4 hard gate: cursor interaction on this epoch.
        self.s.epoch_interact_attempts += 1
        self.s.capture_action_snapshot(target_index=None)
        await self.s.inter_action_pause()
        if text is None:
            text = ""

        # Cross-index repeat-type guard. Same purpose as in BrowserTypeTool —
        # catches the cascade where the brain types the same value into
        # different vision indices / dom indices in rapid succession
        # without verifying via screenshot.
        # Phase L: pass target_key for the per-target turn-window guard.
        if vision_index is not None:
            type_target_key = f"type_at(V{int(vision_index)})"
        elif x is not None and y is not None:
            type_target_key = f"type_at({round(float(x)/5)*5},{round(float(y)/5)*5})"
        else:
            type_target_key = "type_at(?)"
        repeat_block = self.s.check_repeat_type(text, target_key=type_target_key)
        if repeat_block:
            self.s.record_step(
                "browser_type_at",
                f"vision_index={vision_index} text={text[:30]!r}",
                "REPEAT_TYPE: refused (cross-index cascade)",
            )
            return repeat_block

        # Resolve target point: vision_index first, then (x, y).
        target_x: float
        target_y: float
        label: str
        if vision_index is not None:
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
                _max_age = int(
                    os.environ.get("VISION_MAX_AGE_TURNS") or "1"
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
            from superbrowser_bridge.type_verify import verify_and_correct
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
        # Cross-index ledger update — symmetric with BrowserTypeTool so
        # the next type-call's check_repeat_type sees this attempt.
        self.s.record_typed_value(text, target_key=type_target_key)
        # Surface before/after for the action-delta renderer.
        self.s.action_snapshot_extras = {
            "before": before,
            "after": after,
            "changed": changed,
        }
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
        self.s._brain_turn_counter += 1
        # Phase 4 hard gate: cursor interaction on this epoch.
        self.s.epoch_interact_attempts += 1
        self.s.capture_action_snapshot(target_index=None)
        await self.s.inter_action_pause()

        # Cross-index repeat-type guard. fix_text_at is the worst offender
        # for the "type 40 six times" cascade because it's pitched as the
        # idempotent atomic-set tool — the brain reaches for it on every
        # retry. Block on the 3rd attempt so a screenshot-and-think gate
        # gets enforced.
        if vision_index is not None:
            fix_target_key = f"fix_text_at(V{int(vision_index)})"
        elif x is not None and y is not None:
            fix_target_key = f"fix_text_at({round(float(x)/5)*5},{round(float(y)/5)*5})"
        else:
            fix_target_key = "fix_text_at(?)"
        repeat_block = self.s.check_repeat_type(text, target_key=fix_target_key)
        if repeat_block:
            self.s.record_step(
                "browser_fix_text_at",
                f"vision_index={vision_index} text={text[:30]!r}",
                "REPEAT_TYPE: refused (cross-index cascade)",
            )
            return repeat_block

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
        # Cross-index ledger update — count this as a typed value too.
        self.s.record_typed_value(text, target_key=fix_target_key)
        # Wrap result in the same shape build_text_only expects.
        synthetic_data = {
            "success": True,
            "before": before,
            "after": after,
            "changed": changed,
            "diff": diff,
        }
        if changed:
            from superbrowser_bridge.type_verify import verify_and_correct
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
        # CURSOR_ONLY_MODE: when the orchestrator decomposed a multi-
        # condition query (task_brief is set), DOM-index typing is
        # disabled. DOM indices drift and the keystrokes often land on
        # an adjacent non-input element. Force the brain through
        # browser_type_at(V_n, text=...) which uses the bbox bbox path.
        if (
            getattr(self.s, "task_brief", None) is not None
            and os.environ.get("CURSOR_ONLY_MODE", "1") not in ("0", "false", "no")
        ):
            self.s.record_step(
                "browser_type", f"index={index} text={text[:30]!r}",
                "REFUSED: CURSOR_ONLY_MODE active",
            )
            return (
                f"[CURSOR_ONLY_MODE] browser_type([{index}], …) by DOM "
                f"index is DISABLED in multi-condition mode. DOM "
                f"indices drift; the keystrokes often land on an "
                f"adjacent non-input element. Use:\n"
                f"  browser_type_at(vision_index=V_n, text={text!r})\n"
                f"Pick the V_n that vision labels as the input field "
                f"you want. Call browser_screenshot first if no V_n "
                f"matches."
            )
        # Phase 1.1: hard sync gate.
        sync_block = await self.s.ensure_vision_synced(reason="browser_type")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        # Phase 4 hard gate: cursor interaction on this epoch.
        self.s.epoch_interact_attempts += 1
        self.s.capture_action_snapshot(target_index=index)
        await self.s.inter_action_pause()

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
                    f"An autocomplete dropdown probably appeared — take a "
                    f"browser_screenshot, then browser_click_at the right "
                    f"suggestion (or browser_keys ArrowDown+Enter). Only "
                    f"retype if you pass clear=true AND the field is empty."
                )

        # Cross-index repeat-type guard — catches the cascade where the
        # brain types the same value into different addresses (DOM index
        # 33, then 41, then vision_index=4) without verifying.
        type_dom_target_key = f"type[{index}]"
        repeat_block = self.s.check_repeat_type(text, target_key=type_dom_target_key)
        if repeat_block:
            self.s.record_step(
                "browser_type",
                f"index={index}, text={text[:30]!r}",
                "REPEAT_TYPE: refused (cross-index cascade)",
            )
            return repeat_block

        # --- DOM ↔ vision crosscheck for type ---------------------------
        # Mirror of the click crosscheck (above). Without this the brain
        # can type "40" into a DOM index that points at a button or wrong
        # input — the page silently ignores the keystrokes and the brain
        # has no signal that it missed. Same IoU thresholds: refuse
        # below 0.5, warn 0.5–0.7, allow ≥0.7. Skipped silently when
        # vision isn't fresh.
        try:
            vr_age = max(
                0,
                self.s._brain_turn_counter - 1
                - (self.s._vision_epoch_turn or 0),
            )
            if (
                vr_age <= 2
                and getattr(self.s, "_last_vision_response", None) is not None
                and len(getattr(self.s._last_vision_response, "bboxes", []) or []) > 0
            ):
                fetched = await _fetch_elements_with_bounds(session_id, self.s)
                best_iou, best_v, best_label = _dom_vision_crosscheck(
                    self.s, index
                )
                if fetched and self.s.elements_bounds.get(index):
                    if best_v is not None:
                        _vs = f"V{best_v}('{best_label[:30]}')"
                    else:
                        _vs = "(no vision overlap)"
                    print(
                        f"[type_crosscheck] [{index}] vs {_vs} "
                        f"IoU={best_iou:.2f} "
                        f"(threshold: refuse<0.5, warn<0.7, allow≥0.7)"
                    )
                if best_v is not None:
                    if best_iou >= 0.7:
                        pass  # strong agreement — allow silently
                    elif best_iou >= 0.5:
                        print(
                            f"[type_crosscheck] PARTIAL overlap — "
                            f"type allowed with warning."
                        )
                        # Stash a warning to append to the success caption
                        # below. We can't return early — type still
                        # proceeds, the warning is informational.
                        self.s.log_activity(
                            f"type([{index}])(weak_vision_overlap)",
                            f"V{best_v} IoU={best_iou:.2f}",
                        )
                    else:
                        # IoU < 0.5 — refuse. Brain almost certainly
                        # picked the wrong DOM index for the input it
                        # wanted; the trace pattern was type([33], "40")
                        # then type([41], "40") into adjacent non-input
                        # elements until the repeat-type ledger fired.
                        # Catching it here saves 2 iterations + prevents
                        # ledger pollution.
                        self.s.record_step(
                            "browser_type",
                            f"index={index}, text={text[:30]!r}",
                            f"TYPE_DOM_VISION_MISMATCH iou={best_iou:.2f}",
                        )
                        print(
                            f"[type_crosscheck] REFUSED — IoU "
                            f"{best_iou:.2f} < 0.5 threshold. Brain "
                            f"must use type_at(V{best_v}) instead."
                        )
                        return (
                            f"[TYPE_DOM_VISION_MISMATCH] DOM index "
                            f"[{index}] only weakly overlaps vision "
                            f"V{best_v} ('{best_label[:40]}', IoU="
                            f"{best_iou:.2f} — below the 0.5 safe "
                            f"threshold). The DOM index is pointing "
                            f"at an adjacent or overlapping element, "
                            f"NOT the input vision saw. Use:\n"
                            f"  browser_type_at(vision_index=V{best_v}, "
                            f"text={text!r})\n"
                            f"This dispatches the keystrokes against "
                            f"the bbox vision actually identified — "
                            f"pixel-exact, no DOM-index drift. Do NOT "
                            f"retry browser_type([{index}]) with the "
                            f"same value into different indices; the "
                            f"repeat-type ledger will refuse the third "
                            f"attempt and the brief will mark the "
                            f"focus as exhausted."
                        )
                else:
                    # No vision overlap at all — DOM index addresses
                    # something vision didn't see. Almost always means
                    # off-screen / hidden / culled. Refuse + ask for a
                    # screenshot.
                    if self.s.elements_bounds.get(index):
                        self.s.record_step(
                            "browser_type",
                            f"index={index}, text={text[:30]!r}",
                            "TYPE_NO_VISION_OVERLAP",
                        )
                        print(
                            f"[type_crosscheck] REFUSED — [{index}] "
                            f"has zero overlap with any vision bbox."
                        )
                        return (
                            f"[TYPE_NO_VISION_MATCH] DOM index "
                            f"[{index}] does not overlap ANY vision "
                            f"bbox. Either the input is off-screen, "
                            f"covered by an overlay, or vision "
                            f"deliberately culled it. Typing here "
                            f"sends keystrokes nowhere visible. "
                            f"Recovery:\n"
                            f"  1) browser_screenshot — refresh the "
                            f"V_n bbox list and retry via "
                            f"browser_type_at(vision_index=V_n, "
                            f"text={text!r}).\n"
                            f"  2) browser_scroll_until(target_text=…) "
                            f"if the input you wanted is below the "
                            f"fold."
                        )
        except Exception as exc:
            # Defensive — never fail the type because the crosscheck
            # itself errored. Just log and proceed.
            print(f"[type_crosscheck_error] {exc}")

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
        # And the cross-index ledger.
        self.s.record_typed_value(text, target_key=type_dom_target_key)

        # --- Post-type autocomplete dropdown scan -----------------------
        # Probe the page for newly-appeared autocomplete suggestions. If
        # we find any, surface them inline so the LLM picks one instead
        # of re-typing the full phrase.
        suggestions: list[dict] = []
        try:
            scan_js = """
            (() => {
              const seen = new Set();
              const out = [];
              const selectors = [
                '[role="listbox"] [role="option"]',
                '[role="combobox"] + * li',
                '.autocomplete-suggestions li, .autocomplete li',
                'ul.suggestions li, .suggestions li',
                '.MuiAutocomplete-listbox li',
                '[aria-live] li',
                '.dropdown-menu.show li, .dropdown-menu[style*="display: block"] li',
                '.ui-autocomplete li',
                '[class*="autocomplete"][class*="option"]',
                '[class*="suggestion"] li, [class*="suggestions"] li',
              ];
              for (const sel of selectors) {
                document.querySelectorAll(sel).forEach(el => {
                  const r = el.getBoundingClientRect();
                  if (r.width < 30 || r.height < 10) return;
                  if (r.top > window.innerHeight * 1.5) return;
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
              return out.slice(0, 8);
            })();
            """
            sr = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": scan_js},
                timeout=5.0,
            )
            if sr.status_code == 200:
                body = sr.json()
                got = body.get("result") if isinstance(body, dict) else None
                if isinstance(got, list):
                    suggestions = [s for s in got if isinstance(s, dict) and s.get("text")]
        except Exception as exc:
            print(f"  [dropdown scan failed: {exc}]")

        self.s.record_step(
            "browser_type",
            f'index={index}, text="{text[:30]}"',
            f"ok ({len(suggestions)} suggestions)" if suggestions else "ok",
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
        if suggestions:
            caption += (
                f"\n\nAutocomplete suggestions visible ({len(suggestions)}):"
            )
            for i, s in enumerate(suggestions, start=1):
                caption += f"\n  {i}. {s['text']!r} → browser_click_at(x={s['x']}, y={s['y']})"
            caption += (
                "\nDO NOT browser_type again into this field — pick a "
                "suggestion above via browser_click_at or use browser_keys "
                "(ArrowDown + Enter) to select the first one."
            )

        # Post-type semantic verification (index-addressed variant).
        # Skip when the tool no-op'd (field already matched).
        if pre_action != "skip_match":
            from superbrowser_bridge.type_verify import verify_and_correct_by_index
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

        # Surface before/after to the action-delta renderer so the brain
        # gets "field updated to '40' (was '')" instead of just the
        # generic structural diff. `pre_action == 'skip_match'` means
        # the field already matched so changed=False.
        self.s.action_snapshot_extras = {
            "before": pre_value or "",
            "after": text if pre_action != "skip_match" else (pre_value or text),
            "changed": pre_action != "skip_match",
        }

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
    description = "Send keyboard keys or shortcuts."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, session_id: str, keys: str, **kw: Any) -> Any:
        print(f"\n>> browser_keys({keys})")
        sync_block = await self.s.ensure_vision_synced(reason="browser_keys")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        # Phase 4 hard gate: cursor interaction on this epoch.
        self.s.epoch_interact_attempts += 1
        self.s.capture_action_snapshot(target_index=None)
        await self.s.inter_action_pause()
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/keys",
            json={"keys": keys},
            timeout=15.0,
        )
        # browser_keys also needs to record_step before build_text_only so
        # the action-delta renderer can identify the tool by name.
        self.s.record_step("browser_keys", f"keys={keys[:30]}", "ok")
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


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        startX=NumberSchema("Start X coordinate"),
        startY=NumberSchema("Start Y coordinate"),
        endX=NumberSchema("End X coordinate"),
        endY=NumberSchema("End Y coordinate"),
        steps=IntegerSchema("Number of intermediate steps (default 25, higher = smoother)", nullable=True),
        required=["session_id", "startX", "startY", "endX", "endY"],
    )
)
class BrowserDragTool(Tool):
    name = "browser_drag"
    description = "Drag from (startX, startY) to (endX, endY). Useful for slider CAPTCHAs and drag-to-verify puzzles."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, startX: float, startY: float, endX: float, endY: float, steps: int | None = None, **kw: Any) -> str:
        print(f"\n>> browser_drag(({startX},{startY}) -> ({endX},{endY}))")
        sync_block = await self.s.ensure_vision_synced(reason="browser_drag")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1
        # Phase 4 hard gate: drag is also a cursor interaction.
        self.s.epoch_interact_attempts += 1
        self.s.capture_action_snapshot(target_index=None)
        await self.s.inter_action_pause()
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        payload: dict[str, Any] = {
            "startX": startX, "startY": startY,
            "endX": endX, "endY": endY,
        }
        if steps is not None:
            payload["steps"] = steps

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/drag",
            json=payload,
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()

        self.s.record_step("browser_drag", f"({startX},{startY})->({endX},{endY})", data.get("url", ""))
        return self.s.build_text_only(
            data,
            f"Dragged from ({startX},{startY}) to ({endX},{endY})",
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        selector=StringSchema(
            "CSS selector of the element to click. Prefer stable hooks: "
            "`#id`, `[data-test-id=\"...\"]`, `[name=\"...\"]`, "
            "`[aria-label=\"...\"]`. Avoid brittle selectors like "
            "`div > div:nth-child(3) > span`."
        ),
        button=StringSchema("Mouse button: left|right|middle", nullable=True),
        click_count=IntegerSchema(
            "Number of clicks (1 single, 2 double).",
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
        required=["session_id", "selector"],
    )
)
class BrowserClickSelectorTool(Tool):
    """Pixel-exact click on a DOM element by CSS selector. Restored from
    v2 to give the brain a fast path that skips the vision pipeline
    entirely when a stable selector is available."""

    name = "browser_click_selector"
    description = (
        "Click the centre of a DOM element by CSS selector. Pixel-exact, "
        "zero Gemini cost. **PREFERRED click pathway** when the target "
        "has a stable hook — chess squares (.square-54), form fields "
        "(#email), buttons with [data-test-id=...], captcha handles. "
        "Fails fast if the selector is missing or zero-size, in which "
        "case fall back to `browser_click_at(vision_index=V_n)`. "
        "**Never** reach for `browser_run_script` to click — JS clicks "
        "fire `isTrusted=false` and are bot-detected."
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
        # Post-mutation observation gate — same contract as click_at.
        if self.s._mutation_needs_observation:
            return (
                "[click_selector_refused:observe_first] Your last action "
                "changed the page but you haven't observed what happened. "
                "Call browser_screenshot or browser_get_markdown BEFORE "
                "clicking again."
            )
        # Hard sync gate — wait for any in-flight vision prefetch from
        # the previous action to land. Consistent with click_at; the
        # contract is "any mutation needs a fresh observation epoch".
        sync_block = await self.s.ensure_vision_synced(
            reason="browser_click_selector"
        )
        if sync_block:
            return sync_block

        self.s._brain_turn_counter += 1
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls += 1
        self.s._scripts_since_observation = 0
        # Phase 4 hard gate: count this as a cursor interaction.
        self.s.epoch_interact_attempts += 1
        self.s.capture_action_snapshot(target_index=None)
        await self.s.inter_action_pause()

        payload: dict[str, Any] = {"selector": selector, "ensureVisible": True}
        if button is not None:
            payload["button"] = button
        if click_count is not None:
            payload["clickCount"] = click_count
        if linear is not None:
            payload["linear"] = linear

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
            # Record this as a tried-and-failed cursor strategy so the
            # run_script lockout counts it (the brain has earned the
            # right to escalate to a script after a cursor attempt).
            self.s.record_cursor_failure(
                strategy="click_selector",
                target=selector,
                reason=str(err)[:120],
            )
            self.s.log_activity(
                f"click_selector({selector[:60]})(FAIL)",
                str(err)[:120],
            )
            return (
                f"[click_selector_failed] {err}\n"
                "Fallback: call browser_screenshot to refresh the bbox "
                "list, then `browser_click_at(vision_index=V_n)`."
            )
        data = r.json()
        clicked = data.get("clicked", {}) or {}
        actual_url = data.get("url", self.s.current_url) or ""
        if actual_url:
            self.s.record_url(actual_url)

        self.s.record_step(
            "browser_click_selector",
            f"{selector} @ ({clicked.get('x','?')},{clicked.get('y','?')})",
            f"url={actual_url[:60] if actual_url else '?'}",
        )
        # Mark the brain must observe before the next mutation — same
        # contract as click_at.
        self.s._mutation_needs_observation = True

        _vision_task = _schedule_vision_prefetch(self.s, session_id)
        caption = (
            f"Clicked {selector!r} at "
            f"({clicked.get('x','?')},{clicked.get('y','?')})"
        )
        return await _append_fresh_vision(
            _vision_task,
            self.s.build_text_only(data, caption),
            state=self.s,
        )


