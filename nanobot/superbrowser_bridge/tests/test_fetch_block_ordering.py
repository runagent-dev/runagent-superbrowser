"""Engine ladder + block-escalation ordering for antibot.fetch.fetch.

Verifies that (a) a blocked engine escalates to the next, (b) the accepted
body is the clean one, and (c) a block stub is NEVER turned into "clean"
markdown (extraction runs only on the non-blocked body).

    source venv/bin/activate && \
        python -m pytest nanobot/superbrowser_bridge/tests/test_fetch_block_ordering.py
"""

from __future__ import annotations

import asyncio

from superbrowser_bridge.antibot import fetch as F

_CF_STUB = (
    "<html><body>Just a moment... Checking your browser before accessing. "
    "cf-browser-verification</body></html>"
)
_CLEAN = (
    "<html><head><title>Real</title></head><body><article>"
    "<p>This is the genuine article body with plenty of real words to extract "
    "cleanly and meaningfully for the reader right here and now.</p>"
    "</article></body></html>"
)


class _NoopRate:
    async def wait_if_needed(self, url):  # noqa: D401
        pass

    def observe(self, url, status):
        return True


def _patch(monkeypatch, engine_results):
    async def fake_run_engine(engine, url, *, timeout_s, headless):
        return engine_results[engine]

    monkeypatch.setattr(F, "_run_engine", fake_run_engine)
    monkeypatch.setattr(F._rl, "default", lambda: _NoopRate())
    # Don't write to the real routing ledger from a unit test.
    monkeypatch.setattr(F, "_record_routing", lambda *a, **k: None)


def test_escalates_past_block_to_clean_engine(monkeypatch):
    _patch(monkeypatch, {
        "plain": {"html": _CF_STUB, "status": 200, "tier_used": 1,
                  "block_class": "cloudflare", "reason": "CF challenge", "elapsed_ms": 1},
        "impersonate": {"html": _CLEAN, "status": 200, "tier_used": 2,
                        "block_class": "", "reason": "", "elapsed_ms": 1},
    })
    res = asyncio.run(F.fetch("https://example.com", max_tier=2))
    assert res["engine_used"] == "impersonate"
    assert [a["engine"] for a in res["attempts"]] == ["plain", "impersonate"]
    # The clean article is extracted...
    assert "genuine article body" in res["fit_markdown"]
    # ...and the CF stub was NEVER turned into clean markdown.
    assert "Just a moment" not in res["fit_markdown"]
    assert "Just a moment" not in res["raw_markdown"]


def test_block_recorded_on_first_attempt(monkeypatch):
    _patch(monkeypatch, {
        "plain": {"html": _CF_STUB, "status": 200, "tier_used": 1,
                  "block_class": "cloudflare", "reason": "CF challenge", "elapsed_ms": 1},
        "impersonate": {"html": _CLEAN, "status": 200, "tier_used": 2,
                        "block_class": "", "reason": "", "elapsed_ms": 1},
    })
    res = asyncio.run(F.fetch("https://example.com", max_tier=2))
    first = res["attempts"][0]
    assert first["engine"] == "plain" and first["block_class"] == "cloudflare"


def test_all_blocked_returns_empty_fit(monkeypatch):
    blocked = {"html": _CF_STUB, "status": 200, "tier_used": 1,
               "block_class": "cloudflare", "reason": "CF", "elapsed_ms": 1}
    _patch(monkeypatch, {"plain": blocked, "impersonate": blocked})
    res = asyncio.run(F.fetch("https://example.com", max_tier=2))
    assert res["engine_used"] is None
    assert res["fit_markdown"] == ""
    assert res["block_class"] == "cloudflare"


def test_jina_markdown_folds_into_result(monkeypatch):
    _patch(monkeypatch, {
        "plain": {"html": _CF_STUB, "status": 200, "tier_used": 1,
                  "block_class": "cloudflare", "reason": "CF", "elapsed_ms": 1},
        "impersonate": {"html": _CF_STUB, "status": 200, "tier_used": 2,
                        "block_class": "cloudflare", "reason": "CF", "elapsed_ms": 1},
        "browser": {"html": _CF_STUB, "status": 200, "tier_used": 3,
                    "block_class": "cloudflare", "reason": "CF", "elapsed_ms": 1},
        "jina": {"markdown": "# Title\n\nClean jina markdown body with a [link](/x).",
                 "status": 200, "tier_used": 3, "engine": "jina", "source": "jina",
                 "block_class": "", "reason": "", "elapsed_ms": 1, "final_url": "https://example.com"},
        "archive": {"html": "", "status": 0, "tier_used": 4, "block_class": "empty",
                    "reason": "", "elapsed_ms": 1},
    })
    res = asyncio.run(F.fetch("https://example.com", max_tier=4))
    assert res["engine_used"] == "jina"
    assert "Clean jina markdown body" in res["fit_markdown"]


if __name__ == "__main__":
    # Minimal monkeypatch shim so the file runs without pytest too.
    class _MP:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, val):
            old = getattr(obj, name)
            self._undo.append((obj, name, old))
            setattr(obj, name, val)

        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)
            self._undo.clear()

    for fname, fn in sorted(globals().items()):
        if fname.startswith("test_") and callable(fn):
            mp = _MP()
            try:
                fn(mp)
                print("ok", fname)
            finally:
                mp.undo()
