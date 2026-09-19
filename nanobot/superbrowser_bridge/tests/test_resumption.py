"""Resumption hand-off between workers.

The bug these guard: a worker that exhausted its step budget saved a
resumption artifact keyed to its Puppeteer session. The loader threw the
whole artifact away the moment that session was gone — which it usually
is, because the worker is told to close it — so the successor started
from a blank page and rediscovered the URL, the filters and the dead
ends its predecessor had already paid ~47 iterations to find.
"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from superbrowser_bridge.session_tools import resumption as R
from superbrowser_bridge.session_tools.telemetry import _extract_recent_failures


class _State:
    """Minimal stand-in for BrowserSessionState."""

    def __init__(self, **kw):
        self.session_id = kw.get("session_id", "sess-abc")
        self.current_url = kw.get("current_url", "https://shop.example/search?q=x&page=4")
        self.best_checkpoint_url = kw.get("best_checkpoint_url", "https://shop.example/search?q=x&page=3")
        self.task_id = "t1"
        self.step_history = kw.get("step_history", [])


class ResumptionArtifactTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._saved_path = R.RESUMPTION_PATH
        R.RESUMPTION_PATH = os.path.join(self._dir, "resumption.json")

    def tearDown(self):
        R.RESUMPTION_PATH = self._saved_path

    def _load(self, domain="shop.example", alive=False):
        """Load with the session-liveness probe stubbed."""
        class _Resp:
            status_code = 200 if alive else 404

        async def _fake(*a, **kw):
            if not alive:
                raise RuntimeError("session gone")
            return _Resp()

        with mock.patch.object(R, "_request_with_backoff", _fake):
            return asyncio.run(R.load_resumption_artifact(domain))

    def test_dead_session_still_yields_a_cold_url_hint(self):
        R.save_resumption_artifact(_State(), "shop.example", progress_note="filters applied; 12 results")
        got = self._load(alive=False)
        self.assertIsNotNone(got, "a dead session must not discard where the worker got to")
        self.assertFalse(got["warm"])
        self.assertEqual(got["current_url"], "https://shop.example/search?q=x&page=4")
        self.assertEqual(got["best_checkpoint_url"], "https://shop.example/search?q=x&page=3")
        self.assertIn("filters applied", got["progress_note"])

    def test_live_session_is_warm_so_the_successor_attaches(self):
        R.save_resumption_artifact(_State(), "shop.example")
        got = self._load(alive=True)
        self.assertTrue(got["warm"])

    def test_past_the_warm_window_a_live_session_is_still_only_cold(self):
        R.save_resumption_artifact(_State(), "shop.example")
        payload = json.load(open(R.RESUMPTION_PATH))
        payload["written_at"] = time.time() - (R.RESUMPTION_TTL_SEC + 30)
        json.dump(payload, open(R.RESUMPTION_PATH, "w"))
        got = self._load(alive=True)
        self.assertFalse(got["warm"], "an old page should be re-navigated, not adopted mid-flow")
        self.assertTrue(got["current_url"])

    def test_dead_session_does_not_delete_the_artifact(self):
        R.save_resumption_artifact(_State(), "shop.example")
        self._load(alive=False)
        self.assertTrue(os.path.exists(R.RESUMPTION_PATH), "the URL hint must survive a dead session")

    def test_past_the_cold_window_the_artifact_is_dropped(self):
        R.save_resumption_artifact(_State(), "shop.example")
        payload = json.load(open(R.RESUMPTION_PATH))
        payload["written_at"] = time.time() - (R.RESUMPTION_COLD_TTL_SEC + 60)
        json.dump(payload, open(R.RESUMPTION_PATH, "w"))
        self.assertIsNone(self._load(alive=False))
        self.assertFalse(os.path.exists(R.RESUMPTION_PATH))

    def test_another_domain_gets_nothing(self):
        R.save_resumption_artifact(_State(), "shop.example")
        self.assertIsNone(self._load(domain="other.example", alive=False))

    def test_no_url_means_no_hint_worth_carrying(self):
        self.assertFalse(R.save_resumption_artifact(_State(current_url=""), "shop.example"))

    def test_failed_tactics_travel_with_the_hint(self):
        history = [
            {"tool": "browser_click_at", "args": "{}", "result": "no effect", "success": False},
            {"tool": "browser_get_markdown", "args": "{}", "result": "ok", "success": True},
            {"tool": "browser_eval", "args": "{}", "result": "Script error: boom"},
        ]
        R.save_resumption_artifact(_State(step_history=history), "shop.example")
        got = self._load(alive=False)
        tools = [f["tool"] for f in got["recent_failures"]]
        self.assertIn("browser_click_at", tools, "an explicit success=False is a tactic not to repeat")
        self.assertIn("browser_eval", tools)
        self.assertNotIn("browser_get_markdown", tools, "successful steps are not dead ends")


class ExtractFailuresTests(unittest.TestCase):
    def test_success_false_counts_even_without_an_error_marker(self):
        out = _extract_recent_failures([
            {"tool": "browser_scroll", "args": "{}", "result": "no movement", "success": False},
        ])
        self.assertEqual(len(out), 1)

    def test_successful_steps_are_excluded(self):
        out = _extract_recent_failures([
            {"tool": "browser_open", "args": "{}", "result": "loaded", "success": True},
        ])
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()


class DemoteTests(unittest.TestCase):
    """Progress must survive more than one handoff.

    The bug: when a worker resumed from an artifact and ALSO failed, the
    orchestrator called `clear_resumption_artifact()`. The intent was
    sound — re-seeding the next worker with a LIVE session walks its LLM
    straight back into the stuck page — but the remedy threw away the
    furthest URL reached and both workers' dead ends along with it. With
    a 50-step worker cap, a task needing three workers got knowledge
    transfer across the first handoff and none afterwards, so worker 3
    started at the home page rediscovering what workers 1 and 2 had each
    spent a full budget learning.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._saved_path = R.RESUMPTION_PATH
        R.RESUMPTION_PATH = os.path.join(self._dir, "resumption.json")

    def tearDown(self):
        R.RESUMPTION_PATH = self._saved_path

    def _payload(self):
        with open(R.RESUMPTION_PATH) as f:
            return json.load(f)

    def test_the_live_session_is_dropped(self):
        # This is the actual poison: a session_id the successor attaches
        # to, landing it back on the page that already defeated someone.
        R.save_resumption_artifact(_State(), "shop.example")
        self.assertTrue(R.demote_resumption_artifact(_State(), "shop.example"))
        self.assertNotIn("session_id", self._payload())

    def test_the_furthest_url_survives(self):
        R.save_resumption_artifact(_State(), "shop.example")
        R.demote_resumption_artifact(_State(), "shop.example")
        self.assertEqual(
            self._payload()["current_url"],
            "https://shop.example/search?q=x&page=4",
        )

    def test_a_worker_that_regressed_does_not_erase_a_better_url(self):
        R.save_resumption_artifact(_State(), "shop.example")
        R.demote_resumption_artifact(_State(current_url="https://shop.example/"), "shop.example")
        self.assertEqual(
            self._payload()["current_url"],
            "https://shop.example/search?q=x&page=4",
            "bouncing to the home page must not overwrite the deep URL",
        )

    def test_dead_ends_accumulate_across_workers(self):
        first = [{"tool": "browser_click_at", "args": "{}", "result": "no effect", "success": False}]
        R.save_resumption_artifact(_State(step_history=first), "shop.example")
        second = [{"tool": "browser_scroll", "args": "{}", "result": "no movement", "success": False}]
        R.demote_resumption_artifact(_State(step_history=second), "shop.example")
        tools = [f["tool"] for f in self._payload()["recent_failures"]]
        self.assertIn("browser_click_at", tools, "worker 1's dead end was lost")
        self.assertIn("browser_scroll", tools, "worker 2's dead end was not added")

    def test_hops_increment_and_are_visible(self):
        R.save_resumption_artifact(_State(), "shop.example")
        R.demote_resumption_artifact(_State(), "shop.example")
        self.assertEqual(self._payload()["hops"], 1)
        R.demote_resumption_artifact(_State(), "shop.example")
        self.assertEqual(self._payload()["hops"], 2)

    def test_the_lineage_ends_rather_than_carrying_forever(self):
        R.save_resumption_artifact(_State(), "shop.example")
        for _ in range(R.RESUMPTION_MAX_HOPS):
            R.demote_resumption_artifact(_State(), "shop.example")
        self.assertFalse(R.demote_resumption_artifact(_State(), "shop.example"))
        self.assertFalse(os.path.exists(R.RESUMPTION_PATH))

    def test_another_domain_is_not_ours_to_touch(self):
        R.save_resumption_artifact(_State(), "shop.example")
        self.assertFalse(R.demote_resumption_artifact(_State(), "other.example"))
        self.assertTrue(os.path.exists(R.RESUMPTION_PATH))

    def test_a_demoted_artifact_loads_cold_and_still_carries_progress(self):
        R.save_resumption_artifact(_State(), "shop.example")
        R.demote_resumption_artifact(_State(), "shop.example")

        async def _fake(*a, **kw):
            raise AssertionError("no session_id, so liveness must not be probed")

        with mock.patch.object(R, "_request_with_backoff", _fake):
            got = asyncio.run(R.load_resumption_artifact("shop.example"))
        self.assertIsNotNone(got)
        self.assertFalse(got["warm"], "a demoted artifact can never be warm")
        self.assertEqual(got["current_url"], "https://shop.example/search?q=x&page=4")
        self.assertEqual(got["hops"], 1)

    def test_demoting_nothing_is_a_no_op(self):
        self.assertFalse(R.demote_resumption_artifact(_State(), "shop.example"))
