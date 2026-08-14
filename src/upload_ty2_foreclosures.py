"""Upload active future-auction foreclosures into ty+2 (the FTM stream), per county.

Reads a foreclosure_master_active_*.csv, keeps records whose auction_date is STRICTLY
AFTER today, splits them BY COUNTY (so each upload gets a uniform tag set), and drives the
current DataSift 6-step upload wizard (sift_upload_wizard) once per county. The FTM routing
tag is applied at the wizard's "Add tags" step (the CSV Tags column is not a mappable target),
so the records land in 01 FTM-CALL / 02 FTM-MAIL, and has_auction feeds 07 Hot Auction.

Safe by default: STOPS at the Review step (nothing committed) and writes per-step screenshots
to output/_wizard_run/. Pass --finish to actually click "Finish Upload".

  # dry preview (stops at Review, screenshots only):
  python src/upload_ty2_foreclosures.py --csv output/foreclosure_master_active_2026-06-19.csv
  python src/upload_ty2_foreclosures.py --csv ... --county Knox            # one county
  # commit:
  python src/upload_ty2_foreclosures.py --csv ... --finish
"""

import argparse
import asyncio
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402  (loads .env)
from data_formatter import read_csv  # noqa: E402
from datasift_formatter import write_datasift_split_csvs  # noqa: E402
from sift_upload_wizard import run_upload  # noqa: E402

SHOT_DIR = Path("output/_wizard_run")


def _parse_date(s: str):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Upload future-auction foreclosures to ty+2 (FTM), per county")
    ap.add_argument("--csv", required=True, help="foreclosure_master_active_*.csv path")
    ap.add_argument("--county", default=None, help="Only this county (e.g. Knox)")
    ap.add_argument("--list", default="Foreclosure", help="existing key list to add records to")
    ap.add_argument("--finish", action="store_true", help="Click Finish Upload (commit). Default: stop at Review.")
    ap.add_argument("--headed", action="store_true", help="Run browser headed (default headless)")
    ap.add_argument("--include-today", action="store_true", help="keep auctions dated exactly today")
    args = ap.parse_args()

    today = date.today()
    notices = read_csv(args.csv)
    print(f"Loaded {len(notices)} records from {args.csv}")

    kept = []
    for n in notices:
        d = _parse_date(getattr(n, "auction_date", "") or "")
        if d is None:
            continue
        if d > today or (args.include_today and d == today):
            kept.append(n)
    print(f"Future-auction records (auction > {today}): {len(kept)}")

    by_county = defaultdict(list)
    for n in kept:
        by_county[(n.county or "Unknown").strip()].append(n)
    if args.county:
        want = args.county.strip().lower()
        by_county = {c: v for c, v in by_county.items() if c.lower() == want}
        if not by_county:
            print(f"No records for county {args.county!r}. Available: {dict(Counter((n.county or '?') for n in kept))}")
            return
    print("By county:", {c: len(v) for c, v in by_county.items()})

    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    iso_year, iso_week, _ = today.isocalendar()
    week_tag = f"{iso_year}-W{iso_week:02d}"
    key_list = args.list
    mode = "COMMIT (will Finish Upload)" if args.finish else "DRY (stop at Review)"
    print(f"\nMode: {mode} | key list={key_list!r} (existing) | week={week_tag} | "
          f"headless={not args.headed} | account={config.DATASIFT_EMAIL}\n")

    results = []
    for county, ns in by_county.items():
        csv_infos = write_datasift_split_csvs(ns)
        csv_path = csv_infos[0]["path"]  # foreclosures (living) -> single DMs CSV
        # existing tags (FTM/foreclosure/has_auction/Courthouse Data) + location (county) + week
        tags = ["FTM", "foreclosure", "has_auction", "Courthouse Data", county.lower(), week_tag]
        print(f"--- {county}: {len(ns)} records | list={key_list!r} (existing) | tags={tags}")
        print(f"    CSV: {csv_path}")
        res = asyncio.run(run_upload(
            csv_path, key_list, tags,
            do_finish=args.finish,
            existing_list=True,
            headless=not args.headed,
            shot_base=str(SHOT_DIR / county.lower()),
        ))
        print(f"    RESULT: {res}")
        results.append((county, res))

    print("\n=== SUMMARY ===")
    for county, res in results:
        ok = "OK" if res.get("success") else "FAIL"
        print(f"  {county}: {ok} | finished={res.get('finished')} | "
              f"tags_added={res.get('tags_added')} | {res.get('message')}")
    if not args.finish:
        print("\nDRY run — nothing committed. Review screenshots in output/_wizard_run/, then re-run with --finish.")


if __name__ == "__main__":
    main()
