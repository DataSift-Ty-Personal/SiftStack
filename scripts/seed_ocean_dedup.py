"""One-time recovery: seed the `ocean_probate` dedup bucket from RAW CSVs.

Why this exists: Ocean was wired into nj_weekly_all with a 180-day window
against an EMPTY `ocean_probate` dedup bucket, so the entire Ocean history
(~1,579 records) came in as "new" on 2026-06-17 and the enrichment pass blew
past the 8-hour timeout before it could write output OR save the dedup
tracker. Because tracking never saved, every retry re-faced the full flood.

This script marks those already-scraped Ocean records as "seen" so steady-
state weekly runs only pick up genuinely NEW Ocean filings. The RAW data
itself is not lost — it stays on the Modal volume and can be re-enriched in
a controlled batch later if wanted.

It is filesystem-only (no Modal SDK) so YOU control every volume read/write.

── Workflow ─────────────────────────────────────────────────────────────────
1. Pull the two RAW snapshots + the tracking file off the volume:

     modal volume get siftstack-tracking \
       output/2026-06-17/nj_weekly_all_RAW_2026-06-17_103257.csv ./_recover/
     modal volume get siftstack-tracking \
       output/2026-06-17/nj_weekly_all_RAW_2026-06-17_183459.csv ./_recover/
     modal volume get siftstack-tracking processed_ids.json ./_recover/processed_ids.json

2. Dry-run to see what WOULD be seeded (no write):

     PYTHONPATH=src python scripts/seed_ocean_dedup.py \
       --raw _recover/nj_weekly_all_RAW_2026-06-17_103257.csv \
             _recover/nj_weekly_all_RAW_2026-06-17_183459.csv \
       --tracking _recover/processed_ids.json --dry-run

3. Run for real (writes the updated tracking file in place):

     PYTHONPATH=src python scripts/seed_ocean_dedup.py \
       --raw _recover/nj_weekly_all_RAW_2026-06-17_103257.csv \
             _recover/nj_weekly_all_RAW_2026-06-17_183459.csv \
       --tracking _recover/processed_ids.json

4. Push the updated tracking file back to the volume:

     modal volume put --force siftstack-tracking \
       _recover/processed_ids.json processed_ids.json

IDs are extracted with the SAME dedup_tracker.extract_id() the live pipeline
uses, so seeded IDs match exactly what filter_new would have stored.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dedup_tracker import load_tracking, save_tracking, extract_id  # noqa: E402
from notice_parser import NoticeData  # noqa: E402

_SOURCE = "ocean_probate"


def _ocean_ids_from_csv(path: Path) -> tuple[list[str], int, int]:
    """Return (ids, ocean_probate_rows, total_rows) for one RAW CSV."""
    ids: list[str] = []
    ocean_rows = 0
    total = 0
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            county = (row.get("county") or "").strip().lower()
            ntype = (row.get("notice_type") or "").strip().lower()
            if county != "ocean" or "probate" not in ntype:
                continue
            ocean_rows += 1
            # Reconstruct just enough of NoticeData for extract_id. Ocean's
            # detail source_url carries Q_PK_ID, which is the dedup key.
            n = NoticeData(
                source_url=row.get("source_url") or "",
                raw_text=row.get("raw_text") or "",
                county=row.get("county") or "",
                notice_type=row.get("notice_type") or "",
            )
            rid = extract_id(n, _SOURCE)
            if rid:
                ids.append(rid)
    return ids, ocean_rows, total


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed ocean_probate dedup bucket from RAW CSVs")
    ap.add_argument("--raw", nargs="+", required=True, type=Path,
                    help="One or more RAW snapshot CSVs (local copies)")
    ap.add_argument("--tracking", required=True, type=Path,
                    help="Local copy of processed_ids.json to update in place")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be seeded; do not write")
    args = ap.parse_args()

    for p in args.raw:
        if not p.exists():
            print(f"ERROR: RAW CSV not found: {p}", file=sys.stderr)
            return 1
    if not args.tracking.exists():
        print(f"ERROR: tracking file not found: {args.tracking}", file=sys.stderr)
        return 1

    all_ids: list[str] = []
    for p in args.raw:
        ids, ocean_rows, total = _ocean_ids_from_csv(p)
        print(f"  {p.name}: {total} rows, {ocean_rows} Ocean-probate rows, "
              f"{len(ids)} with extractable IDs")
        all_ids.extend(ids)

    unique_ids = set(all_ids)
    print(f"\nUnique Ocean IDs across all RAW files: {len(unique_ids)} "
          f"(from {len(all_ids)} total rows)")

    tracking = load_tracking(args.tracking)
    bucket = tracking.setdefault(_SOURCE, {})
    before = len(bucket)
    already = sum(1 for rid in unique_ids if rid in bucket)
    to_add = [rid for rid in unique_ids if rid not in bucket]
    print(f"ocean_probate bucket before: {before} IDs "
          f"({already} of the scraped IDs already present, {len(to_add)} new to seed)")

    if args.dry_run:
        print("\n[dry-run] No changes written. "
              f"Would seed {len(to_add)} IDs → bucket would hold {before + len(to_add)}.")
        return 0

    now_iso = datetime.utcnow().isoformat()
    for rid in to_add:
        bucket[rid] = now_iso
    save_tracking(tracking, args.tracking)
    print(f"\n✅ Seeded {len(to_add)} new IDs. ocean_probate bucket now holds {len(bucket)}.")
    print(f"   Wrote {args.tracking}. Push it back with:")
    print(f"   modal volume put --force siftstack-tracking {args.tracking} processed_ids.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
