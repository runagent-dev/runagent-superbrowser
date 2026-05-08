"""Captcha detection + solver dispatch tools.

`BrowserDetectCaptchaTool` (poll), `BrowserCaptchaScreenshotTool` (close-up
screenshot of the widget), `BrowserSolveCaptchaTool` (orchestrator that
routes to token vendors / vision iterative solver / CF wait / human handoff).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

from ..captcha_solver import _solve_captcha_iterative
from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserDetectCaptchaTool(Tool):
    name = "browser_detect_captcha"
    description = "Check if the page has a captcha."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> str:
        # Route through _request_with_backoff so t3 sessions go to the
        # in-process patchright captcha detector, not the TS server.
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/captcha/detect",
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        captcha = data.get("captcha")
        if not captcha:
            return "No captcha detected."
        # Normalize across tiers: t3 returns {present: bool, type: ...};
        # TS may return a different shape where absence is represented as
        # falsy. Either way, bail if nothing is active.
        if isinstance(captcha, dict) and (
            captcha.get("present") is False
            or captcha.get("type") in ("none", None, "")
        ):
            return "No captcha detected."
        # Detecting a captcha triggers captcha_mode: screenshot dedup +
        # "no-actions-since-last-shot" rules are relaxed so the solver can
        # take repeated before/after screenshots.
        self.s.enter_captcha_mode()
        # Surface the live-view URL the moment a captcha is detected, not
        # only when handoff fires. Gives the LLM (and any UI piping tool
        # output to a human) a link to offer immediately. For t3 sessions
        # this is the Python viewer port; for t1 it's the TS server.
        view_url: str | None = None
        if self.s.backend == "t3":
            try:
                from superbrowser_bridge.antibot import t3_viewer as _v
                await _v.ensure_started()
                view_url = _v.view_url(session_id)
            except Exception:
                view_url = None
        else:
            view_url = data.get("viewUrl") or (
                f"{os.environ['SUPERBROWSER_PUBLIC_HOST'].rstrip('/')}"
                f"/session/{session_id}/view"
                if os.environ.get("SUPERBROWSER_PUBLIC_HOST")
                else None
            )
        lines = [
            f"Captcha detected: type={captcha['type']}, "
            f"siteKey={captcha.get('siteKey', 'N/A')} "
            f"(captcha_mode active for next {self.s.CAPTCHA_MODE_ITERATIONS} iterations)",
        ]
        if view_url:
            lines.append(
                f"Live view for human handoff: {view_url} "
                f"(open this URL if you decide to hand off to the user)"
            )
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(session_id=StringSchema("Session ID"), required=["session_id"])
)
class BrowserCaptchaScreenshotTool(Tool):
    name = "browser_captcha_screenshot"
    description = "Take a close-up screenshot of the captcha area."

    def __init__(self, state: BrowserSessionState):
        self.s = state

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, session_id: str, **kw: Any) -> Any:
        r = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/captcha/screenshot",
            timeout=10.0,
        )
        if r.status_code == 404:
            return "No captcha area found."
        r.raise_for_status()
        # t3 returns a dict with image_base64; t1 returns raw JPEG bytes.
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        if isinstance(body, dict) and body.get("image_base64"):
            b64 = body["image_base64"]
        else:
            b64 = base64.b64encode(r.content).decode()
        return await self.s.build_tool_result_blocks(
            b64,
            "Captcha area — analyze to solve",
            intent="solve captcha — locate widget + tiles + handles",
            url=self.s.current_url,
        )


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        method=StringSchema("Solving method: 'auto', 'token', 'ai_vision', 'grid'", nullable=True),
        provider=StringSchema("Captcha solver: '2captcha' or 'anticaptcha'", nullable=True),
        api_key=StringSchema("API key for solver service", nullable=True),
        required=["session_id"],
    )
)
class BrowserSolveCaptchaTool(Tool):
    name = "browser_solve_captcha"
    description = "Solve a detected captcha automatically."

    def __init__(self, state: BrowserSessionState):
        self.s = state
        # Per-session lock so concurrent solve calls on the same session
        # serialize around captcha_solve_round / captcha_screenshots_used.
        # The agent fires single-threaded, but retry loops or stray parallel
        # tool calls can race otherwise.
        self._session_locks: dict[str, asyncio.Lock] = {}

    async def execute(self, session_id: str, method: str | None = None, provider: str | None = None, api_key: str | None = None, **kw: Any) -> str:
        # Any solve attempt unblocks the CF nav-guard — whether or not
        # the solve succeeds, the agent has acknowledged the interstitial,
        # and re-navigating is no longer a blind loop.
        self.s.nav_solve_called_since_block = True

        # Vision-based solve path. Replaces the three deleted TS vision
        # strategies (recaptcha-ai-grid, slider-drag, generic-vision) with
        # a single call to our dedicated Python vision agent. When the
        # brain (or fast-to-human policy) picks method='vision' we run the
        # full solve loop here and return a structured result without ever
        # hitting the server's captcha/solve endpoint.
        if method == "vision":
            return await self._solve_via_vision(session_id)

        # Auto-route captcha types that the TS server registry has no
        # strategy for (text_captcha, generic) straight to the Python
        # vision loop. Without this, method='auto' on T1 would fall all
        # the way through to human_handoff for a classic distorted-word
        # captcha that vision can solve directly.
        #
        # cf_interstitial gets its own solver (wait-based, not vision):
        # the whole page IS the challenge, no clicking needed — CF's JS
        # just needs time + humanization to auto-pass.
        if method in (None, "auto"):
            try:
                dr = await _request_with_backoff(
                    "GET",
                    f"{SUPERBROWSER_URL}/session/{session_id}/captcha/detect",
                    timeout=10.0,
                )
                if dr.status_code == 200:
                    det_cap = (dr.json() or {}).get("captcha") or {}
                    det_type = (det_cap.get("type") or "").lower()
                    det_present = bool(det_cap.get("present"))
                    det_notes = det_cap.get("notes") or []
                    print(
                        f"  [auto-route] detect -> type={det_type!r} "
                        f"present={det_present} notes={det_notes}"
                    )
                    if det_type == "cf_interstitial":
                        return await self._solve_via_cf_wait(session_id)
                    if det_type in ("text_captcha", "text", "generic", "image"):
                        return await self._solve_via_vision(session_id)
                    if det_type in ("recaptcha-v2", "hcaptcha"):
                        # Cheap first attempt: let the vision loop click the
                        # widget checkbox. On a trusted-fingerprint session
                        # (patchright + good proxy) this alone often passes.
                        # Small step budget so we bail fast if the image-tile
                        # challenge appears — the token vendor / human handoff
                        # below handle that class better.
                        print(
                            f"  [vision checkbox] trying widget click "
                            f"(max_steps=3) for {det_type!r} before token vendor"
                        )
                        vr = await self._try_vision_solve_for_widget(
                            session_id, det_cap, max_steps=3,
                        )
                        if vr and vr.get("solved"):
                            self.s.captcha_mode = False
                            self.s.captcha_mode_remaining = 0
                            self.s.record_step(
                                "browser_solve_captcha",
                                "vision_checkbox",
                                f"solved=True via vision checkbox-click | "
                                f"{json.dumps(vr, default=str)[:300]}",
                            )
                            return (
                                f"Captcha SOLVED via vision checkbox-click "
                                f"({vr.get('steps', 0)} step(s))"
                                f"\n\nResult JSON:\n"
                                f"{json.dumps(vr, indent=2, default=str)}"
                            )
                        if vr is not None:
                            self.s.record_step(
                                "browser_solve_captcha",
                                "vision_checkbox",
                                f"solved=False via vision checkbox-click, "
                                f"falling through | "
                                f"{json.dumps(vr, default=str)[:300]}",
                            )
                else:
                    print(
                        f"  [auto-route] detect HTTP {dr.status_code} — "
                        f"falling through to TS solver"
                    )
            except Exception as exc:
                # Detection failure is non-fatal — fall through to the
                # standard TS solver path which runs its own detection.
                print(
                    f"  [auto-route] detect raised "
                    f"{type(exc).__name__}: {exc} — falling through"
                )

        payload: dict[str, Any] = {}
        if method:
            payload["method"] = method
        if provider:
            payload["provider"] = provider
        if api_key:
            payload["apiKey"] = api_key
        # Advance the solve round BEFORE the call so the next screenshot
        # (which the LLM will take to inspect the result) gets a fresh dedup
        # allowance distinct from the pre-solve shots.
        self.s.captcha_solve_round += 1
        r = await _request_with_backoff(
            "POST",
            f"{SUPERBROWSER_URL}/session/{session_id}/captcha/solve",
            json=payload,
            timeout=180.0,
        )
        r.raise_for_status()
        data = r.json()

        # Build a structured result the orchestrator can parse — keeps the
        # method + subMethod + vendor + trace so per-domain captcha learnings
        # can be written automatically. The LLM sees JSON; a human-readable
        # summary is injected at the top for quick scanning.
        summary: str
        if data.get("solved"):
            summary = (
                f"Captcha SOLVED via {data.get('method', '?')}"
                f"/{data.get('subMethod', '?')} in {data.get('durationMs', 0)}ms "
                f"({data.get('attempts', 1)} attempt(s))"
            )
        else:
            summary = f"Captcha NOT solved: {data.get('error', 'all methods failed')}"

        # Record the structured method info so orchestrator can learn from it.
        structured = {
            "solved": bool(data.get("solved")),
            "captchaType": data.get("captchaType") or data.get("captcha", {}).get("type"),
            "vendorDetected": data.get("vendorDetected"),
            "method": data.get("method"),
            "subMethod": data.get("subMethod"),
            "attempts": data.get("attempts"),
            "totalRounds": data.get("totalRounds"),
            "durationMs": data.get("durationMs"),
            "siteKey": data.get("siteKey"),
            "iframeUrl": data.get("iframeUrl"),
            "error": data.get("error"),
        }
        # Drop None values so the JSON stays compact.
        structured = {k: v for k, v in structured.items() if v is not None}

        self.s.record_step(
            "browser_solve_captcha",
            method or "auto",
            f"{summary} | {json.dumps(structured, default=str)[:300]}",
        )

        # The solve freed us from captcha — end captcha_mode so the normal
        # budget rules resume.
        if data.get("solved"):
            self.s.captcha_mode = False
            self.s.captcha_mode_remaining = 0
            return f"{summary}\n\nResult JSON:\n{json.dumps(structured, indent=2, default=str)}"

        # --- Auto-escalation to human handoff (deterministic) -------------
        # When auto-solve fails AND human handoff is enabled, immediately
        # POST to the human-input endpoint instead of returning to the LLM
        # and hoping it calls browser_ask_user. This removes the LLM
        # decision loop from the critical captcha→human path.
        if self.s.human_handoff_enabled and self.s.human_handoff_budget > 0:
            print(f"   [auto-escalation] captcha auto-solve failed, requesting human handoff")
            self.s.human_handoff_budget -= 1
            try:
                handoff_timeout = int(os.environ.get("SUPERBROWSER_HANDOFF_TIMEOUT_MS", "180000")) / 1000
                async with httpx.AsyncClient(timeout=handoff_timeout + 10) as hclient:
                    hr = await hclient.post(
                        f"{SUPERBROWSER_URL}/session/{session_id}/human-input/ask",
                        json={
                            "type": "captcha",
                            "message": (
                                "Auto-solve failed for captcha. Please open the live view URL "
                                "and click through the challenge — the agent will detect when "
                                "it clears and resume automatically."
                            ),
                        },
                    )
                    if hr.status_code == 200:
                        hdata = hr.json()
                        if hdata.get("cancelled") or hdata.get("timeout"):
                            human_result = "Human handoff timed out or was cancelled."
                        else:
                            human_result = "Human solved the captcha. Resuming task."
                            self.s.captcha_mode = False
                            self.s.captcha_mode_remaining = 0
                    else:
                        human_result = f"Human handoff request failed (HTTP {hr.status_code})."
            except Exception as exc:
                human_result = f"Human handoff request error: {exc}"

            self.s.record_step(
                "browser_solve_captcha",
                "auto_escalation",
                human_result,
            )
            return (
                f"{summary}\n\n"
                f"[AUTO-ESCALATION] {human_result}\n\n"
                f"Result JSON:\n{json.dumps(structured, indent=2, default=str)}"
            )

        return f"{summary}\n\nResult JSON:\n{json.dumps(structured, indent=2, default=str)}"

    async def _solve_via_cf_wait(self, session_id: str) -> str:
        """Solve a Cloudflare Managed Challenge by waiting for auto-pass.

        Routes to `/captcha/solve` with method='cf_wait' — the T3 HTTP
        dispatcher picks up `cf_interstitial` detect type and calls the
        dedicated humanized wait loop (`antibot.captcha.solve_cf`). On
        T1 the TS server's own CF handling runs.
        """
        print(f"  [cf_wait] entering solver for session={session_id}")
        self.s.captcha_solve_round += 1
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            try:
                r = await _request_with_backoff(
                    "POST",
                    f"{SUPERBROWSER_URL}/session/{session_id}/captcha/solve",
                    json={"method": "cf_wait"},
                    timeout=120.0,
                )
                if r.status_code >= 400:
                    body_snippet = ""
                    try:
                        body_snippet = json.dumps(r.json(), default=str)[:400]
                    except Exception:
                        body_snippet = (getattr(r, "text", "") or "")[:400]
                    print(
                        f"  [cf_wait] dispatcher returned HTTP {r.status_code}: "
                        f"{body_snippet}"
                    )
                    data: dict[str, Any] = {
                        "solved": False, "method": "cf_wait",
                        "error": f"HTTP {r.status_code}",
                        "dispatcher_body": body_snippet,
                    }
                else:
                    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    if not isinstance(data, dict):
                        data = {"solved": False, "method": "cf_wait",
                                "error": "non-dict response"}
                    print(
                        f"  [cf_wait] result: solved={data.get('solved')} "
                        f"durationMs={data.get('durationMs')} "
                        f"iterations={data.get('iterations')} "
                        f"cookies={data.get('cookies_landed')}"
                    )
            except Exception as exc:
                import traceback as _tb
                print(
                    f"  [cf_wait] dispatch raised {type(exc).__name__}: {exc}"
                )
                print(_tb.format_exc())
                data = {
                    "solved": False, "method": "cf_wait",
                    "error": f"dispatch failed: {exc}",
                }

        self.s.record_step(
            "browser_solve_captcha",
            "cf_wait",
            f"solved={data.get('solved')} | "
            f"{json.dumps(data, default=str)[:300]}",
        )

        if data.get("solved"):
            self.s.captcha_mode = False
            self.s.captcha_mode_remaining = 0
            # Clear any nav-guard set by a prior failed navigate.
            self.s.last_nav_cf_blocked_url = ""
            return (
                f"Captcha SOLVED via cf_wait (CF interstitial auto-passed "
                f"in {data.get('durationMs', 0)}ms)"
                f"\n\nResult JSON:\n{json.dumps(data, indent=2, default=str)}"
            )
        return (
            f"Captcha NOT solved: CF interstitial did not auto-clear. "
            f"Consider residential proxy (PROXY_POOL_RESIDENTIAL) or "
            f"headful mode (SUPERBROWSER_ALLOW_HEADFUL=1 + xvfb), or "
            f"call browser_ask_user for human handoff."
            f"\n\nResult JSON:\n{json.dumps(data, indent=2, default=str)}"
        )

    async def _try_slider_dispatch(self, session_id: str) -> dict | None:
        """POST to /captcha/solve with method='slider' and return the parsed
        result, or None if the call itself errored.

        Used by `_solve_via_vision` to give sliders first chance at the
        dedicated bezier-drag solver before falling back to vision-driven
        drag. Keeps the solver boundary clean — we just forward and let
        the tier's native slider code (antibot.captcha.solve_slider on
        T3, src/browser/captcha equivalent on T1) do its thing.
        """
        try:
            r = await _request_with_backoff(
                "POST",
                f"{SUPERBROWSER_URL}/session/{session_id}/captcha/solve",
                json={"method": "slider"},
                timeout=60.0,
            )
            if r.status_code >= 400:
                return None
            data = r.json()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return None

    async def _try_vision_solve_for_widget(
        self,
        session_id: str,
        det_cap: dict,
        *,
        max_steps: int,
    ) -> dict[str, Any] | None:
        """Attempt a vision+cursor solve for widget captchas (recaptcha-v2,
        hcaptcha) using the already-detected widget bbox. Returns the
        structured result dict from `_solve_captcha_iterative`, or None
        when vision is unavailable — the caller reads None as "skip vision,
        go straight to token vendor." Never raises.
        """
        try:
            from vision_agent import get_vision_agent, vision_agent_enabled
        except ImportError:
            return None
        if not vision_agent_enabled():
            return None

        from superbrowser_bridge.antibot.captcha.detect import CaptchaInfo
        captcha_info = CaptchaInfo(
            type=det_cap.get("type", "none"),
            present=bool(det_cap.get("present", True)),
            site_key=det_cap.get("site_key", "") or "",
            widget_selector=det_cap.get("widget_selector", "") or "",
            widget_bbox=det_cap.get("widget_bbox"),
            input_bbox=det_cap.get("input_bbox"),
            frame_url=det_cap.get("frame_url", "") or "",
            notes=list(det_cap.get("notes") or []),
        )

        self.s.captcha_solve_round += 1
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            agent = get_vision_agent()
            return await _solve_captcha_iterative(
                session_id,
                captcha_info,
                agent,
                task_instruction=self.s.task_instruction,
                solve_round=self.s.captcha_solve_round,
                max_steps=max_steps,
            )

    async def _solve_via_vision(self, session_id: str) -> str:
        """Python-side captcha solver — delegates to the iterative loop.

        Per-step flow lives in `_solve_captcha_iterative` so the T3 HTTP
        dispatch path (antibot/captcha/solve_vision.py shim) and this
        Python-tool path share one implementation. This method owns the
        session-state plumbing around the loop: enable check, per-session
        serialization, captcha_mode reset, and structured-result logging.
        """
        try:
            from vision_agent import get_vision_agent, vision_agent_enabled
        except ImportError:
            return (
                "Captcha NOT solved: vision_agent package not importable. "
                "Install / configure the vision agent or use method='auto'."
            )
        if not vision_agent_enabled():
            return (
                "Captcha NOT solved: method='vision' requires VISION_ENABLED=1. "
                "Set the env flag and a VISION_API_KEY, or use method='auto'."
            )

        self.s.captcha_solve_round += 1
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())

        async with lock:
            # Best-effort detect for dispatcher context (slider widgets need
            # the server-side pixel bbox to clamp drag endpoints). Failure
            # is non-fatal — the loop proceeds with what vision sees.
            captcha_info: Any = None
            try:
                dr = await _request_with_backoff(
                    "GET",
                    f"{SUPERBROWSER_URL}/session/{session_id}/captcha/detect",
                    timeout=10.0,
                )
                if dr.status_code == 200:
                    cap = (dr.json() or {}).get("captcha") or {}
                    from superbrowser_bridge.antibot.captcha.detect import CaptchaInfo
                    captcha_info = CaptchaInfo(
                        type=cap.get("type", "none"),
                        present=bool(cap.get("present", True)),
                        site_key=cap.get("site_key", "") or "",
                        widget_selector=cap.get("widget_selector", "") or "",
                        widget_bbox=cap.get("widget_bbox"),
                        input_bbox=cap.get("input_bbox"),
                        frame_url=cap.get("frame_url", "") or "",
                        notes=list(cap.get("notes") or []),
                    )
            except Exception:
                captcha_info = None

            # Slider pre-dispatch: the dedicated bezier-drag solver
            # (antibot.captcha.solve_slider on T3 / TS equivalent on T1)
            # has a motion profile tuned to defeat Geetest/Tencent/DataDome
            # fingerprinting. Vision-driven drag via our /drag endpoint
            # works, but the bezier solver is materially better at this
            # specific class. Route sliders to it first; fall through to
            # the iterative loop only if the dedicated solver returns
            # unsolved.
            if captcha_info is not None and captcha_info.type == "slider":
                slider_result = await self._try_slider_dispatch(session_id)
                if slider_result is not None and slider_result.get("solved"):
                    self.s.record_step(
                        "browser_solve_captcha",
                        "vision->slider",
                        f"solved=True via dedicated slider solver | "
                        f"{json.dumps(slider_result, default=str)[:280]}",
                    )
                    self.s.captcha_mode = False
                    self.s.captcha_mode_remaining = 0
                    return (
                        f"Captcha SOLVED via dedicated slider bezier-drag"
                        f"\n\nResult JSON:\n{json.dumps(slider_result, indent=2, default=str)}"
                    )
                # Dedicated solver failed — fall through to the iterative
                # loop so vision can try. Error paths end up reported by
                # the loop result.

            agent = get_vision_agent()
            result = await _solve_captcha_iterative(
                session_id,
                captcha_info,
                agent,
                task_instruction=self.s.task_instruction,
                solve_round=self.s.captcha_solve_round,
            )

        self.s.record_step(
            "browser_solve_captcha",
            "vision",
            f"solved={result.get('solved')} | "
            f"{json.dumps(result, default=str)[:300]}",
        )

        if result.get("solved"):
            self.s.captcha_mode = False
            self.s.captcha_mode_remaining = 0
            return (
                f"Captcha SOLVED via vision_iterative "
                f"({result.get('steps', 0)} step(s))"
                f"\n\nResult JSON:\n{json.dumps(result, indent=2, default=str)}"
            )
        return (
            f"Captcha NOT solved after {result.get('steps', 0)} step(s). "
            f"Per fast-to-human policy, call browser_ask_user next."
            f"\n\nResult JSON:\n{json.dumps(result, indent=2, default=str)}"
        )
