"""Priority-1 tag skip-trace: 3-source OWNER coverage + Trestle scoring.

For every record carrying the "Priority 1" tag (exported from DataSift as a CSV),
skip-trace the SUBJECT PROPERTY OWNER in two extra sources and merge the phones
back into reisift, then Trestle-score the accumulated numbers and tag dial tiers:

  1. DataSift built-in skip trace  (already run inside DataSift)
  2. Tracerfy                      (this script, ~$0.02/record)
  3. Enformion / Endato            (this script, ~$0.10-0.35/match)

reisift MERGES phones by address, so the three sources ACCUMULATE on the same
record. Trestle then scores every unique number and tags the dial tier
(81-100 Dial First ... <=20 Drop).

This is OWNER-ONLY — no heir resolution. Deep-prospecting's Enformion heir path
(enformion_heir.resolve_heirs_enformion) is separate and intentionally untouched.

Prep: in DataSift, Records -> filter by the "Priority 1" tag -> Manage -> Export,
and pass the downloaded CSV with --csv.

  # DRY preview — no API calls, no spend, no CRM change:
  python src/priority_skiptrace.py --csv output/priority1.csv

  # Full chain — Tracerfy + Enformion merge, then Trestle score + tag:
  python src/priority_skiptrace.py --csv output/priority1.csv --run

  # Watch the browser merge steps:
  python src/priority_skiptrace.py --csv output/priority1.csv --run --headed
"""
import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402  (loads .env -> Enformion / Tracerfy / Trestle creds)
from notice_parser import NoticeData  # noqa: E402
from tracerfy_skip_tracer import PHONE_FIELDS, batch_skip_trace  # noqa: E402
from enformion_heir import person_search, first_match, is_configured as enf_configured  # noqa: E402
from enformion_ftm import enf_phones, clean_owner_name, ENTITY_MARKERS  # noqa: E402
from datasift_formatter import write_datasift_split_csvs  # noqa: E402
from sift_upload_wizard import run_upload  # noqa: E402
from phone_scorer import score_and_tag  # noqa: E402

OUTDIR = Path(__file__).resolve().parent.parent / "output"

# Municipal/agency owners beyond enformion_ftm's ENTITY_MARKERS.
_ENTITY_EXTRA = ("city of", "redevelopmen", "authority", "housing", "estate of", "borough")


def _read_reisift_export(path: str) -> list[NoticeData]:
    """Parse a REISift 'Export' CSV into owner-level NoticeData.

    Columns used: First Name, Last Name, Business Name, Property address/city/state/
    zip(5), Property county, Tags. Rows tagged Do Not Market / Do Not Call are
    dropped. The SUBJECT PROPERTY address is the merge key back into reisift.
    """
    import csv as _csv
    out: list[NoticeData] = []
    with open(path, encoding="utf-8-sig") as fh:
        for r in _csv.DictReader(fh):
            tags = (r.get("Tags") or "").lower()
            if "do not market" in tags or "do not call" in tags:
                continue
            first = (r.get("First Name") or "").strip()
            last = (r.get("Last Name") or "").strip()
            owner = f"{first} {last}".strip() or (r.get("Business Name") or "").strip()
            out.append(NoticeData(
                owner_name=owner,
                address=(r.get("Property address") or "").strip(),
                city=(r.get("Property city") or "").strip(),
                state=(r.get("Property state") or "").strip(),
                zip=(r.get("Property zip5") or r.get("Property zip") or "").strip(),
                county=(r.get("Property county") or "").strip(),
            ))
    return out


def _phone_count(n: NoticeData) -> int:
    return sum(1 for f in PHONE_FIELDS if getattr(n, f, ""))


def _is_entity(name: str) -> bool:
    low = name.lower()
    return any(m in f" {low} " for m in ENTITY_MARKERS) or any(m in low for m in _ENTITY_EXTRA)


async def _merge(csv_path: str, list_name: str, tags: list[str], headed: bool, label: str) -> dict:
    """Add-Data upsert the merge CSV into `list_name` by address (phones accumulate)."""
    shot_dir = OUTDIR / "_priority_skiptrace"
    shot_dir.mkdir(parents=True, exist_ok=True)
    return await run_upload(
        csv_path, list_name, tags, existing_list=True, do_finish=True,
        headless=not headed, shot_base=str(shot_dir / label),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Priority-1 tag: 3-source owner skip trace + Trestle")
    ap.add_argument("--csv", required=True, help="DataSift export of the Priority-1 tagged segment")
    ap.add_argument("--list", default="Priority 1", dest="list_name",
                    help="DataSift list holding these records (merge target). Default: 'Priority 1'")
    ap.add_argument("--run", action="store_true",
                    help="Execute the billed chain (Tracerfy + Enformion merge + Trestle). "
                         "Default is a dry preview with no API calls and no CRM change.")
    ap.add_argument("--no-trestle", action="store_true", help="Skip the Trestle scoring step")
    ap.add_argument("--limit", type=int, default=0, help="Process only the first N records (testing)")
    ap.add_argument("--headed", action="store_true", help="Show the browser during merges")
    a = ap.parse_args()

    notices = _read_reisift_export(a.csv)

    # Owner-only: drop entity/LLC owners (no person to skip trace) and blank names.
    owners: list[NoticeData] = []
    skipped_entity = 0
    for n in notices:
        name = (n.owner_name or "").strip()
        if len(name.split()) < 2:
            continue
        if _is_entity(name):
            skipped_entity += 1
            continue
        owners.append(n)
    if a.limit:
        owners = owners[: a.limit]

    print(f"Priority-1 skip trace  |  CSV: {a.csv}")
    print(f"  records in export : {len(notices)}")
    print(f"  entity owners skip: {skipped_entity}")
    print(f"  owners to trace   : {len(owners)}")
    est_tracerfy = len(owners) * 0.02
    est_enformion = len(owners) * 0.35  # max rack; DataSift affiliate rate is ~$0.10, misses are free
    print(f"  est. cost         : Tracerfy ~${est_tracerfy:.2f}  +  Enformion <=${est_enformion:.2f}")

    if not a.run:
        print("\nDRY preview — no API calls, no merges, no Trestle. Re-run with --run to execute.")
        return
    if not owners:
        print("\nNothing to trace.")
        return

    iy, iw, _ = date.today().isocalendar()
    week_tag = f"{iy}-W{iw:02d}"
    merge_tags = ["Priority 1", "Courthouse Data", f"priority1_skiptrace_{date.today():%Y-%m}", week_tag]

    # ── Source 2: Tracerfy (mutates owner notices in place) ────────────────────
    print("\n── Tracerfy skip trace (owner) ──")
    tr_stats = batch_skip_trace(owners, lookup_heir_addresses=False)
    tr_found = [n for n in owners if _phone_count(n) > 0]
    print(f"Tracerfy: {len(tr_found)}/{len(owners)} owners with phones, ${tr_stats.get('cost', 0):.2f}")
    if tr_found:
        tr_csv = write_datasift_split_csvs(tr_found)[0]["path"]
        print("  merge CSV:", tr_csv)
        res = asyncio.run(_merge(tr_csv, a.list_name, merge_tags, a.headed, "tracerfy"))
        print("  Tracerfy merge:", "OK" if res.get("success") else f"FAILED: {res.get('message')}")

    # ── Source 3: Enformion / Endato (owner) — separate merge so it ACCUMULATES ─
    print("\n── Enformion skip trace (owner) ──")
    if not enf_configured():
        print("  ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD not set — skipping Enformion source.")
    else:
        enf_found: list[NoticeData] = []
        for n in owners:
            first, last = clean_owner_name(n.owner_name)
            if not first or not last:
                continue
            data = person_search(first, last, city=n.city, state=n.state, zip_code=n.zip)
            phones = enf_phones(first_match(data))
            if not phones:
                continue
            e = NoticeData(owner_name=f"{first} {last}", address=n.address,
                           city=n.city, state=n.state, zip=n.zip,
                           notice_type=n.notice_type, county=n.county)
            for i, fld in enumerate(PHONE_FIELDS):
                if i < len(phones):
                    setattr(e, fld, phones[i])
            enf_found.append(e)
        print(f"Enformion: {len(enf_found)}/{len(owners)} owners matched with phones")
        if enf_found:
            enf_csv = write_datasift_split_csvs(enf_found)[0]["path"]
            print("  merge CSV:", enf_csv)
            res = asyncio.run(_merge(enf_csv, a.list_name, merge_tags, a.headed, "enformion"))
            print("  Enformion merge:", "OK" if res.get("success") else f"FAILED: {res.get('message')}")

    # ── Trestle scoring: score every accumulated number, tag dial tiers ─────────
    if a.no_trestle:
        print("\nTrestle scoring skipped (--no-trestle).")
        return
    if not config.TRESTLE_API_KEY:
        print("\nTRESTLE_API_KEY not set — skipping Trestle scoring.")
        return
    print("\n── Trestle scoring (dial tiers) ──")
    score = asyncio.run(score_and_tag(
        list_name=a.list_name,
        email=config.DATASIFT_EMAIL, password=config.DATASIFT_PASSWORD,
        api_key=config.TRESTLE_API_KEY, do_upload=True,
    ))
    print(f"Trestle: {score.get('phones_scored', 0)} phones scored, "
          f"tiers={score.get('tier_counts', {})}, ${score.get('cost', 0):.2f} "
          f"(upload: {'OK' if score.get('upload_ok') else 'FAIL'})")


if __name__ == "__main__":
    main()
