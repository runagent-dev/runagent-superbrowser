"""
Test captcha solving with SuperBrowser.

Prerequisites:
  1. Start SuperBrowser server:  cd .. && npm start
     (with ANTHROPIC_API_KEY or OPENAI_API_KEY for AI vision,
      and optionally CAPTCHA_PROVIDER=2captcha CAPTCHA_API_KEY=... for token/grid methods)
  2. Run:  python3 test_captcha.py [method]
     Methods: auto (default), token, ai_vision, grid
"""

import asyncio
import sys
import time
import httpx

SUPERBROWSER_URL = "http://localhost:3100"
RECAPTCHA_DEMO = "https://www.google.com/recaptcha/api2/demo"


async def test_captcha(method: str = "auto"):
    print(f"Testing captcha solving with method: {method}")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=180.0) as client:
        # 1. Create session on reCAPTCHA demo page
        print("\n1. Creating session on reCAPTCHA demo page...")
        r = await client.post(
            f"{SUPERBROWSER_URL}/session/create",
            json={"url": RECAPTCHA_DEMO},
        )
        r.raise_for_status()
        data = r.json()
        session_id = data["sessionId"]
        print(f"   Session: {session_id}")
        print(f"   URL: {data.get('url', 'N/A')}")

        # 2. Detect captcha
        print("\n2. Detecting captcha...")
        r = await client.get(f"{SUPERBROWSER_URL}/session/{session_id}/captcha/detect")
        r.raise_for_status()
        captcha_data = r.json()
        captcha = captcha_data.get("captcha")
        if captcha:
            print(f"   Captcha found: type={captcha['type']}, siteKey={captcha.get('siteKey', 'N/A')[:20]}...")
        else:
            print("   No captcha detected on the page")

        # 3. Solve captcha
        print(f"\n3. Solving captcha (method={method})...")
        start_time = time.time()
        r = await client.post(
            f"{SUPERBROWSER_URL}/session/{session_id}/captcha/solve",
            json={"method": method},
        )
        r.raise_for_status()
        solve_data = r.json()
        elapsed = time.time() - start_time

        if solve_data.get("solved"):
            print(f"   SOLVED in {elapsed:.1f}s")
            print(f"   Method: {solve_data.get('method', 'unknown')}")
            print(f"   Attempts: {solve_data.get('attempts', 'N/A')}")
        else:
            print(f"   NOT SOLVED after {elapsed:.1f}s")
            print(f"   Error: {solve_data.get('error', 'unknown')}")
            print(f"   Method tried: {solve_data.get('method', 'unknown')}")

        # 4. Take screenshot to verify
        print("\n4. Taking verification screenshot...")
        r = await client.get(
            f"{SUPERBROWSER_URL}/session/{session_id}/state",
            params={"vision": "false"},
        )
        r.raise_for_status()
        state = r.json()
        print(f"   Current URL: {state.get('url', 'N/A')}")
        print(f"   Title: {state.get('title', 'N/A')}")

        # 5. Close session
        print("\n5. Closing session...")
        await client.delete(f"{SUPERBROWSER_URL}/session/{session_id}")
        print("   Done")

    print("\n" + "=" * 60)
    print(f"Result: {'PASSED' if solve_data.get('solved') else 'FAILED'}")


async def test_with_nanobot(method: str = "auto"):
    """Test captcha solving via nanobot agent."""
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from nanobot import Nanobot
    from superbrowser_bridge.tools import register_all_tools

    bot = Nanobot.from_config(workspace=str(Path(__file__).parent / "workspace"))
    register_all_tools(bot)

    task = (
        f"Go to {RECAPTCHA_DEMO}, detect and solve the captcha using method='{method}', "
        "then take a screenshot to verify it was solved. Report whether it worked."
    )

    print(f"Nanobot task: {task}")
    print("-" * 60)

    result = await bot.run(task)
    print(f"\nAgent: {result.content}")


if __name__ == "__main__":
    method = sys.argv[1] if len(sys.argv) > 1 else "auto"
    use_nanobot = "--nanobot" in sys.argv

    if use_nanobot:
        asyncio.run(test_with_nanobot(method))
    else:
        asyncio.run(test_captcha(method))
