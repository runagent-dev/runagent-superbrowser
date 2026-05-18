"""Slider widget tools.

`BrowserSetSliderTool` (CSS selector path), `BrowserSetSliderAtTool`
(open-loop vision-driven drag), `BrowserListSliderHandlesTool` (DOM
enumeration of every slider thumb), `BrowserDragSliderUntilTool`
(closed-loop drag-while-reading-label).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    IntegerSchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)

from .._label import clean_label
from ..effects import _maybe_no_effect_prefix
from ..feedback import _feedback_gate
from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState
from ..vision_pipeline import _schedule_vision_prefetch


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        selector=StringSchema(
            "CSS selector for the slider element (e.g. 'input[type=range][name=monthlyContribution]'). "
            "Frame-aware: the backend probes every frame on the page."
        ),
        value_json=StringSchema(
            "Target value as JSON. Examples: '300' for a single slider, '[25, 75]' for a "
            "dual-thumb range. Values are absolute (use the slider's own units) unless "
            "as='ratio' is set, in which case they are 0.0–1.0 positions along the track."
        ),
        value_mode=StringSchema(
            "Value interpretation: 'absolute' (default; matches the slider's own min/max) "
            "or 'ratio' (0.0-1.0 position along the track).",
            nullable=True,
        ),
        method=StringSchema(
            "Strategy: 'auto' (default), 'range-input' (direct value+input event), "
            "'keyboard' (focus + arrow keys), 'drag' (pixel drag). Use 'auto' unless debugging.",
            nullable=True,
        ),
        required=["session_id", "selector", "value_json"],
    )
)
class BrowserSetSliderTool(Tool):
    name = "browser_set_slider"
    description = (
        "Set a slider's value by number. Works for native <input type=range>, "
        "ARIA sliders (role=slider / aria-valuenow), and CSS-custom widgets. "
        "Prefer this over browser_drag for sliders — it auto-picks the most "
        "reliable strategy, crosses iframe boundaries, and pierces open "
        "shadow DOM (Chase mds-slider, Lit/React custom-element wrappers). "
        "For dual-thumb sliders on a single track (e.g. an age range) pass "
        "value_json='[lo, hi]'. Returns the strategy used plus before/after "
        "values so you can verify the slide. CALL ONE AT A TIME — when "
        "setting multiple sliders (e.g. min + max filter inputs), wait for "
        "each call to return before issuing the next; parallel calls are "
        "serialized by an internal lock and re-screenshot is needed between "
        "them so vision indices stay fresh."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        selector: str,
        value_json: str,
        value_mode: str | None = None,
        method: str | None = None,
        **kw: Any,
    ) -> str:
        try:
            parsed = json.loads(value_json)
        except (TypeError, ValueError) as exc:
            return f"[set_slider_failed] value_json is not valid JSON: {exc}"
        if isinstance(parsed, (int, float)):
            value_payload: Any = float(parsed)
        elif (
            isinstance(parsed, list)
            and len(parsed) == 2
            and all(isinstance(n, (int, float)) for n in parsed)
        ):
            value_payload = [float(parsed[0]), float(parsed[1])]
        else:
            return "[set_slider_failed] value_json must decode to a number or [lo, hi] list"

        print(f"\n>> browser_set_slider({selector!r} → {value_payload})")
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        payload: dict[str, Any] = {"selector": selector, "value": value_payload}
        if value_mode is not None:
            payload["as"] = value_mode
        if method is not None:
            payload["method"] = method

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/set-slider",
            json=payload,
            timeout=30.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            self.s.record_cursor_failure(
                strategy="slider_set",
                target=selector,
                reason=f"http_{r.status_code}: {str(err)[:80]}",
            )
            return f"[set_slider_failed] {err}"
        data = r.json()
        outcome = data.get("outcome", {}) or {}
        strategy = outcome.get("strategy", "?")
        before = outcome.get("before")
        after = outcome.get("after")
        err = outcome.get("error")
        self.s.record_step(
            "browser_set_slider",
            f"{selector} → {value_payload} via {strategy}",
            data.get("url", ""),
        )
        if strategy == "unresolved" or err:
            self.s.record_cursor_failure(
                strategy="slider_set",
                target=selector,
                reason=str(err or "unresolved")[:80],
            )
            return f"[set_slider_failed] {err or 'unresolved'} (selector={selector})"
        caption = (
            f"Set slider {selector} via {strategy}: {before} → {after} "
            f"(min={outcome.get('min')}, max={outcome.get('max')})"
        )
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index of the slider HANDLE (the "
                "draggable thumb), as shown in the latest screenshot's "
                "`V_n` listing. The tool automatically finds the adjacent "
                "slider_widget bbox (the track) for target-x computation."
            ),
        ),
        value=NumberSchema(
            description=(
                "Target value. Interpreted per value_mode. For 'absolute' "
                "pass the actual reading (e.g. 300 for $300/month); the "
                "tool reads min/max from the adjacent rendered label text. "
                "For 'ratio' pass 0.0–1.0 position along the track."
            ),
        ),
        value_mode=StringSchema(
            "'absolute' (default) or 'ratio'. Use 'ratio' when you can't "
            "read the min/max from the page.",
            nullable=True,
        ),
        value_min=NumberSchema(
            "Override for the slider's minimum if vision didn't surface it. "
            "Only used when value_mode='absolute'.",
            nullable=True,
        ),
        value_max=NumberSchema(
            "Override for the slider's maximum if vision didn't surface it. "
            "Only used when value_mode='absolute'.",
            nullable=True,
        ),
        required=["session_id", "vision_index", "value"],
    )
)
class BrowserSetSliderAtTool(Tool):
    name = "browser_set_slider_at"
    description = (
        "Drag a slider to a target value using its VISION bbox index. "
        "Prefer this over browser_set_slider when the page uses custom "
        "slider widgets (Chase/JPM calculators, filter ranges, any "
        "React/Angular slider with no native range input or aria-valuenow). "
        "Workflow: (1) call browser_screenshot → the vision agent emits "
        "role=slider_handle / slider_widget / text_block bboxes per "
        "slider; (2) pick the V_n of the HANDLE you want to move; (3) "
        "call this tool with the target numeric value. The tool finds "
        "the adjacent track, dispatches a humanised bezier drag, and "
        "returns the post-drag rendered label text so you can verify. "
        "CALL ONE AT A TIME — multiple sliders MUST be sequenced; the "
        "internal lock serializes them, and you must re-screenshot "
        "between calls so V_n indices are fresh."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        vision_index: int,
        value: float,
        value_mode: str | None = None,
        value_min: float | None = None,
        value_max: float | None = None,
        **kw: Any,
    ) -> str:
        # Share the same lock as browser_drag_slider_until: the CDP/patchright
        # cursor is session-scoped, parallel drags clobber each other.
        if self.s.slider_drag_lock is None:
            self.s.slider_drag_lock = asyncio.Lock()
        async with self.s.slider_drag_lock:
            return await self._execute_inner(
                session_id, vision_index, value,
                value_mode, value_min, value_max,
            )

    async def _execute_inner(
        self,
        session_id: str,
        vision_index: int,
        value: float,
        value_mode: str | None,
        value_min: float | None,
        value_max: float | None,
    ) -> str:
        print(
            f"\n>> browser_set_slider_at(V{vision_index} → {value} "
            f"mode={value_mode or 'absolute'})"
        )
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        resp = self.s.vision_for_target_resolution()
        if resp is None:
            self.s.record_cursor_failure(
                strategy="slider_set_at",
                target=f"V{vision_index}",
                reason="no_vision",
            )
            return (
                "[set_slider_at_failed:no_vision] No recent vision response. "
                "Call browser_screenshot first so slider bboxes are indexed."
            )

        handle_bbox = resp.get_bbox(int(vision_index))
        if handle_bbox is None:
            self.s.record_cursor_failure(
                strategy="slider_set_at",
                target=f"V{vision_index}",
                reason=f"bad_vision_index ({len(resp.bboxes)} bboxes)",
            )
            return (
                f"[set_slider_at_failed:bad_vision_index] V{vision_index} "
                f"is out of range (only {len(resp.bboxes)} bboxes cached)."
            )
        handle_role = (getattr(handle_bbox, "role", "") or "").lower()
        if handle_role not in ("slider_handle", "slider", "input"):
            # Accept other roles loudly but keep going — vision may
            # mis-tag a handle as 'input' or 'other'.
            print(
                f"   (note: V{vision_index} role={handle_role!r}, "
                "expected slider_handle — continuing)"
            )

        iw, ih = resp.image_width, resp.image_height
        if iw <= 0 or ih <= 0:
            self.s.record_cursor_failure(
                strategy="slider_set_at",
                target=f"V{vision_index}",
                reason="no_image_dims",
            )
            return "[set_slider_at_failed:no_image_dims] Re-screenshot first."
        dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
        hx0, hy0, hx1, hy1 = handle_bbox.to_pixels(iw, ih, dpr=dpr_val)
        handle_pix = {"x": hx0, "y": hy0, "w": hx1 - hx0, "h": hy1 - hy0}
        handle_cy = hy0 + (hy1 - hy0) / 2.0

        # Find the associated track: nearest role=slider_widget whose
        # vertical centre sits within ±handle.h of the handle centre and
        # whose horizontal span encloses the handle.
        track_pix: dict[str, int] | None = None
        track_bbox = None
        best_dy: float | None = None
        for cand in resp.bboxes:
            if (getattr(cand, "role", "") or "").lower() != "slider_widget":
                continue
            cx0, cy0, cx1, cy1 = cand.to_pixels(iw, ih, dpr=dpr_val)
            ccy = cy0 + (cy1 - cy0) / 2.0
            dy = abs(ccy - handle_cy)
            if dy > max(hy1 - hy0, 24):
                continue
            if cx0 > hx0 or cx1 < hx1:
                # Track must enclose the handle horizontally.
                # (Handles at either extreme still sit within the track
                # bounds because the track includes min→max span.)
                continue
            if best_dy is None or dy < best_dy:
                best_dy = dy
                track_bbox = cand
                track_pix = {
                    "x": cx0, "y": cy0, "w": cx1 - cx0, "h": cy1 - cy0,
                }

        # Find the adjacent value-label text_block for min/max parsing +
        # before-readback. Heuristic: role=text_block whose centre Y is
        # within ±(handle.h + 40) of the handle's centre.
        label_text: str = ""
        for cand in resp.bboxes:
            if (getattr(cand, "role", "") or "").lower() != "text_block":
                continue
            cx0, cy0, cx1, cy1 = cand.to_pixels(iw, ih, dpr=dpr_val)
            ccy = cy0 + (cy1 - cy0) / 2.0
            if abs(ccy - handle_cy) <= (hy1 - hy0) + 40:
                label_text = (getattr(cand, "label", "") or "").strip()
                break

        mode = (value_mode or "absolute").lower()
        if mode == "ratio":
            ratio = max(0.0, min(1.0, float(value)))
        else:
            mn, mx = value_min, value_max
            if (mn is None or mx is None) and label_text:
                # Parse "0 to 10" / "$0 — $583" / "25 to 75" / "0 - 100"
                import re as _re
                nums = _re.findall(
                    r"-?\d+(?:\.\d+)?",
                    label_text.replace("$", "").replace("%", ""),
                )
                if len(nums) >= 2:
                    try:
                        parsed = [float(n) for n in nums[:2]]
                        mn = parsed[0] if mn is None else mn
                        mx = parsed[1] if mx is None else mx
                    except ValueError:
                        pass
            if mn is None or mx is None:
                self.s.record_cursor_failure(
                    strategy="slider_set_at",
                    target=f"V{vision_index}",
                    reason="no_minmax",
                )
                return (
                    "[set_slider_at_failed:no_minmax] Cannot infer min/max "
                    "from adjacent label; pass value_min/value_max or use "
                    "value_mode='ratio'. label_seen="
                    + (repr(label_text) if label_text else "none")
                )
            span = float(mx) - float(mn)
            if abs(span) < 1e-9:
                ratio = 0.5
            else:
                ratio = (float(value) - float(mn)) / span
                ratio = max(0.0, min(1.0, ratio))

        if track_pix is None:
            # Fall back: use the handle's bbox as a tiny pseudo-track.
            # The drag still fires from the handle centre, but end = start,
            # so this is effectively a no-op. Return diagnostic.
            self.s.record_cursor_failure(
                strategy="slider_set_at",
                target=f"V{vision_index}",
                reason="no_track",
            )
            return (
                f"[set_slider_at_failed:no_track] Could not find a "
                f"role='slider_widget' bbox adjacent to V{vision_index}. "
                "Re-screenshot; if the issue persists, use browser_set_slider "
                "with a DOM selector instead."
            )

        payload = {"handle": handle_pix, "track": track_pix, "ratio": ratio}
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/set-slider-at",
            json=payload,
            timeout=30.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            self.s.record_cursor_failure(
                strategy="slider_set_at",
                target=f"V{vision_index}",
                reason=f"http_{r.status_code}: {str(err)[:80]}",
            )
            return f"[set_slider_at_failed] {err}"
        data = r.json()
        outcome = data.get("outcome", {}) or {}
        self.s.record_step(
            "browser_set_slider_at",
            f'V{vision_index}|"{clean_label(label_text)}" → {value} (ratio={ratio:.3f})',
            data.get("url", ""),
        )
        lines = [
            f"Dragged slider V{vision_index} to {value} "
            f"(ratio={ratio:.2f}) via vision-drag",
            f"  handle={outcome.get('handle_bbox')}",
            f"  track={outcome.get('track_bbox')}",
            f"  target_px={outcome.get('target_px')}",
        ]
        if label_text:
            lines.append(f"  label_before={label_text!r}")
        lines.append(
            "  NEXT: call browser_screenshot to read "
            "the rendered post-drag value label."
        )
        if data.get("elements"):
            lines.append(str(data["elements"]))
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        required=["session_id"],
    )
)
class BrowserListSliderHandlesTool(Tool):
    name = "browser_list_slider_handles"
    description = (
        "Enumerate all slider handles on the page via DOM introspection "
        "(NO vision). Walks every frame (including cross-origin) and "
        "every open shadow root, returning each handle's index, "
        "frame_url, kind, bbox (in document CSS pixels), and the closest "
        "row-level label text. Use this when vision is flaky or "
        "returning empty bboxes, or when you already know the slider's "
        "logical label (e.g. 'Monthly contribution') and want to pick "
        "by text. Then pass the bbox directly into "
        "browser_drag_slider_until via the `handle_bbox` arg — skips the "
        "vision lookup entirely. For filter widgets with separate min "
        "and max sliders, call this once, then iterate set_slider / "
        "drag_slider_until per handle SEQUENTIALLY."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        print("\n>> browser_list_slider_handles()")
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/list-slider-handles",
            json={},
            timeout=20.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            self.s.record_cursor_failure(
                strategy="slider_list",
                target="list_slider_handles",
                reason=f"http_{r.status_code}: {str(err)[:80]}",
            )
            return f"[list_slider_handles_failed] {err}"
        data = r.json() or {}
        handles = data.get("handles") or []
        if not handles:
            self.s.record_cursor_failure(
                strategy="slider_list",
                target="list_slider_handles",
                reason="empty",
            )
            return (
                "[list_slider_handles:empty] No slider handles found in "
                "any frame. Page may still be loading — scroll or wait, "
                "then retry. If the page clearly has sliders, they may "
                "use non-standard markup; fall back to vision via "
                "browser_screenshot."
            )
        lines = [f"Found {len(handles)} slider handle(s):"]
        for h in handles:
            lines.append(
                f"  [{h.get('index')}] kind={h.get('kind')} "
                f"bbox={h.get('bbox')} "
                f"label={h.get('label', '')!r} "
                f"frame={(h.get('frame_url') or '')[:80]}"
            )
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        vision_index=IntegerSchema(
            description=(
                "1-based vision bbox index of the slider HANDLE "
                "(the draggable thumb) from the most recent screenshot. "
                "Either this OR handle_bbox_json is required."
            ),
            nullable=True,
        ),
        handle_bbox_json=StringSchema(
            description=(
                "Alternative to vision_index: pass the handle bbox "
                "directly as a JSON object {\"x\":.., \"y\":.., \"w\":.., \"h\":..} "
                "in CSS pixel document coords. Use this when vision is "
                "unreliable — call browser_list_slider_handles to get "
                "bboxes straight from the DOM, then pass one here."
            ),
            nullable=True,
        ),
        label_hint=StringSchema(
            description=(
                "Alternative discovery: a substring of the slider's label "
                "(e.g. 'Monthly contribution'). The tool will call "
                "browser_list_slider_handles, pick the handle whose label "
                "contains this hint (case-insensitive, whitespace collapsed), "
                "and use its bbox. Use when neither vision_index nor "
                "handle_bbox_json is convenient."
            ),
            nullable=True,
        ),
        target_value=NumberSchema(
            description=(
                "The numeric value to slide to. The tool drags the handle "
                "while watching the rendered label text, stopping when the "
                "label shows this value (±tolerance)."
            ),
        ),
        label_pattern=StringSchema(
            description=(
                "JS regex matching the rendered label — the FIRST capture "
                "group must be the numeric value. Example for Chase: "
                "'Monthly contribution[^:]*:\\s*\\$?(\\d+(?:\\.\\d+)?)'. "
                "If omitted, matches any number in a text node on the same "
                "visual row as the handle (works for most sliders with a "
                "single value near the track)."
            ),
            nullable=True,
        ),
        tolerance=NumberSchema(
            "Allowed |target - observed| gap. Default 0 (exact match).",
            nullable=True,
        ),
        max_iterations=IntegerSchema(
            "Safety cap on step iterations. Default 25.",
            nullable=True,
        ),
        step_px=IntegerSchema(
            "Initial pixel step. The tool auto-adapts from observed "
            "value-per-pixel sensitivity. Default 8.",
            nullable=True,
        ),
        direction=StringSchema(
            "'auto' (default; inferred from current vs target), 'left', 'right'.",
            nullable=True,
        ),
        required=["session_id", "target_value"],
    )
)
class BrowserDragSliderUntilTool(Tool):
    name = "browser_drag_slider_until"
    description = (
        "Closed-loop slider drag. Holds the mouse down on the handle, "
        "steps incrementally, reads the rendered value label (across "
        "all frames AND open shadow roots) after each step, and stops "
        "when the label shows the target value. THE right tool for "
        "custom widgets where vision can't reliably identify the full "
        "track geometry (Chase/JPM calculators, React/Angular sliders "
        "with no aria-valuenow). Unlike browser_set_slider_at "
        "(open-loop), this never overshoots and recovers automatically "
        "from non-linear widget scaling. Workflow: (1) "
        "browser_screenshot → vision returns slider_handle V_n values "
        "(or use browser_list_slider_handles to skip vision); (2) call "
        "this tool with vision_index=V_n / handle_bbox / label_hint and "
        "your numeric target; (3) inspect the returned trace + "
        "final_value to verify. CALL ONE AT A TIME — the internal lock "
        "serializes parallel calls; re-screenshot between sliders."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        target_value: float,
        vision_index: int | None = None,
        handle_bbox_json: str | None = None,
        label_hint: str | None = None,
        label_pattern: str | None = None,
        tolerance: float | None = None,
        max_iterations: int | None = None,
        step_px: int | None = None,
        direction: str | None = None,
        **kw: Any,
    ) -> str:
        gate = await _feedback_gate("browser_drag_slider_until")
        if gate:
            return gate
        sync_block = await self.s.ensure_vision_synced(
            reason=f"browser_drag_slider_until(target={target_value})",
        )
        if sync_block:
            return sync_block
        # Serialize drags on this session. If the LLM fired this tool in
        # parallel for multiple sliders, we queue them up so the cursor
        # owns one slider at a time. Without this lock, concurrent drags
        # fight for the same CDP mouse and produce garbage.
        if self.s.slider_drag_lock is None:
            self.s.slider_drag_lock = asyncio.Lock()
        async with self.s.slider_drag_lock:
            return await self._execute_inner(
                session_id, target_value, vision_index, handle_bbox_json,
                label_hint, label_pattern, tolerance, max_iterations,
                step_px, direction,
            )

    async def _resolve_handle_bbox(
        self, session_id: str,
        vision_index: int | None,
        handle_bbox_json: str | None,
        label_hint: str | None,
    ) -> tuple[dict[str, float] | None, str]:
        """Returns (bbox | None, source_description). Tries, in order:
        direct handle_bbox_json → label_hint via DOM enum → vision_index.
        Returns None + reason string on failure."""
        # 1. Direct bbox — highest priority, no indirection.
        if handle_bbox_json:
            try:
                bb = json.loads(handle_bbox_json)
            except (TypeError, ValueError) as exc:
                return None, f"bad handle_bbox_json: {exc}"
            if not isinstance(bb, dict):
                return None, "handle_bbox_json must decode to a dict"
            for k in ("x", "y", "w", "h"):
                if not isinstance(bb.get(k), (int, float)):
                    return None, f"handle_bbox_json missing numeric {k!r}"
            return bb, "handle_bbox_json"

        # 2. Label hint — DOM enum, pick best fuzzy match.
        if label_hint:
            try:
                r = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/list-slider-handles",
                    json={},
                    timeout=20.0,
                )
                handles = (r.json() or {}).get("handles") or [] if r.status_code < 400 else []
            except Exception as exc:
                return None, f"list-slider-handles failed: {exc}"
            if not handles:
                return None, "list-slider-handles returned no sliders"
            norm = label_hint.lower().strip()
            # Score each handle: label contains hint → big win; else token overlap.
            best = None
            best_score = -1.0
            for h in handles:
                lab = (h.get("label") or "").lower().strip()
                if not lab:
                    continue
                if norm in lab:
                    score = 1.0 + min(1.0, len(norm) / max(1, len(lab)))
                else:
                    ht = set(norm.split())
                    lt = set(lab.split())
                    if not ht:
                        continue
                    score = len(ht & lt) / len(ht)
                if score > best_score:
                    best_score = score
                    best = h
            if not best or best_score < 0.5:
                sample = [f"[{h.get('index')}] {h.get('label')!r}" for h in handles[:8]]
                return None, (
                    f"no handle label matched {label_hint!r}. "
                    f"candidates: {sample}"
                )
            return best.get("bbox"), f"label_hint={label_hint!r}"

        # 3. Vision index — legacy path.
        if vision_index is not None:
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return None, (
                    "no cached vision response (call browser_screenshot "
                    "first, or pass handle_bbox_json / label_hint)"
                )
            handle_bbox = resp.get_bbox(int(vision_index))
            if handle_bbox is None:
                return None, (
                    f"V{vision_index} out of range "
                    f"({len(resp.bboxes)} bboxes cached)"
                )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return None, "vision has no image dims; re-screenshot first"
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            hx0, hy0, hx1, hy1 = handle_bbox.to_pixels(iw, ih, dpr=dpr_val)
            return (
                {"x": hx0, "y": hy0, "w": hx1 - hx0, "h": hy1 - hy0},
                f"vision_index=V{vision_index}",
            )

        return None, "provide vision_index, handle_bbox_json, or label_hint"

    async def _execute_inner(
        self,
        session_id: str,
        target_value: float,
        vision_index: int | None,
        handle_bbox_json: str | None,
        label_hint: str | None,
        label_pattern: str | None,
        tolerance: float | None,
        max_iterations: int | None,
        step_px: int | None,
        direction: str | None,
    ) -> str:
        src = (
            f"V{vision_index}" if vision_index is not None
            else (f"hint={label_hint!r}" if label_hint
                  else ("bbox=json" if handle_bbox_json else "?"))
        )
        print(
            f"\n>> browser_drag_slider_until({src} → {target_value}"
            f"{' pattern=' + repr(label_pattern) if label_pattern else ''})"
        )
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        handle_pix, source_desc = await self._resolve_handle_bbox(
            session_id, vision_index, handle_bbox_json, label_hint,
        )
        if handle_pix is None:
            self.s.record_cursor_failure(
                strategy="slider_drag_until",
                target=str(source_desc)[:120],
                reason="no_handle",
            )
            msg = f"[drag_slider_until_failed:no_handle] {source_desc}"
            print(f"   {msg}")
            return msg
        print(f"   resolved handle via {source_desc}: {handle_pix}")

        payload: dict[str, Any] = {
            "handle": handle_pix,
            "target_value": float(target_value),
        }
        if label_pattern is not None:
            payload["label_pattern"] = label_pattern
        if tolerance is not None:
            payload["tolerance"] = float(tolerance)
        if max_iterations is not None:
            payload["max_iterations"] = int(max_iterations)
        if step_px is not None:
            payload["step_px"] = int(step_px)
        if direction is not None:
            payload["direction"] = direction

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/drag-slider-until",
            json=payload,
            timeout=60.0,  # longer — closed-loop can take a few seconds
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            self.s.record_cursor_failure(
                strategy="slider_drag_until",
                target=str(source_desc)[:120],
                reason=f"http_{r.status_code}: {str(err)[:80]}",
            )
            return f"[drag_slider_until_failed] {err}"
        data = r.json()
        out = data.get("outcome", {}) or {}
        self.s.record_step(
            "browser_drag_slider_until",
            f"{source_desc} → {target_value} in {out.get('iterations')} iters",
            data.get("url", ""),
        )
        # The drag moved the handle — treat as a page mutation so the
        # next click waits for fresh vision of the post-drag DOM.
        self.s.advance_observation_token("drag_slider_until")
        _schedule_vision_prefetch(self.s, session_id)
        final_v = out.get("final_value")
        init_v = out.get("initial_value")
        completed = bool(out.get("completed"))
        lines: list[str] = []
        if not completed:
            # Prefix with the FAILED tag so the agent treats this as a
            # loud failure (same convention as all other _failed returns).
            if init_v is None:
                reason = "initial_readback_failed"
            elif final_v is None:
                reason = "value_lost_mid_drag"
            else:
                reason = "target_not_reached"
            self.s.record_cursor_failure(
                strategy="slider_drag_until",
                target=str(source_desc)[:120],
                reason=f"{reason} (target={target_value}, final={final_v})",
            )
            lines.append(
                f"[drag_slider_until_failed:{reason}] "
                f"{source_desc} target={target_value} "
                f"final={final_v} initial={init_v}"
            )
        else:
            lines.append(
                f"Closed-loop slider {source_desc} → target={target_value} "
                f"COMPLETED in {out.get('iterations')} iterations: "
                f"{init_v} → {final_v}"
            )
        lines.append(f"  label_text={out.get('label_text')!r}")
        trace = out.get("trace") or []
        if trace:
            lines.append("  trace (last 4):")
            for row in trace[-4:]:
                lines.append(
                    f"    iter={row.get('iter')} "
                    f"cursor_x={row.get('cursor_x')} "
                    f"value={row.get('value')}"
                )
        if not completed:
            lines.append(
                "  Fix: if label_text shows NO_MATCH, your label_pattern "
                "regex didn't match any nearby text — the 'nearby text' "
                "list in label_text shows what IS there. Adjust the regex. "
                "If label_text looks right but target wasn't reached, widen "
                "tolerance or raise max_iterations. Always call sliders "
                "SEQUENTIALLY — never in a parallel batch."
            )
        if data.get("elements"):
            lines.append(str(data["elements"]))
        return _maybe_no_effect_prefix(
            data, "browser_drag_slider_until", "\n".join(lines),
            session_state=self.s,
        )
