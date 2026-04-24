"""Integration test for the split-label DOM scanner in
dragSliderUntil.

Verifies the scanner finds the regex match when the label is split
across spans — the Chase-style layout that broke the previous
text-node walker:

    <label>Age Range: <span>25</span> to <span>75</span></label>

Run (tier-1 SuperBrowser must be up on :3100):
    source venv/bin/activate && python nanobot/superbrowser_bridge/tests/test_split_label_scanner.py
"""
from __future__ import annotations

import asyncio
import sys

import httpx

SUPERBROWSER_URL = "http://localhost:3100"

INJECT_JS = r"""
(async () => {
  document.documentElement.innerHTML = `
    <head><meta charset="utf-8"></head>
    <body style="font-family:sans-serif">
      <div id="row1" style="height:60px;padding:6px">
        <label style="display:block">Age Range: <span id="lo">25</span> to <span id="hi">75</span></label>
        <div id="handle1" style="width:20px;height:18px;background:#06f;border-radius:50%;
             position:relative;left:50px;top:8px" tabindex="0"></div>
      </div>
      <div id="row2" style="height:60px;padding:6px">
        <label style="display:block">Monthly contribution ($): <span id="mc">0</span></label>
        <div id="handle2" style="width:20px;height:18px;background:#06f;border-radius:50%;
             position:relative;left:30px;top:8px" tabindex="0"></div>
      </div>
    </body>`;

  // Simulate widgets: dragging the handle right increases the value.
  const wire = (handleId, valueIds, opts) => {
    const el = document.getElementById(handleId);
    const values = valueIds.map(id => document.getElementById(id));
    let startX = 0, baseVals = [];
    el.addEventListener('pointerdown', (e) => {
      el.setPointerCapture(e.pointerId);
      startX = e.clientX;
      baseVals = values.map(v => parseFloat(v.textContent));
    });
    el.addEventListener('pointermove', (e) => {
      if (!el.hasPointerCapture(e.pointerId)) return;
      const dx = e.clientX - startX;
      values.forEach((v, i) => {
        const b = baseVals[i];
        const delta = Math.round(dx / (opts.pxPerUnit || 4));
        const nv = Math.max(opts.min, Math.min(opts.max, b + delta));
        v.textContent = String(nv);
      });
    });
    el.addEventListener('pointerup', (e) => {
      try { el.releasePointerCapture(e.pointerId); } catch {}
    });
  };
  // Handle1 drives the "low" thumb only (first span).
  wire('handle1', ['lo'], { min: 18, max: 85, pxPerUnit: 4 });
  wire('handle2', ['mc'], { min: 0, max: 1000, pxPerUnit: 2 });
  return 'ready';
})()
"""


async def main() -> int:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{SUPERBROWSER_URL}/session/create", json={})
        r.raise_for_status()
        data = r.json()
        session_id = data.get("sessionId") or data.get("id")
        assert session_id
        print(f"session: {session_id}")

        r = await client.post(
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": INJECT_JS},
        )
        r.raise_for_status()
        print("fixture:", r.json().get("result"))

        # Get handle bboxes in CSS pixels via getBoundingClientRect.
        r = await client.post(
            f"{SUPERBROWSER_URL}/session/{session_id}/rect",
            json={"selectors": ["#handle1", "#handle2"]},
        )
        r.raise_for_status()
        rects = r.json().get("rects", [])
        print("handle rects:", rects)
        if not rects[0] or not rects[1]:
            print("FAIL: missing handle rects")
            return 1

        async def drag_slider(handle_rect, target, pattern):
            hb = {
                "x": handle_rect["x"], "y": handle_rect["y"],
                "w": handle_rect["w"], "h": handle_rect["h"],
            }
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/drag-slider-until",
                json={
                    "handle": hb,
                    "target_value": target,
                    "label_pattern": pattern,
                    "max_iterations": 40,
                    "step_px": 8,
                },
                timeout=60.0,
            )
            if r.status_code >= 400:
                print(f"  HTTP {r.status_code}: {r.text}")
            r.raise_for_status()
            return r.json()["outcome"]

        failures = []

        # Test 1: split label across spans (the Chase pattern).
        out = await drag_slider(rects[0], 40, r"Age Range:\s*(\d+)")
        print(f"\nAge Range (split label) → target 40:")
        print(f"  completed={out.get('completed')} "
              f"initial={out.get('initial_value')} "
              f"final={out.get('final_value')}")
        print(f"  label_text={out.get('label_text')!r}")
        print(f"  iterations={out.get('iterations')}")
        if not out.get("completed") or out.get("final_value") != 40:
            failures.append(f"split-label: {out}")

        # Test 2: single-span label (control case).
        out = await drag_slider(rects[1], 300, r"Monthly contribution[^:]*:\s*\$?(\d+)")
        print(f"\nMonthly contribution → target 300:")
        print(f"  completed={out.get('completed')} "
              f"initial={out.get('initial_value')} "
              f"final={out.get('final_value')}")
        print(f"  label_text={out.get('label_text')!r}")
        print(f"  iterations={out.get('iterations')}")
        if not out.get("completed") or out.get("final_value") != 300:
            failures.append(f"monthly: {out}")

        try:
            await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/close")
        except Exception:
            pass

        if failures:
            print("\nFAIL:")
            for f in failures:
                print(f"  {f}")
            return 1
        print("\nOK — split-label scanner works")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
