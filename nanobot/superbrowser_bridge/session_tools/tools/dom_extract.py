"""DOM-extraction tools — markdown / plan-next-steps / brief-mark /
verify-fact / get-rect."""

from __future__ import annotations

from ._common import *  # noqa: F401,F403

@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserGetMarkdownTool(Tool):
    name = "browser_get_markdown"
    description = "Extract page content as markdown. FREE — no screenshot cost."

    def __init__(self, state: BrowserSessionState | None = None):
        # state is optional so the tool stays usable in stateless contexts.
        # When attached, the latest markdown body is cached on
        # _last_markdown so the worker hook can run page_text reconciliation
        # without an extra round-trip.
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/markdown",
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        body = data.get("content", "No content extracted")[:10000]
        if self.s is not None:
            self.s._last_markdown = body
            # Reading the page is a deliberation event — see comment on
            # BrowserSessionState.last_deliberation_turn.
            self.s.last_deliberation_turn = self.s._brain_turn_counter
        return body


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserPlanNextStepsTool(Tool):
    """Re-run the hierarchical action planner against the cached vision
    + blocker state without taking a fresh screenshot.

    Useful after a click that missed its postcondition — the brain wants
    an updated plan without burning screenshot budget. The planner itself
    is cached by scene fingerprint, so rapid re-plans on an unchanged
    scene return the same queue in near-zero time.

    Returns the ordered [PLAN] block as plain text. The worker LLM can
    read it and pick the top action to execute, or override.
    """

    name = "browser_plan_next_steps"
    description = (
        "Re-compute the planned action queue (dismiss blockers → main goal) "
        "from the most recent vision + DOM-blocker snapshot. No screenshot, "
        "no vision call — cheap. Call after a failed dismiss or when the "
        "scene may have changed and you want the planner's latest ranking "
        "before deciding the next tool."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        if not session_id.startswith("t3-"):
            return (
                "[plan_unavailable] Planner currently runs only for t3 "
                "(undetected Chromium) sessions. Call browser_screenshot "
                "to get a fresh vision + suggested_actions instead."
            )
        resp = self.s._last_vision_response
        if resp is None:
            return (
                "[plan_unavailable] No cached vision response. Run "
                "browser_screenshot first."
            )
        try:
            from superbrowser_bridge.antibot import interactive_session as _t3mgr
            from superbrowser_bridge.antibot.ui_blockers import detect as _detect_blockers
            from superbrowser_bridge.action_planner import plan as _plan_actions
            mgr = _t3mgr.default()
            blockers = await _detect_blockers(mgr, session_id)
            self.s._last_blockers = blockers
            queue = _plan_actions(
                vresp=resp,
                blockers=blockers,
                task_instruction=self.s.task_instruction or "",
                url=self.s.current_url or "",
                recent_steps=self.s.step_history[-8:] if self.s.step_history else [],
            )
            self.s._last_action_queue = queue
            return queue.to_brain_text()
        except Exception as exc:
            return f"[plan_failed] {str(exc)[:200]}"


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        constraint_id=IntegerSchema(
            "ID of the constraint from the [CHECKLIST] block. Numbers are "
            "stable across iterations — pick the leftmost integer of the "
            "row you want to flip."
        ),
        status=StringSchema(
            "New status: 'done' (constraint satisfied), 'failed' (cannot "
            "be satisfied on this site), or 'not_applicable' (constraint "
            "doesn't apply to this page/flow). 'open' / 'active' are "
            "never set manually."
        ),
        evidence=StringSchema(
            "One short sentence describing the evidence — what you saw, "
            "where, how. Stored on the constraint and shown to the "
            "orchestrator. Keep under 200 chars.",
            nullable=True,
        ),
        required=["session_id", "constraint_id", "status"],
    )
)
class BrowserBriefMarkTool(Tool):
    """Manually flip a checklist item — for action / extraction
    constraints whose ``predicate`` is ``manual: true`` and so cannot be
    auto-flipped from the URL or vision response. The auto-reconciler
    handles every ``filter`` constraint without intervention."""

    name = "browser_brief_mark"
    description = (
        "Mark a [CHECKLIST] constraint as done | failed | not_applicable. "
        "Use ONLY when you've actually verified the evidence — the "
        "orchestrator surfaces an INCOMPLETE_CHECKLIST report when items "
        "remain open. Auto-flipped filter constraints don't need this; "
        "call it for action / extraction items."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        constraint_id: int,
        status: str,
        evidence: str | None = None,
        **kw: Any,
    ) -> str:
        brief = getattr(self.s, "task_brief", None)
        if brief is None:
            return (
                "[brief_mark_unavailable] No task checklist is attached to "
                "this session. The orchestrator did not pass task_checklist "
                "for this delegation, so there is nothing to mark."
            )
        try:
            cid = int(constraint_id)
        except (TypeError, ValueError):
            return f"[brief_mark_failed] constraint_id must be an integer, got {constraint_id!r}"
        if status not in ("done", "failed", "not_applicable"):
            return (
                f"[brief_mark_failed] status must be 'done' | 'failed' | "
                f"'not_applicable', got {status!r}"
            )
        ok = brief.mark(cid, status, evidence or "")
        if not ok:
            ids = ", ".join(str(c.id) for c in brief.constraints)
            return (
                f"[brief_mark_failed] no constraint with id={cid}. "
                f"Known ids: {ids}"
            )
        # Marking a constraint counts as deliberation.
        self.s.last_deliberation_turn = self.s._brain_turn_counter
        self.s.record_step(
            "browser_brief_mark",
            f"#{cid}={status}",
            (evidence or "")[:120],
        )
        return (
            f"[brief_mark_ok] #{cid} -> {status}. "
            f"Open: {brief.open_count()} of {len(brief.constraints)}. "
            f"Next focus: "
            f"{brief.next_focus().label if brief.next_focus() else '(all done)'}"
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        claim=StringSchema(
            "The exact factual claim you're about to report. Include the value, unit, "
            "and what it refers to. E.g. 'Total price for 2 nights at Agoda Grand Sylhet "
            "May 3-5 is BDT 14,500' or '5-star hotel count in Sylhet on GoZayaan is 3'."
        ),
        required=["session_id", "claim"],
    )
)
class BrowserVerifyFactTool(Tool):
    """Visual sanity check before reporting an extracted value.

    Takes a fresh screenshot and frames a narrow verification question for
    the next model turn. The LLM must look at the actual page and say whether
    it supports the claim. Catches the common failure mode where an extraction
    script returned null/wrong-element and the model filled in a plausible
    number downstream.

    Intentionally bypasses normal dedup — verification screenshots are a
    deliberate, infrequent request and must see the current state.
    """

    name = "browser_verify_fact"
    description = (
        "Visually verify a factual claim against the current page before reporting it. "
        "Call this with the EXACT value you're about to return. Then look at the "
        "returned screenshot and answer honestly: does the page actually show this? "
        "If not, do NOT report the original value — go back and fix your extraction."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, claim: str, **kw: Any) -> Any:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            params={"vision": "true"},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()

        self.s.record_step("browser_verify_fact", claim[:80], "screenshot taken for verification")

        caption = (
            f"[VERIFY CLAIM]\n"
            f"Claim under review: {claim}\n\n"
            f"Look at the screenshot below carefully. In your NEXT reply, respond ONLY "
            f"with a JSON object of the form:\n"
            f'  {{"supported": <bool>, "observed_value": "<what you actually see on the page, '
            f"verbatim, or null if absent>\", "
            f'"reason": "<one sentence explaining what you saw>"}}\n\n'
            f"Rules:\n"
            f"- supported=true only if the claim matches what's visible on the page exactly "
            f"(values, units, context). A crossed-out price is NOT the current price.\n"
            f"- If the page shows a DIFFERENT value than the claim, set supported=false "
            f"and put the real value in observed_value.\n"
            f"- If the page doesn't show enough to tell, set supported=false with a "
            f"'cannot verify' reason — do NOT rubber-stamp.\n"
            f"- After this verify, if supported=false, FIX your extraction and retry. "
            f"If supported=true, report the claim as your final answer."
        )

        if data.get("screenshot"):
            # Don't let verify-fact screenshots eat the captcha cap (they're
            # not for captcha) nor trigger normal dedup (verification must
            # see the live page state). Route through the same async
            # vision-preprocessor hook every other screenshot tool uses, so
            # the brain never sees the raw image when VISION_ENABLED=1.
            self.s.vision_calls += 1
            return await self.s.build_tool_result_blocks(
                data["screenshot"],
                caption,
                intent="verify fact against page",
                url=data.get("url", self.s.current_url),
                elements=data.get("elements"),
            )
        # No screenshot available — still return the caption so the caller
        # can at least reason about the textual state.
        return caption + "\n\n[No screenshot available — verify against browser_get_markdown output.]"


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


