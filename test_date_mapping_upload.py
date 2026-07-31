"""Headed test: verify the new step-4 date-column mapping actually lands.

Picks 3 real sheriff-sale records that are in DataSift but MISSING a
foreclosure date (per the 2026-07-29 export), rebuilds their rows from the
May recovery CSV (which has the real 5/5/2026 sale date), and uploads just
those 3 with enrich/skip-trace OFF. DataSift merges by address, so on success
this backfills 3 of the ~62 missing records — no junk records created.

Watch the log for:
    Mapped column: Foreclosure Date (...)   <- the new mapping working
    Mapped column: Tax Auction Date (...)   <- target search finds Tax Sale Date

Then confirm in the UI: record -> Property Debts & Encumbrances ->
Foreclosure Sale Date = 5/5/2026.

Usage: python test_date_mapping_upload.py
"""

import asyncio
import csv
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from dotenv import load_dotenv
load_dotenv()

from datasift_uploader import login, upload_csv  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

RECOVER_CSV = Path("output/recover_foreclosure_409recs_20260501_161255.csv")
EXPORT_CSV = Path("output/phone_enrichment_export.csv")
TEST_CSV = Path("output/test_date_mapping_3recs.csv")
TEST_LIST = "Mapping Test 2026-07-30"


def build_test_csv() -> list[str]:
    """Pick 3 sheriff records missing a date in Sift; write their rows verbatim."""
    # Addresses that already have SOME foreclosure date in DataSift (provider's)
    have_date = set()
    with open(EXPORT_CSV, encoding="utf-8-sig", errors="replace") as fh:
        for r in csv.DictReader(fh):
            if (r.get("Foreclosure date") or "").strip():
                have_date.add((r.get("Property address") or "").strip().upper())

    with open(RECOVER_CSV, encoding="utf-8-sig", errors="replace") as fh:
        rd = csv.DictReader(fh)
        header = rd.fieldnames
        picked = []
        for row in rd:
            addr = (row.get("Property Street Address") or "").strip().upper()
            if addr and addr not in have_date and (row.get("Foreclosure Date") or "").strip():
                picked.append(row)
            if len(picked) == 3:
                break

    if len(picked) < 3:
        sys.exit(f"Only found {len(picked)} candidate records — aborting")

    for row in picked:
        row["Notes"] = ""  # don't re-add duplicate note entries on merge

    with open(TEST_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(picked)

    addrs = [r["Property Street Address"] for r in picked]
    for r in picked:
        logger.info("Test record: %-30s Foreclosure Date=%s",
                    r["Property Street Address"], r["Foreclosure Date"])
    return addrs


async def main():
    addrs = build_test_csv()

    email = os.environ.get("DATASIFT_EMAIL", "")
    password = os.environ.get("DATASIFT_PASSWORD", "")
    if not email or not password:
        sys.exit("DATASIFT_EMAIL / DATASIFT_PASSWORD missing from .env")

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # HEADED
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
        )
        page = await context.new_page()

        logger.info("Logging in to DataSift...")
        if not await login(page, email, password):
            sys.exit("Login failed")
        logger.info("Login OK: %s", page.url)

        result = await upload_csv(page, TEST_CSV, list_name=TEST_LIST)
        logger.info("Upload result: %s", result)

        logger.info("NO enrich / NO skip trace (deliberate). Test addresses: %s", addrs)
        logger.info("Browser stays open 20s for inspection...")
        await page.wait_for_timeout(20000)
        await browser.close()

    logger.info("Done. Verify Foreclosure Sale Date on: %s", addrs)


if __name__ == "__main__":
    asyncio.run(main())
