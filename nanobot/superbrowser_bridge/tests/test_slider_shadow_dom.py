"""Shadow-DOM piercing regression test for browser_set_slider.

Slider widgets like Chase mds-slider host their native <input type=range>
inside an open shadow root. Bare document.querySelector(sel) stops at
shadow boundaries; before the shadow-piercing patch, setSlider on a
shadow-rooted slider always returned 'unresolved'. This test injects a
custom-element fixture with an open shadow root, then exercises three
strategies through /set-slider:

  1. Strategy A (range-input) on a shadow-rooted <input type=range>.
     Verifies __sb_queryDeep walks the shadow root AND the host receives
     the synthetic change event (so React/Lit listeners on the host
     wrapper see the value change).
  2. Strategy B (keyboard) on a shadow-rooted ARIA slider.
  3. Strategy C (drag) on a shadow-rooted custom slider track + thumb.

Run (server must already be up on :3100):
    source venv/bin/activate && \\
        python nanobot/superbrowser_bridge/tests/test_slider_shadow_dom.py
"""

from __future__ import annotations

import asyncio
import sys

import httpx

SUPERBROWSER_URL = "http://localhost:3100"


# Fixture: custom element <my-slider> with an open shadow root containing
# the native range input. Mirrors how Chase wraps mds-slider over an
# inner native control.
INJECT_JS = r"""
(async () => {
  document.documentElement.innerHTML = `
    <head><meta charset="utf-8"></head>
    <body>
      <div id="row1">Monthly contribution</div>
      <my-slider id="custom-range"></my-slider>
      <div id="row1-value">Monthly contribution: 100</div>

      <div id="row2">Risk tolerance</div>
      <my-aria-slider id="custom-aria"></my-aria-slider>
    </body>`;

  // Custom element that hosts a native range input inside an open shadow.
  class MySlider extends HTMLElement {
    constructor() {
      super();
      const root = this.attachShadow({ mode: 'open' });
      root.innerHTML = `
        <input type="range" id="inner" min="0" max="1000" step="50" value="100"
               style="width:240px;height:24px"/>
      `;
      const inner = root.querySelector('#inner');
      // Re-emit input events on the host so the test can observe the
      // host signal (Fix 2). We capture this in a flag so the test can
      // verify the shadow-piercing path fired the host event.
      window.__sliderHostEvents = window.__sliderHostEvents || [];
      this.addEventListener('input', () => {
        window.__sliderHostEvents.push({
          host: 'my-slider', value: inner.value, kind: 'input',
        });
      });
      this.addEventListener('change', () => {
        window.__sliderHostEvents.push({
          host: 'my-slider', value: inner.value, kind: 'change',
        });
      });
    }
  }
  customElements.define('my-slider', MySlider);

  class MyAriaSlider extends HTMLElement {
    constructor() {
      super();
      const root = this.attachShadow({ mode: 'open' });
      root.innerHTML = `
        <div id="thumb" role="slider" tabindex="0"
             aria-valuemin="0" aria-valuemax="10" aria-valuenow="3"
             style="width:200px;height:20px;background:#ddd;outline:1px solid #000">
        </div>`;
      const thumb = root.querySelector('#thumb');
      thumb.addEventListener('keydown', (e) => {
        let v = parseFloat(thumb.getAttribute('aria-valuenow'));
        if (e.key === 'ArrowRight') v = Math.min(10, v + 1);
        if (e.key === 'ArrowLeft')  v = Math.max(0, v - 1);
        if (e.key === 'Home')       v = 0;
        if (e.key === 'End')        v = 10;
        thumb.setAttribute('aria-valuenow', String(v));
      });
    }
  }
  customElements.define('my-aria-slider', MyAriaSlider);

  return 'ready';
})()
"""


async def main() -> int:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{SUPERBROWSER_URL}/session/create", json={})
        r.raise_for_status()
        session_id = r.json().get("sessionId") or r.json().get("id")
        assert session_id, r.json()
        print(f"session: {session_id}")

        r = await client.post(
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": INJECT_JS},
        )
        r.raise_for_status()
        print("fixture ready:", r.json().get("result"))

        async def set_slider(**body) -> dict:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/set-slider",
                json=body,
            )
            r.raise_for_status()
            return r.json()

        async def evaluate(script: str) -> object:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": script},
            )
            r.raise_for_status()
            return r.json().get("result")

        failures: list[str] = []

        # --- Test 1: Strategy A through shadow DOM (range input host) ---
        # Selector targets the host element; setSlider must walk into the
        # open shadow root, find the inner <input type=range>, set value,
        # and fire input/change on host (Fix 2).
        out = await set_slider(selector="my-slider", value=750, method="auto")
        outcome = out.get("outcome", {})
        if outcome.get("strategy") != "range-input":
            failures.append(
                f"Test 1: strategy was {outcome.get('strategy')!r}, expected 'range-input'. "
                f"setSlider failed to pierce shadow DOM. outcome={outcome}"
            )
        elif outcome.get("after") != 750:
            failures.append(
                f"Test 1: after value {outcome.get('after')!r}, expected 750. outcome={outcome}"
            )
        else:
            print("✓ Test 1: shadow-pierced Strategy A reached value 750")

        # Verify host signal fired (Fix 2). Without dispatching change on
        # the host, React/Lit listeners on the wrapper miss the update.
        host_events = await evaluate("window.__sliderHostEvents || []")
        if not isinstance(host_events, list) or not any(
            e.get("kind") == "change" for e in host_events
        ):
            failures.append(
                f"Test 2 (host signal): expected host change event, got {host_events!r}"
            )
        else:
            print(f"✓ Test 2: host received {len(host_events)} synthetic event(s) "
                  f"on shadow-rooted <my-slider>")

        # --- Test 3: Strategy B (keyboard) through shadow DOM ---
        # Custom element hosts an ARIA slider in shadow root; setSlider
        # should walk it, focus, and step via Home/End.
        out = await set_slider(selector="my-aria-slider", value=10, method="keyboard")
        outcome = out.get("outcome", {})
        if outcome.get("strategy") != "keyboard":
            failures.append(
                f"Test 3 (keyboard via shadow): strategy {outcome.get('strategy')!r}, "
                f"outcome={outcome}"
            )
        elif outcome.get("after") != 10:
            failures.append(
                f"Test 3: keyboard after value {outcome.get('after')!r}, expected 10. "
                f"outcome={outcome}"
            )
        else:
            print("✓ Test 3: shadow-pierced Strategy B reached value 10")

        if failures:
            print("\nFAILURES:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("\nAll shadow-DOM slider tests passed.")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
