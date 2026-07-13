"""Regression test for wrapper-descent in `_ATOMIC_FIX_TEXT_JS`.

Many real inputs (petfinder.com's #findAPetLocation, Google Maps'
#searchboxinput, MUI Autocomplete, plenty of Tailwind designs) are
wrapped in a styled clickable parent. `document.elementFromPoint(x, y)`
at the visible centerpoint returns the wrapper — which is neither
input/textarea nor contenteditable — so the pre-fix atomic JS bailed
with `not_input` and the bridge fell back to the (broken) clearField
keyboard path, which APPENDS to existing content.

This test asserts:

  1. A `<div>` wrapper around an `<input>` resolves through pass-A
     (containment-scoped descendant): atomic JS finds the input and
     replaces its value.
  2. A wrapper with a SINGLE `<input>` descendant whose bbox does not
     geometrically contain the click point still resolves through
     pass-B (single-input fallback).
  3. A wrapper with TWO inputs and a click point outside both does NOT
     guess — must still return `not_input`. Negative case proves the
     descent doesn't silently pick a random descendant.
  4. The default replace path on the resolved element is exact: a
     pre-filled value of "finland" + target "94587" yields "94587",
     not "finland94587".

Run (server must be up on :3100):
    source venv/bin/activate && \\
        python nanobot/superbrowser_bridge/tests/test_atomic_fix_wrapper_descent.py
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

SUPERBROWSER_URL = "http://localhost:3100"


# Three fixtures, three click points:
#   #wrap-a   wraps  #real-a   point falls INSIDE #real-a's bbox     → pass A
#   #wrap-b   wraps  #real-b   point falls OUTSIDE #real-b's bbox    → pass B
#                              (single-input fallback)
#   #wrap-c   wraps  #real-c1 + #real-c2  point falls OUTSIDE both   → must bail
#
# Wrapper #wrap-d is the petfinder analogue: pre-filled "finland",
# wrapped, point on wrapper. Asserts replace not append.
INJECT_JS = r"""
(() => {
  document.documentElement.innerHTML = `
    <head><meta charset="utf-8"></head>
    <body style="margin:0;padding:0">
      <div id="wrap-a" style="position:absolute;left:50px;top:50px;width:300px;height:60px;padding:15px;background:#eee;cursor:text">
        <input id="real-a" type="text"
               style="width:200px;height:30px"/>
      </div>

      <div id="wrap-b" style="position:absolute;left:50px;top:140px;width:300px;height:60px;padding:15px;background:#ddd;cursor:text">
        <input id="real-b" type="text"
               style="position:absolute;left:1px;top:1px;width:50px;height:20px"/>
      </div>

      <div id="wrap-c" style="position:absolute;left:50px;top:230px;width:300px;height:60px;padding:5px;background:#ccc">
        <input id="real-c1" type="text"
               style="position:absolute;left:5px;top:5px;width:80px;height:25px"/>
        <input id="real-c2" type="text"
               style="position:absolute;left:200px;top:5px;width:80px;height:25px"/>
      </div>

      <div id="wrap-d" style="position:absolute;left:50px;top:320px;width:300px;height:60px;padding:15px;background:#fafafa;cursor:text">
        <input id="real-d" type="text" value="finland"
               style="width:200px;height:30px"/>
      </div>
    </body>`;
  return 'ready';
})()
"""


def _build_atomic_js(target_x: float, target_y: float, target_text: str) -> str:
    from superbrowser_bridge.session_tools import render_atomic_text_js
    return render_atomic_text_js(target_x, target_y, target_text, mode="replace")


async def _read_value(client: httpx.AsyncClient, session_id: str, sel: str) -> str:
    r = await client.post(
        f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
        json={"script": (
            f"(() => {{ const el = document.querySelector('{sel}'); "
            f"return el ? el.value : null; }})()"
        )},
    )
    r.raise_for_status()
    return str(r.json().get("result") or "")


async def main() -> int:
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
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": INJECT_JS},
            )
            r.raise_for_status()
            print("fixture ready:", r.json().get("result"))

            # ── Case 1: pass A (containment) ─────────────────────────────
            # #wrap-a is at (50,50,350,110) with 15px padding.
            # #real-a is INSIDE that, roughly (65,65,265,95). Point (150,80)
            # falls on the input itself — but elementFromPoint lands on the
            # wrapper because the input is z-stacked beneath the div's
            # padding-clipped click target. Even when the point falls
            # inside both, the wrapper-descent should still pick the input.
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": _build_atomic_js(150.0, 80.0, "ALPHA")},
            )
            r.raise_for_status()
            result = r.json().get("result") or {}
            if not result.get("ok") or result.get("after") != "ALPHA":
                failures.append(f"passA: atomic JS rejected — {result}")
            else:
                print(f"OK  passA: typed {result.get('after')!r}")

            # ── Case 2: pass B (single-input fallback) ───────────────────
            # #wrap-b at (50,140,350,200) with #real-b inset to a small
            # 50x20 box at top-left. Click center of wrap-b (200,170) is
            # OUTSIDE #real-b's bbox, but #real-b is the wrapper's only
            # input descendant — pass B should pick it.
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": _build_atomic_js(200.0, 170.0, "BETA")},
            )
            r.raise_for_status()
            result = r.json().get("result") or {}
            if not result.get("ok") or result.get("after") != "BETA":
                failures.append(f"passB: atomic JS rejected — {result}")
            else:
                print(f"OK  passB: typed {result.get('after')!r}")

            # ── Case 3: NEGATIVE — two inputs, point outside both ────────
            # #wrap-c has #real-c1 (5,5,85,30) and #real-c2 (200,5,280,30).
            # Click (150, 260) is at the wrapper but inside the gap
            # between the two inputs. Pass A finds no containing
            # descendant. Pass B doesn't fire because there are TWO
            # inputs. Result must be the unchanged `not_input` bail.
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": _build_atomic_js(150.0, 260.0, "GAMMA")},
            )
            r.raise_for_status()
            result = r.json().get("result") or {}
            if result.get("ok") or result.get("reason") != "not_input":
                failures.append(f"negative: should have bailed not_input — {result}")
            else:
                print(f"OK  negative: bailed cleanly with {result.get('reason')!r}")

            # Confirm neither input was touched.
            v1 = await _read_value(client, session_id, "#real-c1")
            v2 = await _read_value(client, session_id, "#real-c2")
            if v1 or v2:
                failures.append(
                    f"negative: descent silently wrote — c1={v1!r} c2={v2!r}"
                )

            # ── Case 4: petfinder analogue — pre-filled REPLACE ──────────
            # #wrap-d wraps an input pre-filled with "finland". Click
            # center of wrapper (200, 350). Atomic JS must replace, not
            # append: final value === "94587", not "finland94587".
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": _build_atomic_js(200.0, 350.0, "94587")},
            )
            r.raise_for_status()
            result = r.json().get("result") or {}
            if not result.get("ok") or result.get("after") != "94587":
                failures.append(f"petfinder: atomic JS rejected — {result}")
            elif result.get("before") != "finland":
                failures.append(
                    f"petfinder: pre-value not seen — before={result.get('before')!r}"
                )
            else:
                print(
                    f"OK  petfinder: replaced "
                    f"{result.get('before')!r} → {result.get('after')!r}"
                )

            v_d = await _read_value(client, session_id, "#real-d")
            if v_d != "94587":
                failures.append(f"petfinder: DOM value wrong — {v_d!r}")

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
