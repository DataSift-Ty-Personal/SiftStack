"""Targeted recovery: re-scrape li_violations and upload Code Enforcement niche list.

Use when the Code Enforcement niche upload failed during a GHA run but the other
niche uploads succeeded.  Re-scrapes via the Carto SQL API (~10s, no browser),
writes a Code Enforcement CSV, and uploads it to DataSift.

Usage:
    python recover_code_enforcement_upload.py
    python recover_code_enforcement_upload.py --lookback 2
    python recover_code_enforcement_upload.py --dry-run       # scrape + write CSV, skip upload
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

sys.path.insert(0, "src")

import config
from philadelphia_scrapers import scrape_li_violations
from philly_pipeline import _NICHE_LISTS, _dedup_notices
from datasift_formatter import write_datasift_csv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("recover_ce")

for _noisy in ("httpx", "httpcore", "asyncio", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


async def main(lookback_days: int, dry_run: bool) -> None:
    t_start = time.time()

    # ── 1. Scrape li_violations ──────────────────────────────────────────────
    logger.info("Scraping li_violations (lookback=%dd)...", lookback_days)
    notices = await scrape_li_violations(lookback_days=lookback_days)
    logger.info("Scraped %d raw li_violations records", len(notices))

    if not notices:
        logger.error("No records scraped — aborting")
        sys.exit(1)

    # ── 2. Dedup ─────────────────────────────────────────────────────────────
    notices, removed = _dedup_notices(notices)
    logger.info("After dedup: %d records (%d removed)", len(notices), removed)

    # ── 3. Filter to Code Enforcement (CODE_VIOLATION) ───────────────────────
    ce_list_name = _NICHE_LISTS["CODE_VIOLATION"]   # "Code Enforcement"
    ce_records = [
        n for n in notices
        if "CODE_VIOLATION" in (n.all_notice_types or n.notice_type or "").split(";")
    ]
    logger.info("Code Enforcement records: %d / %d", len(ce_records), len(notices))

    if not ce_records:
        logger.error("No CODE_VIOLATION records found — nothing to upload")
        sys.exit(1)

    # ── 4. Write CSV ──────────────────────────────────────────────────────────
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = write_datasift_csv(
        ce_records,
        filename=f"recover_code_enforcement_{len(ce_records)}recs_{timestamp}.csv",
    )
    logger.info("CSV written: %s (%d records)", csv_path, len(ce_records))

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

        # Try existing list first, fall back to creating new
        result = await _upload_csv(page, csv_path, list_name=ce_list_name, existing_list=True)
        if not result.get("success"):
            logger.info("Existing list not found — creating new list '%s'", ce_list_name)
            result = await _upload_csv(page, csv_path, list_name=ce_list_name, existing_list=False)

        await browser.close()

    elapsed = time.time() - t_start
    status = "OK" if result.get("success") else f"FAILED: {result.get('message', '')}"
    logger.info("Upload '%s' (%d records): %s  (%.0fs)", ce_list_name, len(ce_records), status, elapsed)

    print(f"\n{'=' * 55}")
    print(f"  Code Enforcement Recovery Upload")
    print(f"{'=' * 55}")
    print(f"  Records scraped   : {len(notices)}")
    print(f"  CE records        : {len(ce_records)}")
    print(f"  CSV               : {csv_path}")
    print(f"  Upload            : {status}")
    print(f"  Elapsed           : {elapsed:.0f}s")
    print(f"{'=' * 55}\n")

    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Recover Code Enforcement niche upload")
    parser.add_argument("--lookback", type=int, default=1, metavar="DAYS",
                        help="Lookback window in days (default: 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape and write CSV but skip DataSift upload")
    args = parser.parse_args()

    asyncio.run(main(lookback_days=args.lookback, dry_run=args.dry_run))
