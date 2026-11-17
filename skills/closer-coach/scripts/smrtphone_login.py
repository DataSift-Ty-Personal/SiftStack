"""smrtphone_login.py - one-time interactive smrtPhone web login.

Opens a headed browser at the smrtPhone login page. Log in with your email and
password (or use Forgot Password right there). The moment you land on the
dashboard, the full session (cookies + localStorage) is saved to
smrtphone_state.json in your project folder, and pull_calls.py can run
unattended until the session expires (typically weeks).

SETUP (once): pip install playwright && playwright install chromium
USAGE:        python scripts/smrtphone_login.py
"""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

LOGIN_URL = "https://phone.smrt.studio/login"
STATE_FILE = Path.cwd() / "smrtphone_state.json"
NOT_LOGGED = ("/login", "/signin", "/forgot", "/reset", "/verify", "/2fa", "/sign-in")


def logged_in(url: str) -> bool:
    u = (url or "").lower()
    return "smrt.studio" in u and not any(x in u for x in NOT_LOGGED)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print("\n>>> A browser window is open at the smrtPhone login (phone.smrt.studio).", flush=True)
        print(">>> Log in with your email + password (or click 'Forgot Password' there).", flush=True)
        print(">>> Do NOT close the window. Waiting up to 8 minutes...\n", flush=True)
        ok = False
        for _ in range(240):  # 240 x 2s = 8 min
            try:
                if logged_in(page.url):
                    await page.wait_for_timeout(3500)  # let the dashboard settle
                    await context.storage_state(path=str(STATE_FILE))
                    print(f"LOGGED IN - session saved to {STATE_FILE.name}. You can close the window.", flush=True)
                    ok = True
                    break
                await page.wait_for_timeout(2000)
            except Exception as e:  # window closed
                print(f"(window closed during wait: {e})", flush=True)
                return
        if not ok:
            print("TIMED OUT (8 min) - no completed login detected. Re-run when ready.", flush=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
