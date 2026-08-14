"""One-time interactive DataSift login -> saves a fresh session cookie for the wizard.

The automated upload reuses datasift_cookies.json. When that session expires (and the
stored-password fresh-login path is blocked by a security challenge / stale password),
run this once: a headed browser opens, you log in (any verification too), and the moment
you land on records/dashboard it saves the cookies and exits. Then the wizard runs headless.

  python src/manual_login.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datasift_core import (  # noqa: E402
    create_browser, save_cookies, load_cookies,
    DATASIFT_LOGIN_URL, DATASIFT_RECORDS_URL,
)


def logged_in(url: str) -> bool:
    return "/login" not in url and ("/dashboard" in url or "/records" in url)


async def main():
    async with create_browser(headless=False) as (browser, context, page):
        await load_cookies(context)
        await page.goto(DATASIFT_RECORDS_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        if logged_in(page.url):
            await save_cookies(page)
            print("Existing session still valid - cookies refreshed. Done.")
            return
        await page.goto(DATASIFT_LOGIN_URL, wait_until="domcontentloaded")
        print("\n>>> A browser window is open. Log into DataSift (ty+2@dataflik.com).")
        print(">>> Complete any verification. Waiting up to 5 minutes...\n", flush=True)
        for _ in range(150):  # 150 x 2s = 5 min
            if logged_in(page.url):
                await page.wait_for_timeout(1500)
                await save_cookies(page)
                print("LOGGED IN - fresh session saved to datasift_cookies.json. You can close the window.")
                return
            await page.wait_for_timeout(2000)
        print("Timed out waiting for login (5 min).")


if __name__ == "__main__":
    asyncio.run(main())
