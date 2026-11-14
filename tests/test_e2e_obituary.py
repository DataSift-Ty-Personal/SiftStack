"""End-to-end check of the obituary enrichment on ONE record.

LIVE: it searches the web and calls a model on this repo's keys. It therefore runs only when a
person starts it by hand and names the record on the command line. Under pytest it does nothing:
it used to run at import with a real record written into the file, so `pytest tests` reached the
network (and a public repo carried a real owner's name).

    python tests/test_e2e_obituary.py --owner "LAST FIRST M" --address "123 Example St" \
        --city Knoxville --zip 37900 --county Knox --notice-type tax_sale
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--owner", required=True, help="the owner of record, as the county writes it")
    ap.add_argument("--address", required=True)
    ap.add_argument("--city", default="Knoxville")
    ap.add_argument("--state", default="TN")
    ap.add_argument("--zip", default="")
    ap.add_argument("--county", default="Knox")
    ap.add_argument("--notice-type", default="tax_sale")
    ap.add_argument("--parcel", default="")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    import config
    from notice_parser import NoticeData
    from obituary_enricher import enrich_obituary_data

    notice = NoticeData(address=args.address, city=args.city, state=args.state, zip=args.zip, county=args.county,
                        notice_type=args.notice_type, parcel_id=args.parcel, tax_owner_name=args.owner)
    if not config.ANTHROPIC_API_KEY:
        print("ERROR: No ANTHROPIC_API_KEY configured in .env")
        return 1
    print("Testing obituary enrichment for the record given on the command line (live: web search and a model call).")
    enrich_obituary_data([notice], config.ANTHROPIC_API_KEY)

    print()
    print("=" * 60)
    print("RESULTS:")
    for field in ("owner_deceased", "date_of_death", "obituary_url", "decision_maker_name", "decision_maker_relationship",
                  "decision_maker_street", "decision_maker_city", "decision_maker_state", "decision_maker_zip"):
        print("  %-30s %r" % (field + ":", getattr(notice, field, None)))
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
