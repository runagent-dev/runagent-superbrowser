"""Unit tests for the box_2d vision schema and denormalization.

Run from the repo's nanobot directory:
    source ../venv/bin/activate
    python3 test_vision_bbox.py

These cover the layer between Gemini's [0, 1000] normalized output and
the CSS-pixel coords the browser server consumes. The end-to-end
snap-to-element behaviour lives in TypeScript and is verified manually
by clicking through a known-tricky page with the live overlay enabled.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def test_bbox_to_pixels_denorm() -> None:
    """box_2d normalized to [0, 1000] denorms to expected CSS px ±2."""
    from vision_agent.schemas import BBox

    # Gemini returns [ymin, xmin, ymax, xmax] in [0, 1000].
    # Image is 1280×1100 (the SuperBrowser default viewport).
    b = BBox(box_2d=[100, 200, 300, 500], label="test", clickable=True)
    x0, y0, x1, y1 = b.to_pixels(1280, 1100)
    # ymin=100/1000*1100=110, xmin=200/1000*1280=256
    # ymax=300/1000*1100=330, xmax=500/1000*1280=640
    assert abs(x0 - 256) <= 2, f"x0={x0}"
    assert abs(y0 - 110) <= 2, f"y0={y0}"
    assert abs(x1 - 640) <= 2, f"x1={x1}"
    assert abs(y1 - 330) <= 2, f"y1={y1}"

    cx, cy = b.center_pixels(1280, 1100)
    assert abs(cx - 448) <= 2, f"cx={cx}"
    assert abs(cy - 220) <= 2, f"cy={cy}"
    print("✓ test_bbox_to_pixels_denorm")


def test_bbox_clamps_and_swaps() -> None:
    """Out-of-range and reversed coordinates are sanitised, not rejected."""
    from vision_agent.schemas import BBox

    # Reversed (model emitted ymax first by mistake).
    b1 = BBox(box_2d=[500, 800, 100, 200])
    assert b1.box_2d == [100, 200, 500, 800], b1.box_2d

    # Out of range — clamp to [0, 1000].
    b2 = BBox(box_2d=[-50, 50, 1500, 1100])
    assert b2.box_2d == [0, 50, 1000, 1000], b2.box_2d

    # Wrong shape — fall back to zeros, do not raise.
    b3 = BBox(box_2d=[100, 200])  # type: ignore[arg-type]
    assert b3.box_2d == [0, 0, 0, 0], b3.box_2d
    print("✓ test_bbox_clamps_and_swaps")


def test_vision_response_parses_box_2d() -> None:
    """A Gemini-shaped JSON parses cleanly and renders pixel coords."""
    from vision_agent.schemas import VisionResponse

    payload = {
        "summary": "Login page with email and password fields.",
        "relevant_text": "Sign in to your account",
        "bboxes": [
            {
                "label": "Email",
                "box_2d": [350, 200, 410, 800],
                "clickable": True,
                "role": "input",
                "confidence": 0.95,
                "intent_relevant": True,
            },
            {
                "label": "Sign in",
                "box_2d": [600, 400, 660, 600],
                "clickable": True,
                "role": "button",
                "confidence": 0.99,
                "intent_relevant": True,
            },
        ],
        "flags": {"captcha_present": False, "modal_open": False, "loading": False, "login_wall": True},
        "suggested_actions": [
            {"action": "type", "target_bbox_index": 0, "description": "Type email", "priority": 1},
        ],
    }
    resp = VisionResponse.model_validate(payload).with_image_dims(1280, 1100)
    assert len(resp.bboxes) == 2
    assert resp.image_width == 1280
    assert resp.image_height == 1100

    # get_bbox follows the same ranking as as_brain_text — both hits are
    # intent_relevant + clickable, so confidence breaks the tie.
    top = resp.get_bbox(1)
    assert top is not None
    assert top.label == "Sign in"  # 0.99 > 0.95
    assert top.center_pixels(1280, 1100) == (640, 693)

    text = resp.as_brain_text()
    assert "[V1]" in text
    assert "→" in text  # pixel-coord rendering uses an arrow
    assert "Sign in" in text
    print("✓ test_vision_response_parses_box_2d")


def test_brain_text_falls_back_when_no_dims() -> None:
    """Without image dims, brain text shows normalized box_2d (debug-friendly)."""
    from vision_agent.schemas import VisionResponse

    resp = VisionResponse.model_validate({
        "summary": "test",
        "bboxes": [{"label": "x", "box_2d": [10, 20, 30, 40], "clickable": True, "role": "button"}],
    })
    text = resp.as_brain_text()
    assert "box_2d=[10,20,30,40]" in text
    print("✓ test_brain_text_falls_back_when_no_dims")


def test_legacy_xywh_is_rejected_silently() -> None:
    """Legacy {x,y,w,h} payloads from older traces produce zeroed box_2d.

    We dropped the legacy field rather than maintaining two parsers; the
    schema's `extra='ignore'` swallows old keys instead of raising, and
    the missing box_2d defaults to [0, 0, 0, 0]. This is intentional —
    re-running a stale trace shouldn't crash, and the click pipeline
    already rejects empty bboxes downstream.
    """
    from vision_agent.schemas import BBox

    b = BBox.model_validate({"x": 100, "y": 200, "w": 50, "h": 40, "label": "old", "role": "button"})
    assert b.box_2d == [0, 0, 0, 0]
    assert b.label == "old"
    print("✓ test_legacy_xywh_is_rejected_silently")


def test_dead_click_guard_blocks_on_third_attempt() -> None:
    """3 consecutive same-target clicks with no DOM change → block on the 3rd."""
    from superbrowser_bridge.session_tools import BrowserSessionState

    s = BrowserSessionState()
    s._last_dom_hash = "fingerprint-1"

    assert s.check_dead_click("click[5]") is None, "1st attempt should pass"
    s.register_click_attempt("click[5]")
    assert s.check_dead_click("click[5]") is None, "2nd attempt should pass"
    s.register_click_attempt("click[5]")
    blocked = s.check_dead_click("click[5]")
    assert blocked is not None and "[dead_click_blocked]" in blocked, blocked
    print("✓ test_dead_click_guard_blocks_on_third_attempt")


def test_dead_click_guard_resets_on_dom_change() -> None:
    """A DOM change between identical clicks clears the strike count."""
    from superbrowser_bridge.session_tools import BrowserSessionState

    s = BrowserSessionState()
    s._last_dom_hash = "h1"
    s.check_dead_click("click[5]"); s.register_click_attempt("click[5]")
    s.check_dead_click("click[5]"); s.register_click_attempt("click[5]")
    # Page actually moved
    s._last_dom_hash = "h2"
    assert s.check_dead_click("click[5]") is None
    assert s.consecutive_dead_clicks == 1
    print("✓ test_dead_click_guard_resets_on_dom_change")


def test_dead_click_guard_resets_on_target_switch() -> None:
    """Switching to a different target clears the strike count."""
    from superbrowser_bridge.session_tools import BrowserSessionState

    s = BrowserSessionState()
    s._last_dom_hash = "h1"
    s.check_dead_click("click[5]"); s.register_click_attempt("click[5]")
    s.check_dead_click("click[5]"); s.register_click_attempt("click[5]")
    # Brain picks a different element
    assert s.check_dead_click("click[7]") is None
    assert s.consecutive_dead_clicks == 1
    print("✓ test_dead_click_guard_resets_on_target_switch")


# ── SoM feedback tests ────────────────────────────────────────────────
#
# These stub the provider with an async shim that records the screenshot
# it received, so we can assert the overlay fired (or didn't) without
# needing a live Gemini call.

def _make_jpeg(width: int = 200, height: int = 200) -> str:
    """Build a tiny solid-color JPEG and return its base64 encoding."""
    import base64, io
    from PIL import Image
    img = Image.new("RGB", (width, height), (30, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")


class _RecordingProvider:
    """Fake VisionProvider that records every screenshot it receives."""
    name = "recording"
    model = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat_with_image(self, *, screenshot_b64, system_prompt, user_prompt, mime_type="image/jpeg"):
        from vision_agent.providers.base import ProviderResponse
        self.calls.append(screenshot_b64)
        # Return an empty-but-valid vision response so .analyze() succeeds.
        return ProviderResponse(
            text='{"summary":"ok","bboxes":[],"flags":{},"suggested_actions":[]}',
            tokens_used=0, model=self.model, provider=self.name,
        )


def _make_agent() -> tuple[object, _RecordingProvider]:
    """Build a VisionAgent with a recording provider and in-memory cache."""
    import asyncio
    from vision_agent.cache import VisionCache
    from vision_agent.client import VisionAgent

    provider = _RecordingProvider()
    # VisionCache.from_env() with no env is fine (in-process dict).
    agent = VisionAgent(provider=provider, cache=VisionCache.from_env())
    return agent, provider


def test_som_skipped_on_first_pass() -> None:
    """No previous bboxes → screenshot forwarded to provider verbatim."""
    import asyncio, os
    from hashlib import sha1

    os.environ.pop("VISION_SOM_OVERLAY", None)  # default: enabled
    agent, provider = _make_agent()
    img_b64 = _make_jpeg()
    input_sha = sha1(img_b64.encode()).hexdigest()

    asyncio.run(agent.analyze(
        screenshot_b64=img_b64, intent="observe",
        session_id="s1", url="http://a", dom_hash="h",
    ))

    assert len(provider.calls) == 1
    got_sha = sha1(provider.calls[0].encode()).hexdigest()
    assert got_sha == input_sha, "first-pass screenshot must be unmodified"
    print("✓ test_som_skipped_on_first_pass")


def test_som_overlays_on_second_pass() -> None:
    """Seeded previous bboxes → next screenshot has the overlay drawn."""
    import asyncio, os
    from hashlib import sha1
    from vision_agent.schemas import BBox

    os.environ.pop("VISION_SOM_OVERLAY", None)  # default: enabled
    agent, provider = _make_agent()
    img_b64 = _make_jpeg()
    input_sha = sha1(img_b64.encode()).hexdigest()

    # Seed as if a prior pass produced this bbox. box_2d spans roughly
    # the center so the overlay strokes land on non-background pixels.
    agent._last_response_bboxes["s1"] = [
        BBox(label="prev", box_2d=[400, 400, 600, 600], clickable=True, role="button"),
    ]

    asyncio.run(agent.analyze(
        screenshot_b64=img_b64, intent="observe",
        session_id="s1", url="http://b", dom_hash="h",
    ))

    assert len(provider.calls) == 1
    got_sha = sha1(provider.calls[0].encode()).hexdigest()
    assert got_sha != input_sha, "SoM overlay must modify the screenshot"
    # Dimensions are preserved by build_highlighted_screenshot.
    import base64, io
    from PIL import Image
    img = Image.open(io.BytesIO(base64.b64decode(provider.calls[0])))
    assert img.size == (200, 200), f"unexpected output size: {img.size}"

    # Kill-switch: set VISION_SOM_OVERLAY=0, rerun, expect raw passthrough.
    os.environ["VISION_SOM_OVERLAY"] = "0"
    try:
        provider.calls.clear()
        asyncio.run(agent.analyze(
            screenshot_b64=img_b64, intent="observe",
            session_id="s1", url="http://c", dom_hash="h",
        ))
        got_sha2 = sha1(provider.calls[0].encode()).hexdigest()
        assert got_sha2 == input_sha, "kill-switch must forward raw bytes"
    finally:
        os.environ.pop("VISION_SOM_OVERLAY", None)
    print("✓ test_som_overlays_on_second_pass")


def main() -> int:
    tests = [
        test_bbox_to_pixels_denorm,
        test_bbox_clamps_and_swaps,
        test_vision_response_parses_box_2d,
        test_brain_text_falls_back_when_no_dims,
        test_legacy_xywh_is_rejected_silently,
        test_dead_click_guard_blocks_on_third_attempt,
        test_dead_click_guard_resets_on_dom_change,
        test_dead_click_guard_resets_on_target_switch,
        test_som_skipped_on_first_pass,
        test_som_overlays_on_second_pass,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            print(f"✗ {t.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"✗ {t.__name__} raised {type(exc).__name__}: {exc}")
            failed += 1
    print()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
