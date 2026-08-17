"""
smrtphone_login.py - one-time interactive SmrtPhone login. Saves the browser
session (cookies + localStorage) to smrtphone_state.json next to this script so
the monitor can read your call log without asking for your password again.

Self-contained: needs only Playwright.
  pip install playwright
  python -m playwright install chromium

Run:
  python smrtphone_login.py

A browser window opens at the SmrtPhone login. Log in with your normal email and
password (or use "Forgot Password" right there). The moment you land in the
dashboard the session is saved and you can close the window.

Re-run whenever the monitor reports the session expired (typically every few weeks).
The session file is a credential: keep it private, never commit or share it.
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

LOGIN_URL = "https://phone.smrt.studio/login"
STATE_FILE = Path(__file__).resolve().parent / "smrtphone_state.json"
NOT_LOGGED = ("/login", "/signin", "/forgot", "/reset", "/verify", "/2fa", "/sign-in")


def logged_in(url: str) -> bool:
    u = (url or "").lower()
    return "smrt.studio" in u and not any(x in u for x in NOT_LOGGED)


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print("\n>>> A browser window is open at the SmrtPhone login (phone.smrt.studio).", flush=True)
        print(">>> Log in with your email + password (or click 'Forgot Password' there).", flush=True)
        print(">>> Do NOT close the window. Waiting up to 8 minutes...\n", flush=True)
        try:
            for _ in range(240):  # 240 x 2s = 8 min
                if logged_in(page.url):
                    await page.wait_for_timeout(3500)  # let the dashboard settle / tokens land
                    await context.storage_state(path=str(STATE_FILE))
                    print(f"LOGGED IN - session saved to {STATE_FILE}", flush=True)
                    print("You can close the window now.", flush=True)
                    await browser.close()
                    return 0
                await page.wait_for_timeout(2000)
        except Exception as e:  # window closed mid-wait
            print(f"(window closed during wait: {e})", flush=True)
            return 1
        print("TIMED OUT (8 min) - no completed login detected. Re-run when ready.", flush=True)
        await browser.close()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
