"""The eval tracers must actually write.

Regression: _memory_dir read ``state.memory`` but BrowserSessionState stores it
as ``_memory``; the AttributeError was swallowed and a full 192-run sweep
produced zero vision_calls.jsonl / clicks.jsonl, losing the redundant-perception
and click-recovery metrics. A tracer that fails silently is worse than none.
"""
import glob
import inspect
import os
import shutil
import unittest


class TracingWritesTests(unittest.TestCase):
    def setUp(self):
        os.environ["SUPERBROWSER_TRACE_VISION"] = "1"
        os.environ["SUPERBROWSER_TRACE_CLICKS"] = "1"
        shutil.rmtree("/tmp/superbrowser/tracing-unit", ignore_errors=True)

    def tearDown(self):
        shutil.rmtree("/tmp/superbrowser/tracing-unit", ignore_errors=True)
        os.environ.pop("SUPERBROWSER_TRACE_VISION", None)
        os.environ.pop("SUPERBROWSER_TRACE_CLICKS", None)

    def test_vision_trace_lands_next_to_the_ledger(self):
        from superbrowser_bridge.memory import Memory
        from superbrowser_bridge.session_tools import BrowserSessionState
        from superbrowser_bridge.session_tools import tracing as T

        s = BrowserSessionState(memory=Memory("tracing-unit", session_key="worker:tu", role="worker"))
        self.assertIsNotNone(T._memory_dir(s), "tracer must resolve the state's memory dir")
        kw = {k: None for k in list(inspect.signature(T.trace_vision).parameters)[1:]}
        kw.update(path="sync", url="https://example.com", dom_hash="h")
        T.trace_vision(s, **kw)
        self.assertTrue(glob.glob("/tmp/superbrowser/tracing-unit/memory/vision_calls.jsonl"),
                        "vision_calls.jsonl was not written")


if __name__ == "__main__":
    unittest.main()
