"""Diagnose proxy + cars.com setup.

Run from `runagent-superbrowser/nanobot/` with the venv active:
    source ../venv/bin/activate && python diag_proxy.py

Reports:
  1. .env values picked up at import time (PROXY_POOL, RESIDENTIAL, DISPLAY, T3_*).
  2. proxy_tiers.pick('cars.com') — what proxy the T3 path will actually use.
  3. Proxy reachability via curl_cffi (T1 layer).
  4. T3 launch with current proxy → ipv4.icanhazip.com → reports the IP
     the launched Chrome actually egresses through.
  5. T3 launch → cars.com → reports the HTTP status, title, and whether
     CF interstitial is detected.
"""

from __future__ import annotations
import asyncio, os, sys, traceback
sys.path.insert(0, "/root/agentic-browser/runagent-superbrowser/nanobot")

# Trigger .env load FIRST.
import superbrowser_bridge  # noqa: F401

print("=" * 70)
print("ENV (post-dotenv)")
print("=" * 70)
for k in [
    "DISPLAY", "T3_HEADLESS", "T3_AUTO_XVFB", "T3_XVFB_DISPLAY",
    "T3_PERSIST_PROFILE", "PROXY_POOL", "PROXY_POOL_RESIDENTIAL",
    "PROXY_DEFAULT", "CHROME_PATH", "CAPTCHA_API_KEY",
]:
    v = os.environ.get(k, "<unset>")
    if k == "CAPTCHA_API_KEY" and v != "<unset>":
        v = f"<set, len={len(v)}>"
    if "PROXY" in k and v != "<unset>" and len(v) > 70:
        v = v[:50] + "..." + v[-15:]
    print(f"  {k} = {v}")


async def main() -> int:
    # ---- 2. proxy_tiers.pick ---------------------------------------------
    print()
    print("=" * 70)
    print("PROXY_TIERS")
    print("=" * 70)
    from superbrowser_bridge.antibot import proxy_tiers
    pt = proxy_tiers.default()
    print(f"  snapshot: {pt.snapshot()}")
    pick = pt.pick("cars.com")
    if pick:
        print(f"  pick('cars.com') -> {pick[:50]}...{pick[-15:]}")
    else:
        print(f"  pick('cars.com') -> None  (will go direct, no proxy applied)")

    # ---- 3. T1-style proxy reachability via curl_cffi ---------------------
    print()
    print("=" * 70)
    print("PROXY REACHABILITY (curl_cffi)")
    print("=" * 70)
    if not pick:
        print("  no proxy picked, skipping")
    else:
        try:
            from curl_cffi import requests as _cc
            r = _cc.get(
                "https://ipv4.icanhazip.com",
                proxies={"http": pick, "https": pick},
                timeout=15,
            )
            print(f"  HTTP {r.status_code}  IP-from-proxy: {r.text.strip()}")
        except Exception as exc:
            print(f"  PROXY CURL FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    # ---- 4. T3 browser launch + ip check ---------------------------------
    print()
    print("=" * 70)
    print("T3 BROWSER LAUNCH (via patchright + applied proxy)")
    print("=" * 70)
    from superbrowser_bridge.antibot.interactive_session import T3SessionManager
    mgr = T3SessionManager()
    try:
        info = await mgr.open(
            url="https://ipv4.icanhazip.com/",
            task_id="diag-proxy",
            timeout_s=30.0,
            max_stealth=True,
        )
        sid = info["sessionId"]
        s = mgr._get(sid)
        body = await s.page.evaluate(
            "() => (document.body ? document.body.innerText : '').trim()"
        )
        print(f"  T3 sid={sid}")
        print(f"  T3 url={s.page.url}")
        print(f"  T3 IP-from-browser: {body!r}")
        await mgr.close(sid)
    except Exception as exc:
        print(f"  T3 LAUNCH FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()

    # ---- 5. T3 -> cars.com headers + status ------------------------------
    print()
    print("=" * 70)
    print("T3 → cars.com (CF check)")
    print("=" * 70)
    mgr2 = T3SessionManager()
    try:
        info = await mgr2.open(
            url="https://www.cars.com/",
            task_id="diag-cars",
            timeout_s=30.0,
            max_stealth=True,
        )
        sid = info["sessionId"]
        s = mgr2._get(sid)
        # Settle for CF iframe.
        await asyncio.sleep(4.0)
        title = await s.page.title()
        url = s.page.url
        body_top = await s.page.evaluate(
            "() => (document.body ? document.body.innerText.slice(0, 200) : '')"
        )
        ray = ""
        try:
            ray = await s.page.evaluate(
                "() => { const m = (document.body.innerText.match(/Ray ID:\\s*([0-9a-f]+)/i) || []); return m[1] || ''; }"
            )
        except Exception:
            pass
        cf_frames = [f.url for f in s.page.frames if "challenges.cloudflare.com" in (f.url or "")]
        print(f"  url:       {url}")
        print(f"  title:     {title!r}")
        print(f"  status:    {info.get('statusCode')!r}")
        print(f"  block_class: {info.get('block_class') or info.get('blockClass')!r}")
        print(f"  body[:200]: {body_top!r}")
        print(f"  CF Ray ID: {ray!r}")
        print(f"  CF frames: {cf_frames}")
        await mgr2.close(sid)
    except Exception as exc:
        print(f"  T3->cars.com FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
