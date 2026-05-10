"""Date-picker tool — sidesteps the type-into-calendar-cell hallucination.

Calendar cells are <button>/<td>/<div role="gridcell">, not <input>, so
`browser_type_at` rejects them with `not_input` and the brain escalates
to `browser_run_script`. `browser_pick_date` consolidates the three
calendar shapes the worker actually meets — native HTML5 `<input
type=date>`, ARIA `[role=grid]` widgets, and class-/data-attribute
libraries (react-datepicker, MUI X DatePicker) — into one
deterministic call.
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
        date=StringSchema(
            "Target date as ISO 8601 (YYYY-MM-DD). The tool will pick "
            "this exact date in whichever calendar widget the page "
            "exposes — HTML5 <input type=date>, ARIA grid, or library "
            "calendar (react-datepicker / MUI X / etc.)."
        ),
        selector=StringSchema(
            "Optional CSS selector hint scoping the calendar root or "
            "the native date input. Useful when the page has multiple "
            "calendars open (start-date + end-date pickers). Omit to "
            "let the tool find the calendar itself.",
            nullable=True,
        ),
        vision_index=IntegerSchema(
            description=(
                "Optional 1-based vision bbox index (V_n). When set, "
                "the tool restricts its search to the bbox area — use "
                "this when vision has already pointed at the right "
                "calendar. Honors the same freshness/epoch-age gates "
                "as browser_click_at."
            ),
            nullable=True,
        ),
        required=["session_id", "date"],
    )
)
class BrowserPickDateTool(Tool):
    """Pick a date in a calendar widget across native, ARIA, and library shapes."""

    name = "browser_pick_date"
    description = (
        "Pick a date — fast paths only. Handles native <input "
        "type=date> and library cells exposing [data-date=\"YYYY-MM-"
        "DD\"] (react-datepicker, MUI X DatePicker). For ARIA grid "
        "calendars (Chakra, custom widgets), the tool returns a "
        "[pick_date_use_vision_bbox] hint and the brain drives via "
        "browser_click_at(vision_index=V_n) on the day cell directly "
        "— vision labels each cell with its day number, so V_n click "
        "is the uniform path across calendar libraries. Never use "
        "browser_type_at on calendar cells."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        session_id: str,
        date: str,
        selector: str | None = None,
        vision_index: int | None = None,
        **kw: Any,
    ) -> Any:
        # Validate ISO format up-front so the brain gets a tight error.
        if not (
            isinstance(date, str)
            and len(date) == 10
            and date[4] == "-"
            and date[7] == "-"
            and date[:4].isdigit()
            and date[5:7].isdigit()
            and date[8:10].isdigit()
        ):
            return (
                f"[pick_date_failed:bad_iso] date must be YYYY-MM-DD, got "
                f"{date!r}. Format the user's date as e.g. '2026-05-17' "
                f"and retry."
            )

        # Hard sync gate before mutation (mirrors browser_type_at /
        # browser_select_option).
        sync_block = await self.s.ensure_vision_synced(reason="browser_pick_date")
        if sync_block:
            return sync_block
        self.s._brain_turn_counter += 1

        payload: dict[str, Any] = {"date": date}
        if selector:
            payload["selector"] = selector
        label = f"date={date}"

        # Optional vision_index path — resolve V_n into a bbox using the
        # same freshness gates browser_select_option uses (form.py).
        if vision_index is not None:
            resp = self.s.vision_for_target_resolution()
            if resp is None:
                return (
                    "[pick_date_failed:no_vision] No recent vision "
                    "response to resolve vision_index against. "
                    "browser_screenshot, then retry — or omit "
                    "vision_index and pass `selector`."
                )
            bbox = resp.get_bbox(int(vision_index))
            if bbox is None:
                return (
                    f"[pick_date_failed:bad_vision_index] V{vision_index} "
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
                        f"[pick_date_failed:epoch_too_old "
                        f"age_turns={age_turns} max={max_age_turns}] "
                        f"V{vision_index} resolves against a vision "
                        f"snapshot taken {age_turns} actions ago. "
                        f"browser_screenshot to refresh, then retry."
                    )
            iw, ih = resp.image_width, resp.image_height
            if iw <= 0 or ih <= 0:
                return (
                    "[pick_date_failed:no_image_dims] Vision response "
                    "has no image dimensions; cannot denormalize bbox. "
                    "Re-screenshot."
                )
            dpr_val = float(getattr(resp, "dpr", 1.0) or 1.0)
            x0, y0, x1, y1 = bbox.to_pixels(iw, ih, dpr=dpr_val)
            payload["bbox"] = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
            label = f"V{vision_index}, date={date}"

        print(f"\n>> browser_pick_date({label})")

        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/pick-date",
                json=payload,
                timeout=30.0,
            )
        except Exception as exc:
            self.s.record_step("browser_pick_date", label, f"http_error: {exc}")
            return f"[pick_date_failed:http_error] {exc}"

        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            self.s.record_step("browser_pick_date", label, f"http_{r.status_code}")
            return f"[pick_date_failed:http_{r.status_code}] {err}"

        data = r.json() or {}
        # Refresh per-index fingerprints so a follow-up DOM-index click
        # doesn't collide on cached state (matches select_option pattern).
        fp_map = data.get("fingerprints") if isinstance(data, dict) else None
        if isinstance(fp_map, dict):
            self.s.element_fingerprints = {
                int(k): v for k, v in fp_map.items() if isinstance(v, str)
            }

        ok = bool(data.get("ok"))
        strategy = str(data.get("strategy") or "?")
        verified = bool(data.get("verified"))
        reason = data.get("reason")
        picked = str(data.get("picked_date") or date)
        iters = data.get("iters")
        header = data.get("header_text") or ""

        if ok:
            verify_note = "" if verified else " (selection not yet visible — re-screenshot to confirm)"
            note = f"Picked {picked} via {strategy}{verify_note}"
            if iters:
                note += f" after {iters} month-nav iter(s)"
            print(f"   [pick_date] ok strategy={strategy} verified={verified} iters={iters}")
            self.s.record_step("browser_pick_date", label, f"ok via {strategy}")
            self.s.log_activity(f"pick_date({date})", strategy)
            return self.s.build_text_only(data, note)

        # Failure path — surface the page-side reason verbatim.
        detail_bits: list[str] = []
        if reason:
            detail_bits.append(f"reason={reason}")
        if header:
            detail_bits.append(f"header={header!r}")
        if iters is not None:
            detail_bits.append(f"iters={iters}")
        detail = " ".join(detail_bits) if detail_bits else "no detail"
        print(f"   [pick_date] FAIL strategy={strategy} {detail}")
        self.s.record_step(
            "browser_pick_date", label, f"failed ({strategy}): {reason or '?'}",
        )
        # use_vision_bbox: native + data_attr both miss → calendar is
        # an ARIA grid (or custom widget) without ISO-keyed cells. The
        # brain takes over via vision-bbox clicks on the day cell —
        # reliably cross-library where DOM walking is not.
        if reason == "use_vision_bbox" or strategy == "use_vision_bbox":
            return (
                f"[pick_date_use_vision_bbox] No native <input "
                f"type=date> and no [data-date] cell match for {date}. "
                f"This is an ARIA grid / custom calendar — click the "
                f"day cell directly:\n"
                f"  1. browser_screenshot (the calendar is on-screen "
                f"already if you opened it; if not, click the date "
                f"trigger first).\n"
                f"  2. Find V_n for day {date[8:10]} in the calendar "
                f"month/year that matches {date[:7]}. Vision labels "
                f"each cell with its day number.\n"
                f"  3. If wrong month is showing, browser_click_at on "
                f"the prev/next chevron V_n, browser_screenshot, "
                f"repeat.\n"
                f"  4. browser_click_at(vision_index=V_n) on day {date[8:10]}.\n"
                f"Do NOT call browser_run_script / browser_eval to "
                f"navigate the calendar."
            )
        return f"[pick_date_failed:{reason or strategy}] {detail}"
