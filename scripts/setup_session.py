"""
One-time manual login script.

Run this ONCE before starting main.py for the first time, or any time
your session expires and Facebook is showing CAPTCHAs.

What it does:
  1. Opens a real visible Chromium window
  2. Navigates to facebook.com/login
  3. Waits for YOU to log in manually (take as long as you need)
  4. Detects when you're logged in successfully
  5. Saves the session (cookies + localStorage) to data/fb_session.json
  6. Closes the browser

After this script completes, main.py will restore the saved session
automatically on every startup — no login, no CAPTCHA.

Usage:
    python scripts/setup_session.py
"""

import asyncio
import json
import sys
from pathlib import Path

# Allow imports from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright

try:
    from playwright_stealth import Stealth
    _STEALTH_V2 = True
except ImportError:
    from playwright_stealth import stealth_async as _stealth_async_v1  # type: ignore
    _STEALTH_V2 = False

from config.settings import settings

SESSION_PATH = Path(settings.session_file)
FB_LOGIN = "https://www.facebook.com/login"
CHECK_INTERVAL = 2  # seconds between logged-in checks


async def is_logged_in(page) -> bool:
    """Return True when Facebook shows a logged-in home page."""
    try:
        url = page.url
        if not url or "login" in url or "checkpoint" in url:
            return False
        # Look for the Marketplace link or the top nav — only present when logged in
        marker = await page.query_selector(
            "a[href*='/marketplace'], [aria-label='Facebook'], div[role='banner']"
        )
        return marker is not None
    except Exception:
        return False


async def main() -> None:
    print("\n" + "=" * 60)
    print(" MarketplaceScraper — First-Time Session Setup")
    print("=" * 60)
    print("
A browser window will open. Please:")
    print("  1. Log in to Facebook as your bot account")
    print("  2. Solve any CAPTCHA or security check if prompted")
    print("  3. Wait until the home feed loads")
    print("\nThe script will detect login automatically and save")
    print(f"the session to: {SESSION_PATH}")
    print("\nDo NOT close the browser window yourself.")
    print("=" * 60 + "\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,  # Always visible — you need to interact
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--start-maximized",
            ],
        )

        context = await browser.new_context(
            viewport=None,  # Use maximized window size
            locale="en-US",
            timezone_id="America/New_York",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()

        # Apply stealth patches
        if _STEALTH_V2:
            stealth = Stealth()
            await stealth.apply_stealth_async(page)
        else:
            await _stealth_async_v1(page)  # type: ignore

        await page.goto(FB_LOGIN, wait_until="domcontentloaded", timeout=60_000)
        print("Browser opened. Waiting for you to log in...\n")

        # Poll until logged in — no timeout, wait as long as needed
        dots = 0
        while True:
            if await is_logged_in(page):
                break
            dots = (dots + 1) % 4
            print(f"\r  Waiting for login{'.' * dots}   ", end="", flush=True)
            await asyncio.sleep(CHECK_INTERVAL)

        print("\n\n  Login detected! Saving session...")

        # Give FB a moment to fully settle before saving
        await asyncio.sleep(3)

        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        state = await context.storage_state()
        SESSION_PATH.write_text(json.dumps(state))

        print(f"  Session saved to: {SESSION_PATH}")
        print("\n  Closing browser in 3 seconds...")
        await asyncio.sleep(3)
        await browser.close()

    print("\n" + "=" * 60)
    print(" Setup complete! You can now run: python main.py")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
