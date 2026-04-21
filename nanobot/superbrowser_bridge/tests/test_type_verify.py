"""Unit tests for post-type auto-verification + surgical correction.

Runs via stdlib unittest so no pytest dependency is required:

    source venv/bin/activate && python -m unittest \
        nanobot.superbrowser_bridge.tests.test_type_verify -v

External calls (LLM reflector, /evaluate endpoint) are mocked so the
tests exercise pure logic without hitting the network.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

import sys
from pathlib import Path

# Tests are invoked from the runagent-superbrowser root with `-m unittest
# discover -s nanobot`; make the bridge package importable the same way
# test_superbrowser.py does (adds nanobot/ to sys.path).
_NANOBOT_ROOT = Path(__file__).resolve().parents[2]
if str(_NANOBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_NANOBOT_ROOT))

from superbrowser_bridge import type_verify as tv  # noqa: E402


class _FakeState:
    """Minimal stand-in for BrowserSessionState. Captures record_step calls."""

    def __init__(self, task_instruction: str = "", current_url: str = "") -> None:
        self.task_instruction = task_instruction
        self.current_url = current_url
        self.steps: list[tuple[str, str, str]] = []
        self._verify_in_progress = False

    def record_step(self, tool: str, args: str, result: str) -> None:
        self.steps.append((tool, args, result))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class SkipPredicateTests(unittest.TestCase):
    def test_skips_short_text(self) -> None:
        skip, reason = tv.skip_predicate(typed_text="hi")
        self.assertTrue(skip)
        self.assertEqual(reason, "too_short")

    def test_skips_password_input_type(self) -> None:
        skip, reason = tv.skip_predicate(
            typed_text="correcthorsebatterystaple", input_type="password",
        )
        self.assertTrue(skip)
        self.assertTrue(reason.startswith("input_type=password"))

    def test_skips_email_like_text(self) -> None:
        skip, reason = tv.skip_predicate(typed_text="bob@example.com")
        self.assertTrue(skip)
        self.assertEqual(reason, "email_like")

    def test_skips_url_like_text(self) -> None:
        skip, reason = tv.skip_predicate(typed_text="https://foo.bar/baz")
        self.assertTrue(skip)
        self.assertEqual(reason, "url_like")

    def test_skips_sensitive_label(self) -> None:
        skip, reason = tv.skip_predicate(
            typed_text="somepassphrase", label="Your Password",
        )
        self.assertTrue(skip)
        self.assertEqual(reason, "sensitive_label")

    def test_skips_numeric_id(self) -> None:
        skip, reason = tv.skip_predicate(typed_text="4111 1111 1111 1111")
        self.assertTrue(skip)
        self.assertEqual(reason, "numeric_id")

    def test_allows_normal_word(self) -> None:
        skip, _ = tv.skip_predicate(typed_text="dhakka", label="City")
        self.assertFalse(skip)


class PlanSurgicalEditTests(unittest.TestCase):
    def test_identical_returns_keep_only(self) -> None:
        plan = tv.plan_surgical_edit("dhaka", "dhaka")
        self.assertIsNotNone(plan)
        distance, ops = plan  # type: ignore[misc]
        self.assertEqual(distance, 0)

    def test_single_extra_char_is_one_delete(self) -> None:
        plan = tv.plan_surgical_edit("dhakka", "dhaka")
        self.assertIsNotNone(plan)
        distance, ops = plan  # type: ignore[misc]
        self.assertEqual(distance, 1)
        # Expect exactly one 'del' of size 1 somewhere in the ops.
        del_ops = [op for op in ops if op[0] == "del"]
        self.assertEqual(len(del_ops), 1)
        self.assertEqual(del_ops[0], ("del", 1))
        # No inserts needed — we're only removing a character.
        self.assertEqual([op for op in ops if op[0] == "ins"], [])

    def test_far_apart_returns_none(self) -> None:
        plan = tv.plan_surgical_edit("completely different text", "target")
        self.assertIsNone(plan)

    def test_single_substitution_is_del_plus_ins(self) -> None:
        plan = tv.plan_surgical_edit("catt", "rat")
        self.assertIsNotNone(plan)
        distance, ops = plan  # type: ignore[misc]
        self.assertLessEqual(distance, 3)
        # Reconstruct what the ops would produce to make sure they're valid.
        reconstructed = _apply_ops("catt", ops)
        self.assertEqual(reconstructed, "rat")


def _apply_ops(before: str, ops: list[tuple[str, Any]]) -> str:
    """Python mirror of the JS surgical applier — lets tests validate
    that the planned ops really transform `before` into the intended
    target without running actual JS."""
    cursor = 0
    cur = before
    for op in ops:
        kind = op[0]
        if kind == "keep":
            cursor += op[1]
        elif kind == "del":
            cur = cur[:cursor] + cur[cursor + op[1]:]
        elif kind == "ins":
            s = op[1]
            cur = cur[:cursor] + s + cur[cursor:]
            cursor += len(s)
    return cur


class VerifyAndCorrectTests(unittest.TestCase):
    def setUp(self) -> None:
        # The module-level LRU persists across tests; wipe it so each case
        # starts from a clean slate.
        tv._recent_cache.clear()

    def _state(self, task: str) -> _FakeState:
        return _FakeState(task_instruction=task, current_url="https://example.com")

    def test_skips_when_task_contains_typed_text(self) -> None:
        # typed "dhaka" and task already has "dhaka" → trivially OK,
        # reflector should NOT be called at all.
        state = self._state("Find restaurants in dhaka tonight")
        with patch.object(tv, "_reflect_typo", new=AsyncMock(return_value=None)) as m:
            outcome = _run(tv.verify_and_correct(
                state, "sess-1",
                target_x=10, target_y=20,
                typed_text="dhaka", label="V3",
                page_url="https://example.com",
                field_meta={"input_type": "text", "label": "City"},
            ))
            m.assert_not_awaited()
        self.assertEqual(outcome.kind, "ok")
        self.assertIn("[verify: ok]", outcome.caption_suffix)

    def test_corrects_dhakka_to_dhaka_surgically(self) -> None:
        state = self._state("Find restaurants in dhaka tonight")
        reflector_response = {
            "is_correct": False,
            "suggested_correction": "dhaka",
            "confidence": 0.93,
            "reason": "typed_text contains an extra k compared to task token 'dhaka'",
        }
        async def fake_evaluate(session_id: str, script: str, *, timeout: float = 15.0):
            # Return an OK shape regardless of which script was sent.
            return {"ok": True, "before": "dhakka", "after": "dhaka", "changed": True}

        with patch.object(tv, "_reflect_typo",
                          new=AsyncMock(return_value=reflector_response)), \
             patch.object(tv, "_run_evaluate", new=AsyncMock(side_effect=fake_evaluate)):
            outcome = _run(tv.verify_and_correct(
                state, "sess-1",
                target_x=10, target_y=20,
                typed_text="dhakka", label="V3",
                page_url="https://example.com",
                field_meta={"input_type": "text", "label": "City"},
            ))
        self.assertEqual(outcome.kind, "corrected")
        self.assertEqual(outcome.corrected_to, "dhaka")
        self.assertEqual(outcome.after, "dhaka")
        self.assertIn("auto-corrected", outcome.caption_suffix)
        self.assertIn("dhakka", outcome.caption_suffix)
        self.assertIn("dhaka", outcome.caption_suffix)

    def test_rejects_correction_not_in_task_prompt(self) -> None:
        # Reflector hallucinates a correction that never appeared in the task.
        # Anti-hallucination rule must demote it to "ok" and never rewrite.
        state = self._state("Find restaurants tonight")
        reflector_response = {
            "is_correct": False,
            "suggested_correction": "dhaka",
            "confidence": 0.95,
            "reason": "hallucinated city name",
        }
        fake_eval = AsyncMock(return_value={"ok": True, "after": "dhaka"})
        with patch.object(tv, "_reflect_typo",
                          new=AsyncMock(return_value=reflector_response)), \
             patch.object(tv, "_run_evaluate", new=fake_eval):
            outcome = _run(tv.verify_and_correct(
                state, "sess-1",
                target_x=10, target_y=20,
                typed_text="dhakka", label="V3",
                page_url="https://example.com",
                field_meta={"input_type": "text", "label": "City"},
            ))
            # No rewrite should have happened.
            fake_eval.assert_not_awaited()
        self.assertEqual(outcome.kind, "ok")
        self.assertEqual(outcome.corrected_to, None)

    def test_medium_confidence_only_warns(self) -> None:
        state = self._state("Go to dhaka now")
        reflector_response = {
            "is_correct": False,
            "suggested_correction": "dhaka",
            "confidence": 0.72,
            "reason": "maybe a typo",
        }
        fake_eval = AsyncMock(return_value={"ok": True, "after": "dhaka"})
        with patch.object(tv, "_reflect_typo",
                          new=AsyncMock(return_value=reflector_response)), \
             patch.object(tv, "_run_evaluate", new=fake_eval):
            outcome = _run(tv.verify_and_correct(
                state, "sess-1",
                target_x=10, target_y=20,
                typed_text="dhakka", label="V3",
                page_url="https://example.com",
                field_meta={"input_type": "text", "label": "City"},
            ))
            fake_eval.assert_not_awaited()
        self.assertEqual(outcome.kind, "flagged")
        self.assertIn("WARNING", outcome.caption_suffix)
        self.assertIn("dhaka", outcome.caption_suffix)

    def test_reentrance_guard_prevents_loop(self) -> None:
        state = self._state("Dhaka is the target")
        state._verify_in_progress = True
        # Must not call reflector or /evaluate when guard is set.
        with patch.object(tv, "_reflect_typo", new=AsyncMock()) as reflector, \
             patch.object(tv, "_run_evaluate", new=AsyncMock()) as evaluator:
            outcome = _run(tv.verify_and_correct(
                state, "sess-1",
                target_x=10, target_y=20,
                typed_text="dhakka", label="V3",
                page_url="https://example.com",
                field_meta={"input_type": "text", "label": "City"},
            ))
            reflector.assert_not_awaited()
            evaluator.assert_not_awaited()
        self.assertEqual(outcome.kind, "skipped")


if __name__ == "__main__":
    unittest.main()
