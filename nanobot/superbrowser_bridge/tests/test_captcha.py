"""Unit tests for the iterative captcha solver.

Runs via stdlib unittest — no pytest dependency required:

    source venv/bin/activate && python -m unittest \
        nanobot.superbrowser_bridge.tests.test_captcha -v

HTTP calls (`_request_with_backoff`) and vision-agent calls
(`vision_agent.analyze`) are both patched so the tests exercise loop
control flow without hitting the network or Gemini. Fixtures are
deliberately minimal — we only care that the loop (1) asks vision on
every step, (2) bails after a dead-action streak, (3) stops when vision
reports the captcha cleared, and (4) honors the role-preference order.
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

_NANOBOT_ROOT = Path(__file__).resolve().parents[2]
if str(_NANOBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_NANOBOT_ROOT))

from superbrowser_bridge import session_tools as st  # noqa: E402


# --- Fixture builders -------------------------------------------------------


def _bbox(role: str, label: str = "", box=(100, 100, 200, 200), **kw) -> MagicMock:
    """Minimal stand-in for vision_agent.schemas.BBox.

    Supports the subset the loop actually uses: `.role`, `.label`,
    `.to_pixels(w,h)`, `.center_pixels(w,h)`.
    """
    x0, y0, x1, y1 = box
    b = MagicMock()
    b.role = role
    b.label = label
    b.to_pixels = MagicMock(return_value=(x0, y0, x1, y1))
    b.center_pixels = MagicMock(return_value=((x0 + x1) // 2, (y0 + y1) // 2))
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _vision_response(
    *,
    bboxes: list[Any] | None = None,
    captcha_present: bool = True,
    widget_bbox: Any = None,
    next_action: Any = None,
    model: str = "gemini-stub",
    provider: str = "stub",
) -> MagicMock:
    """Minimal stand-in for vision_agent.schemas.VisionResponse."""
    resp = MagicMock()
    resp.bboxes = bboxes or []
    resp.flags = MagicMock()
    resp.flags.captcha_present = captcha_present
    resp.flags.captcha_widget_bbox = widget_bbox
    # next_action must be explicitly None when absent — plain MagicMock
    # would produce a truthy auto-spec attribute and trip the step-mode
    # dispatch path in the loop.
    resp.next_action = next_action
    resp.model = model
    resp.provider = provider
    return resp


def _next_action(
    *,
    action_type: str = "click_tile",
    target_bbox: Any = None,
    target_input_bbox: Any = None,
    type_value: str = "",
    expect_change: str = "new_tile",
    label: str = "",
    reasoning: str = "",
) -> MagicMock:
    """Stand-in for vision_agent.schemas.NextAction."""
    na = MagicMock()
    na.action_type = action_type
    na.target_bbox = target_bbox
    na.target_input_bbox = target_input_bbox
    na.type_value = type_value
    na.expect_change = expect_change
    na.label = label
    na.reasoning = reasoning
    return na


def _http_ok(payload: dict[str, Any], status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=payload)
    r.raise_for_status = MagicMock()
    return r


def _http_status(status_code: int) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value={})
    r.raise_for_status = MagicMock()
    return r


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# --- _first_actionable preference order -------------------------------------


class FirstActionableTests(unittest.TestCase):
    def test_empty_response_returns_none(self) -> None:
        resp = _vision_response(bboxes=[])
        self.assertIsNone(st._first_actionable(resp))

    def test_prefers_captcha_tile_over_handle_and_submit(self) -> None:
        tile = _bbox("captcha_tile", "car_5_3")
        handle = _bbox("slider_handle", "drag_me")
        submit = _bbox("button", "verify now")
        resp = _vision_response(bboxes=[submit, handle, tile])
        self.assertIs(st._first_actionable(resp), tile)

    def test_returns_handle_when_no_tiles(self) -> None:
        handle = _bbox("slider_handle", "drag")
        submit = _bbox("button", "submit")
        resp = _vision_response(bboxes=[submit, handle])
        self.assertIs(st._first_actionable(resp), handle)

    def test_returns_verify_button_only_when_tiles_gone(self) -> None:
        submit = _bbox("button", "verify")
        others = _bbox("link", "privacy policy")
        resp = _vision_response(bboxes=[others, submit])
        self.assertIs(st._first_actionable(resp), submit)

    def test_button_without_submit_keyword_ignored(self) -> None:
        # Generic button labels don't qualify as a submit target.
        other_button = _bbox("button", "close dialog")
        resp = _vision_response(bboxes=[other_button])
        self.assertIsNone(st._first_actionable(resp))

    def test_falls_back_to_captcha_widget(self) -> None:
        widget = _bbox("captcha_widget", "recaptcha checkbox")
        resp = _vision_response(bboxes=[widget])
        self.assertIs(st._first_actionable(resp), widget)


# --- Iterative loop control flow -------------------------------------------


class IterativeLoopTests(unittest.TestCase):
    """Exercises the loop against mocked HTTP + vision callables."""

    def setUp(self) -> None:
        self.http_calls: list[tuple[str, str, dict | None]] = []
        self.vision_calls: list[dict] = []

    def _http_factory(
        self, *, click_status: int = 200, state_payload: dict | None = None,
    ):
        """Returns an AsyncMock standing in for _request_with_backoff."""
        state_payload = state_payload or {
            "screenshot": _tiny_jpeg_b64(),
            "url": "https://example.test/challenge",
            "clickableElementsToString": "<button>tile</button>",
        }

        async def fake(method: str, url: str, **kw) -> MagicMock:
            self.http_calls.append((method, url, kw.get("json")))
            if "/state" in url:
                return _http_ok(state_payload)
            if "/click" in url:
                return _http_status(click_status) if click_status != 200 else _http_ok({})
            if "/drag" in url:
                return _http_ok({})
            if "/captcha/detect" in url:
                return _http_ok({"captcha": {"present": True, "type": "grid"}})
            return _http_ok({})

        return AsyncMock(side_effect=fake)

    def _vision_factory(self, responses: list[MagicMock]) -> MagicMock:
        """Returns a vision_agent stub where .analyze yields from `responses`."""
        agent = MagicMock()
        queue = list(responses)

        async def analyze(**kw) -> MagicMock:
            self.vision_calls.append(kw)
            if not queue:
                # Default: captcha cleared when we exhaust canned responses.
                return _vision_response(bboxes=[], captcha_present=False)
            return queue.pop(0)

        agent.analyze = AsyncMock(side_effect=analyze)
        return agent

    def test_loop_re_queries_vision_each_step(self) -> None:
        """Core behavior: vision is called on every iteration, not just once."""
        tile_a = _bbox("captcha_tile", "A", box=(10, 10, 50, 50))
        tile_b = _bbox("captcha_tile", "B", box=(100, 100, 140, 140))
        # 3 passes then captcha cleared on verify.
        responses = [
            _vision_response(bboxes=[tile_a]),
            _vision_response(bboxes=[tile_b]),
            _vision_response(bboxes=[], captcha_present=False),  # verify step
        ]
        agent = self._vision_factory(responses)
        fake_http = self._http_factory()
        with patch.object(st, "_request_with_backoff", fake_http):
            result = _run(st._solve_captcha_iterative(
                "t3-abc", None, agent, max_steps=4,
            ))
        # At minimum: 2 per-step vision calls + 1 verify call = 3.
        self.assertGreaterEqual(agent.analyze.await_count, 3)
        self.assertGreaterEqual(result["steps"], 2)

    def test_dead_action_streak_aborts(self) -> None:
        """Three visits to the same 40px neighborhood → bail with error."""
        dup = _bbox("captcha_tile", "stuck", box=(200, 200, 240, 240))
        # Same bbox returned over and over — loop must detect the streak.
        responses = [_vision_response(bboxes=[dup]) for _ in range(6)]
        agent = self._vision_factory(responses)
        fake_http = self._http_factory()
        with patch.object(st, "_request_with_backoff", fake_http):
            result = _run(st._solve_captcha_iterative(
                "t3-xyz", None, agent, max_steps=12,
            ))
        self.assertFalse(result["solved"])
        self.assertEqual(result["error"], "dead_action_streak")
        # Should have bailed well before max_steps.
        self.assertLess(result["steps"], 6)

    def test_no_actionable_bbox_returns_error(self) -> None:
        """Vision returns nothing on step 0 and no widget_bbox → give up."""
        responses = [_vision_response(bboxes=[], widget_bbox=None)]
        agent = self._vision_factory(responses)
        fake_http = self._http_factory()
        with patch.object(st, "_request_with_backoff", fake_http):
            result = _run(st._solve_captcha_iterative(
                "t3-empty", None, agent, max_steps=4,
            ))
        self.assertFalse(result["solved"])
        self.assertEqual(result["error"], "no_actionable_bbox")

    def test_widget_fallback_on_step_zero(self) -> None:
        """Empty bboxes + widget present → click widget center (reCAPTCHA checkbox)."""
        widget = _bbox("captcha_widget", "recaptcha", box=(300, 300, 380, 340))
        # Step 0: no actionable bboxes but widget present.
        # Step 1: captcha cleared.
        responses = [
            _vision_response(bboxes=[], widget_bbox=widget),
            _vision_response(bboxes=[], captcha_present=False),
        ]
        agent = self._vision_factory(responses)
        fake_http = self._http_factory()
        with patch.object(st, "_request_with_backoff", fake_http):
            result = _run(st._solve_captcha_iterative(
                "t3-widget", None, agent, max_steps=4,
            ))
        # Expect a click at the widget center.
        clicks = [c for c in self.http_calls if "/click" in c[1]]
        self.assertGreaterEqual(len(clicks), 1)
        self.assertIn("x", clicks[0][2])
        self.assertIn("y", clicks[0][2])

    def test_next_action_done_exits_loop_early(self) -> None:
        """When vision returns next_action=done, loop must break before verify."""
        responses = [
            _vision_response(
                bboxes=[_bbox("captcha_tile", "x")],
                next_action=_next_action(action_type="done", expect_change="static"),
            ),
        ]
        agent = self._vision_factory(responses)
        fake_http = self._http_factory()
        with patch.object(st, "_request_with_backoff", fake_http):
            result = _run(st._solve_captcha_iterative(
                "t3-done", None, agent, max_steps=6,
            ))
        # Should see exactly one per-step vision call + one verify call.
        self.assertEqual(agent.analyze.await_count, 2)
        self.assertEqual(result["steps"], 1)

    def test_next_action_stuck_escalates_with_error(self) -> None:
        """next_action=stuck returns vision_stuck error (triggers handoff upstream)."""
        responses = [
            _vision_response(
                bboxes=[],
                next_action=_next_action(
                    action_type="stuck",
                    reasoning="no matching tiles visible and no verify button",
                ),
            ),
        ]
        agent = self._vision_factory(responses)
        fake_http = self._http_factory()
        with patch.object(st, "_request_with_backoff", fake_http):
            result = _run(st._solve_captcha_iterative(
                "t3-stuck", None, agent, max_steps=6,
            ))
        self.assertFalse(result["solved"])
        self.assertEqual(result["error"], "vision_stuck")

    def test_next_action_drag_slider_uses_drag_endpoint(self) -> None:
        """action_type=drag_slider forces drag dispatch regardless of bbox role."""
        tile = _bbox("captcha_tile", "mislabeled", box=(100, 100, 140, 140))
        # Vision calls it drag_slider even though the role is captcha_tile.
        responses = [
            _vision_response(
                bboxes=[tile],
                next_action=_next_action(
                    action_type="drag_slider",
                    target_bbox=tile,
                    expect_change="widget_replace",
                ),
            ),
            _vision_response(bboxes=[], captcha_present=False),
        ]
        agent = self._vision_factory(responses)
        fake_http = self._http_factory()
        with patch.object(st, "_request_with_backoff", fake_http):
            _run(st._solve_captcha_iterative(
                "t3-drag", None, agent, max_steps=4,
            ))
        drags = [c for c in self.http_calls if "/drag" in c[1]]
        clicks = [c for c in self.http_calls if "/click" in c[1]]
        self.assertGreaterEqual(len(drags), 1)
        self.assertEqual(len(clicks), 0)

    def test_next_action_type_text_posts_to_type_at(self) -> None:
        """action_type=type_text dispatches to /type-at with type_value + input center."""
        img_bbox = _bbox("image", "captcha_image", box=(100, 100, 300, 160))
        input_bbox = _bbox("input", "verify_input", box=(100, 180, 300, 210))
        responses = [
            _vision_response(
                bboxes=[img_bbox, input_bbox],
                next_action=_next_action(
                    action_type="type_text",
                    target_bbox=img_bbox,
                    target_input_bbox=input_bbox,
                    type_value="7B3K9",
                    expect_change="widget_replace",
                ),
            ),
            _vision_response(bboxes=[], captcha_present=False),
        ]
        agent = self._vision_factory(responses)
        fake_http = self._http_factory()
        with patch.object(st, "_request_with_backoff", fake_http):
            _run(st._solve_captcha_iterative(
                "t3-text", None, agent, max_steps=4,
            ))
        type_calls = [c for c in self.http_calls if "/type-at" in c[1]]
        self.assertEqual(len(type_calls), 1)
        payload = type_calls[0][2] or {}
        self.assertEqual(payload.get("text"), "7B3K9")
        # Center of input_bbox (100..300, 180..210) = (200, 195)
        self.assertEqual(payload.get("x"), 200)
        self.assertEqual(payload.get("y"), 195)
        self.assertTrue(payload.get("clear"))

    def test_next_action_type_text_without_value_aborts(self) -> None:
        """type_text with empty type_value treated as stuck-equivalent error."""
        responses = [
            _vision_response(
                bboxes=[_bbox("input", "x")],
                next_action=_next_action(
                    action_type="type_text",
                    target_input_bbox=_bbox("input", "x", box=(50, 50, 100, 80)),
                    type_value="",
                ),
            ),
        ]
        agent = self._vision_factory(responses)
        fake_http = self._http_factory()
        with patch.object(st, "_request_with_backoff", fake_http):
            result = _run(st._solve_captcha_iterative(
                "t3-nopass", None, agent, max_steps=4,
            ))
        self.assertFalse(result["solved"])
        self.assertEqual(result["error"], "type_text_missing_value")
        # Should not have posted to /type-at.
        type_calls = [c for c in self.http_calls if "/type-at" in c[1]]
        self.assertEqual(len(type_calls), 0)

    def test_http_409_dead_zone_re_analyzes_without_advancing(self) -> None:
        """409 from /click must not count as dead-action or advance cursor."""
        tile = _bbox("captcha_tile", "target", box=(50, 50, 90, 90))
        # Two passes returning the same tile — normally the dead-action
        # streak would fire on the 3rd attempt. With 409 in between, the
        # streak counter is NOT incremented, so the loop keeps trying.
        responses = [
            _vision_response(bboxes=[tile]),
            _vision_response(bboxes=[tile]),
            _vision_response(bboxes=[], captcha_present=False),
        ]
        agent = self._vision_factory(responses)
        fake_http = self._http_factory(click_status=409)

        with patch.object(st, "_request_with_backoff", fake_http):
            result = _run(st._solve_captcha_iterative(
                "t3-dead", None, agent, max_steps=4,
            ))
        # 409 is transient; should NOT produce a dead_action_streak error.
        self.assertNotEqual(result.get("error"), "dead_action_streak")


# --- Helpers ---------------------------------------------------------------


# --- Cloudflare interstitial handling ---------------------------------------


class CFInterstitialTests(unittest.TestCase):
    """Type coercion + detector literal coverage."""

    def test_cf_interstitial_type_in_union(self) -> None:
        from superbrowser_bridge.antibot.captcha.detect import CaptchaType
        self.assertIn("cf_interstitial", CaptchaType.__args__)

    def test_captcha_info_accepts_cf_interstitial(self) -> None:
        from superbrowser_bridge.antibot.captcha.detect import CaptchaInfo
        ci = CaptchaInfo(type="cf_interstitial", present=True)
        self.assertEqual(ci.type, "cf_interstitial")
        self.assertTrue(ci.present)


class CFLearningsTests(unittest.TestCase):
    """Per-domain cf_failure_streak + needs_headful learnings."""

    def setUp(self) -> None:
        import tempfile
        from superbrowser_bridge import routing
        self._routing = routing
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = routing.LEARNINGS_DIR
        routing.LEARNINGS_DIR = self._tmp.name

    def tearDown(self) -> None:
        self._routing.LEARNINGS_DIR = self._orig_dir
        self._tmp.cleanup()

    def test_streak_increments_and_sets_needs_headful_at_two(self) -> None:
        self.assertFalse(self._routing.needs_headful("example.com"))
        self.assertEqual(self._routing.record_cf_failure("example.com"), 1)
        self.assertFalse(self._routing.needs_headful("example.com"))
        self.assertEqual(self._routing.record_cf_failure("example.com"), 2)
        self.assertTrue(self._routing.needs_headful("example.com"))

    def test_success_resets_streak_but_needs_headful_sticky(self) -> None:
        self._routing.record_cf_failure("sticky.test")
        self._routing.record_cf_failure("sticky.test")
        self.assertTrue(self._routing.needs_headful("sticky.test"))
        self._routing.record_cf_success("sticky.test")
        # needs_headful remains true — the underlying fingerprint issue
        # is the same regardless of one lucky pass.
        self.assertTrue(self._routing.needs_headful("sticky.test"))

    def test_empty_domain_is_no_op(self) -> None:
        self.assertEqual(self._routing.record_cf_failure(""), 0)
        self.assertFalse(self._routing.needs_headful(""))


class TierEscalationLearningTests(unittest.TestCase):
    """choose_starting_tier learns from T1 block outcomes."""

    def setUp(self) -> None:
        import tempfile
        from superbrowser_bridge import routing
        self._routing = routing
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = routing.LEARNINGS_DIR
        routing.LEARNINGS_DIR = self._tmp.name

    def tearDown(self) -> None:
        self._routing.LEARNINGS_DIR = self._orig_dir
        self._tmp.cleanup()

    def test_fresh_domain_returns_zero(self) -> None:
        self.assertEqual(self._routing.choose_starting_tier("fresh.test"), 0)

    def test_t1_block_promotes_to_t3(self) -> None:
        self._routing._record_routing_outcome(
            "blocked.test", approach="browser", success=False,
            tier=1, block_class="antibot_403",
        )
        self.assertEqual(self._routing.choose_starting_tier("blocked.test"), 3)

    def test_t3_success_becomes_lowest_tier(self) -> None:
        self._routing._record_routing_outcome(
            "works.test", approach="browser", success=False,
            tier=1, block_class="antibot_403",
        )
        self._routing._record_routing_outcome(
            "works.test", approach="browser", success=True, tier=3,
        )
        # lowest_successful_tier=3 stays 3; fallback never fires.
        self.assertEqual(self._routing.choose_starting_tier("works.test"), 3)

    def test_transient_fail_without_class_still_promotes(self) -> None:
        # record_routing_outcome with tier=1 success=False writes
        # "fail:<class>" or "fail" when block_class is empty.
        self._routing._record_routing_outcome(
            "rate.test", approach="browser", success=False,
            tier=1, block_class="",
        )
        # "fail" starts with "fail" so promotion kicks in.
        self.assertEqual(self._routing.choose_starting_tier("rate.test"), 3)


class CFSolverTests(unittest.TestCase):
    """solve_cf_interstitial wraps _wait_for_cf_clear and produces the
    right structured result."""

    def _mk_manager(self, *, cleared: bool, cookies=None):
        """Stub T3SessionManager exposing _get + _wait_for_cf_clear."""
        mgr = MagicMock()
        s = MagicMock()
        s.page = MagicMock()
        s.page.url = "https://example.test/challenge"
        mgr._get = MagicMock(return_value=s)

        async def wait(session_id, *, timeout_s, origin_url):
            return {
                "cleared": cleared,
                "iterations": 3,
                "cookies_landed": cookies or [],
                "final_url": "https://example.test/clear" if cleared else origin_url,
                "final_title": "example" if cleared else "just a moment",
                "error": "",
            }
        mgr._wait_for_cf_clear = AsyncMock(side_effect=wait)
        return mgr

    def test_solved_path_returns_cleared_true(self) -> None:
        from superbrowser_bridge.antibot.captcha.solve_cf import solve_cf_interstitial
        from superbrowser_bridge.antibot.captcha.detect import CaptchaInfo
        mgr = self._mk_manager(cleared=True, cookies=["cf_clearance"])
        info = CaptchaInfo(type="cf_interstitial", present=True)
        result = _run(solve_cf_interstitial(mgr, "t3-ok", info, timeout_s=5))
        self.assertTrue(result["solved"])
        self.assertEqual(result["method"], "cf_wait")
        self.assertIn("cf_clearance", result["cookies_landed"])
        self.assertNotIn("error", result)

    def test_timeout_path_returns_block_class_cloudflare(self) -> None:
        from superbrowser_bridge.antibot.captcha.solve_cf import solve_cf_interstitial
        from superbrowser_bridge.antibot.captcha.detect import CaptchaInfo
        import tempfile
        from superbrowser_bridge import routing
        tmp = tempfile.TemporaryDirectory()
        orig = routing.LEARNINGS_DIR
        routing.LEARNINGS_DIR = tmp.name
        try:
            mgr = self._mk_manager(cleared=False)
            info = CaptchaInfo(type="cf_interstitial", present=True)
            result = _run(solve_cf_interstitial(mgr, "t3-fail", info, timeout_s=5))
            self.assertFalse(result["solved"])
            self.assertEqual(result["block_class"], "cloudflare")
            self.assertIn("escalation_hints", result)
            # Failure streak incremented on the domain.
            self.assertGreaterEqual(result.get("cf_failure_streak", 0), 1)
        finally:
            routing.LEARNINGS_DIR = orig
            tmp.cleanup()


class NavGuardTests(unittest.TestCase):
    """BrowserSessionState nav-guard state machine."""

    def test_guard_state_initial(self) -> None:
        from superbrowser_bridge.session_tools import BrowserSessionState
        s = BrowserSessionState()
        self.assertEqual(s.last_nav_cf_blocked_url, "")
        self.assertFalse(s.nav_solve_called_since_block)

    def test_guard_transitions(self) -> None:
        from superbrowser_bridge.session_tools import BrowserSessionState
        s = BrowserSessionState()
        # Simulate navigate returning CF block.
        s.last_nav_cf_blocked_url = s._normalize_url("https://cars.com/x")
        s.nav_solve_called_since_block = False
        # Repeat nav to same URL → guard fires (caller checks externally).
        self.assertEqual(
            s._normalize_url("https://cars.com/x"),
            s.last_nav_cf_blocked_url,
        )
        self.assertFalse(s.nav_solve_called_since_block)
        # After solve call, the flag flips, allowing re-nav.
        s.nav_solve_called_since_block = True
        self.assertTrue(s.nav_solve_called_since_block)


_TINY_JPEG_B64_CACHE: str | None = None


def _tiny_jpeg_b64() -> str:
    """Smallest real JPEG for `_read_image_dims` to decode.

    Generated once per process via PIL to avoid baking in a magic hex
    blob. The loop calls `_read_image_dims(b64)`; an invalid JPEG would
    return (0, 0) and break `.to_pixels()` denormalization.
    """
    global _TINY_JPEG_B64_CACHE
    if _TINY_JPEG_B64_CACHE is not None:
        return _TINY_JPEG_B64_CACHE
    import base64
    import io
    from PIL import Image
    img = Image.new("RGB", (400, 400), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=50)
    _TINY_JPEG_B64_CACHE = base64.b64encode(buf.getvalue()).decode("ascii")
    return _TINY_JPEG_B64_CACHE


if __name__ == "__main__":
    unittest.main(verbosity=2)
