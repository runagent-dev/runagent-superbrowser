"""Regression test for the React `_valueTracker` invalidation in
`_ATOMIC_FIX_TEXT_JS`.

The atomic-fix JS in `session_tools/effects.py` writes via the prototype
`value` setter and dispatches `InputEvent('input')`. On React-controlled
inputs this is not enough by itself: React 16+ caches the prior value in
`el._valueTracker.getValue()` and short-circuits its synthetic onChange
when the cached value matches the new value at dispatch time. The fix
is to call `tracker.setValue('')` before the prototype setter so the
tracker can never appear in-sync. This test asserts:

  1. The atomic JS calls `tracker.setValue('')` on a React-style input
     fixture (proves the reset path fires).
  2. A listener that mimics React's short-circuit (skip if
     `tracker.getValue() === el.value`) sees a real change and bumps
     its counter (proves the user-visible behavior).
  3. A vanilla input (no `_valueTracker`) still types correctly — the
     guard `if (tracker)` keeps the non-React path a no-op.

Run (server must be up on :3100):
    source venv/bin/activate && \\
        python nanobot/superbrowser_bridge/tests/test_atomic_fix_react_tracker.py
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

SUPERBROWSER_URL = "http://localhost:3100"


INJECT_JS = r"""
(() => {
  document.documentElement.innerHTML = `
    <head><meta charset="utf-8"></head>
    <body style="margin:0;padding:0">
      <input id="with-tracker" type="text"
             style="position:absolute;left:50px;top:50px;width:200px;height:30px"/>
      <input id="without-tracker" type="text"
             style="position:absolute;left:50px;top:120px;width:200px;height:30px"/>
    </body>`;

  // Fixture state lives on window so we can read it back from a later
  // /evaluate call without polluting the inputs.
  window.__rt = { setValueCalls: [], onChangeCount: 0 };

  // React-style tracker on #with-tracker. Mirrors the contract of
  // ReactDOM/inputValueTracking: { getValue(), setValue(v) }.
  const reactInput = document.getElementById('with-tracker');
  let trackerValue = '';
  reactInput._valueTracker = {
    getValue() { return trackerValue; },
    setValue(v) {
      trackerValue = String(v);
      window.__rt.setValueCalls.push(String(v));
    },
  };

  // Listener that mimics React's onChange short-circuit: if the tracker
  // already matches the input's current value at dispatch time, React
  // assumes "no change" and skips the synthetic onChange. Bumping the
  // counter only when they DIFFER lets us assert the user-visible path.
  reactInput.addEventListener('input', () => {
    if (reactInput._valueTracker.getValue() === reactInput.value) return;
    window.__rt.onChangeCount += 1;
    // React would also sync the tracker after dispatching onChange.
    reactInput._valueTracker.setValue(reactInput.value);
  });

  return 'ready';
})()
"""


# Mirrors session_tools/tools/input_text.py:177-183 — same atomic JS the
# production browser_type_at tool uses, with the same placeholder swap.
def _build_atomic_js(target_x: float, target_y: float, target_text: str) -> str:
    from superbrowser_bridge.session_tools import _ATOMIC_FIX_TEXT_JS
    return (
        _ATOMIC_FIX_TEXT_JS
        .replace("__TARGET_X__", str(float(target_x)))
        .replace("__TARGET_Y__", str(float(target_y)))
        .replace("__TARGET_TEXT__", json.dumps(target_text))
    )


async def main() -> int:
    # Make `from superbrowser_bridge...` importable when run as a script
    # (matches the pattern at the top of test_type_verify.py).
    from pathlib import Path
    nanobot_root = Path(__file__).resolve().parents[2]
    if str(nanobot_root) not in sys.path:
        sys.path.insert(0, str(nanobot_root))

    failures: list[str] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(f"{SUPERBROWSER_URL}/session/create", json={})
        except httpx.ConnectError:
            print(f"SKIP: superbrowser not running at {SUPERBROWSER_URL}")
            return 0
        r.raise_for_status()
        data = r.json()
        session_id = data.get("sessionId") or data.get("id")
        assert session_id, data
        print(f"session: {session_id}")

        try:
            # Inject the fixture (about:blank → controlled inputs).
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": INJECT_JS},
            )
            r.raise_for_status()
            print("fixture ready:", r.json().get("result"))

            # ── Case 1: React-style input ────────────────────────────────
            # Centerpoint of the #with-tracker input (left:50, top:50,
            # width:200, height:30) is (150, 65).
            atomic = _build_atomic_js(150.0, 65.0, "coffee near times square")
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": atomic},
            )
            r.raise_for_status()
            result = r.json().get("result") or {}
            if not result.get("ok") or result.get("after") != "coffee near times square":
                failures.append(f"react: atomic JS rejected — {result}")
            else:
                print(f"OK  react: typed {result.get('after')!r}")

            # Read back the fixture state.
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": "JSON.stringify(window.__rt)"},
            )
            r.raise_for_status()
            rt = json.loads(r.json().get("result") or "{}")

            calls = rt.get("setValueCalls") or []
            if "" not in calls:
                failures.append(
                    f"react: tracker.setValue('') was NOT called — calls={calls}"
                )
            else:
                print(f"OK  react: tracker.setValue('') fired (calls={calls})")

            if (rt.get("onChangeCount") or 0) < 1:
                failures.append(
                    f"react: onChange counter never incremented — {rt}"
                )
            else:
                print(f"OK  react: onChange fired {rt.get('onChangeCount')}x")

            # ── Case 2: Vanilla input (no _valueTracker) ─────────────────
            # Centerpoint of the #without-tracker input (left:50, top:120,
            # width:200, height:30) is (150, 135).
            atomic = _build_atomic_js(150.0, 135.0, "vanilla input")
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": atomic},
            )
            r.raise_for_status()
            result = r.json().get("result") or {}
            if not result.get("ok") or result.get("after") != "vanilla input":
                failures.append(f"vanilla: atomic JS rejected — {result}")
            else:
                print(f"OK  vanilla: typed {result.get('after')!r}")

        finally:
            try:
                await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/close")
            except Exception:
                pass

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
