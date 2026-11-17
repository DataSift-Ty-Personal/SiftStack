"""Live headed-browser test for SiftMap sold properties management.

Usage:
    python test_manage_sold.py                          # Knox + Blount, last 1 month
    python test_manage_sold.py --counties Knox           # Knox only
    python test_manage_sold.py --months-back 2           # Last 2 months of sales
    python test_manage_sold.py --min-sale-price 5000     # Min $5K sale price
    python test_manage_sold.py --sold-tag-date 2026-03   # Custom tag date

Runs in headed mode so you can watch the browser and identify selector issues.
"""

import argparse
import asyncio
import logging
import os
import sys

# Add src/ to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from dotenv import load_dotenv

load_dotenv()


async def main():
    parser = argparse.ArgumentParser(description="Test SiftMap sold properties workflow")
    parser.add_argument(
        "--counties",
        type=str,
        default=None,
        help='Comma-separated counties (default: Knox,Blount)',
    )
    parser.add_argument(
        "--months-back",
        type=int,
        default=1,
        help="Months of sales to pull (default: 1)",
    )
    parser.add_argument(
        "--min-sale-price",
        type=int,
        default=1000,
        help="Min sale price filter (default: 1000)",
    )
    parser.add_argument(
        "--sold-tag-date",
        type=str,
        default=None,
        help="Tag date YYYY-MM (default: current month)",
    )
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(
                open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
            ),
        ],
    )

    counties = None
    if args.counties:
        counties = [c.strip().title() for c in args.counties.split(",")]

    from datasift_uploader import run_manage_sold_workflow

    result = await run_manage_sold_workflow(
        counties=counties,
        months_back=args.months_back,
        min_sale_price=args.min_sale_price,
        sold_tag_date=args.sold_tag_date,
        headless=False,
    )

    print("\n" + "=" * 60)
    print("RESULT:")
    print(f"  Success: {result.get('success')}")
    print(f"  Message: {result.get('message')}")
    print(f"  Counties: {result.get('counties_processed', [])}")
    print(f"  Total records: {result.get('total_records', 0)}")

    if result.get("month_details"):
        print("\n  MONTH DETAILS:")
        for md in result["month_details"]:
            status = "OK" if md.get("success") else "FAIL"
            print(f"    {md['county']} {md['month']}: {md['records']} records [{status}]")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
