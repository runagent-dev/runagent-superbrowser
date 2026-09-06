"""Human-assist tools: login handoff, approval gate, remember/forget site.

`browser_login_handoff` is the "log me into this site" flow: link to the
user's chat (via the human-assist emitter) + Done button in the live view,
resolved by the LoginDetector (Done > chat reply > URL+password heuristic),
then the full-session identity is captured so the login persists across
sessions (identity jar, ``SUPERBROWSER_IDENTITY_JAR=1``).

Runs on the session's CURRENT tier — the T1 live view is click+type
interactive (verified), and T3 persists logins via its Chrome profile anyway.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema

from ...human_assist.detectors import LoginDetector, make_t1_probes, make_t3_probes
from ...human_assist.models import HumanAssistRequest
from ...human_assist.service import open_and_notify, view_url_for, wait_for_resolution
from ...human_assist.store import default_store
from ..http_client import SUPERBROWSER_URL, _request_with_backoff
from ..state import BrowserSessionState

_YES_WORDS = ("yes", "y", "approve", "approved", "ok", "confirm", "go ahead", "do it")


def _login_timeout_s() -> float:
    return int(os.environ.get("SUPERBROWSER_LOGIN_HANDOFF_TIMEOUT_MS", "600000")) / 1000.0


def _ask_timeout_s() -> float:
    return int(os.environ.get("SUPERBROWSER_ASK_TIMEOUT_MS", "300000")) / 1000.0


def _domain_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname or ""
    except ValueError:
        return ""


async def _grab_screenshot_b64(session_id: str) -> str | None:
    try:
        resp = await _request_with_backoff(
            "GET",
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            params={"vision": "true"},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json().get("screenshot") or None
    except Exception:  # noqa: BLE001
        return None


def _reply_hint(session_id: str, *, expects_text: bool) -> dict[str, Any]:
    return {
        # Internal URL on purpose: the gateway sits next to the engine and
        # answers on the user's behalf via POST /session/:id/human-input.
        "humanInputUrl": f"{SUPERBROWSER_URL}/session/{session_id}/human-input",
        "expectsText": expects_text,
    }


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        site=StringSchema("Site being logged into (informational)", nullable=True),
        instructions=StringSchema("Extra instructions for the human (e.g. which account)", nullable=True),
        required=["session_id"],
    )
)
class BrowserLoginHandoffTool(Tool):
    name = "browser_login_handoff"
    description = (
        "Hand the browser to the human so THEY log in (never type credentials "
        "yourself unless the user gave them). Sends the live-view link to the "
        "user's chat, waits up to 10 minutes, auto-detects login completion "
        "(Done button, chat reply, or URL/password heuristics), then saves the "
        "session identity so future tasks skip the login. Returns "
        "[login_complete] / [login_timeout] sentinels."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(
        self,
        session_id: str,
        site: str | None = None,
        instructions: str | None = None,
        **kw: Any,
    ) -> str:
        tier = "t3" if self.s.backend == "t3" else "t1"
        domain = _domain_of(self.s.current_url) or (site or "")
        store = default_store()

        duplicate = store.recent_duplicate(session_id=session_id, domain=domain, assist_type="login")
        if duplicate is not None and not duplicate.expired and duplicate.state in ("pending", "notified", "active"):
            return (
                f"[login_handoff_already_open] A login handoff for {domain or 'this site'} is already "
                f"waiting at {duplicate.view_url} (request {duplicate.id}). Do not open another."
            )

        question = f"Please log into {site or domain or 'the site'} in the live browser view."
        if instructions:
            question = f"{question} {instructions}"
        request = HumanAssistRequest(
            type="login",
            session_id=session_id,
            view_url=view_url_for(session_id, tier),
            question=question,
            domain=domain,
            task_id=self.s.task_id or "",
            tier=tier,
            page_url=self.s.current_url or "",
            timeout_s=_login_timeout_s(),
        )
        screenshot = await _grab_screenshot_b64(session_id) if tier == "t1" else None
        await open_and_notify(
            request,
            store=store,
            screenshot_b64=screenshot,
            reply_hint=_reply_hint(session_id, expects_text=False) if tier == "t1" else None,
        )
        self.s.record_step("browser_login_handoff", domain or "login", f"view={request.view_url}")

        # T1: park a human-input request so the live view shows the instruction
        # banner + Done button. Fire-and-forget — the DETECTOR is what resumes
        # us (a held connection would be fragile over a 10-minute login).
        ask_task: asyncio.Task | None = None
        if tier == "t1":
            ask_task = asyncio.create_task(self._park_ask(session_id, question, request.timeout_s))

        probes = make_t1_probes(session_id) if tier == "t1" else make_t3_probes(session_id)
        detector = LoginDetector(
            start_url=self.s.current_url or "",
            get_url=probes["get_url"],
            has_password_field=probes["has_password_field"],
            pending_done=probes.get("pending_done"),
        )
        try:
            resolved = await wait_for_resolution(request, detector.poll, store=store)
        finally:
            if ask_task is not None:
                ask_task.cancel()

        if resolved.state == "resolved":
            identity_note = await self._capture_identity(session_id, tier)
            how = (resolved.resolution or {}).get("how", "detector")
            return (
                f"[login_complete] Human finished logging in ({how}). {identity_note} "
                f"Resume the task — do NOT navigate back to the login page."
            )
        return (
            f"[login_timeout] No login detected within {int(request.timeout_s // 60)} min "
            f"(link was {request.view_url}). Ask the user to confirm, retry once, or call "
            f"done(success=False)."
        )

    async def _park_ask(self, session_id: str, question: str, timeout_s: float) -> None:
        try:
            async with httpx.AsyncClient(timeout=timeout_s + 10) as client:
                await client.post(
                    f"{SUPERBROWSER_URL}/session/{session_id}/human-input/ask",
                    json={
                        "type": "text",
                        "message": f"{question} Click 'Done' here when finished.",
                        "timeout": int(timeout_s * 1000),
                    },
                )
        except Exception:  # noqa: BLE001 - the detector is the real resume path
            pass

    async def _capture_identity(self, session_id: str, tier: str) -> str:
        if tier == "t3":
            return "T3 profile persists the login automatically."
        try:
            resp = await _request_with_backoff(
                "POST", f"{SUPERBROWSER_URL}/session/{session_id}/identity/save", json={}, timeout=15.0
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("saved"):
                    return f"Saved identity for {data.get('domain')} ({data.get('cookieCount')} cookies)."
                return "Identity jar disabled (set SUPERBROWSER_IDENTITY_JAR=1 to persist logins)."
        except Exception:  # noqa: BLE001
            pass
        return "Identity capture unavailable (older engine build)."


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        summary=StringSchema("What you are about to do, in one sentence (e.g. 'Place the $214 order')"),
        screenshot=BooleanSchema(description="Attach a screenshot of the current page", nullable=True),
        required=["session_id", "summary"],
    )
)
class BrowserRequestApprovalTool(Tool):
    name = "browser_request_approval"
    description = (
        "Ask the human to approve an irreversible action (checkout, payment, "
        "submit, delete) and BLOCK until they answer yes/no. Returns "
        "[approved] or [rejected]/[approval_timeout]. Use BEFORE the final "
        "click of anything that spends money or can't be undone."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, summary: str, screenshot: bool | None = True, **kw: Any) -> str:
        tier = "t3" if self.s.backend == "t3" else "t1"
        request = HumanAssistRequest(
            type="approval",
            session_id=session_id,
            view_url=view_url_for(session_id, tier),
            question=f"Approve? {summary} (reply yes/no)",
            domain=_domain_of(self.s.current_url),
            task_id=self.s.task_id or "",
            tier=tier,
            page_url=self.s.current_url or "",
            timeout_s=_ask_timeout_s(),
        )
        shot = await _grab_screenshot_b64(session_id) if (screenshot and tier == "t1") else None
        store = default_store()
        await open_and_notify(
            request,
            store=store,
            screenshot_b64=shot,
            reply_hint=_reply_hint(session_id, expects_text=True) if tier == "t1" else None,
        )
        self.s.record_step("browser_request_approval", summary[:60], f"view={request.view_url}")

        if tier != "t1":
            request.transition("cancelled")
            store.save(request)
            return (
                "[approval_unavailable] Approval prompts need a tier-1 session (chat reply relay). "
                "State what you were about to do and call done(success=False) asking the user to confirm."
            )

        timeout_s = request.timeout_s
        try:
            async with httpx.AsyncClient(timeout=timeout_s + 10) as client:
                resp = await client.post(
                    f"{SUPERBROWSER_URL}/session/{session_id}/human-input/ask",
                    json={
                        "type": "confirmation",
                        "message": request.question,
                        "screenshot": shot,
                        "timeout": int(timeout_s * 1000),
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            request.transition("cancelled")
            store.save(request)
            return f"[approval_error: {exc}] Could not reach the human. Do NOT proceed with the action."

        if data.get("timedOut"):
            request.transition("expired")
            store.save(request)
            return "[approval_timeout] No answer. Do NOT proceed; call done(success=False) explaining why."

        payload = (data.get("response") or {}).get("data") or {}
        text = " ".join(str(v) for v in payload.values()).strip().lower()
        approved = (payload.get("confirmed") is True) or any(word in text for word in _YES_WORDS)
        request.resolve("chat_reply", {"approved": approved, "text": text})
        store.save(request)
        return "[approved] Proceed with the action." if approved else "[rejected] Do NOT proceed. Ask what to change."


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session ID"),
        required=["session_id"],
    )
)
class BrowserRememberSiteTool(Tool):
    name = "browser_remember_site"
    description = (
        "Persist the current site's login (full cookie snapshot) so future "
        "tasks start already signed in. Use after a successful login. "
        "Requires SUPERBROWSER_IDENTITY_JAR=1; T3 sessions persist via their "
        "Chrome profile automatically."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, session_id: str, **kw: Any) -> str:
        if self.s.backend == "t3":
            return "[remembered] T3 sessions persist logins in their Chrome profile automatically."
        try:
            resp = await _request_with_backoff(
                "POST", f"{SUPERBROWSER_URL}/session/{session_id}/identity/save", json={}, timeout=15.0
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return f"[remember_failed: {exc}]"
        if data.get("saved"):
            return f"[remembered] {data.get('domain')} ({data.get('cookieCount')} cookies)."
        return "[not_saved] Identity jar is disabled — set SUPERBROWSER_IDENTITY_JAR=1."


@tool_parameters(
    tool_parameters_schema(
        domain=StringSchema("Domain to forget, e.g. example.com"),
        required=["domain"],
    )
)
class BrowserForgetSiteTool(Tool):
    name = "browser_forget_site"
    description = (
        "Delete everything remembered about a domain: saved identity cookies, "
        "the T3 Chrome profile, and bot-protection jar entries. Use when the "
        "user says 'forget my login on X' or a saved identity misbehaves."
    )

    def __init__(self, state: BrowserSessionState):
        self.s = state

    async def execute(self, domain: str, **kw: Any) -> str:
        import shutil
        from pathlib import Path

        removed: list[str] = []
        # NOTE: strip a leading "www." PREFIX only — str.lstrip("www.") would
        # strip any leading w/. characters ("wine.com" -> "ine.com").
        safe = domain.strip().lower()
        if safe.startswith("www."):
            safe = safe[4:]
        try:
            resp = await _request_with_backoff(
                "DELETE", f"{SUPERBROWSER_URL}/identity/{safe}", timeout=10.0
            )
            if resp.status_code == 200 and resp.json().get("deleted"):
                removed.append("identity cookies")
        except Exception:  # noqa: BLE001
            pass

        sanitized = "".join(c if (c.isalnum() or c in "._-") else "_" for c in safe)
        profile_root = Path(os.environ.get("T3_PROFILE_ROOT", "~/.superbrowser/profiles")).expanduser()
        profile_dir = profile_root / sanitized
        if profile_dir.is_dir():
            shutil.rmtree(profile_dir, ignore_errors=True)
            removed.append("T3 profile")

        jar_dir = Path(
            os.environ.get("SUPERBROWSER_COOKIE_JAR_PATH", "~/.superbrowser/cookie-jar")
        ).expanduser()
        jar_file = jar_dir / f"{sanitized}.json"
        if jar_file.is_file():
            jar_file.unlink(missing_ok=True)
            removed.append("bot-protection cookies")

        self.s.record_step("browser_forget_site", safe, ",".join(removed) or "nothing found")
        if removed:
            return f"[forgotten] {safe}: removed {', '.join(removed)}."
        return f"[nothing_to_forget] No stored state found for {safe}."
