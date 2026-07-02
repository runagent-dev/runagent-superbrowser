"""Token accounting must survive a cancelled / timed-out run.

Repro: on the 900s wall-clock the nanobot runner re-raises ``CancelledError``
and never calls ``hook.after_run``, so the brain (orchestrator/worker) tokens
were lost and ``vision`` — recorded inline at its provider chokepoint — became
the ONLY role, making the reported ``total`` equal ``vision``. ``UsageHook`` now
banks per-iteration in ``after_iteration`` (which the runner calls at the end of
every iteration), so every completed iteration's tokens survive; ``after_run``
reconciles to the authoritative cumulative on success with no double count.

Run:
    source venv/bin/activate && PYTHONPATH=nanobot python \\
        nanobot/superbrowser_bridge/tests/test_usage_cancel_safe.py
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from superbrowser_bridge import usage as U


def _iter_ctx(inp: int, out: int) -> SimpleNamespace:
    """A per-iteration hook context (usage is this iteration's raw usage)."""
    return SimpleNamespace(
        usage={"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out},
        messages=None,
    )


async def _run_cancelled() -> "U.TaskUsage | None":
    """3 iterations complete, then the run is cancelled (after_run never fires)."""
    tid = "test-cancel"
    with U.track_task(tid):
        hook = U.UsageHook("worker")
        await hook.after_iteration(_iter_ctx(100, 10))
        await hook.after_iteration(_iter_ctx(200, 20))
        await hook.after_iteration(_iter_ctx(300, 30))
        # Vision is recorded inline (as it is at the real provider chokepoint).
        U.record_vision(50, prompt_tokens=40, completion_tokens=10)
        # <-- 900s timeout cancels here: after_run is skipped by the runner.
        snap = U.snapshot(tid)
    U.pop(tid)
    return snap


async def _run_success() -> "U.TaskUsage | None":
    """3 iterations, then after_run reconciles to the authoritative cumulative."""
    tid = "test-success"
    with U.track_task(tid):
        hook = U.UsageHook("worker")
        await hook.after_iteration(_iter_ctx(100, 10))
        await hook.after_iteration(_iter_ctx(200, 20))
        await hook.after_iteration(_iter_ctx(300, 30))
        run_ctx = SimpleNamespace(
            usage={"input_tokens": 600, "output_tokens": 60, "total_tokens": 660},
            messages=None,
        )
        await hook.after_run(run_ctx)
        snap = U.snapshot(tid)
    U.pop(tid)
    return snap


def test_brain_tokens_survive_cancellation() -> None:
    snap = asyncio.run(_run_cancelled())
    assert snap is not None
    worker = snap.by_role.get("worker")
    assert worker is not None, "worker tokens LOST on cancel (the original bug)"
    assert worker.input_tokens == 600, worker.input_tokens
    assert worker.output_tokens == 60, worker.output_tokens
    assert worker.total_tokens == 660, worker.total_tokens
    assert worker.calls == 3, worker.calls
    # The original bug was vision == total; it must now be a strict subset.
    assert snap.vision_tokens == 50, snap.vision_tokens
    assert snap.total_tokens == 710, snap.total_tokens
    assert snap.vision_tokens < snap.total_tokens
    print("✓ test_brain_tokens_survive_cancellation")


def test_success_reconciles_without_double_count() -> None:
    snap = asyncio.run(_run_success())
    assert snap is not None
    worker = snap.by_role["worker"]
    # after_iteration summed to 660; after_run reconciles to authoritative 660 —
    # it must NOT double to 1320.
    assert worker.total_tokens == 660, worker.total_tokens
    assert worker.input_tokens == 600, worker.input_tokens
    assert worker.output_tokens == 60, worker.output_tokens
    assert worker.calls == 3, worker.calls
    print("✓ test_success_reconciles_without_double_count")


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"✗ {t.__name__}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
