"""Time-picker tool — symmetric counterpart to `browser_pick_date`.

The brain otherwise thrashes on `browser_run_script(mutates=true)` to
set `<select>.value` directly when a site's time picker is hidden
behind a custom widget (SpotHero "Starts" / "Ends" buttons reveal
`<select>` children whose accessible label is "Starts", not "Start
time", so `browser_select_option` mismatches). Single deterministic
call covers native <input type=time>, native <select>, ARIA listbox/
menu options, and library [data-time]-style attributes.
"""

from __future__ import annotations

import os
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)

from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        time=StringSchema(
            'Target time. Accepts "HH:MM" 24-hour ("13:00", "08:30") '
            'or "h:mm AM/PM" ("1:00 PM", "8:30 AM"). The tool matches '
            'either form against visible options on the page.'
        ),
        selector=StringSchema(
            "Optional CSS selector hint scoping the time picker root "
            "or the native time input. Useful when the page has "
            "multiple time pickers (start-time + end-time). Omit to "
            "let the tool find the picker itself.",
            nullable=True,
        ),
        vision_index=IntegerSchema(
            description=(
                "Optional 1-based vision bbox index (V_n). When set, "
                "the tool restricts its search to the bbox area. "
                "Honors the same freshness/epoch-age gates as "
                "browser_click_at."
            ),
            nullable=True,
        ),
        required=["session_id", "time"],
    )
)
class BrowserPickTimeTool(Tool):
    """Pick a time-of-day in any picker shape (native input/select, ARIA, data-attr)."""

    name = "browser_pick_time"
    description = (
        "Pick a time-of-day in a time picker. Handles native "
        "<input type=time>, native <select> with time options, ARIA "
        "listbox/menu time options, and library widgets ([data-time], "
        "[data-hour]/[data-minute]). Use this on SpotHero-style "
        "Starts/Ends widgets that reveal <select> children — never "
        "browser_run_script to set <select>.value directly (isTrusted="
        "false, bot-detected). For dates, pair with browser_pick_date."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        time: str,
        selector: str | None = None,
        vision_index: int | None = None,
        **kw: Any,
    ) -> Any:
        if not isinstance(time, str) or not time.strip():
            return (
                f"[pick_time_failed:bad_args] time must be a non-empty "
                f"string (\"HH:MM\" 24h or \"h:mm AM/PM\"), got {time!r}."
            )

        sync_block = await self.s.ensure_vision_synced(reason="browser_pick_time")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1

        payload: dict[str, Any] = {"time": time.strip()}
        if selector:
            payload["selector"] = selector
        label = f"time={time.strip()}"

        if vision_index is not None:
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return (
                    "[pick_time_failed:no_vision] No recent vision "
                    "response to resolve vision_index against. "
                    "browser_screenshot, then retry — or omit "
                    "vision_index and pass `selector`."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                return (
                    f"[pick_time_failed:bad_vision_index] V{vision_index} "
                    f"is out of range (only {len(resp.bboxes)} bboxes in "
                    f"the last vision response)."
                )
            try:
                max_age_turns = int(os.environ.get("VISION_MAX_AGE_TURNS") or "1")
            except ValueError:
                max_age_turns = 1
            if max_age_turns > 0:
                age_turns = max(
                    0,
                    self.s._brain_turn_counter - 1 - self.s._vision_epoch_turn,
                )
                if age_turns > max_age_turns:
                    return (
                        f"[pick_time_failed:epoch_too_old "
                        f"age_turns={age_turns} max={max_age_turns}] "
                        f"V{vision_index} resolves against a vision "
                        f"snapshot taken {age_turns} actions ago. "
                        f"browser_screenshot to refresh, then retry."
                    )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return (
                    "[pick_time_failed:no_image_dims] Vision response "
                    "has no image dimensions; cannot denormalize bbox. "
                    "Re-screenshot."
                )
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
            payload["bbox"] = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
            label = f"V{vision_index}, time={time.strip()}"

        print(f"\n>> browser_pick_time({label})")

        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/pick-time",
                json=payload,
                timeout=30.0,
            )
        except Exception as exc:
            self.s.record_step("browser_pick_time", label, f"http_error: {exc}")
            return f"[pick_time_failed:http_error] {exc}"

        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            self.s.record_step("browser_pick_time", label, f"http_{r.status_code}")
            return f"[pick_time_failed:http_{r.status_code}] {err}"

        data = r.json() or {}
        fp_map = data.get("fingerprints") if isinstance(data, dict) else None
        if isinstance(fp_map, dict):
            self.s.element_fingerprints = {
                int(k): v for k, v in fp_map.items() if isinstance(v, str)
            }

        ok = bool(data.get("ok"))
        strategy = str(data.get("strategy") or "?")
        verified = bool(data.get("verified"))
        reason = data.get("reason")
        picked = str(data.get("picked_time") or time.strip())

        if ok:
            verify_note = "" if verified else " (selection not yet visible — re-screenshot to confirm)"
            note = f"Picked {picked} via {strategy}{verify_note}"
            print(f"   [pick_time] ok strategy={strategy} verified={verified}")
            self.s.record_step("browser_pick_time", label, f"ok via {strategy}")
            self.s.log_activity(f"pick_time({time.strip()})", strategy)
            return self.s.build_text_only(data, note)

        # Failure path — surface specific reasons with concrete next-steps.
        print(f"   [pick_time] FAIL strategy={strategy} reason={reason}")
        self.s.record_step(
            "browser_pick_time", label, f"failed ({strategy}): {reason or '?'}",
        )
        if reason == "bad_time_format":
            return (
                f"[pick_time_failed:bad_time_format] time must be "
                f"\"HH:MM\" 24h or \"h:mm AM/PM\", got {time!r}. "
                f"Examples: '13:00', '1:00 PM', '08:30'."
            )
        if reason in {
            "no_native_input",
            "no_select_match",
        }:
            return (
                f"[pick_time_failed:no_picker_found] No native time "
                f"picker matched {time!r}. The picker may be hidden "
                f"behind a trigger button (e.g. \"Starts\", \"Time\", a "
                f"clock icon). Click the trigger first "
                f"(browser_click_at), then retry browser_pick_time. If "
                f"the picker uses a custom shape, fall back to "
                f"browser_select_option(label='<trigger label>', "
                f"value='<time text>')."
            )
        if reason == "use_vision_bbox" or strategy == "use_vision_bbox":
            return (
                f"[pick_time_use_vision_bbox] No native input/select/"
                f"data-attr matched {time!r}. The picker is a custom "
                f"ARIA dropdown. browser_screenshot — vision will label "
                f"each visible time option as V_n; pick the matching "
                f"V_n via browser_click_at(vision_index=V_n). Do NOT "
                f"call browser_run_script / browser_eval to enumerate "
                f"options."
            )
        if reason and reason.startswith("set_failed:"):
            return (
                f"[pick_time_failed:set_failed] Native input rejected "
                f"the value: {reason[len('set_failed:'):]}. The input "
                f"may be disabled or have an unusual constraint."
            )
        return f"[pick_time_failed:{reason or strategy}] reason={reason}"
