"""Quick verification that browser_set_slider handles the real Chase IRA
calculator's native range inputs. No LLM involved — directly probes the
page and calls /set-slider for each slider.

Run: source venv/bin/activate && python nanobot/superbrowser_bridge/tests/test_chase_slider.py
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

SUPERBROWSER_URL = "http://localhost:3100"
TARGET_URL = (
    "https://www.chase.com/personal/investments/retirement/retirement-calculators/"
    "traditional-ira-calculator"
)


async def main() -> int:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{SUPERBROWSER_URL}/session/create",
            json={"url": TARGET_URL},
        )
        if r.status_code >= 400:
            print(f"create failed: {r.status_code} {r.text}")
            return 1
        data = r.json()
        session_id = data.get("sessionId") or data.get("id")
        print(f"session: {session_id}")

        # Wait a beat for the calculator to hydrate.
        await asyncio.sleep(4)

        # Enumerate candidate range inputs in main frame + all iframes.
        probe = r"""
        (() => {
          const out = { main: [], frames: [] };
          const readAll = (doc, tag) => {
            const els = Array.from(doc.querySelectorAll('input[type=range], [role=slider]'));
            return els.map((el) => ({
              tag: el.tagName.toLowerCase(),
              id: el.id || null,
              name: el.name || null,
              role: el.getAttribute('role'),
              min: el.min || el.getAttribute('aria-valuemin'),
              max: el.max || el.getAttribute('aria-valuemax'),
              value: el.value || el.getAttribute('aria-valuenow'),
              ariaLabel: el.getAttribute('aria-label'),
            }));
          };
          out.main = readAll(document);
          Array.from(document.querySelectorAll('iframe')).forEach((f, i) => {
            try {
              const doc = f.contentDocument;
              if (doc) out.frames.push({ idx: i, src: f.src, els: readAll(doc) });
              else out.frames.push({ idx: i, src: f.src, els: null, reason: 'no contentDocument' });
            } catch (e) {
              out.frames.push({ idx: i, src: f.src, els: null, reason: String(e) });
            }
          });
          return out;
        })()
        """
        r = await client.post(
            f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
            json={"script": probe},
        )
        r.raise_for_status()
        result = r.json().get("result") or {}
        print("sliders on main frame:", len(result.get("main", [])))
        for el in result.get("main", []):
            print(" ", el)
        print("sliders in iframes:")
        for fr in result.get("frames", []):
            print(f"  [{fr['idx']}] src={fr['src']!r}")
            if fr.get("els") is None:
                print(f"    (no access: {fr.get('reason')})")
            else:
                for el in fr["els"]:
                    print(f"    {el}")

        # If we found any range inputs, try to set the first one to a mid-ish value.
        all_targets = []
        for el in result.get("main", []):
            if el.get("tag") == "input":
                sel = f"input[type=range]#{el['id']}" if el.get("id") else "input[type=range]"
                all_targets.append((sel, el))
        if not all_targets:
            # Look inside same-origin iframes.
            for fr in result.get("frames", []):
                for el in fr.get("els") or []:
                    if el.get("tag") == "input" and el.get("id"):
                        all_targets.append((f"#{el['id']}", el))

        if not all_targets:
            # Cross-origin iframe uses custom slider widgets (no native range
            # inputs, no standard ARIA attributes). Confirm: the setSlider
            # tool resolves into the iframe and dispatches the drag.
            print("main-frame probe empty (cross-origin iframe).")
            print("Chase uses custom slider widgets — attempting drag via setSlider...")
            # Snap screenshot of the sliders region before + after.
            # Try hitting the 3rd slider-ish element (Monthly contribution is
            # typically 3rd in the form: Age, Balance, Contribution).
            sel = "[class*=slider i]"
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/set-slider",
                json={"selector": sel, "value": 0.75, "as": "ratio", "method": "drag"},
            )
            outcome = r.json().get("outcome", {})
            print(f"  setSlider({sel!r}, 0.75 ratio, drag): strategy={outcome.get('strategy')} "
                  f"frame={outcome.get('frameUrl', '')[:80]}")

            # Give the page a moment to repaint, then confirm the iframe
            # is still functional (we didn't crash it) by reading the
            # main-frame title.
            await asyncio.sleep(1.5)
            r = await client.post(
                f"{SUPERBROWSER_URL}/session/{session_id}/evaluate",
                json={"script": "document.title"},
            )
            print(f"  page still alive: title={r.json().get('result', '')[:80]!r}")
            try:
                await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/close")
            except Exception:
                pass
            return 0 if outcome.get("strategy") in ("drag", "range-input", "keyboard") else 1

        sel, meta = all_targets[0]
        mn = float(meta.get("min") or 0)
        mx = float(meta.get("max") or 100)
        tgt = round(mn + 0.7 * (mx - mn), 4)
        print(f"\nattempting: set {sel} → {tgt} (range {mn}..{mx})")
        r = await client.post(
            f"{SUPERBROWSER_URL}/session/{session_id}/set-slider",
            json={"selector": sel, "value": tgt},
        )
        if r.status_code >= 400:
            print(f"  HTTP {r.status_code}: {r.text}")
        else:
            print(json.dumps(r.json().get("outcome"), indent=2))

        try:
            await client.post(f"{SUPERBROWSER_URL}/session/{session_id}/close")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
