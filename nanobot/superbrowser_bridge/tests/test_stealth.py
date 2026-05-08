"""Unit tests for the T3 real-browser fingerprint upgrades.

Covers env propagation / profile-dir resolver / xvfb gate — the
logic-only parts of Phase A (CHROME_PATH), Phase B (persistent profile),
and Phase C (Xvfb autostart). Does not launch an actual browser.

Run:
    source venv/bin/activate && python -m unittest \
        superbrowser_bridge.tests.test_stealth -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_NANOBOT_ROOT = Path(__file__).resolve().parents[2]
if str(_NANOBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_NANOBOT_ROOT))

from superbrowser_bridge.antibot import interactive_session as _is  # noqa: E402


class DomainSafeTests(unittest.TestCase):
    def test_strips_www_and_lowercases(self) -> None:
        self.assertEqual(_is._domain_safe("WWW.Cars.Com"), "cars.com")

    def test_sanitizes_unsafe_chars(self) -> None:
        self.assertEqual(_is._domain_safe("foo/bar?evil"), "foo_bar_evil")

    def test_empty_becomes_blank_sentinel(self) -> None:
        self.assertEqual(_is._domain_safe(""), "_blank")

    def test_preserves_subdomains(self) -> None:
        self.assertEqual(
            _is._domain_safe("shop.acme.example.com"),
            "shop.acme.example.com",
        )


class ProfileDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_env = os.environ.get("T3_PROFILE_ROOT")
        os.environ["T3_PROFILE_ROOT"] = self._tmp.name

    def tearDown(self) -> None:
        if self._orig_env is None:
            os.environ.pop("T3_PROFILE_ROOT", None)
        else:
            os.environ["T3_PROFILE_ROOT"] = self._orig_env
        self._tmp.cleanup()

    def test_creates_per_domain_directory(self) -> None:
        d = _is._resolve_profile_dir("cars.com")
        self.assertTrue(d.exists())
        self.assertEqual(d.name, "cars.com")

    def test_env_override_root_honored(self) -> None:
        d = _is._resolve_profile_dir("example.test")
        self.assertTrue(str(d).startswith(self._tmp.name))

    def test_evicts_oversized_profile(self) -> None:
        # Seed a tiny oversized profile; cap to 0 MB so eviction fires.
        d = _is._resolve_profile_dir("big.test")
        (d / "payload.bin").write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MB
        os.environ["T3_PROFILE_MAX_MB"] = "0"
        try:
            d2 = _is._resolve_profile_dir("big.test")
            self.assertFalse((d2 / "payload.bin").exists())
            self.assertTrue(d2.exists())
        finally:
            os.environ.pop("T3_PROFILE_MAX_MB", None)


class EvaluateWrapTests(unittest.TestCase):
    """T3SessionManager.evaluate wraps statement-body scripts in an IIFE
    so `return`-at-top-level doesn't raise 'Illegal return statement'.
    We test the wrap heuristic by running the same regex locally — the
    real page.evaluate is too heavy to bring up in unit tests.
    """

    def _would_wrap(self, script: str) -> bool:
        import re as _re
        body = script.strip()
        if not body:
            return False
        starts_function = body.startswith(
            ("(", "async ", "async(", "function", "=>", "{")
        )
        if starts_function:
            return False
        has_top_return = _re.search(
            r"(?:^|[\s;{])return(?:$|[\s(;])", body,
        ) is not None
        has_multi_stmt = ";" in body.rstrip(" \t\n;")
        return has_top_return or has_multi_stmt

    def test_bare_expression_not_wrapped(self) -> None:
        self.assertFalse(self._would_wrap("document.title"))
        self.assertFalse(self._would_wrap("window.location.href"))

    def test_arrow_function_not_wrapped(self) -> None:
        self.assertFalse(self._would_wrap("() => document.title"))
        self.assertFalse(self._would_wrap(
            "async () => { const x = 1; return x; }"
        ))

    def test_already_iife_not_wrapped(self) -> None:
        self.assertFalse(self._would_wrap(
            "(() => { const x = 1; return x; })()"
        ))

    def test_return_at_top_level_is_wrapped(self) -> None:
        self.assertTrue(self._would_wrap(
            "const el = document.querySelector('a'); return el.href;"
        ))

    def test_multi_statement_body_is_wrapped(self) -> None:
        self.assertTrue(self._would_wrap(
            "const a = 1; const b = 2; a + b"
        ))


class XvfbGateTests(unittest.TestCase):
    """The xvfb autostart is all control flow; we only verify the
    no-op paths (actually spawning Xvfb requires the binary + display
    infra, out of scope for unit tests).
    """

    def setUp(self) -> None:
        # Reset module state so each test starts fresh.
        _is._XVFB_STARTED = False
        # Save + clear env so tests don't pollute each other.
        self._saved = {
            k: os.environ.get(k)
            for k in ("T3_AUTO_XVFB", "DISPLAY", "T3_XVFB_DISPLAY")
        }
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_headless_is_noop(self) -> None:
        _is._maybe_start_xvfb(headless=True)
        self.assertIsNone(os.environ.get("DISPLAY"))
        self.assertFalse(_is._XVFB_STARTED)

    def test_opt_out_respected(self) -> None:
        os.environ["T3_AUTO_XVFB"] = "0"
        _is._maybe_start_xvfb(headless=False)
        self.assertIsNone(os.environ.get("DISPLAY"))
        self.assertFalse(_is._XVFB_STARTED)

    def test_existing_display_not_overridden(self) -> None:
        os.environ["DISPLAY"] = ":0"
        _is._maybe_start_xvfb(headless=False)
        # DISPLAY unchanged, _XVFB_STARTED stays False (no new spawn).
        self.assertEqual(os.environ["DISPLAY"], ":0")
        self.assertFalse(_is._XVFB_STARTED)

    def test_started_once_only(self) -> None:
        # Prime the flag so the function bails on the "already started"
        # branch — exercises the idempotency path without actually
        # spawning a child process.
        _is._XVFB_STARTED = True
        _is._maybe_start_xvfb(headless=False)
        self.assertIsNone(os.environ.get("DISPLAY"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
