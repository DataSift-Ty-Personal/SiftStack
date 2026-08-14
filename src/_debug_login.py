"""Debug the headless DataSift login: attempt it, screenshot, and surface why it fails."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
from datasift_core import create_browser, DATASIFT_LOGIN_URL

OUT = Path("output")


async def main():
    async with create_browser(headless=True) as (browser, context, page):
        await page.goto(DATASIFT_LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        try:
            await page.get_by_role("textbox", name="Email").fill(config.DATASIFT_EMAIL)
            await page.get_by_role("textbox", name="Password").fill(config.DATASIFT_PASSWORD)
            ev = await page.get_by_role("textbox", name="Email").input_value()
            pv = await page.get_by_role("textbox", name="Password").input_value()
            print(f"email field='{ev}'  password filled len={len(pv)}")
        except Exception as e:
            print("FILL ERROR:", str(e)[:200])
        for lbl in ['label:has-text("Remember me")', 'label:has-text("agree")']:
            loc = page.locator(lbl)
            if await loc.count() > 0:
                try:
                    await loc.first.click()
                except Exception:
                    pass
        btn = page.get_by_role("button", name="Sign In")
        print("Sign In button count:", await btn.count(), "| disabled:", (await btn.first.is_disabled()) if await btn.count() else "n/a")
        await page.screenshot(path=str(OUT / "_login_before.png"))
        try:
            await btn.first.click()
        except Exception as e:
            print("CLICK ERROR:", str(e)[:200])
        await page.wait_for_timeout(9000)
        print("URL after Sign In:", page.url)
        await page.screenshot(path=str(OUT / "_login_after.png"), full_page=True)
        body = (await page.inner_text("body")).strip()
        for kw in ["invalid", "incorrect", "verify", "verification", "wrong", "does not", "not match",
                   "required", "captcha", "unusual", "locked", "too many", "expired", "code"]:
            i = body.lower().find(kw)
            if i >= 0:
                print(f"  HIT '{kw}': ...{body[max(0,i-70):i+70]}...".replace(chr(10), ' '))
        print("--- visible text (first 600 chars) ---")
        print(body[:600].replace(chr(10), ' | '))


if __name__ == "__main__":
    asyncio.run(main())
