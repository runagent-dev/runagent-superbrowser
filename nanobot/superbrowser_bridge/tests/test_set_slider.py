"""Integration test for browser_set_slider.

Creates a session, injects a slider fixture into about:blank via
/session/:id/evaluate (no TOKEN needed), then exercises /set-slider
against native range input, ARIA slider, and a same-origin iframe.

Run (server must already be up on :3100):
    source venv/bin/activate && python nanobot/superbrowser_bridge/tests/test_set_slider.py
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
    <body>
      <input id="native" type="range" min="0" max="1000" step="50" value="100" />
      <div id="aria" role="slider" tabindex="0"
           aria-valuemin="0" aria-valuemax="10" aria-valuenow="3"
           style="width:200px;height:20px;background:#ddd;outline:1px solid #000">
      </div>
      <iframe id="frame" style="width:400px;height:100px"></iframe>
    </body>`;
  const aria = document.getElementById('aria');
  aria.addEventListener('keydown', (e) => {
    let v = parseFloat(aria.getAttribute('aria-valuenow'));
    if (e.key === 'ArrowRight') v = Math.min(10, v + 1);
    if (e.key === 'ArrowLeft')  v = Math.max(0, v - 1);
    if (e.key === 'Home')       v = 0;
    if (e.key === 'End')        v = 10;
    aria.setAttribute('aria-valuenow', String(v));
  });
  const f = document.getElementById('frame');
  await new Promise((r) => {
    f.onload = r;
    f.srcdoc = '<input id="inner" type="range" min="0" max="100" value="10"/>';
  });
  return 'ready';
})()
"""


async def main() -> int:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{SUPERBROWSER_URL}/session/create", json={})
        r.raise_for_status()
        data = r.json()
        session_id = data.get("sessionId") or data.get("id")
        assert session_id, data
        print(f"session: {session_id}")

        # Inject fixture HTML.
        r = await client.post(
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": INJECT_JS},
        )
        r.raise_for_status()
        print("fixture ready:", r.json().get("result"))

        # Sanity check: does the fixture actually exist right before we set?
        r = await client.post(
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": (
                "(() => ({ url: location.href, native: !!document.getElementById('native'),"
                " aria: !!document.getElementById('aria'),"
                " frameCount: document.querySelectorAll('iframe').length }))()"
            )},
        )
        r.raise_for_status()
        print("sanity:", r.json().get("result"))

        async def set_slider(**body) -> dict:
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/set-slider",
                json=body,
            )
            if r.status_code >= 400:
                print(f"  HTTP {r.status_code}: {r.text}")
            r.raise_for_status()
            return r.json()["outcome"]

        failures: list[str] = []

        def check(label: str, out: dict, want_strategy: str, want_after) -> None:
            got = out.get("after")
            if out.get("strategy") != want_strategy or got != want_after:
                failures.append(f"{label}: want strategy={want_strategy} after={want_after}, got {out}")
            else:
                print(f"OK  {label}: strategy={want_strategy} after={got}")

        out = await set_slider(selector="#native", value=700)
        check("native absolute", out, "range-input", 700)

        # Ratio mode: 0.5 on [0,1000] = 500.
        out = await set_slider(selector="#native", value=0.5, **{"as": "ratio"})
        check("native ratio", out, "range-input", 500)

        out = await set_slider(selector="#aria", value=7)
        check("aria keyboard", out, "keyboard", 7)

        out = await set_slider(selector="#inner", value=85)
        check("iframe native", out, "range-input", 85)

        # Cleanup.
        try:
            await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/close")
        except Exception:
            pass

        if failures:
            print("\nFAIL:")
            for f in failures:
                print(f"  {f}")
            return 1
        print("\nOK — all strategies moved their sliders.")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
