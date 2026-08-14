"""Probe: can the upload wizard proceed with NO list (uncheck 'Create new list')?
Logs into ty+2, opens the wizard, selects 'new list' upload type, then unchecks
'Create new list' and reports whether the list-name requirement disappears and Next
becomes available (i.e. a true tag-only, no-list upload is possible)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from datasift_core import create_browser, login  # noqa: E402
from sift_upload_wizard import open_upload_wizard  # noqa: E402

OUT = Path("output/_nolist")
OUT.mkdir(parents=True, exist_ok=True)

STATE_JS = """() => {
  const vis = e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0;
  const nameInput = !!document.querySelector('input[placeholder*="Enter new list name"], input[placeholder*="list name"]');
  const next = [...document.querySelectorAll('button')].find(b => b.textContent.trim().startsWith('Next'));
  const selectAList = [...document.querySelectorAll('*')].some(e => e.textContent.trim() === 'Select a list' && vis(e));
  const createNewList = [...document.querySelectorAll('*')].some(e => e.textContent.trim() === 'Create new list' && vis(e));
  return {
    nameInput, selectAList, createNewList,
    nextDisabled: next ? (next.disabled || next.getAttribute('aria-disabled') === 'true') : 'no-next',
  };
}"""


async def main():
    async with create_browser(headless=True) as (browser, context, page):
        await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD)
        if not await open_upload_wizard(page):
            print("could not open wizard")
            return
        ad = page.locator('text="Add Data"')
        if await ad.count() > 0:
            await ad.first.click()
            await page.wait_for_timeout(1000)
        dd = page.locator('text="Select one option"')
        if await dd.count() > 0:
            await dd.first.click()
            await page.wait_for_timeout(800)
            opt = page.locator('text="Uploading a new list not in DataSift yet"')
            if await opt.count() > 0:
                await opt.first.click()
                await page.wait_for_timeout(900)
        await page.screenshot(path=str(OUT / "assoc_before.png"))
        print("BEFORE uncheck:", await page.evaluate(STATE_JS))

        cb = page.locator('text="Create new list"')
        if await cb.count() > 0:
            await cb.first.click()
            await page.wait_for_timeout(1300)
        else:
            print("'Create new list' label not found")
        await page.screenshot(path=str(OUT / "assoc_after.png"))
        print("AFTER uncheck:", await page.evaluate(STATE_JS))


if __name__ == "__main__":
    asyncio.run(main())
