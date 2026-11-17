"""Run obituary enrichment on an existing enriched CSV.

Loads the CSV (with ZIP, lat/lon, tax, Smarty data intact),
runs ONLY the obituary search step, and writes a new CSV with obituary fields.

Usage: python tests/run_obituary_enrichment.py <input_csv> [-v]
"""

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
from data_formatter import read_csv, write_csv
from obituary_enricher import enrich_obituary_data


def main():
    parser = argparse.ArgumentParser(description="Run obituary enrichment on a CSV.")
    parser.add_argument("input_csv", help="Path to input CSV file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    log_file = "output/obituary_enrichment_log.txt"
    handlers = [
        logging.StreamHandler(),
        logging.FileHandler(log_file, mode="w"),
    ]
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    logging.info("Log file: %s", log_file)

    # Load CSV with all existing enrichment data intact
    notices = read_csv(args.input_csv)
    logging.info("Loaded %d records from %s", len(notices), args.input_csv)

    # Check API key
    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        print("ERROR: No ANTHROPIC_API_KEY configured in .env")
        sys.exit(1)

    # Run obituary enrichment
    logging.info("Starting obituary search for deceased owner detection...")
    enrich_obituary_data(notices, api_key)

    # Summary
    total = len(notices)
    deceased = sum(1 for n in notices if n.owner_deceased)
    with_dm = sum(1 for n in notices if n.decision_maker_name)
    with_dod = sum(1 for n in notices if n.date_of_death)
    with_url = sum(1 for n in notices if n.obituary_url)
    dm_verified = sum(1 for n in notices if n.decision_maker_status == "verified_living")
    dm_from_tax = sum(1 for n in notices if n.decision_maker_source == "tax_record_joint_owner")
    full_page = sum(1 for n in notices if n.obituary_source_type == "full_page")
    snippet = sum(1 for n in notices if n.obituary_source_type == "snippet")
    with_dm2 = sum(1 for n in notices if n.decision_maker_2_name)
    with_dm3 = sum(1 for n in notices if n.decision_maker_3_name)

    logging.info("=" * 60)
    logging.info("OBITUARY ENRICHMENT RESULTS")
    logging.info("=" * 60)
    logging.info("Total records:           %d", total)
    logging.info("Confirmed deceased:      %d/%d (%.1f%%)", deceased, total, 100 * deceased / total if total else 0)
    logging.info("  Full-page matches:     %d", full_page)
    logging.info("  Snippet matches:       %d", snippet)
    logging.info("Has date of death:       %d/%d", with_dod, deceased if deceased else 1)
    logging.info("Has obituary URL:        %d/%d", with_url, deceased if deceased else 1)
    logging.info("Decision-maker ID'd:     %d/%d (%.0f%%)", with_dm, deceased if deceased else 1, 100 * with_dm / deceased if deceased else 0)
    logging.info("  DM verified living:    %d", dm_verified)
    logging.info("  DM from tax record:    %d", dm_from_tax)
    logging.info("  Has 2nd DM:            %d", with_dm2)
    logging.info("  Has 3rd DM:            %d", with_dm3)

    if deceased:
        logging.info("")
        logging.info("Deceased owners:")
        for n in notices:
            if n.owner_deceased:
                logging.info(
                    "  %s | %s, %s | DOD: %s | DM: %s (%s) [%s] | Src: %s",
                    n.owner_name,
                    n.address,
                    n.city,
                    n.date_of_death or "unknown",
                    n.decision_maker_name or "none",
                    n.decision_maker_relationship or "",
                    n.decision_maker_status or "n/a",
                    n.decision_maker_source or n.obituary_source_type,
                )

    # Export
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"obituary_enriched_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)


if __name__ == "__main__":
    main()
