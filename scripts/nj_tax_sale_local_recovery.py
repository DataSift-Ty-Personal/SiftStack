"""Local tax-sale recovery scrape.

Mirror of scripts/nj_probate_local_backfill.py for the tax-sale source —
use when Modal's egress IPs are getting 403'd at the AWS-ELB layer on
*.newjerseytaxsale.com (observed 2026-05-23: site added cloud-IP
blocking; residential IPs work fine with a real browser User-Agent).

Bypasses dedup + enrichment + DataSift upload — same contract as
modal_app.py::nj_tax_sale_recovery. Output is the 24-col TaxSaleRecord
schema (block, lot, qualifier, municipality, ...) for use with
scripts/resolve_block_lot.py.

Run with:
  PYTHONPATH=src python scripts/nj_tax_sale_local_recovery.py
  PYTHONPATH=src python scripts/nj_tax_sale_local_recovery.py --counties Middlesex
  PYTHONPATH=src python scripts/nj_tax_sale_local_recovery.py --headed --no-fetch-details
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def _run(counties: list[str], fetch_details: bool, headless: bool) -> list[dict]:
    from nj_tax_sale_monitor import scrape_nj_tax_sales

    logger = logging.getLogger("nj_tax_sale_local_recovery")
    logger.info(
        "Local tax-sale scrape: counties=%s, fetch_details=%s, headless=%s",
        counties, fetch_details, headless,
    )
    records = await scrape_nj_tax_sales(
        counties=counties,
        fetch_details=fetch_details,
        headless=headless,
    )
    logger.info("Scrape returned %d records", len(records))
    return records


def main() -> int:
    p = argparse.ArgumentParser(
        description="Local NJ tax-sale recovery scrape (residential IP).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--counties", default="Middlesex,Essex,Somerset,Union",
                   help="Comma-separated county names (default: all 4)")
    p.add_argument("--no-fetch-details", dest="fetch_details", action="store_false",
                   help="Skip per-record detail pages (faster but sparser data)")
    p.add_argument("--headed", action="store_true",
                   help="Show the Playwright browser windows")
    p.add_argument("--out-dir", type=Path, default=ROOT / "output",
                   help="Output directory (default: ./output)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("nj_tax_sale_local_recovery")

    counties = [c.strip() for c in args.counties.split(",") if c.strip()]
    if not counties:
        logger.error("No counties specified")
        return 2

    records = asyncio.run(_run(
        counties=counties,
        fetch_details=args.fetch_details,
        headless=not args.headed,
    ))
    if not records:
        logger.error("0 records scraped — site may still be blocking, "
                     "or all municipal sites are legitimately offline")
        return 1

    # Write rich-schema CSV matching the Modal recovery function output
    # so scripts/resolve_block_lot.py can consume either source.
    from nj_tax_sale_monitor import TaxSaleRecord

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / f"nj_tax_sale_recovery_{ts}.csv"

    fieldnames = [f.name for f in fields(TaxSaleRecord)]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in records:
            row = asdict(r) if hasattr(r, "__dataclass_fields__") else dict(r)
            w.writerow({k: row.get(k, "") for k in fieldnames})

    logger.info("Wrote %d records to %s", len(records), csv_path)
    logger.info("Next: PYTHONPATH=src python scripts/resolve_block_lot.py "
                "--csv %s --out %s",
                csv_path, csv_path.with_name(csv_path.stem + "_resolved.csv"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
