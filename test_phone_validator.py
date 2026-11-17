"""Headed Playwright test for phone validation → DataSift upload flow.

Tests the full pipeline: export phones from DataSift (or use local CSV),
validate via Trestle, upload phone tags back to DataSift.

Usage:
    # Full E2E with DataSift export + validation + upload (headed browser)
    python test_phone_validator.py --list-name "TN Public Notice 2026-03-23 - DMs"

    # Validate a local CSV only (no DataSift interaction)
    python test_phone_validator.py --csv-path "Phone Enrichment.csv" --no-upload

    # Estimate cost only
    python test_phone_validator.py --csv-path "Phone Enrichment.csv" --estimate

    # Validate + upload tags (skip export, use local CSV)
    python test_phone_validator.py --csv-path "Phone Enrichment.csv"
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from dotenv import load_dotenv
load_dotenv()

from phone_validator import estimate_cost, print_estimate, run_phone_validation
from datasift_uploader import (
    login, export_phone_enrichment, upload_phone_tags,
    DATASIFT_LOGIN_URL,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Test phone validation pipeline with headed browser"
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--list-name", help="DataSift list name to export phones from")
    target.add_argument("--preset-folder", help="DataSift preset folder")
    target.add_argument("--all-records", action="store_true", help="Export all records")
    target.add_argument("--csv-path", help="Use a local CSV instead of exporting")

    parser.add_argument("--estimate", action="store_true", help="Cost estimate only")
    parser.add_argument("--no-upload", action="store_true", help="Skip uploading tags to DataSift")
    parser.add_argument("--max-phones", type=int, default=0,
                        help="Limit number of phones to validate (0 = all)")
    return parser.parse_args()


async def main():
    args = parse_args()

    email = os.environ.get("DATASIFT_EMAIL", "")
    password = os.environ.get("DATASIFT_PASSWORD", "")
    trestle_key = os.environ.get("TRESTLE_API_KEY", "")

    # Estimate mode
    if args.estimate:
        if args.csv_path:
            est = estimate_cost(args.csv_path)
            print_estimate(est)
        else:
            logger.error("--estimate requires --csv-path")
        return

    # If using local CSV, skip the export step
    phone_csv_path = args.csv_path

    if not phone_csv_path:
        # Need to export from DataSift
        if not email or not password:
            logger.error("DATASIFT_EMAIL and DATASIFT_PASSWORD must be set in .env")
            return

        from playwright.async_api import async_playwright

        logger.info("Starting headed Playwright browser for export...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
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
            logger.info("Login successful! URL: %s", page.url)

            # Export phone enrichment CSV
            logger.info("=== EXPORTING PHONE ENRICHMENT CSV ===")
            export_result = await export_phone_enrichment(
                page,
                list_name=args.list_name,
                preset_folder=args.preset_folder,
                all_records=args.all_records,
            )
            logger.info("Export result: %s", export_result)

            if not export_result.get("success"):
                logger.error("Export failed: %s", export_result.get("message"))
                logger.info("Browser staying open 30s for inspection...")
                await page.wait_for_timeout(30000)
                await browser.close()
                return

            phone_csv_path = export_result["download_path"]
            await browser.close()

    # Run phone validation
    if not trestle_key:
        logger.error("TRESTLE_API_KEY must be set in .env")
        return

    logger.info("=== PHONE VALIDATION ===")
    logger.info("Input: %s", phone_csv_path)

    # Show estimate first
    est = estimate_cost(phone_csv_path)
    print_estimate(est)

    if est["unique_phones"] == 0:
        logger.error("No phones found in CSV")
        return

    logger.info("Proceeding with validation (%d unique phones, $%.2f)...",
                est["unique_phones"], est["estimated_cost"])

    validation_result = run_phone_validation(
        csv_path=phone_csv_path,
        api_key=trestle_key,
    )
    logger.info("Validation result: %s", {
        k: v for k, v in validation_result.items()
        if k != "tag_csv_path" and k != "detail_csv_path" and k != "summary_path"
    })

    if not validation_result.get("success"):
        logger.error("Validation failed: %s", validation_result.get("message"))
        return

    tag_csv = validation_result["tag_csv_path"]
    logger.info("Phone tags CSV: %s", tag_csv)

    # Upload tags to DataSift
    if not args.no_upload and email and password:
        from playwright.async_api import async_playwright

        logger.info("=== UPLOADING PHONE TAGS TO DATASIFT ===")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            logged_in = await login(page, email, password)
            if not logged_in:
                logger.error("Login failed for tag upload!")
                await browser.close()
                return

            upload_result = await upload_phone_tags(page, tag_csv)
            logger.info("Upload result: %s", upload_result)

            if upload_result.get("success"):
                logger.info("Phone tags uploaded successfully!")
                logger.info("  Tags: Dial First, Dial Second, Dial Third, Dial Fourth, Drop")
                logger.info("  Check DataSift → click-to-call interface to verify tags")
            else:
                logger.error("Upload failed: %s", upload_result.get("message"))

            # Keep browser open for inspection
            logger.info("Browser staying open 30s for inspection...")
            await page.wait_for_timeout(30000)
            await browser.close()
    elif args.no_upload:
        logger.info("Skipping DataSift upload (--no-upload)")
    else:
        logger.info("Skipping DataSift upload (no credentials)")

    logger.info("Done!")


if __name__ == "__main__":
    asyncio.run(main())
