"""Targeted recovery: re-scrape bid4assets and upload Foreclosure niche list only.

Use when the Foreclosure niche upload failed (NPS popup) but Tax Sale succeeded.
Scrapes via Scrapfly SDK, writes Foreclosure-only CSV, uploads to DataSift.

Usage:
    python recover_foreclosure_upload.py
    python recover_foreclosure_upload.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

sys.path.insert(0, "src")

import config
from philadelphia_scrapers import scrape_bid4assets_mortgage, scrape_bid4assets_tax
from philly_pipeline import _NICHE_LISTS, _dedup_notices
from datasift_formatter import write_datasift_csv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("recover_foreclosure")

for _noisy in ("httpx", "httpcore", "asyncio", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


async def main(dry_run: bool) -> None:
    t_start = time.time()

    # ── 1. Scrape both bid4assets sources ────────────────────────────────────
    logger.info("Scraping bid4assets_mortgage...")
    mortgage = await scrape_bid4assets_mortgage()
    logger.info("bid4assets_mortgage: %d records", len(mortgage))

    logger.info("Scraping bid4assets_tax...")
    tax = await scrape_bid4assets_tax()
    logger.info("bid4assets_tax: %d records", len(tax))

    notices = mortgage + tax
    logger.info("Total raw: %d records", len(notices))

    if not notices:
        logger.error("No records scraped — aborting")
        sys.exit(1)

    # ── 2. Dedup ─────────────────────────────────────────────────────────────
    notices, removed = _dedup_notices(notices)
    logger.info("After dedup: %d records (%d removed)", len(notices), removed)

    # ── 3. Filter to Foreclosure only ────────────────────────────────────────
    foreclosure_list_name = _NICHE_LISTS["SHERIFF_MORTGAGE_FORECLOSURE"]   # "Foreclosure"
    fc_records = [
        n for n in notices
        if "SHERIFF_MORTGAGE_FORECLOSURE" in (n.all_notice_types or n.notice_type or "").split(";")
    ]
    logger.info("Foreclosure records: %d / %d", len(fc_records), len(notices))

    if not fc_records:
        logger.error("No FORECLOSURE records found — nothing to upload")
        sys.exit(1)

    # ── 4. Write CSV ──────────────────────────────────────────────────────────
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = write_datasift_csv(
        fc_records,
        filename=f"recover_foreclosure_{len(fc_records)}recs_{timestamp}.csv",
    )
    logger.info("CSV written: %s (%d records)", csv_path, len(fc_records))

    if dry_run:
        logger.info("Dry run — skipping DataSift upload")
        print(f"\nDry run complete. CSV at: {csv_path}")
        return

    # ── 5. Upload to DataSift ─────────────────────────────────────────────────
    from playwright.async_api import async_playwright
    from datasift_core import login as _ds_login
    from datasift_uploader import upload_csv as _upload_csv

    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    result = {"success": False, "message": "not run"}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=UA,
        )
        page = await ctx.new_page()

        ok = await _ds_login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD)
        if not ok:
            logger.error("DataSift login failed — check DATASIFT_EMAIL / DATASIFT_PASSWORD")
            await browser.close()
            sys.exit(1)

        result = await _upload_csv(page, csv_path, list_name=foreclosure_list_name, existing_list=True)
        if not result.get("success"):
            logger.info("Existing list not found — creating new list '%s'", foreclosure_list_name)
            result = await _upload_csv(page, csv_path, list_name=foreclosure_list_name, existing_list=False)

        await browser.close()

    elapsed = time.time() - t_start
    status = "OK" if result.get("success") else f"FAILED: {result.get('message', '')}"
    logger.info("Upload '%s' (%d records): %s  (%.0fs)", foreclosure_list_name, len(fc_records), status, elapsed)

    print(f"\n{'=' * 55}")
    print(f"  Foreclosure Recovery Upload")
    print(f"{'=' * 55}")
    print(f"  Records scraped   : {len(notices)}")
    print(f"  Foreclosure recs  : {len(fc_records)}")
    print(f"  CSV               : {csv_path}")
    print(f"  Upload            : {status}")
    print(f"  Elapsed           : {elapsed:.0f}s")
    print(f"{'=' * 55}\n")

    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Recover Foreclosure niche upload")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape and write CSV but skip DataSift upload")
    args = parser.parse_args()

    asyncio.run(main(dry_run=args.dry_run))
