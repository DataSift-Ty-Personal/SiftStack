"""Franklin County (Columbus), Ohio foreclosure scraper.

Pulls sheriff-sale foreclosures from `franklin.sheriffsaleauction.ohio.gov`
via the shared RealAuction base class. All logic lives in
`scrapers.realauction_base.RealAuctionScraper`; this module only sets the
4 county-specific class attributes.

Domain rules (per CLAUDE.md):
  * county = "Franklin", state = "OH", notice_type = "foreclosure".
  * date_added = the sheriff-sale auction date (the public "publish" date).
  * auction_date = same ISO date as date_added.
  * owner_name = defendant from the DETAILS page when logged in; blank
    otherwise — enrichment pipeline resolves via Franklin County Auditor.
  * source_url = direct link to the sale-day preview page.
"""

from __future__ import annotations

from scrapers.realauction_base import RealAuctionScraper


class Scraper(RealAuctionScraper):
    """Franklin County Foreclosure — RealAuction sheriff-sale scraper."""

    realauction_subdomain = "franklin"
    county = "Franklin"
    source_name = "Franklin County Sheriff Sale Auction"
    source_url = "https://franklin.sheriffsaleauction.ohio.gov"


# ── Standalone test harness ────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import asyncio
    import csv
    import logging
    import sys
    from dataclasses import asdict
    from datetime import date, timedelta
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Test Franklin foreclosure scraper")
    parser.add_argument("--days", type=int, default=30, help="Look back N days")
    parser.add_argument(
        "--output",
        type=str,
        default="output/test_oh_franklin_foreclosure.csv",
        help="CSV output path",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    since = date.today() - timedelta(days=args.days)
    records = asyncio.run(Scraper().scrape(since_date=since))

    print(f"Scraped {len(records)} foreclosure records since {since}")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        if records:
            writer = csv.DictWriter(f, fieldnames=list(asdict(records[0]).keys()))
            writer.writeheader()
            for r in records:
                writer.writerow(asdict(r))
        else:
            f.write("")
    print(f"Wrote {out_path}")
    sys.exit(0)
