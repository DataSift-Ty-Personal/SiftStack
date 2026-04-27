"""Montgomery County (Dayton), Ohio foreclosure scraper — RealAuction source.

Pulls sheriff-sale foreclosures from `montgomery.sheriffsaleauction.ohio.gov`
via the shared RealAuction base class.

Runs alongside the existing `oh_montgomery_foreclosure.py` scraper which
hits the county's ColdFusion sheriff site (`go.mcohio.org`). The two
sources cover the SAME stage (auction listing) from different vantage
points; data dedups downstream on `parcel_id` + `case_number`. Keeping
both in the registry provides resilience if either source goes down.

Domain rules: same as Franklin (see `oh_franklin_foreclosure.py`).
"""

from __future__ import annotations

from scrapers.realauction_base import RealAuctionScraper


class Scraper(RealAuctionScraper):
    """Montgomery County Foreclosure — RealAuction sheriff-sale scraper."""

    realauction_subdomain = "montgomery"
    county = "Montgomery"
    source_name = "Montgomery County Sheriff Sale Auction (RealAuction)"
    source_url = "https://montgomery.sheriffsaleauction.ohio.gov"


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

    parser = argparse.ArgumentParser(description="Test Montgomery RealAuction scraper")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--output",
        default="output/test_oh_montgomery_realauction.csv",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    since = date.today() - timedelta(days=args.days)
    records = asyncio.run(Scraper().scrape(since_date=since))

    print(f"Scraped {len(records)} Montgomery RealAuction records since {since}")

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
