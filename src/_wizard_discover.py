"""Discovery driver: walk the CURRENT DataSift 6-step upload wizard and dump the live
DOM (visible buttons / inputs / styled-selects / step nav) + a screenshot at each step.
STOPS before "Finish Upload" so nothing is created in the account.

Run:  python src/_wizard_discover.py
Output: output/_wizard/NN_label.{png,json}
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402  (loads .env)
from datasift_core import create_browser, login  # noqa: E402

OUT = Path("output/_wizard")
OUT.mkdir(parents=True, exist_ok=True)
CSV = "output/datasift_upload_DMs_2026-06-19_174047.csv"
RECORDS_URL = "https://app.reisift.io/records"

DUMP_JS = r"""() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = el => (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 70);
  const cls = el => (el.className || '').toString().slice(0, 55);
  const buttons = [...document.querySelectorAll('button, [role=button]')].filter(vis)
    .map(b => ({ t: txt(b), disabled: b.disabled || b.getAttribute('aria-disabled') === 'true', cls: cls(b) }));
  const inputs = [...document.querySelectorAll('input, textarea')]
    .map(i => ({ type: i.type, ph: i.placeholder, name: i.name, val: (i.value || '').slice(0, 30),
                 hidden: !vis(i), cls: cls(i) }));
  const selects = [...document.querySelectorAll('[class*="Select"]')].filter(vis)
    .map(s => ({ t: txt(s), cls: cls(s) })).slice(0, 50);
  const dropzones = [...document.querySelectorAll('[class*="rop"],[class*="pload"],label')].filter(vis)
    .map(d => ({ t: txt(d), cls: cls(d) })).slice(0, 250);
  return { url: location.href, buttons, inputs, selects, dropzones };
}"""


async def dump(page, label):
    await page.wait_for_timeout(1500)
    try:
        await page.screenshot(path=str(OUT / f"{label}.png"))
    except Exception as e:
        print(f"  screenshot {label} failed: {e}")
    try:
        info = await page.evaluate(DUMP_JS)
    except Exception as e:
        info = {"error": str(e)}
    (OUT / f"{label}.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    b = info.get("buttons", [])
    print(f"[{label}] url={info.get('url','?')[-40:]} buttons={len(b)} "
          f"inputs={len(info.get('inputs',[]))} selects={len(info.get('selects',[]))}")
    return info


async def dismiss_popups(page):
    for sel in ['button:has-text("NO, THANKS")', 'button:has-text("No, thanks")',
                'button:has-text("No, Thanks")']:
        try:
            b = page.locator(sel)
            if await b.count() > 0:
                await b.first.click()
                await page.wait_for_timeout(500)
        except Exception:
            pass


async def next_step(page, label=""):
    await page.wait_for_timeout(1200)
    for sel in ['button:has-text("Next Step")', 'button:has-text("Next")', 'text="Next Step"']:
        b = page.locator(sel)
        if await b.count() > 0:
            try:
                await b.first.click(timeout=8000)
                await page.wait_for_timeout(2800)
                print(f"  next_step({label}): clicked '{sel}'")
                return True
            except Exception as e:
                print(f"  next_step({label}): '{sel}' click failed: {e}")
    print(f"  next_step({label}): NO clickable Next button found")
    return False


async def fill_setup(page, list_name):
    b = page.locator('text="Add Data"')
    if await b.count() > 0:
        await b.first.click()
        await page.wait_for_timeout(1200)
    dd = page.locator('text="Select one option"')
    if await dd.count() > 0:
        await dd.first.click()
        await page.wait_for_timeout(1000)
        opt = page.locator('text="Uploading a new list not in DataSift yet"')
        if await opt.count() > 0:
            await opt.first.click()
            await page.wait_for_timeout(1000)
    try:
        pd = page.locator('text="WHERE DID YOU PURCHASE THIS LIST?"').locator('..').locator('text="Select an option"')
        if await pd.count() > 0:
            await pd.first.click()
            await page.wait_for_timeout(500)
            o = page.locator('text="Other"')
            if await o.count() > 0:
                await o.first.click()
            await page.wait_for_timeout(500)
    except Exception:
        pass
    try:
        ph = page.locator('text="DOES DATA CONTAIN PHONE NUMBERS?"').locator('..').locator('text="Select an option"')
        if await ph.count() > 0:
            await ph.first.click()
            await page.wait_for_timeout(500)
            n = page.locator('text="No"')
            if await n.count() > 0:
                await n.first.click()
            await page.wait_for_timeout(500)
    except Exception:
        pass
    li = page.locator('input[placeholder*="Enter new list name"], input[placeholder*="list name"]')
    if await li.count() > 0:
        await li.first.fill(list_name)
        await page.wait_for_timeout(500)


async def main():
    async with create_browser(headless=True) as (browser, context, page):
        ok = await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD)
        print("login:", ok, "| url:", page.url)
        if "/records" not in page.url:
            await page.goto(RECORDS_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(8000)
        await dismiss_popups(page)

        ub = page.locator('text="Upload File"')
        if await ub.count() == 0:
            print("Upload File button not found")
            await dump(page, "00_no_upload_btn")
            return
        await ub.first.click()
        await page.wait_for_timeout(3500)
        await dismiss_popups(page)
        await dump(page, "01_setup_open")

        await fill_setup(page, "ZZ_DISCOVERY_DELETE_ME")
        await dismiss_popups(page)
        await dump(page, "02_setup_filled")

        await next_step(page, "setup->?")
        await dump(page, "03_step2")

        await next_step(page, "step2->?")
        await dump(page, "04_step3")

        await next_step(page, "step3->?")
        await dump(page, "05_step4_file")

        # File step: try to set the file input (hidden inputs are fine for set_input_files)
        fi = page.locator('input[type="file"]')
        if await fi.count() > 0:
            try:
                await fi.first.set_input_files(CSV)
                await page.wait_for_timeout(5000)
                print("  set file input OK")
            except Exception as e:
                print("  set file input failed:", e)
        else:
            print("  NO file input on this step")
        await dump(page, "06_after_file")

        await next_step(page, "file->map")
        await dump(page, "07_map")

        await next_step(page, "map->review")
        await dump(page, "08_review")

        print("STOPPED before Finish — nothing uploaded.")


if __name__ == "__main__":
    asyncio.run(main())
