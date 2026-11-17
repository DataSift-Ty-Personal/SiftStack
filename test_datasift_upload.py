"""Headed Playwright test for DataSift upload + enrich + skip trace flow.

Reads a few records from existing output, generates a DataSift CSV,
then runs upload → enrich → skip trace in headed (visible) mode.

Usage:
    python test_datasift_upload.py                    # upload + enrich + skip trace
    python test_datasift_upload.py --no-enrich        # upload only, skip enrichment
    python test_datasift_upload.py --no-skip-trace    # upload + enrich, skip skip trace
"""

import asyncio
import logging
import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from dotenv import load_dotenv
load_dotenv()

from data_formatter import read_csv
from datasift_formatter import write_datasift_split_csvs
from datasift_uploader import login, upload_csv, enrich_records, skip_trace_records, DATASIFT_LOGIN_URL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-enrich", action="store_true", help="Skip enrichment step")
    parser.add_argument("--no-skip-trace", action="store_true", help="Skip skip trace step")
    test_args = parser.parse_args()

    # Read a few records from existing enriched CSV
    test_csv = "output/knox_foreclosure_12mo_enriched_reimport_2026-03-20_125921.csv"
    logger.info("Reading test data from %s", test_csv)

    from pathlib import Path
    csv_path = Path(test_csv)
    if not csv_path.exists():
        logger.error("Test CSV not found: %s", test_csv)
        return

    notices = read_csv(csv_path)
    if not notices:
        logger.error("No records in test CSV")
        return

    # Select records with rich deep prospecting data (verified living DMs + heirs)
    import json as _json
    deceased_with_heirs = [
        n for n in notices
        if n.decision_maker_status == "verified_living" and n.heir_map_json
    ]
    # Sort by heir count descending to get the richest records
    deceased_with_heirs.sort(
        key=lambda n: len(_json.loads(n.heir_map_json)) if n.heir_map_json else 0,
        reverse=True,
    )
    living = [n for n in notices if n.owner_deceased != "yes"]

    # Pick 3 deceased-with-heirs + 2 living to demo both formats
    test_notices = deceased_with_heirs[:3] + living[:2]
    logger.info("Using %d test records (of %d total): %d deceased+heirs, %d living",
                len(test_notices), len(notices),
                min(3, len(deceased_with_heirs)), min(2, len(living)))

    # Generate split CSVs (DMs + Heirs as separate Message Board entries)
    csv_infos = write_datasift_split_csvs(test_notices)
    for info in csv_infos:
        logger.info("Generated %s CSV: %s (list: %s)", info["label"], info["path"], info["list_name"])
        with open(info["path"], "r") as f:
            for i, line in enumerate(f):
                if i < 3:
                    logger.info("  %s line %d: %s", info["label"], i, line.strip()[:200])

    # Run headed Playwright upload
    from playwright.async_api import async_playwright

    email = os.environ.get("DATASIFT_EMAIL", "")
    password = os.environ.get("DATASIFT_PASSWORD", "")

    if not email or not password:
        logger.error("DATASIFT_EMAIL and DATASIFT_PASSWORD must be set in .env")
        return

    logger.info("Starting headed Playwright browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # HEADED for testing
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # Login
        logger.info("Logging in to DataSift...")
        logged_in = await login(page, email, password)
        if not logged_in:
            logger.error("Login failed!")
            await browser.close()
            return

        logger.info("Login successful! Current URL: %s", page.url)
        await page.screenshot(path="datasift_after_login.png")

        # Upload each CSV sequentially (DMs first, then Heirs)
        all_success = True
        for i, info in enumerate(csv_infos):
            logger.info("=== UPLOAD %d/%d: %s ===", i + 1, len(csv_infos), info["label"])
            result = await upload_csv(page, info["path"], list_name=info["list_name"])
            logger.info("Upload %s result: %s", info["label"], result)

            if not result.get("success"):
                logger.error("Upload %s failed — stopping", info["label"])
                all_success = False
                break

            # Wait between uploads for DataSift to process
            if i < len(csv_infos) - 1:
                logger.info("Waiting 15s before next upload...")
                await page.wait_for_timeout(15000)

        if all_success:
            # Use first list name (has all records) for enrich + skip trace
            first_list = csv_infos[0]["list_name"]

            # Enrich property data
            if not test_args.no_enrich:
                logger.info("=== ENRICHMENT STEP ===")
                logger.info("Pausing 10s before enrichment so you can inspect...")
                await page.wait_for_timeout(10000)
                enrich_result = await enrich_records(page, first_list)
                logger.info("Enrich result: %s", enrich_result)
            else:
                logger.info("Skipping enrichment (--no-enrich)")

            # Skip trace
            if not test_args.no_skip_trace:
                logger.info("=== SKIP TRACE STEP ===")
                logger.info("Pausing 10s before skip trace so you can inspect...")
                await page.wait_for_timeout(10000)
                skip_result = await skip_trace_records(page, first_list)
                logger.info("Skip trace result: %s", skip_result)
            else:
                logger.info("Skipping skip trace (--no-skip-trace)")

        # Keep browser open for 30 seconds so user can inspect
        logger.info("Browser will stay open for 30 seconds for inspection...")
        await page.wait_for_timeout(30000)

        await browser.close()

    logger.info("Done!")


if __name__ == "__main__":
    asyncio.run(main())
