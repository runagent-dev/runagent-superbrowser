"""Coordinate / selector / polyline drag tools.

`BrowserDragTool` (raw coords), `BrowserDragSelectorsTool` (CSS source +
destination), `BrowserDragPathTool` (polyline of points).
"""

from __future__ import annotations

import json
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)

from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState


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
        self.s.actions_since_screenshot += 1
        self.s._brain_turn_counter += 1
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
        caption = f"Dragged from ({startX},{startY}) to ({endX},{endY})"
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        from_selector=StringSchema("CSS selector of the drag source element"),
        to_selector=StringSchema("CSS selector of the drag destination element"),
        method=StringSchema(
            "One of 'auto' (default: try click_click, fall back to drag), "
            "'click_click' (two discrete clicks — more robust for chess/grid), "
            "'drag' (mousedown → move → mouseup — classical drag). ",
            nullable=True,
        ),
        hold_ms=IntegerSchema(
            "Milliseconds to pause between the two clicks when method=click_click, "
            "or to hold before drag start. Default 120.",
            nullable=True,
        ),
        linear=BooleanSchema(
            description="If true (default), deterministic paths. Set false for stealth-critical drags.",
            nullable=True,
        ),
        required=["session_id", "from_selector", "to_selector"],
    )
)
class BrowserDragSelectorsTool(Tool):
    name = "browser_drag_selectors"
    description = (
        "Drag from one CSS-selected element to another. Pixel-exact. "
        "Default method 'auto' tries click-click first (safer on react-dnd "
        "and grid boards like chess.com) and falls back to classical drag "
        "if the DOM didn't mutate. PREFER OVER browser_drag(x1,y1,x2,y2) "
        "whenever both endpoints have stable selectors."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        from_selector: str,
        to_selector: str,
        method: str | None = None,
        hold_ms: int | None = None,
        linear: bool | None = None,
        **kw: Any,
    ) -> str:
        method = method or "auto"
        if method not in ("auto", "click_click", "drag"):
            return f"[drag_selectors_failed] method must be auto|click_click|drag, got {method!r}"
        print(f"\n>> browser_drag_selectors({from_selector!r} → {to_selector!r}, method={method})")
        self.s.actions_since_screenshot += 1
        self.s.consecutive_click_calls = 0

        payload: dict[str, Any] = {
            "fromSelector": from_selector,
            "toSelector": to_selector,
            "method": method,
        }
        if hold_ms is not None:
            payload["holdMs"] = hold_ms
        if linear is not None:
            payload["linear"] = linear

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/drag-selectors",
            json=payload,
            timeout=30.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[drag_selectors_failed] {err}"
        data = r.json()
        outcome = data.get("outcome", {})
        self.s.record_step(
            "browser_drag_selectors",
            f"{from_selector}→{to_selector} via {outcome.get('methodUsed','?')}",
            data.get("url", ""),
        )
        frm = outcome.get("from", {})
        to = outcome.get("to", {})
        caption = (
            f"Dragged {from_selector} → {to_selector} "
            f"via {outcome.get('methodUsed','?')} "
            f"({frm.get('x','?')},{frm.get('y','?')}) → ({to.get('x','?')},{to.get('y','?')}) "
            f"mutated={outcome.get('mutated', False)}"
        )
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return caption


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        points_json=StringSchema(
            "JSON array of {x, y} points, e.g. '[{\"x\":100,\"y\":200},{\"x\":150,\"y\":220}]'. "
            "At least two points required."
        ),
        step_ms=IntegerSchema(
            "Milliseconds between intermediate mouseMove events. Default 16 (~60fps).",
            nullable=True,
        ),
        hold_ms=IntegerSchema("Pre-press hold duration at points[0]. Default 50.", nullable=True),
        button=StringSchema("Mouse button: left|right|middle. Default left.", nullable=True),
        required=["session_id", "points_json"],
    )
)
class BrowserDragPathTool(Tool):
    name = "browser_drag_path"
    description = (
        "Drag along an arbitrary polyline of (x,y) points. For jigsaw "
        "captcha traces, connect-the-dots, signature drawing, or any "
        "free-form gesture where a straight start→end drag won't work."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        points_json: str,
        step_ms: int | None = None,
        hold_ms: int | None = None,
        button: str | None = None,
        **kw: Any,
    ) -> str:
        try:
            points = json.loads(points_json)
        except (TypeError, ValueError) as exc:
            return f"[drag_path_failed] points_json is not valid JSON: {exc}"
        if not isinstance(points, list) or len(points) < 2:
            return "[drag_path_failed] points_json must decode to a list of ≥2 {x,y} objects."
        for i, p in enumerate(points):
            if not isinstance(p, dict) or not isinstance(p.get("x"), (int, float)) \
               or not isinstance(p.get("y"), (int, float)):
                return f"[drag_path_failed] point[{i}] must be {{x: number, y: number}}"

        print(f"\n>> browser_drag_path({len(points)} points)")
        self.s.actions_since_screenshot += 1

        payload: dict[str, Any] = {"points": points}
        if step_ms is not None:
            payload["stepMs"] = step_ms
        if hold_ms is not None:
            payload["holdMs"] = hold_ms
        if button is not None:
            payload["button"] = button

        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/drag-path",
            json=payload,
            timeout=30.0,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            return f"[drag_path_failed] {err}"
        data = r.json()
        self.s.record_step(
            "browser_drag_path",
            f"{len(points)} points",
            data.get("url", ""),
        )
        caption = f"Dragged along polyline of {len(points)} points"
        if data.get("elements"):
            caption += f"\n{data['elements']}"
        return caption
