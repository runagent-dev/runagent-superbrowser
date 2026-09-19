"""The vision cache must not learn anything from an unpainted frame.

This is the half of the blank-capture fix that turns a transient fault
into a permanent one. The cache key is built from the DOM (`dom_hash` /
`dom_text_hash`) and never from the image. A component that re-renders
on input commits its DOM one frame before it paints, so a capture
sampled in that window carries the SAME key the settled page is about to
present. Store its empty bboxes and they keep being served long after
the page is visibly fine.

The pre-existing `screenshot_freshness` gate cannot catch this: freshness
is the vision model's own opinion of the image, and a model handed an
empty page will happily call it fresh. Both tests below therefore return
`screenshot_freshness: "fresh"` — the point is that `cacheable=False`
overrides it.

Run:
    source venv/bin/activate && \
        PYTHONPATH=nanobot python -m pytest \
        nanobot/superbrowser_bridge/tests/test_blank_frame_cache.py -q
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest

from vision_agent.client import VisionAgent
from vision_agent.providers.base import ProviderResponse


class _Cache:
    """Stand-in for VisionCache that records what was stored."""

    def __init__(self) -> None:
        self.puts: list[Any] = []

    async def get(self, key: Any) -> None:
        return None

    async def put(self, key: Any, value: Any) -> None:
        self.puts.append(key)


class _Provider:
    name = "stub"
    model = "stub-1"

    async def chat_with_image(self, **kw: Any) -> ProviderResponse:
        body = {
            "summary": "a page",
            "screenshot_freshness": "fresh",
            "bboxes": [],
        }
        return ProviderResponse(
            text=json.dumps(body), tokens_used=1,
            model=self.model, provider=self.name,
        )


def _analyze(*, cacheable: bool) -> _Cache:
    cache = _Cache()
    agent = VisionAgent(_Provider(), cache)  # type: ignore[arg-type]
    asyncio.run(agent.analyze(
        screenshot_b64="QUJD",
        intent="observe page",
        session_id="s1",
        url="https://bank.example/calc",
        dom_hash="dom-abc",
        image_width=1280,
        image_height=800,
        cacheable=cacheable,
    ))
    return cache


def test_analyze_defaults_to_caching() -> None:
    sig = inspect.signature(VisionAgent.analyze)
    assert sig.parameters["cacheable"].default is True, (
        "callers that predate the blank-frame fix must keep caching"
    )


def test_a_painted_frame_is_cached() -> None:
    assert _analyze(cacheable=True).puts, (
        "the ordinary path must still populate the cache"
    )


def test_a_blank_frame_is_not_cached_despite_reporting_fresh() -> None:
    assert _analyze(cacheable=False).puts == [], (
        "empty bboxes keyed on a DOM hash would be re-served for the "
        "settled page, which is the whole fault being fixed"
    )


def test_a_blank_frame_does_not_seed_the_som_anchor() -> None:
    """The overlay memo is a second route to the same fault.

    `_last_response_bboxes` is drawn onto the NEXT screenshot as
    Set-of-Marks anchors. Seeding it from an unpainted frame would carry
    the bad pass forward even with the cache itself clean.
    """
    cache = _Cache()
    agent = VisionAgent(_Provider(), cache)  # type: ignore[arg-type]
    asyncio.run(agent.analyze(
        screenshot_b64="QUJD", intent="observe page", session_id="s1",
        url="https://bank.example/calc", dom_hash="dom-abc",
        image_width=1280, image_height=800, cacheable=False,
    ))
    assert "s1" not in agent._last_response_bboxes


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
