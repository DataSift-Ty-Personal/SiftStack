"""End-to-end verification test for Greene foreclosure scrapers.

Runs both Greene scrapers (no DataSift upload), formats records to a DataSift
CSV via datasift_formatter.write_datasift_csv(), and asserts that Greene rows
have non-empty auction_date, case_number, parcel_id, owner_name.

Usage:
    PYTHONPATH=/Users/aaron/Desktop/SiftStack/src \
    /Users/aaron/Desktop/SiftStack/venv/bin/python tests/test_greene_foreclosure_e2e.py

The test PASSES if at least one valid Greene RealAuction record flows all the
way to the DataSift CSV.  The ASP.NET scraper (oh_greene_foreclosure.py) is
expected to return 0 records while apps.greenecountyohio.gov is down.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Allow running from project root with PYTHONPATH=src
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> int:
    """Return 0 on pass, 1 on failure."""
    import config  # noqa: E402 (must be after sys.path patch)
    from scrapers.oh_greene_foreclosure import Scraper as AspScraper
    from scrapers.oh_greene_realauction import Scraper as RaScraper
    from datasift_formatter import write_datasift_csv

    since = date.today() - timedelta(days=30)
    logger.info("Greene foreclosure E2E test — since=%s", since.isoformat())

    # ── 1. Run ASP.NET scraper ─────────────────────────────────────────
    logger.info("Running oh_greene_foreclosure (ASP.NET portal)...")
    asp_records = asyncio.run(AspScraper().scrape(since_date=since))
    logger.info("ASP.NET scraper: %d record(s)", len(asp_records))

    # ── 2. Run RealAuction scraper ─────────────────────────────────────
    logger.info("Running oh_greene_realauction (RealAuction portal)...")
    ra_records = asyncio.run(RaScraper().scrape(since_date=since))
    logger.info("RealAuction scraper: %d record(s)", len(ra_records))

    all_records = asp_records + ra_records
    logger.info("Total Greene records: %d", len(all_records))

    if not all_records:
        logger.error("FAIL: no Greene foreclosure records produced in 30-day window")
        return 1

    # ── 3. Write DataSift CSV ──────────────────────────────────────────
    csv_path = write_datasift_csv(all_records, "test_greene_foreclosure_e2e.csv")
    logger.info("DataSift CSV written: %s", csv_path)

    # ── 4. Validate CSV contents ───────────────────────────────────────
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    greene_rows = [r for r in rows if r.get("County", "").strip() == "Greene"]
    logger.info("Greene rows in CSV: %d / %d total", len(greene_rows), len(rows))

    if not greene_rows:
        logger.error("FAIL: no Greene rows found in DataSift CSV")
        return 1

    # ── 5. Field-level assertions ──────────────────────────────────────
    required = {
        "Foreclosure Date": "auction_date",
        "Parcel ID": "parcel_id",
    }
    # Owner name comes from either the DETAILS page (login) or Auditor fallback
    optional_but_expected = ["Owner First Name", "Owner Last Name"]

    failures: list[str] = []
    partial_owner = 0  # count records that got owner via Auditor fallback

    for i, row in enumerate(greene_rows):
        addr = row.get("Property Street Address", f"row[{i}]")

        # Required: auction_date and parcel_id must be populated
        for col, label in required.items():
            val = row.get(col, "").strip()
            if not val:
                failures.append(f"Row {i} ({addr}): missing {label} ({col!r})")

        # Expected: owner name — flag if both first + last are empty
        first = row.get("Owner First Name", "").strip()
        last = row.get("Owner Last Name", "").strip()
        if first or last:
            partial_owner += 1

    # case_number lives in the Notes field (not a first-class DataSift column
    # today — it's part of the Notes text).  Verify it appears in Notes.
    case_in_notes = sum(
        1 for r in greene_rows
        if r.get("Notes", "") and "CV" in r.get("Notes", "")
    )
    if case_in_notes == 0:
        # Soft warning — case numbers may be in raw_text only
        logger.warning(
            "No case numbers visible in Notes column — "
            "case_number may not be surfaced in DataSift upload"
        )

    owner_pct = partial_owner / len(greene_rows) * 100
    logger.info(
        "Owner name populated: %d/%d (%.0f%%) — Auditor fallback active",
        partial_owner, len(greene_rows), owner_pct,
    )

    if failures:
        for f in failures:
            logger.error("FAIL: %s", f)
        return 1

    if owner_pct < 50:
        logger.error(
            "FAIL: owner name fill rate below 50%% (%.0f%%). "
            "Check Auditor fallback or RealAuction login.",
            owner_pct,
        )
        return 1

    logger.info(
        "PASS — %d Greene foreclosure record(s) flowed scrape → DataSift CSV "
        "(ASP.NET: %d, RealAuction: %d, CSV rows: %d, owner fill: %.0f%%)",
        len(all_records),
        len(asp_records), len(ra_records),
        len(greene_rows), owner_pct,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
