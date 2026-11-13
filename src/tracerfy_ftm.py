"""Layer 3: Tracerfy re-skip for FTM records, then merge found numbers back into reisift.

Default: trace only the FTM records that currently have NO phones (from ftm_phone_routing.json),
via Tracerfy ($0.02/record). Writes a DataSift merge CSV (address + Phone 1-9). With --finish,
merges the phones into the existing 'Foreclosure'-list records.
After merging, re-run score_ftm_phones + run_phone_tag_upload to score/tag the new numbers.
Records still empty after this auto-land in the Deep Prospecting '01 No / Bad Phone' preset.

THE MERGE GOES OVER THE API (build plan 5.5). It used to drive the browser's Add-Data wizard,
which stalls on a modal instead of failing. api_intake.merge_phones finds each record by address,
adds only the numbers the owner does not already have, counts the trace on the owner, and reads
the first record back before the rest are written. It never creates a record. The wizard is
still there behind --browser (or FTM_UPLOAD_BROWSER=1).

  python src/tracerfy_ftm.py                 # trace no-phone records, write merge CSV (no upload)
  python src/tracerfy_ftm.py --all           # trace ALL FTM records (fresh numbers from a 2nd provider)
  python src/tracerfy_ftm.py --finish        # also merge found phones into reisift, over the API
  python src/tracerfy_ftm.py --finish --browser   # the same merge through the upload wizard
"""
import argparse
import asyncio
import csv
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from notice_parser import NoticeData  # noqa: E402
from tracerfy_skip_tracer import PHONE_FIELDS, batch_skip_trace  # noqa: E402
from datasift_formatter import write_datasift_split_csvs  # noqa: E402

MASTER = "output/foreclosure_master_active_2026-06-22.csv"
ROUTING = "output/ftm_phone_routing.json"


def merge(csv_path, tags: list, *, browser: bool = False, headed: bool = False, merger=None) -> dict:
    """Merge a trace's phones into the existing Foreclosure records. API unless the wizard is asked for."""
    import api_intake

    if browser or api_intake.browser_wanted():
        from sift_upload_wizard import run_upload   # Playwright, imported only when it is used
        Path("output/_tracerfy_merge").mkdir(parents=True, exist_ok=True)
        return {"route": "browser", "result": asyncio.run(run_upload(
            csv_path, "Foreclosure", tags, existing_list=True, do_finish=True, headless=not headed,
            shot_base="output/_tracerfy_merge/run"))}
    with open(csv_path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    res = (merger or api_intake.merge_phones)(rows, commit=True, source_tag="Tracerfy",
                                             list_name="Foreclosure", tags=tags)
    return {"route": "api", "result": res}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=MASTER)
    ap.add_argument("--all", action="store_true", help="trace ALL FTM records (default: only no-phone ones)")
    ap.add_argument("--finish", action="store_true", help="merge found phones into reisift (over the API)")
    ap.add_argument("--browser", action="store_true",
                    help="merge through the browser upload wizard instead of the API (the fallback)")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.csv, encoding="utf-8")))
    empty_addrs = set()
    rp = Path(ROUTING)
    if rp.exists():
        for v in json.loads(rp.read_text(encoding="utf-8")).values():
            if not v.get("phones"):
                empty_addrs.add(v["addr"].strip().lower())

    notices, picked = [], []
    for r in rows:
        addr = (r.get("address") or "").strip()
        if not addr:
            continue
        if not a.all and addr.lower() not in empty_addrs:
            continue
        owner = (r.get("full_name") or f"{(r.get('first_name') or '').strip()} {(r.get('last_name') or '').strip()}").strip()
        if len(owner.split()) < 2:
            print(f"  skip (bad name): {addr!r} owner={owner!r}")
            continue
        n = NoticeData(owner_name=owner, address=addr, city=(r.get("city") or "").strip(),
                       state=(r.get("state") or "TN").strip(), zip=(r.get("zip") or "").strip(),
                       notice_type="foreclosure", county=(r.get("county") or "").strip())
        notices.append(n)
        picked.append((addr, owner))

    print(f"Tracing {len(notices)} records via Tracerfy ({'ALL FTM' if a.all else 'no-phone only'}):")
    for addr, owner in picked:
        print(f"  - {addr}  ({owner})")
    if not notices:
        print("Nothing to trace.")
        return

    stats = batch_skip_trace(notices, max_signing_traces=1, lookup_heir_addresses=False)
    print("\nTracerfy stats:", stats)
    if stats.get("credits_exhausted"):
        print("!! Tracerfy credits exhausted. Fund the account and retry.")
        return

    found = []
    print("\n=== phones found ===")
    for n in notices:
        ph = [getattr(n, f) for f in PHONE_FIELDS if getattr(n, f, "")]
        print(f"  {n.address[:30]:30} -> {len(ph)} phones {ph}")
        if ph:
            found.append(n)
    print(f"\n{len(found)}/{len(notices)} records got phones. "
          f"{len(notices) - len(found)} still empty -> Deep Prospecting (phone:0, skiptraced:1).")

    if not found:
        return
    csv_infos = write_datasift_split_csvs(found)
    csv_path = csv_infos[0]["path"]
    print("\nmerge CSV:", csv_path)

    if a.finish:
        iy, iw, _ = date.today().isocalendar()
        tags = ["FTM", "foreclosure", "has_auction", "Courthouse Data", f"{iy}-W{iw:02d}"]
        out = merge(csv_path, tags, browser=a.browser, headed=a.headed)
        print("merge (%s):" % out["route"], out["result"])
        if out["route"] == "api" and out["result"].get("stopped"):
            print("!! " + out["result"]["stopped"])
            return
        print("\nNext: re-run score_ftm_phones.py + run_phone_tag_upload.py to tier the new numbers.")
    else:
        print("\nDRY: pass --finish to merge the found phones into reisift.")


if __name__ == "__main__":
    main()
