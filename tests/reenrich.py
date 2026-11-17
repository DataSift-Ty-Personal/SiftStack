"""Re-enrich a CSV with the full pipeline.

Loads existing CSV, clears all enrichment fields, and re-runs the full
enrichment pipeline from scratch.

Usage: python tests/reenrich.py <input_csv> [--skip-smarty] [--skip-tax] [--skip-parcel-lookup] [-v]
"""

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from data_formatter import read_csv, write_csv
from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline


def main():
    parser = argparse.ArgumentParser(description="Re-enrich a CSV with the full pipeline.")
    parser.add_argument("input_csv", help="Path to input CSV file")
    parser.add_argument("--skip-smarty", action="store_true")
    parser.add_argument("--skip-tax", action="store_true")
    parser.add_argument("--skip-parcel-lookup", action="store_true")
    parser.add_argument("--skip-geocode", action="store_true")
    parser.add_argument("--skip-obituary", action="store_true")
    parser.add_argument("--skip-zillow", action="store_true")
    parser.add_argument("--skip-entity-filter", action="store_true")
    parser.add_argument("--skip-commercial-filter", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    log_file = "output/reenrich_log.txt"
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
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.info("Log file: %s", log_file)

    notices = read_csv(args.input_csv)
    logging.info("Loaded %d records from %s", len(notices), args.input_csv)

    # Clear enrichment fields so we re-derive everything
    for n in notices:
        n.zip = ""
        n.zip_plus4 = ""
        n.latitude = ""
        n.longitude = ""
        n.dpv_match_code = ""
        n.vacant = ""
        n.rdi = ""
        n.tax_delinquent_amount = ""
        n.tax_delinquent_years = ""
        n.deceased_indicator = ""
        n.tax_owner_name = ""
        n.owner_deceased = ""
        n.date_of_death = ""
        n.obituary_url = ""
        n.decision_maker_name = ""
        n.decision_maker_relationship = ""
        n.decision_maker_status = ""
        n.decision_maker_source = ""
        n.decision_maker_2_name = ""
        n.decision_maker_2_relationship = ""
        n.decision_maker_2_status = ""
        n.decision_maker_3_name = ""
        n.decision_maker_3_relationship = ""
        n.decision_maker_3_status = ""
        n.obituary_source_type = ""
        n.heir_search_depth = ""
        n.heirs_verified_living = ""
        n.heirs_verified_deceased = ""
        n.heirs_unverified = ""
        n.dm_confidence = ""
        n.dm_confidence_reason = ""
        n.missing_data_flags = ""
        n.mailable = ""

    # Run unified enrichment pipeline
    opts = PipelineOptions(
        skip_parcel_lookup=args.skip_parcel_lookup,
        skip_smarty=args.skip_smarty,
        skip_tax=args.skip_tax,
        skip_geocode=args.skip_geocode,
        skip_zillow=args.skip_zillow,
        skip_obituary=args.skip_obituary,
        skip_entity_filter=args.skip_entity_filter,
        skip_commercial_filter=args.skip_commercial_filter,
        source_label="reenrich",
    )
    notices = run_enrichment_pipeline(notices, opts)

    # Export
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"reenriched_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)


if __name__ == "__main__":
    main()
