"""Merge existing foreclosure CSV with newly scraped data, dedup, and export.

Usage: python tests/merge_foreclosure.py <existing_csv> <new_csv> [--output <path>]
"""

import argparse
import csv
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from data_formatter import read_csv, write_csv


def main():
    parser = argparse.ArgumentParser(description="Merge foreclosure CSVs and deduplicate.")
    parser.add_argument("existing_csv", help="Path to existing foreclosure CSV")
    parser.add_argument("new_csv", help="Path to newly scraped CSV")
    parser.add_argument("--output", help="Output filename (default: auto-generated)")
    args = parser.parse_args()

    existing = read_csv(args.existing_csv)
    print(f"Existing: {len(existing)} records from {args.existing_csv}")

    new = read_csv(args.new_csv)
    print(f"New:      {len(new)} records from {args.new_csv}")

    # Filter new to foreclosure only (in case the scrape CSV has mixed types)
    new_foreclosure = [n for n in new if n.notice_type == "foreclosure"]
    if len(new_foreclosure) != len(new):
        print(f"  Filtered to {len(new_foreclosure)} foreclosure records (dropped {len(new) - len(new_foreclosure)} non-foreclosure)")
    new = new_foreclosure

    # Merge
    combined = existing + new
    print(f"Combined: {len(combined)} records before dedup")

    # Deduplicate by source_url (unique notice identifier)
    seen_urls = set()
    deduped = []
    dupes = 0
    for n in combined:
        url = (n.source_url or "").strip()
        if url and url in seen_urls:
            dupes += 1
            continue
        if url:
            seen_urls.add(url)
        deduped.append(n)

    print(f"Deduped:  {len(deduped)} records ({dupes} duplicates removed)")

    # Sort by date_added descending
    def _parse_date(n):
        d = n.date_added or ""
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                return datetime.strptime(d, fmt)
            except ValueError:
                continue
        return datetime.min

    deduped.sort(key=_parse_date, reverse=True)

    # Date range
    dates = [_parse_date(n) for n in deduped if _parse_date(n) != datetime.min]
    if dates:
        print(f"Date range: {min(dates).strftime('%Y-%m-%d')} to {max(dates).strftime('%Y-%m-%d')}")

    # Write output
    if args.output:
        filename = args.output
    else:
        timestamp = datetime.now().strftime("%Y-%m-%d")
        filename = f"knox_foreclosure_complete_{timestamp}.csv"

    path = write_csv(deduped, filename=filename)
    print(f"Output:   {path}")


if __name__ == "__main__":
    main()
