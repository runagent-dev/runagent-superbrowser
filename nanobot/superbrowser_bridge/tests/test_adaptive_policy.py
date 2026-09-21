"""Budget-adaptive hybrid memory policy.

The sweep compared an always-compacting arm (`ledger`) against a
never-compacting one (`full_history`) and the always-compacting arm lost.
Neither arm asks whether compaction pays for itself *when the window is
not under pressure*, because one pays on every turn and the other never
pays at all. This policy separates the two: the Ledger is injected
unconditionally, and the six-phase eviction is spent only once headroom
runs out or a subgoal closes.

Run:
    source venv/bin/activate && PYTHONPATH=nanobot python -m pytest \
        nanobot/superbrowser_bridge/tests/test_adaptive_policy.py -q
"""

from __future__ import annotations

import os
import pytest

from superbrowser_bridge.memory.policy import (
    POLICY_NAMES, MemoryPolicyConfig, adaptive_headroom_threshold, build_policy,
)

ADAPTIVE_HEADROOM_THRESHOLD = adaptive_headroom_threshold()


class _Ctx:
    def __init__(self, messages, iteration=3):
        self.messages = messages
        self.iteration = iteration


class _Mem:
    role = "worker"
    subgoal_message_floor = -1

    class _Ev:
        def __init__(self): self.rows = []
        def log(self, name, payload): self.rows.append((name, payload))

    def __init__(self): self.events = self._Ev()


def _hook(window=200_000, floor=-1, last_floor=-1):
    from superbrowser_bridge.memory.hook import MemoryHook
    h = MemoryHook.__new__(MemoryHook)
    h.memory = _Mem()
    h.memory.subgoal_message_floor = floor
    h._adaptive_last_floor = last_floor
    h._bot = None
    os.environ["SUPERBROWSER_CONTEXT_WINDOW_TOKENS"] = str(window)
    return h


def _msgs(approx_tokens: int):
    # The gate estimates 4 chars per token, so this is exact by construction
    # — and cheap, which the tokenizer chain was not (186s across this file
    # before the estimator was swapped).
    return [{"role": "user", "content": "x" * (approx_tokens * 4)}]


class TestRegistration:
    def test_policy_name_is_selectable(self):
        assert "adaptive" in POLICY_NAMES

    def test_it_runs_the_hook_path_not_the_bypass_path(self):
        # Returning a _Policy object would make the hook skip the six
        # phases outright; this arm has to be able to CHOOSE per turn.
        assert build_policy(MemoryPolicyConfig(name="adaptive")) is None

    def test_the_arm_is_registered_for_the_runner(self):
        from eval.core.arms import get
        assert get("adaptive").env == {"SUPERBROWSER_MEMORY_POLICY": "adaptive"}

    def test_the_calibrated_arm_pins_its_threshold_in_the_run_env(self):
        # Recorded per run, so a reader can tell which threshold produced
        # which numbers without consulting the source.
        from eval.core.arms import get
        assert get("adaptive87").env["SUPERBROWSER_ADAPTIVE_HEADROOM"] == "0.87"


class TestEstimatorCost:
    def test_the_estimate_is_cheap_enough_to_run_every_turn(self):
        # It gates compaction on every iteration; a tokenizer call here
        # would cost more than the work it is deciding to skip.
        import time
        from superbrowser_bridge.memory.hook import _estimate_tokens_cheap
        big = [{"role": "user", "content": "x" * 4_000_000}]
        t = time.time()
        _estimate_tokens_cheap(big)
        assert time.time() - t < 0.1

    def test_it_reads_text_blocks_not_just_plain_strings(self):
        from superbrowser_bridge.memory.hook import _estimate_tokens_cheap
        blocks = [{"role": "user", "content": [
            {"type": "text", "text": "y" * 400},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        ]}]
        assert _estimate_tokens_cheap(blocks) == 100


class TestHeadroomGate:
    def test_defers_compaction_while_the_window_is_roomy(self):
        h = _hook(window=200_000)
        defer, why, hr = h._adaptive_should_defer(_Ctx(_msgs(10_000)))
        assert defer is True and why == "headroom"
        assert hr > ADAPTIVE_HEADROOM_THRESHOLD

    def test_compacts_once_headroom_runs_out(self):
        h = _hook(window=200_000)
        defer, why, hr = h._adaptive_should_defer(_Ctx(_msgs(60_000)))
        assert defer is False and why == "pressure"
        assert hr < ADAPTIVE_HEADROOM_THRESHOLD

    def test_the_threshold_is_calibrated_not_arbitrary(self):
        # 0.30 never fired: on a 200K window it triggers above 140K tokens
        # and these tasks peak at 43-91K, so the arm silently became
        # "Ledger, never evict". 0.87 splits the observed turns near evenly.
        assert ADAPTIVE_HEADROOM_THRESHOLD == 0.87

    def test_the_threshold_is_settable_per_arm(self):
        import os
        from superbrowser_bridge.memory.policy import adaptive_headroom_threshold
        os.environ["SUPERBROWSER_ADAPTIVE_HEADROOM"] = "0.5"
        try:
            assert adaptive_headroom_threshold() == 0.5
        finally:
            del os.environ["SUPERBROWSER_ADAPTIVE_HEADROOM"]

    def test_a_nonsense_threshold_falls_back_rather_than_crashing(self):
        import os
        from superbrowser_bridge.memory.policy import adaptive_headroom_threshold
        for bad in ("bogus", "1.5", "-0.2", ""):
            os.environ["SUPERBROWSER_ADAPTIVE_HEADROOM"] = bad
            try:
                assert adaptive_headroom_threshold() == 0.87
            finally:
                del os.environ["SUPERBROWSER_ADAPTIVE_HEADROOM"]

    def test_an_unmeasurable_window_falls_back_to_compacting(self):
        # Failing toward production behaviour is the safe direction.
        h = _hook(window=0)
        os.environ["SUPERBROWSER_CONTEXT_WINDOW_TOKENS"] = "0"
        defer, why, _ = h._adaptive_should_defer(_Ctx(_msgs(1_000)))
        assert defer is False and why == "no_window"

    def test_an_empty_context_falls_back_to_compacting(self):
        h = _hook(window=200_000)
        defer, why, _ = h._adaptive_should_defer(_Ctx([]))
        assert defer is False and why == "no_estimate"


class TestSubgoalBoundary:
    def test_a_closing_subgoal_forces_compaction_despite_headroom(self):
        # The compactor exists to fold up a finished subgoal's messages;
        # deferring past that seam would strand them.
        h = _hook(window=200_000, floor=42, last_floor=7)
        defer, why, _ = h._adaptive_should_defer(_Ctx(_msgs(1_000)))
        assert defer is False and why == "subgoal_boundary"

    def test_the_first_observed_floor_is_not_a_boundary(self):
        # Nothing closed — this is just the first turn we looked.
        h = _hook(window=200_000, floor=42, last_floor=-1)
        defer, why, _ = h._adaptive_should_defer(_Ctx(_msgs(1_000)))
        assert defer is True and why == "headroom"

    def test_a_stable_subgoal_does_not_keep_forcing_compaction(self):
        h = _hook(window=200_000, floor=42, last_floor=7)
        h._adaptive_should_defer(_Ctx(_msgs(1_000)))          # consumes the boundary
        defer, why, _ = h._adaptive_should_defer(_Ctx(_msgs(1_000)))
        assert defer is True and why == "headroom"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
