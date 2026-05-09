"""Run the 4-phase solver against cars.com end-to-end with whatever
proxy/profile/env the runtime currently has. Prints the full trace."""

from __future__ import annotations
import asyncio, sys, time, traceback
sys.path.insert(0, "/root/agentic-browser/runagent-superbrowser/nanobot")
import superbrowser_bridge  # noqa: F401

from superbrowser_bridge.antibot.interactive_session import T3SessionManager
from superbrowser_bridge.antibot.captcha.detect import detect as run_detect
from superbrowser_bridge.antibot.captcha.solve_cf import solve_cf_interstitial


async def main() -> int:
    mgr = T3SessionManager()
    print("==> opening cars.com (max_stealth=True)...")
    info = await mgr.open(
        url="https://www.cars.com/", task_id="solve-diag",
        timeout_s=45.0, max_stealth=True,
    )
    sid = info["sessionId"]
    s = mgr._get(sid)
    await asyncio.sleep(3.0)

    print("\n==> detect()")
    cap = await run_detect(mgr, sid)
    print(f"  type={cap.type} present={cap.present}")
    print(f"  notes={cap.notes}")
    print(f"  frame_url={cap.frame_url[:100]}")
    print(f"  site_key={cap.site_key!r}")

    if not (cap.present and cap.type == "cf_interstitial"):
        print(f"\n==> not a CF interstitial; final url={s.page.url}")
        await mgr.close(sid)
        return 0

    print("\n==> screenshot before /tmp/diag_before.png")
    await s.page.screenshot(path="/tmp/diag_before.png")

    print("\n==> running solve_cf_interstitial...")
    t0 = time.monotonic()
    try:
        result = await solve_cf_interstitial(mgr, sid, cap)
    except Exception as exc:
        print(f"  RAISED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        await mgr.close(sid)
        return 2
    elapsed = time.monotonic() - t0

    print(f"\n==> RESULT (in {elapsed:.1f}s):")
    for k in ("solved", "subMethod", "iterations", "cookies_landed",
             "final_url", "final_title", "block_class", "error"):
        if k in result:
            print(f"  {k}: {result[k]}")
    if result.get("trace"):
        print("  trace:")
        for line in result["trace"]:
            print(f"    - {line}")

    print("\n==> screenshot after /tmp/diag_after.png")
    await s.page.screenshot(path="/tmp/diag_after.png")
    print("\n==> closing")
    await mgr.close(sid)
    return 0 if result.get("solved") else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
