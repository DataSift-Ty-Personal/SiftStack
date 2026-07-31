"""One-off: upload the combined date backfill CSV (118 records, 2026-07-30).

56 shadow-field migrations (fresh Jun/Jul/Aug sheriff sale dates trapped in the
deactivated Misc. 'Foreclosure Date' field) + 62 May-sale backfills.
Merges by address; maps Foreclosure Date -> Foreclosure Sale Date and
Tax Auction Date -> Tax Sale Date via the new step-4 mapping. No enrich, no
skip trace.

Usage: python run_date_backfill_upload.py
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from dotenv import load_dotenv
load_dotenv()
from datasift_uploader import login, upload_csv  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

from pathlib import Path

CSV = Path("output/shadow_rescue_2026-07-30.csv")
LIST = "Aug 4 Sale Date Rescue 2026-07-30"


async def main():
    email = os.environ.get("DATASIFT_EMAIL", "")
    password = os.environ.get("DATASIFT_PASSWORD", "")
    if not email or not password:
        sys.exit("DATASIFT_EMAIL / DATASIFT_PASSWORD missing from .env")

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
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
        result = await upload_csv(page, CSV, list_name=LIST)
        logger.info("Upload result: %s", result)
        await page.wait_for_timeout(10000)
        await browser.close()
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
