"""Probe: can the Priority Skip merge drop its list?

Two read-only questions, answered in one headed session. NOTHING is ever
committed — this script never clicks "Finish Upload", and the probe CSV carries
a deliberately nonexistent address so an accidental commit would match no
record anyway.

  Pass A — "Tagging existing properties": does this Update-Data flow have any
           list control at all (Select a list / Create new list / ASSOCIATE
           DATA WITH LIST)? If none, tags can be written with no list.
  Pass B — "Update property data": does its column mapping expose Notes and
           Text Touch 1-4? If yes, the touch fields can be written with no list.

Run:  .venv/bin/python src/_probe_update_data.py
"""
import asyncio
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from datasift_core import create_browser, login  # noqa: E402
from sift_upload_wizard import RECORDS_URL, _dismiss_popups, _click_next, _shot  # noqa: E402

OUT = Path("output/_probe_update_data")
OUT.mkdir(parents=True, exist_ok=True)
PROBE_CSV = OUT / "probe_touches.csv"

# Nonexistent address on purpose: Update Data only touches records that already
# exist and matches on address, so this row can never hit anything.
PROBE_ROW = {
    "Property Street": "99999 Probe Test Ave",
    "Property City": "Philadelphia",
    "Property State": "PA",
    "Property Zip": "19104",
    "Notes": "probe do not commit",
    "Text Touch 1": "probe 1",
    "Text Touch 2": "probe 2",
    "Text Touch 3": "probe 3",
    "Text Touch 4": "probe 4",
}

# Any list-related control the wizard might render.
LIST_MARKERS = [
    "ASSOCIATE DATA WITH LIST",
    "Select a list",
    "Create new list",
    "Enter new list name",
    "WHERE DID YOU PURCHASE THIS LIST?",
]


def write_probe_csv() -> Path:
    with open(PROBE_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(PROBE_ROW))
        w.writeheader()
        w.writerow(PROBE_ROW)
    return PROBE_CSV


async def open_update_data(page, option_label: str) -> bool:
    """Upload File -> Update Data -> select `option_label`. True if selected."""
    await page.goto(RECORDS_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(6000)
    await _dismiss_popups(page)

    ub = page.locator('text="Upload File"')
    if await ub.count() == 0:
        print("  FAIL: no 'Upload File' link")
        return False
    await ub.first.click()
    await page.wait_for_timeout(2500)
    await _dismiss_popups(page)

    ud = page.locator('text="Update Data"')
    if await ud.count() == 0:
        print("  FAIL: no 'Update Data' button")
        return False
    await ud.first.click()
    await page.wait_for_timeout(2000)

    dd = page.locator('text="Select one or more options"')
    if await dd.count() == 0:
        dd = page.locator('text="Select one option"')
    if await dd.count() > 0:
        await dd.first.click()
        await page.wait_for_timeout(1500)

    opt = page.locator(f'text="{option_label}"')
    if await opt.count() == 0:
        print(f"  FAIL: option not found: {option_label!r}")
        return False
    await opt.first.click(force=True)
    await page.wait_for_timeout(1200)

    # Close the multi-select via the heading (Escape clears the selection).
    try:
        await page.locator('text="WHAT ARE YOU GOING TO UPDATE?"').first.click()
        await page.wait_for_timeout(600)
    except Exception:
        pass
    return True


async def report_list_markers(page, when: str) -> dict:
    found = {}
    for m in LIST_MARKERS:
        try:
            loc = page.locator(f'text="{m}"')
            found[m] = await loc.count() > 0 and await loc.first.is_visible()
        except Exception:
            found[m] = False
    hits = [m for m, ok in found.items() if ok]
    print(f"  list controls {when}: {hits if hits else 'NONE'}")
    return found


async def pass_a(page):
    print("\n=== PASS A: 'Tagging existing properties' — any list control? ===")
    if not await open_update_data(page, "Tagging existing properties"):
        return
    await _shot(page, str(OUT / "passA"), "setup")
    before = await report_list_markers(page, "at setup")

    await _click_next(page)
    await page.wait_for_timeout(2500)
    await _dismiss_popups(page)
    await _shot(page, str(OUT / "passA"), "after_next")
    after = await report_list_markers(page, "after Next")

    clean = not any(before.values()) and not any(after.values())
    print(f"  VERDICT: {'no list needed' if clean else 'a list control IS present'}")


async def pass_b(page, csv_path: Path):
    print("\n=== PASS B: 'Update property data' — are Notes + Text Touch mappable? ===")
    if not await open_update_data(page, "Update property data"):
        return
    await _shot(page, str(OUT / "passB"), "setup")
    await report_list_markers(page, "at setup")

    # Walk the step machine until the file input appears, then to Map columns.
    file_done = False
    for step in range(12):
        await page.wait_for_timeout(1500)
        await _dismiss_popups(page)

        # Detect Review WITHOUT clicking it. This probe never commits.
        if await page.locator('button:has-text("Finish Upload")').count() > 0:
            print("  reached Review (not clicking Finish)")
            await _shot(page, str(OUT / "passB"), "review")
            break

        file_input = page.locator('input[type="file"]')
        if not file_done and await file_input.count() > 0:
            await file_input.first.set_input_files(str(csv_path))
            print(f"  uploaded probe CSV: {csv_path.name}")
            file_done = True
            for _ in range(10):
                await page.wait_for_timeout(1500)
                if await page.locator('text="File uploaded!"').count() > 0:
                    break
            await _shot(page, str(OUT / "passB"), "file")
            await _click_next(page)
            continue

        # On the Map step the right panel has a search box — use it to ask
        # whether each target field exists at all.
        searches = page.locator('input[placeholder*="Search"]')
        if file_done and await searches.count() >= 2:
            print("  Map columns step reached — querying target fields:")
            right = searches.last
            for target in ["Notes", "Text Touch 1", "Text Touch 2",
                           "Text Touch 3", "Text Touch 4"]:
                try:
                    await right.fill(target)
                    await page.wait_for_timeout(1400)
                    hits = await page.locator(f'text="{target}"').count()
                    print(f"    {target:<14} {'FOUND' if hits > 0 else 'not found'}"
                          f"  (matches={hits})")
                except Exception as e:
                    print(f"    {target:<14} query failed: {e}")
            try:
                await right.fill("")
            except Exception:
                pass
            await _shot(page, str(OUT / "passB"), "map")
            await _click_next(page)
            continue

        await _click_next(page)

    print("  NOTE: nothing was committed — Finish Upload was never clicked.")


async def main():
    csv_path = write_probe_csv()
    print(f"probe CSV: {csv_path}")
    async with create_browser(headless=False) as (browser, context, page):
        ok = await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD)
        if not ok:
            print("DataSift login failed")
            return
        print("logged in")
        await pass_a(page)
        await pass_b(page, csv_path)
    print(f"\nscreenshots: {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
