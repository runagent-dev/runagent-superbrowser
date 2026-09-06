"""Assist orchestration: create → notify → poll detector → resolve.

Poll-based on purpose (NOT a held HTTP connection): a 10-minute login must
survive proxy hiccups and engine restarts of unrelated subsystems. Reminders
re-fire the webhook at 50% and 80% of the timeout — the view URL is
session-stable, so a "re-issued link" is simply a re-notification.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable

from .detectors import POLL_INTERVAL_S
from .emitter import build_payload, notify_gateway, print_assist_banner
from .models import HumanAssistRequest
from .store import AssistStore, default_store


async def open_and_notify(
    request: HumanAssistRequest,
    *,
    store: AssistStore | None = None,
    screenshot_b64: str | None = None,
    reply_hint: dict[str, Any] | None = None,
) -> HumanAssistRequest:
    """Persist the request and notify the gateway (banner fallback)."""
    store = store or default_store()
    store.save(request)
    delivered = await notify_gateway(
        build_payload(request, screenshot_b64=screenshot_b64, reply_hint=reply_hint)
    )
    if delivered:
        request.transition("notified")
        request.notify_count += 1
    else:
        print_assist_banner(request)
    store.save(request)
    return request


async def wait_for_resolution(
    request: HumanAssistRequest,
    poll: Callable[[], Awaitable[dict[str, Any] | None]],
    *,
    store: AssistStore | None = None,
    poll_interval_s: float | None = None,
) -> HumanAssistRequest:
    """Poll until the detector resolves or the request expires. Sends webhook
    reminders at 50% / 80% of the window, and a resolved/expired event at the
    end so chats aren't left with a dead link."""
    store = store or default_store()
    interval = poll_interval_s or POLL_INTERVAL_S
    reminded: set[int] = set()
    total = max(request.expires_at - request.created_at, 1.0)

    while not request.expired:
        await asyncio.sleep(interval)
        try:
            outcome = await poll()
        except Exception:  # noqa: BLE001 - a flaky probe must not kill the wait
            outcome = None
        if outcome is not None:
            request.resolve(outcome.get("how", "detector"), outcome)
            store.save(request)
            await notify_gateway(
                build_payload(
                    request,
                    event="human_assist_resolved",
                    caption=f"Resolved: {request.question}",
                )
            )
            return request

        fraction = (time.time() - request.created_at) / total
        for mark in (50, 80):
            if fraction >= mark / 100 and mark not in reminded:
                reminded.add(mark)
                remaining_min = max(1, int((request.expires_at - time.time()) // 60))
                delivered = await notify_gateway(
                    build_payload(
                        request,
                        event="human_assist_reminder",
                        caption=(
                            f"Reminder: still waiting — {request.question} "
                            f"({remaining_min} min left) {request.view_url}"
                        ),
                    )
                )
                if delivered:
                    request.notify_count += 1
                    store.save(request)

    request.transition("expired")
    store.save(request)
    await notify_gateway(
        build_payload(request, event="human_assist_expired", caption=f"Expired: {request.question}")
    )
    return request


def view_url_for(session_id: str, tier: str) -> str:
    """The live-view link for a session, honoring SUPERBROWSER_PUBLIC_HOST."""
    if tier == "t3":
        try:
            from ..antibot import t3_viewer

            return t3_viewer.view_url(session_id)
        except Exception:  # noqa: BLE001 - fall through to the T1-style URL
            pass
    from ..session_tools.http_client import SUPERBROWSER_URL

    public_host = os.environ.get("SUPERBROWSER_PUBLIC_HOST", SUPERBROWSER_URL.rstrip("/"))
    return f"{public_host}/session/{session_id}/view"
