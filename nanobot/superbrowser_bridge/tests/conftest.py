"""Shared pytest plumbing for the bridge test-suite.

Several unittest-style modules here drive coroutines with
``asyncio.get_event_loop().run_until_complete(...)``. On Python 3.11+ that call
raises ``RuntimeError: There is no current event loop`` once an earlier test
module (anyio/pytest-asyncio based) has closed the loop it created, so the
outcome of those tests depended on collection order (19 order-dependent
failures in a full run, 0 when the same files ran alone). Giving every test a
fresh, current event loop removes the ordering coupling without touching the
tests themselves.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _fresh_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)
