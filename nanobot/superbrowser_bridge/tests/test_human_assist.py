"""Human-assist framework tests: models, store, emitter, detector, service."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from superbrowser_bridge.human_assist import emitter, service
from superbrowser_bridge.human_assist.detectors import LoginDetector
from superbrowser_bridge.human_assist.models import HumanAssistRequest
from superbrowser_bridge.human_assist.store import AssistStore


# ----- models -----


def test_state_machine_transitions() -> None:
    r = HumanAssistRequest(type="login", session_id="s1", view_url="http://v", question="log in")
    assert r.state == "pending"
    assert r.transition("notified") is True
    assert r.transition("active") is True
    assert r.resolve("user_done", {"x": 1}) is True
    assert r.state == "resolved"
    # terminal is sticky
    assert r.transition("active") is False
    assert r.resolution["how"] == "user_done"


def test_bad_transition_rejected() -> None:
    r = HumanAssistRequest(type="captcha", session_id="s", view_url="v", question="q")
    r.resolve("detector")
    assert r.transition("notified") is False


def test_to_from_dict_round_trip() -> None:
    r = HumanAssistRequest(
        type="otp", session_id="s2", view_url="http://v", question="code?", domain="x.com", tier="t1"
    )
    r.transition("notified")
    restored = HumanAssistRequest.from_dict(r.to_dict())
    assert restored.id == r.id
    assert restored.state == "notified"
    assert restored.domain == "x.com"


# ----- store -----


def test_store_save_load_and_dedupe(tmp_path: Path) -> None:
    store = AssistStore(root=tmp_path)
    r = HumanAssistRequest(type="login", session_id="s1", view_url="v", question="q", domain="a.com")
    store.save(r)
    assert store.load(r.id).id == r.id
    assert [x.id for x in store.list()] == [r.id]

    dup = store.recent_duplicate(session_id="s1", domain="a.com", assist_type="login")
    assert dup is not None and dup.id == r.id
    assert store.recent_duplicate(session_id="s1", domain="a.com", assist_type="captcha") is None
    assert store.recent_duplicate(session_id="other", domain="a.com", assist_type="login") is None


def test_store_expired_not_deduped(tmp_path: Path) -> None:
    store = AssistStore(root=tmp_path)
    r = HumanAssistRequest(type="login", session_id="s", view_url="v", question="q", domain="a.com")
    r.transition("expired")
    store.save(r)
    assert store.recent_duplicate(session_id="s", domain="a.com", assist_type="login") is None


def test_store_atomic_write_permissions(tmp_path: Path) -> None:
    import stat

    store = AssistStore(root=tmp_path / "nested")
    r = HumanAssistRequest(type="text", session_id="s", view_url="v", question="q")
    store.save(r)
    path = tmp_path / "nested" / f"{r.id}.json"
    assert stat.S_IMODE((tmp_path / "nested").stat().st_mode) == 0o700
    assert json.loads(path.read_text())["id"] == r.id


# ----- emitter -----


def test_build_payload_v2_fields() -> None:
    r = HumanAssistRequest(type="login", session_id="s1", view_url="http://v", question="log in", tier="t1")
    payload = emitter.build_payload(
        r, screenshot_b64="Zm9v", reply_hint={"humanInputUrl": "http://h", "expectsText": False}
    )
    assert payload["event"] == "human_assist_required"
    assert payload["assistType"] == "login"
    assert payload["requestId"] == r.id
    assert payload["tier"] == "t1"
    assert payload["url"] == "http://v"  # legacy field preserved
    assert payload["screenshot"] == "Zm9v"
    assert payload["replyHint"]["humanInputUrl"] == "http://h"


def test_notify_gateway_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HANDOFF_WEBHOOK_URL", "http://hook.local/handoff")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))

    delivered = asyncio.run(emitter.notify_gateway({"event": "x"}))
    assert delivered is True
    assert calls["n"] == 2


def test_notify_gateway_no_url_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HANDOFF_WEBHOOK_URL", raising=False)
    assert asyncio.run(emitter.notify_gateway({"event": "x"})) is False


# ----- LoginDetector -----


def test_login_detector_user_done_wins() -> None:
    async def main():
        detector = LoginDetector(
            start_url="http://x.com/login",
            get_url=lambda: _val("http://x.com/login"),  # still on login
            has_password_field=lambda: _val(True),
            pending_done=lambda: _val(True),  # but Done was clicked
        )
        return await detector.poll()

    result = asyncio.run(main())
    assert result == {"how": "user_done"}


def test_login_detector_heuristic_needs_two_polls() -> None:
    async def main():
        detector = LoginDetector(
            start_url="http://x.com/login",
            get_url=lambda: _val("http://x.com/dashboard"),  # moved off login
            has_password_field=lambda: _val(False),          # no password field
        )
        first = await detector.poll()
        second = await detector.poll()
        return first, second

    first, second = asyncio.run(main())
    assert first is None  # debounced
    assert second == {"how": "heuristic", "url": "http://x.com/dashboard"}


def test_login_detector_password_present_resets_streak() -> None:
    async def main():
        pw = [False, True, False, False]
        idx = {"i": 0}

        async def has_pw():
            v = pw[idx["i"]]
            idx["i"] += 1
            return v

        detector = LoginDetector(
            start_url="http://x.com/login",
            get_url=lambda: _val("http://x.com/home"),
            has_password_field=has_pw,
        )
        return [await detector.poll() for _ in range(4)]

    results = asyncio.run(main())
    # poll0: no pw -> streak1 (None); poll1: pw -> reset; poll2: no pw -> streak1;
    # poll3: no pw -> streak2 -> resolves
    assert results[0] is None and results[1] is None and results[2] is None
    assert results[3] == {"how": "heuristic", "url": "http://x.com/home"}


def test_login_detector_stays_on_login_page() -> None:
    async def main():
        detector = LoginDetector(
            start_url="http://x.com/login",
            get_url=lambda: _val("http://x.com/signin"),  # still an auth URL
            has_password_field=lambda: _val(False),
        )
        return [await detector.poll() for _ in range(3)]

    assert asyncio.run(main()) == [None, None, None]


# ----- service.wait_for_resolution -----


def test_wait_for_resolution_resolves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HANDOFF_WEBHOOK_URL", raising=False)
    store = AssistStore(root=tmp_path)
    r = HumanAssistRequest(type="login", session_id="s", view_url="v", question="q", timeout_s=5)

    async def main():
        polls = {"n": 0}

        async def poll():
            polls["n"] += 1
            return {"how": "user_done"} if polls["n"] >= 2 else None

        return await service.wait_for_resolution(r, poll, store=store, poll_interval_s=0.01)

    resolved = asyncio.run(main())
    assert resolved.state == "resolved"
    assert store.load(r.id).state == "resolved"


def test_wait_for_resolution_expires(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HANDOFF_WEBHOOK_URL", raising=False)
    store = AssistStore(root=tmp_path)
    r = HumanAssistRequest(type="login", session_id="s", view_url="v", question="q")
    r.expires_at = time.time() + 0.05  # expire almost immediately

    async def main():
        async def poll():
            return None

        return await service.wait_for_resolution(r, poll, store=store, poll_interval_s=0.01)

    resolved = asyncio.run(main())
    assert resolved.state == "expired"


async def _val(value):
    return value
