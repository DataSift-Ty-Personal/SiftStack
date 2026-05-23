"""Resolve NJ tax-sale block/lot descriptions to street addresses.

Standalone CLI — reads the recovery CSV produced by
`modal run modal_app.py::nj_tax_sale_recovery`, looks up each row's
block/lot against taxrecords-nj.com (via nj_taxrecords.lookup_by_block_lot),
and writes a new CSV with resolved street addresses plus a status
column for every row.

Designed for the one-time backfill of the May-2026 tax-sale records
that were dropped by the vacant-land filter — also re-runnable for
later recovery batches. NOT yet wired into the enrichment pipeline;
validate the resolver against a real batch first, then we'll decide
whether to integrate or swap to a paid API (Regrid / DataTree).

Constraints:
  - Only Middlesex, Somerset, Union are supported. Essex MOD-IV lives
    on a different backend (taxdatahub.com) and is flagged
    `unsupported_county`.
  - Block/lot pairs are NOT unique across municipalities within a
    county, so we filter results by the `municipality` column's city
    name appearing in the parcel's `property_location`. Records with
    multiple municipality-matching parcels are flagged `ambiguous`.

Run with:
  PYTHONPATH=src python scripts/resolve_block_lot.py \\
      --csv output/2026-05-23/nj_tax_sale_recovery_*.csv \\
      --out output/2026-05-23/nj_tax_sale_recovery_resolved.csv

Optional flags:
  --limit N            Process only the first N rows (smoke testing)
  --delay-seconds 1.5  Sleep between lookups (default 1.5 — courtesy)
  -v / --verbose       Log per-row lookup detail
"""

from __future__ import annotations

import argparse
import csv
import logging
import random
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nj_taxrecords import COUNTY_CODES, lookup_by_block_lot  # noqa: E402


# Resolution status taxonomy — keep stable; downstream tooling may bucket.
STATUS_RESOLVED = "resolved"
STATUS_NOT_FOUND = "not_found"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNSUPPORTED_COUNTY = "unsupported_county"
STATUS_MISSING_BLOCK_LOT = "missing_block_lot"
STATUS_ERROR = "error"

# Extra columns appended to the output CSV. Original columns are
# preserved untouched so the operator can compare before/after.
OUTPUT_EXTRA_COLS = [
    "resolved_address",
    "resolved_city",
    "resolved_zip",
    "resolution_status",
    "resolution_notes",
]

logger = logging.getLogger("resolve_block_lot")


def _city_matches(city: str, parcel_location: str) -> bool:
    """Loose city match — taxrecords-nj puts the city into the property
    location string and capitalization / suffix vary."""
    c = (city or "").strip().lower()
    p = (parcel_location or "").lower()
    if not c or not p:
        return False
    # Strip common municipal suffixes that bloat the recovery city field
    # without changing the match (Borough / Township / City / Town).
    for suffix in (" borough", " township", " city", " town"):
        if c.endswith(suffix):
            c = c[: -len(suffix)].strip()
            break
    return c in p


def _filter_by_municipality(parcels: list, city: str) -> list:
    """Narrow county-wide block/lot hits to the recovery row's city."""
    if not parcels or not city:
        return parcels
    matched = [p for p in parcels if _city_matches(city, p.property_location)]
    return matched if matched else parcels  # fall back to all if nothing matches


def _resolve_row(row: dict) -> dict:
    """Resolve one recovery row. Returns the 5 OUTPUT_EXTRA_COLS as a dict."""
    county = (row.get("county") or "").strip()
    block = (row.get("block") or "").strip()
    lot = (row.get("lot") or "").strip()
    qualifier = (row.get("qualifier") or "").strip()
    city = (row.get("municipality") or row.get("city") or "").strip()

    if county not in COUNTY_CODES:
        return {
            "resolved_address": "",
            "resolved_city": "",
            "resolved_zip": "",
            "resolution_status": STATUS_UNSUPPORTED_COUNTY,
            "resolution_notes": f"taxrecords-nj does not cover {county!r}",
        }
    if not block or not lot:
        return {
            "resolved_address": "",
            "resolved_city": "",
            "resolved_zip": "",
            "resolution_status": STATUS_MISSING_BLOCK_LOT,
            "resolution_notes": f"block={block!r} lot={lot!r}",
        }

    try:
        parcels = lookup_by_block_lot(county, block, lot, qualifier)
    except Exception as e:
        return {
            "resolved_address": "",
            "resolved_city": "",
            "resolved_zip": "",
            "resolution_status": STATUS_ERROR,
            "resolution_notes": f"{type(e).__name__}: {e}",
        }

    if not parcels:
        return {
            "resolved_address": "",
            "resolved_city": "",
            "resolved_zip": "",
            "resolution_status": STATUS_NOT_FOUND,
            "resolution_notes": "0 parcels returned",
        }

    narrowed = _filter_by_municipality(parcels, city)

    if len(narrowed) > 1:
        return {
            "resolved_address": "",
            "resolved_city": "",
            "resolved_zip": "",
            "resolution_status": STATUS_AMBIGUOUS,
            "resolution_notes": (
                f"{len(parcels)} county hits, {len(narrowed)} after city filter — "
                f"locations: {[p.property_location for p in narrowed[:3]]}"
            ),
        }

    p = narrowed[0]
    # taxrecords-nj returns property_location like "123 MAIN ST" without
    # city/zip — those live in mailing_city_state on the owner record,
    # which may NOT match the property's municipality. Leave city/zip
    # blank when we can't confidently derive them; the downstream Smarty
    # stage will resolve the rest.
    return {
        "resolved_address": (p.property_location or "").strip(),
        "resolved_city": city,
        "resolved_zip": "",
        "resolution_status": STATUS_RESOLVED,
        "resolution_notes": (
            f"district_code={p.district_code} parcel_id={p.parcel_id}"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/resolve_block_lot.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--csv", required=True, type=Path,
                        help="Input recovery CSV (TaxSaleRecord-schema)")
    parser.add_argument("--out", required=True, type=Path,
                        help="Output CSV path — original cols + resolution_*")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process only first N rows (0 = all)")
    parser.add_argument("--delay-seconds", type=float, default=1.5,
                        help="Sleep between taxrecords-nj lookups (default 1.5)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not args.csv.is_file():
        print(f"error: input not found: {args.csv}", file=sys.stderr)
        return 2

    with open(args.csv, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        in_fieldnames = reader.fieldnames or []
        rows = list(reader)
    logger.info("Loaded %d rows from %s", len(rows), args.csv)

    if args.limit:
        rows = rows[: args.limit]
        logger.info("Processing first %d rows only", len(rows))

    out_fieldnames = list(in_fieldnames)
    for col in OUTPUT_EXTRA_COLS:
        if col not in out_fieldnames:
            out_fieldnames.append(col)

    status_counts: Counter = Counter()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fieldnames, extrasaction="ignore")
        writer.writeheader()
        for i, row in enumerate(rows, start=1):
            resolution = _resolve_row(row)
            status = resolution["resolution_status"]
            status_counts[status] += 1
            writer.writerow({**row, **resolution})
            if args.verbose or i % 25 == 0:
                logger.info(
                    "[%d/%d] %s | county=%s block=%s lot=%s -> %s",
                    i, len(rows), row.get("municipality", ""),
                    row.get("county", ""), row.get("block", ""),
                    row.get("lot", ""), status,
                )
            # Rate-limit only when we actually hit the network. Skip the
            # sleep on local-only outcomes to keep the run fast.
            if status not in (STATUS_UNSUPPORTED_COUNTY, STATUS_MISSING_BLOCK_LOT):
                time.sleep(args.delay_seconds + random.uniform(0.0, 0.5))

    logger.info("Done. Wrote %s", args.out)
    logger.info("Status counts:")
    for s, n in status_counts.most_common():
        logger.info("  %s: %d", s, n)
    resolved = status_counts.get(STATUS_RESOLVED, 0)
    if rows:
        logger.info("Resolution rate: %d/%d (%.1f%%)",
                    resolved, len(rows), 100 * resolved / len(rows))

    return 0 if resolved > 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
