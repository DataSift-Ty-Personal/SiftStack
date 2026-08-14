"""Probe the reisift Lists UI: navigate to Lists, screenshot, and report controls on
the 'FTM Foreclosure Blount' row (delete / menu) so we can automate list removal."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from datasift_core import create_browser, login  # noqa: E402
from sift_upload_wizard import _dismiss_popups  # noqa: E402

OUT = Path("output/_lists_ui")
OUT.mkdir(parents=True, exist_ok=True)


async def main():
    async with create_browser(headless=True) as (browser, context, page):
        await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD)
        # Try direct URL, else click sidebar "Lists"
        await page.goto("https://app.reisift.io/lists", wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        await _dismiss_popups(page)
        if "lists" not in page.url.lower():
            link = page.locator('text="Lists"')
            if await link.count() > 0:
                await link.first.click()
                await page.wait_for_timeout(5000)
        await page.screenshot(path=str(OUT / "lists_page.png"))
        print("url:", page.url)
        info = await page.evaluate(
            """() => {
              const vis = e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0;
              const rows = [...document.querySelectorAll('*')].filter(e => {
                const t = (e.textContent || '').trim();
                return t.startsWith('FTM Foreclosure') && t.length < 50 && vis(e);
              }).map(e => ({ t: e.textContent.trim(), cls: (e.className||'').toString().slice(0,40) }));
              const actiony = [...document.querySelectorAll('button,[role=button],a,svg')].filter(vis)
                .map(e => ({ t:(e.textContent||'').trim().slice(0,24), cls:(e.className||'').toString().slice(0,46) }))
                .filter(x => /delete|trash|remove|menu|dots|option|edit/i.test(x.cls) || /delete|remove/i.test(x.t))
                .slice(0, 30);
              return { ftmRows: rows, actionControls: actiony };
            }"""
        )
        print(json.dumps(info, indent=2)[:2500])


if __name__ == "__main__":
    asyncio.run(main())
