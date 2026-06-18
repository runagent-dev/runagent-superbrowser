"""05 — Async + concurrency.

`await sb.arun(...)` is the async entry point (use it instead of `run()` inside
an event loop — `run()` deliberately raises there). Because it's async you can
fan several tasks out concurrently with `asyncio.gather`.

Runs in fetch mode, so no browser engine is needed.

Prerequisites:
  - An LLM configured (`nanobot onboard`)

Run:
  python examples/05_async.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

try:
    import runagent_superbrowser  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "nanobot"))

from runagent_superbrowser import SuperBrowser


async def main() -> int:
    sb = SuperBrowser()

    tasks = [
        "What is the capital of Bangladesh?",
        "What's the latest stable Python version?",
        "Who wrote the book 'The Pragmatic Programmer'?",
    ]

    # Run them concurrently — each arun() is independent.
    results = await asyncio.gather(*(sb.arun(t, mode="fetch") for t in tasks))

    for task, res in zip(tasks, results):
        status = "ok" if res.success else f"FAILED ({res.error})"
        print(f"[{status}] {task}\n    -> {res.text}\n")
    return 0 if all(r.success for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
