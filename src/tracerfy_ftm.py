"""Tracerfy re-skip for the records on a DataSift list, then the numbers merged onto those records and, if asked,
scored and tiered. On the account's own Open API key, with your own Tracerfy key (TRACERFY_API_KEY in .env).

By default the records come from the account itself: every record on the list (Foreclosure unless --list says
otherwise) whose owner has no phone yet, read off the records search's own phone flag. --all traces every record on
the list, for numbers from a second source. --csv traces the rows of a file instead (address, city, state, zip and
the owner's name).

  python src/tracerfy_ftm.py                          # dry: who would be traced and the most it can cost
  python src/tracerfy_ftm.py --commit                 # trace ($0.02 a record), merge the numbers onto the records
  python src/tracerfy_ftm.py --commit --score         # and Trestle-score the new numbers into dial tiers
  python src/tracerfy_ftm.py --list Probate --all --commit
  python src/tracerfy_ftm.py --csv foreclosures.csv --commit

THE MERGE GOES OVER THE API (build plan 5.5). api_intake.merge_phones finds each record by address, adds only the
numbers the owner does not already have, counts the trace on the owner, and reads the first record back before the
rest are written. It never creates a record. The browser wizard is still there behind --browser (or
FTM_UPLOAD_BROWSER=1), for the team's own workstation only. --finish is the old name for --commit.

Records still without a number after this are the deep prospecting queue (no phone, skip traced).
"""
import argparse
import asyncio
import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402,F401  (loads .env)
from notice_parser import NoticeData  # noqa: E402
from tracerfy_skip_tracer import PHONE_FIELDS, batch_skip_trace  # noqa: E402
from datasift_formatter import write_datasift_split_csvs  # noqa: E402

COST_PER_RECORD = 0.02


def merge(csv_path, tags: list, *, browser: bool = False, headed: bool = False, merger=None,
          list_name: str = "Foreclosure") -> dict:
    """Merge a trace's phones into the existing records. API unless the wizard is asked for."""
    import api_intake

    if browser or api_intake.browser_wanted():
        from sift_upload_wizard import run_upload   # staff-only: the old Playwright wizard, imported only when used
        Path("output/_tracerfy_merge").mkdir(parents=True, exist_ok=True)
        return {"route": "browser", "result": asyncio.run(run_upload(
            csv_path, list_name, tags, existing_list=True, do_finish=True, headless=not headed,
            shot_base="output/_tracerfy_merge/run"))}
    with open(csv_path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    res = (merger or api_intake.merge_phones)(rows, commit=True, source_tag="Tracerfy",
                                             list_name=list_name, tags=tags)
    return {"route": "api", "result": res}


def targets(a, api=None) -> list[dict]:
    """The rows to trace: a file's rows, or the account's own records on the list."""
    if a.csv:
        with open(a.csv, encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    import api_intake
    import datasift_api_upload as up
    return api_intake.account_targets(api or up.Api(), a.list_name, no_phone_only=not a.all)


def to_notices(rows: list[dict], notice_type: str) -> tuple[list, list]:
    """NoticeData for every row with a person's name; the rest are named and left out."""
    notices, skipped = [], []
    for r in rows:
        addr = (r.get("address") or "").strip()
        owner = (r.get("full_name") or "%s %s" % ((r.get("first_name") or "").strip(),
                                                   (r.get("last_name") or "").strip())).strip()
        if not addr:
            continue
        if len(owner.split()) < 2:
            skipped.append((addr, (r.get("company") or owner or "no owner name").strip()))
            continue
        notices.append(NoticeData(owner_name=owner, address=addr, city=(r.get("city") or "").strip(),
                                  state=(r.get("state") or "TN").strip(), zip=(r.get("zip") or "").strip()[:5],
                                  notice_type=notice_type, county=(r.get("county") or "").strip()))
    return notices, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", dest="list_name", default="Foreclosure", help="the DataSift list whose records are traced")
    ap.add_argument("--csv", default="", help="trace the rows of this file instead of the account's list")
    ap.add_argument("--all", action="store_true", help="every record on the list (default: only those with no phone)")
    ap.add_argument("--commit", "--finish", dest="commit", action="store_true",
                    help="buy the traces and merge the numbers onto the records (dry without it)")
    ap.add_argument("--score", action="store_true", help="with --commit: Trestle-score the new numbers into dial tiers")
    ap.add_argument("--max-usd", type=float, default=25.0, help="refuse a trace that could cost more than this")
    ap.add_argument("--browser", action="store_true",
                    help="staff only: merge through the browser upload wizard instead of the API")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args(argv)

    import datasift_api_upload as up
    api = None if a.csv and not a.commit else up.Api()
    rows = targets(a, api)
    notices, skipped = to_notices(rows, "foreclosure" if a.list_name.lower() == "foreclosure" else a.list_name.lower())
    source = a.csv or "list %r (%s)" % (a.list_name, "every record" if a.all else "records with no phone")
    print("Tracerfy: %d to trace from %s, %d left out with no person's name; at most $%.2f" % (
        len(notices), source, len(skipped), len(notices) * COST_PER_RECORD))
    for addr, who in skipped[:10]:
        print("  left out: %s (%s)" % (addr, who))
    if not notices:
        print("Nothing to trace.")
        return 0
    if len(notices) * COST_PER_RECORD > a.max_usd:
        raise SystemExit("REFUSING: %d traces could cost $%.2f, over --max-usd %.2f" % (
            len(notices), len(notices) * COST_PER_RECORD, a.max_usd))
    if not a.commit:
        print("DRY RUN. Re-run with --commit to trace and merge%s." % (" and score" if a.score else ""))
        return 0

    stats = batch_skip_trace(notices, max_signing_traces=1, lookup_heir_addresses=False)
    print("Tracerfy stats:", stats)
    if stats.get("credits_exhausted"):
        print("!! Tracerfy credits exhausted. Fund the account and retry.")
        return 1
    found = [n for n in notices if any(getattr(n, f, "") for f in PHONE_FIELDS)]
    print("%d of %d records got numbers; %d still have none (the deep prospecting queue)." % (
        len(found), len(notices), len(notices) - len(found)))
    if not found:
        return 0
    csv_path = write_datasift_split_csvs(found)[0]["path"]
    print("merge file:", csv_path)
    iy, iw, _ = date.today().isocalendar()
    tags = ["FTM", "foreclosure", "has_auction", "Courthouse Data", f"{iy}-W{iw:02d}"] \
        if a.list_name.lower() == "foreclosure" else [f"{iy}-W{iw:02d}"]
    out = merge(csv_path, tags, browser=a.browser, headed=a.headed, list_name=a.list_name)
    print("merge (%s):" % out["route"], {k: v for k, v in out["result"].items() if k != "records"})
    if out["route"] == "api" and out["result"].get("stopped"):
        print("!! " + out["result"]["stopped"])
        return 1
    uuids = out["result"].get("records") or [] if out["route"] == "api" else []
    if a.score and uuids:
        import phone_tiers
        phone_tiers.tier_records(api, uuids, commit=True)
    elif uuids:
        print('Next: python src/phone_tiers.py --list "%s" --commit   (Trestle tiers for the %d records that got '
              'numbers; only numbers without a tier are scored)' % (a.list_name, len(uuids)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
