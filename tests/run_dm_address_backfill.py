"""Backfill decision-maker mailing addresses on an existing obituary-enriched CSV.

Loads the CSV with obituary data intact, runs ONLY the DM address lookup step
for records that have a decision-maker name but no mailing address, then writes
an updated CSV and generates the Excel review workbook.

Usage: python tests/run_dm_address_backfill.py <input_csv> [-v]
"""

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
from data_formatter import read_csv, write_csv
from obituary_enricher import _lookup_dm_address
from excel_exporter import export_review_workbook


def main():
    parser = argparse.ArgumentParser(description="Backfill DM mailing addresses.")
    parser.add_argument("input_csv", help="Path to input CSV file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    log_file = "output/dm_address_backfill_log.txt"
    # Use UTF-8 stream to avoid Windows cp1252 encoding errors on Unicode chars
    console = logging.StreamHandler(
        open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    )
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    handlers = [console, file_handler]
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    # Suppress overly verbose HTTP request body logging from anthropic SDK
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.info("Log file: %s", log_file)

    # Load CSV
    notices = read_csv(args.input_csv)
    logging.info("Loaded %d records from %s", len(notices), args.input_csv)

    # Check API key
    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        print("ERROR: No ANTHROPIC_API_KEY configured in .env")
        sys.exit(1)

    # Find records needing DM address lookup
    candidates = [
        n for n in notices
        if n.owner_deceased == "yes"
        and n.decision_maker_name
        and not n.decision_maker_street
    ]
    logging.info("Found %d deceased records with DM but no mailing address", len(candidates))

    # Run address lookup for each
    found = 0

    for i, notice in enumerate(candidates, 1):
        dm_name = notice.decision_maker_name
        # Use DM city if we have it, otherwise property city
        city = notice.city.strip() or "Knoxville"

        logging.info(
            "[%d/%d] Looking up address for: %s (city: %s)",
            i, len(candidates), dm_name, city,
        )

        addr = _lookup_dm_address(dm_name, city, api_key)

        if addr.get("street"):
            notice.decision_maker_street = addr["street"]
            notice.decision_maker_city = addr.get("city", "")
            notice.decision_maker_state = addr.get("state", "")
            notice.decision_maker_zip = addr.get("zip", "")
            found += 1
            logging.info(
                "  FOUND: %s, %s %s %s",
                addr["street"], addr.get("city", ""),
                addr.get("state", ""), addr.get("zip", ""),
            )
        else:
            logging.info("  Not found")

    # Summary
    logging.info("=" * 60)
    logging.info("DM ADDRESS BACKFILL RESULTS")
    logging.info("=" * 60)
    logging.info("Candidates:          %d", len(candidates))
    logging.info("Addresses found:     %d/%d (%.0f%%)",
                 found, len(candidates),
                 100 * found / len(candidates) if candidates else 0)

    if found:
        logging.info("")
        logging.info("DMs with addresses:")
        for n in notices:
            if n.decision_maker_street:
                logging.info(
                    "  %s -> %s | %s, %s %s %s",
                    n.owner_name, n.decision_maker_name,
                    n.decision_maker_street, n.decision_maker_city,
                    n.decision_maker_state, n.decision_maker_zip,
                )

    # Export CSV
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    csv_filename = f"dm_address_backfill_{timestamp}.csv"
    csv_path = write_csv(notices, filename=csv_filename)
    logging.info("CSV output: %s", csv_path)

    # Export Excel review workbook
    xlsx_path = str(csv_path).replace(".csv", ".xlsx").replace(
        "dm_address_backfill_", "dm_address_review_"
    )
    export_review_workbook(str(csv_path), xlsx_path)
    logging.info("Excel output: %s", xlsx_path)


if __name__ == "__main__":
    main()
