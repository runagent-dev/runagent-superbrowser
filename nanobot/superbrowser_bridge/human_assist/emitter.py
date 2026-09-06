"""Webhook notifications to the channels gateway (+ terminal banner fallback).

Posts payload v2 to ``HANDOFF_WEBHOOK_URL`` — the same env the TS captcha
ladder fires — so Python-initiated asks (login handoffs, OTP prompts,
approval gates) reach the user's chat exactly like TS captcha handoffs do.
Payload v2 keeps every legacy field and adds ``assistType`` / ``requestId`` /
``tier`` / ``replyHint`` so the gateway can answer on the user's behalf.

Failures never block the ask: 2 retries, then the unmissable stderr banner
(terminal users always have a fallback).
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from .models import HumanAssistRequest


def build_payload(
    request: HumanAssistRequest,
    *,
    event: str = "human_assist_required",
    caption: str | None = None,
    screenshot_b64: str | None = None,
    reply_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    minutes = max(1, int(request.timeout_s // 60))
    default_caption = (
        f"{request.question} — tap {request.view_url} ({minutes} min)."
        if request.view_url
        else request.question
    )
    payload: dict[str, Any] = {
        # legacy field set (matches human-handoff.ts) first:
        "event": event,
        "url": request.view_url,
        "sessionId": request.session_id,
        "taskId": request.task_id or os.environ.get("SUPERBROWSER_TASK_ID"),
        "pageUrl": request.page_url,
        "pageTitle": "",
        "timeoutMs": int(request.timeout_s * 1000),
        "caption": caption or default_caption,
        # payload v2 additions:
        "assistType": request.type,
        "requestId": request.id,
        "tier": request.tier,
    }
    if screenshot_b64:
        payload["screenshot"] = screenshot_b64
        payload["screenshotMimeType"] = "image/jpeg"
    if reply_hint:
        payload["replyHint"] = reply_hint
    return payload


async def notify_gateway(payload: dict[str, Any]) -> bool:
    """POST to HANDOFF_WEBHOOK_URL (comma-separated list allowed). True when
    at least one receiver acknowledged."""
    urls = [u.strip() for u in (os.environ.get("HANDOFF_WEBHOOK_URL") or "").split(",") if u.strip()]
    if not urls:
        return False
    delivered = False
    try:
        import httpx
    except ImportError:
        return False
    async with httpx.AsyncClient(timeout=5.0) as client:
        for url in urls:
            for attempt in range(3):
                try:
                    resp = await client.post(url, json=payload)
                    if 200 <= resp.status_code < 300:
                        delivered = True
                        break
                except Exception:  # noqa: BLE001 - webhook down; retry then banner
                    pass
                await asyncio.sleep(2.0 * attempt)
    return delivered


def print_assist_banner(request: HumanAssistRequest) -> None:
    """The terminal fallback — same spirit as the TS printHandoffBanner."""
    line = "=" * 70
    minutes = max(1, int(request.timeout_s // 60))
    sys.stderr.write(
        f"\n{line}\n"
        f"  HUMAN NEEDED [{request.type}] — {request.question}\n"
        f"  Open: {request.view_url or '(no view URL — set SUPERBROWSER_PUBLIC_HOST)'}\n"
        f"  Waiting up to {minutes} min. (request {request.id})\n"
        f"{line}\n"
    )
    sys.stderr.flush()
