"""Human-handoff tools — `BrowserAskUserTool` and `BrowserRequestHelpTool`.

Both block the worker on a human channel; `BrowserAskUserTool` posts a
question to the live-view UI and waits for a reply, while
`BrowserRequestHelpTool` writes a resumption artifact so the orchestrator
can spin up a successor worker on the SAME session with knowledge of
what failed.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema

from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..resumption import save_resumption_artifact
from ..state import BrowserSessionState


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        question=StringSchema("What to ask the user"),
        input_type=StringSchema("Type: credentials, captcha, confirmation, otp, text, choice", nullable=True),
        required=["session_id", "question"],
    )
)
class BrowserAskUserTool(Tool):
    name = "browser_ask_user"
    description = (
        "Ask the user a question and BLOCK until they respond "
        "(up to 5 minutes). Use for credentials, OTP, confirmation, or "
        "when you need a human decision. The user replies via the remote "
        "view UI at /session/<id>/view or any HTTP client. Returns the "
        "user's reply as a string; on timeout returns a sentinel message "
        "you can react to."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def _notify_assist(
        self,
        session_id: str,
        question: str,
        input_type: str | None,
        view_url: str,
        tier: str,
        timeout_ms: int,
        screenshot_b64: str | None = None,
    ) -> None:
        """Record + webhook-notify the ask via the human-assist framework.

        Best-effort: the ask itself blocks on the engine either way; this only
        makes sure a channels gateway (or the terminal banner) surfaces it.
        """
        try:
            from ...human_assist.emitter import build_payload, notify_gateway
            from ...human_assist.models import HumanAssistRequest
            from ...human_assist.store import default_store

            assist_type = (input_type or "text").lower()
            if assist_type not in ("captcha", "login", "otp", "approval", "text"):
                assist_type = "otp" if assist_type in ("credentials", "card") else "text"
            request = HumanAssistRequest(
                type=assist_type,
                session_id=session_id,
                view_url=view_url,
                question=question,
                task_id=self.s.task_id or "",
                tier=tier,
                page_url=self.s.current_url or "",
                timeout_s=timeout_ms / 1000.0,
            )
            default_store().save(request)
            reply_hint = {
                "humanInputUrl": f"{SUPERBROWSER_URL}/session/{session_id}/human-input",
                "expectsText": assist_type in ("otp", "text", "approval"),
            } if tier == "t1" else None
            delivered = await notify_gateway(
                build_payload(request, screenshot_b64=screenshot_b64, reply_hint=reply_hint)
            )
            if delivered:
                request.transition("notified")
                request.notify_count += 1
                default_store().save(request)
        except Exception:  # noqa: BLE001 - notification must never break the ask
            pass

    async def execute(
        self,
        session_id: str,
        question: str,
        input_type: str | None = None,
        **kw: Any,
    ) -> Any:
        # Tier-3 path: spin up the Python live viewer and return its URL
        # as a hint to the LLM. The actual blocking "wait for user" is
        # simpler on t3 — we poll for a state change on the captcha
        # widget and resume when it clears. For now, return the URL and a
        # short wait loop so the user has ~3 min to interact.
        # Same default the TS HumanInputManager uses; both branches honor the
        # env override so a channels gateway can grant humans more time.
        ask_timeout_ms = int(os.environ.get("SUPERBROWSER_ASK_TIMEOUT_MS", str(5 * 60 * 1000)))

        if self.s.backend == "t3":
            try:
                from superbrowser_bridge.antibot import t3_viewer as _v
                from superbrowser_bridge.antibot import captcha as _cap
                from superbrowser_bridge.antibot import interactive_session as _t3mgr

                await _v.ensure_started()
                view = _v.view_url(session_id)
                print(f"\n[HUMAN HANDOFF — t3] Open {view} in your browser.")
                await self._notify_assist(session_id, question, input_type, view, "t3", ask_timeout_ms)
                # Poll every 3s for the captcha to clear.
                mgr = _t3mgr.default()
                import asyncio as _asyncio
                import time as _time
                deadline = _time.time() + ask_timeout_ms / 1000
                cleared = False
                while _time.time() < deadline:
                    await _asyncio.sleep(3.0)
                    try:
                        info = await _cap.detect(mgr, session_id)
                    except Exception:
                        continue
                    if not info.present:
                        cleared = True
                        break
                if cleared:
                    return (
                        f"[human_handoff_cleared] Captcha / verification "
                        f"cleared via human at {view}. Resuming."
                    )
                return (
                    f"[human_handoff_timeout] No state change detected after "
                    f"{ask_timeout_ms // 60000} min at {view}. You can call "
                    f"browser_ask_user again or proceed with done(success=False)."
                )
            except Exception as exc:
                print(f"[t3 human handoff error: {exc}]")
                return f"[browser_ask_user_t3_error: {exc}. Cannot proceed.]"

        # Map nanobot-side hint to the TS server's HumanInputType. Default
        # 'text' is the safest — it accepts free-form replies and the UI's
        # "Done" button also works against it.
        valid_types = {
            "credentials", "captcha", "confirmation", "otp", "card", "text", "choice",
        }
        ht = (input_type or "text").lower()
        if ht not in valid_types:
            ht = "text"

        # Capture a screenshot to include in the request payload so any UI
        # listener (not just the live-view poller) can show what page the
        # agent is stuck on. Best-effort.
        screenshot_b64 = None
        try:
            sr = await _request_with_backoff(
                "GET",
                f"{SUPERBROWSER_URL}/session/{session_id}/state",
                params={"vision": "true"},
                timeout=10.0,
            )
            sr.raise_for_status()
            sdata = sr.json()
            screenshot_b64 = sdata.get("screenshot") or None
        except Exception:
            screenshot_b64 = None

        # View URL for the user — the concrete surface where they interact.
        public_host = os.environ.get(
            "SUPERBROWSER_PUBLIC_HOST", SUPERBROWSER_URL.rstrip("/"),
        )
        view_url = f"{public_host}/session/{session_id}/view"
        message = (
            f"{question}\n\n"
            f"To respond: open {view_url} in your browser. "
            f"Either interact with the page (for captchas) or click the "
            f"'Done' button when finished."
        )

        # The TS server holds the HTTP connection open until the user replies
        # or the timer fires, so client-side we just wait.
        timeout_ms = ask_timeout_ms
        self.s.record_step(
            "browser_ask_user",
            f"type={ht}",
            f"view_url={view_url}",
        )
        # Chat-side notification (gateway webhook; banner fallback) so
        # Python-initiated asks reach the user like TS captcha handoffs do.
        await self._notify_assist(
            session_id, question, ht, view_url, "t1", timeout_ms, screenshot_b64=screenshot_b64
        )
        try:
            async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 10) as client:
                r = await client.post(
                    f"{SUPERBROWSER_URL}/session/{session_id}/human-input/ask",
                    json={
                        "type": ht,
                        "message": message,
                        "screenshot": screenshot_b64,
                        "timeout": timeout_ms,
                    },
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            return (
                f"[browser_ask_user error: {exc}. "
                f"User was not asked. Continue without their input "
                f"or call again.]"
            )

        if data.get("timedOut"):
            return (
                f"[User did not respond within {timeout_ms // 60000} minutes. "
                f"Proceed without their input or call done(success=False).]"
            )

        response = data.get("response") or {}
        if response.get("cancelled"):
            return "[User cancelled the request. Proceed accordingly.]"

        payload = response.get("data") or {}
        if not payload:
            return "[User responded but provided no data.]"

        # Flatten the reply dict into a short readable string for the model.
        parts = [f"{k}: {v}" for k, v in payload.items()]
        return f"[User replied] {' | '.join(parts)}"


@tool_parameters(
    tool_parameters_schema(
        reason=StringSchema(
            "Why you're stuck. Be specific: 'element_covered by cookie banner I can't dismiss', "
            "'captcha solve failed 3 times', 'selector index keeps shifting'."
        ),
        failed_tactics=StringSchema(
            "Comma-separated list of tactics you already tried. E.g., "
            "'click [5] twice, scroll-and-retry, switch to XPath selector'."
        ),
        required=["reason", "failed_tactics"],
    )
)
class BrowserRequestHelpTool(Tool):
    """Escape hatch: worker signals 'I'm stuck' with structured context.

    Writes a resumption artifact so the orchestrator can spin up a new
    worker that RESUMES on the same live Puppeteer session with a
    different tactic — instead of starting from scratch.

    The worker should call `done(success=False, final_answer=...)` on the
    next turn after calling this tool.
    """

    name = "browser_request_help"
    description = (
        "Call this when you're stuck and a fresh tactic is needed. "
        "Writes structured state so the orchestrator can delegate a "
        "SUCCESSOR worker that resumes on the SAME live browser session "
        "with knowledge of what failed. "
        "After calling this tool, call done(success=False) with a short "
        "explanation — do NOT keep trying the same tactics."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, reason: str, failed_tactics: str, **kw: Any) -> str:
        # Lazy-import to avoid circular imports with the orchestrator module.
        from superbrowser_bridge.routing import _domain_from_url
        domain = _domain_from_url(self.s.current_url) if self.s.current_url else ""
        saved = save_resumption_artifact(
            self.s, domain,
            help_reason=reason,
            help_failed_tactics=failed_tactics,
        )
        self.s.record_step(
            "browser_request_help",
            reason[:80],
            f"saved={saved} session={self.s.session_id}",
        )
        hint = (
            "[HELP REQUESTED] Resumption state saved. "
            "Now call done(success=False, final_answer='Need different tactic: ...') "
            "with a ≤30-word summary. "
            "The orchestrator will delegate a fresh worker that resumes on this "
            "same browser session with your failed tactics excluded."
        ) if saved else (
            "[HELP REQUEST NOT SAVED] session_id or current_url is empty — "
            "resumption artifact could not be written. Proceed with done(success=False) "
            "and explain the blocker in final_answer."
        )
        return hint
