"""Priority-1 tag skip-trace: 3-source OWNER coverage + Trestle scoring.

MANUAL AND BILLED — NEVER WIRE THIS INTO AN AUTOMATED RUN.
Every invocation spends real money (~$0.17/record all-in). It is run by hand,
against a hand-picked export, when the operator has decided the spend is worth
it. Nothing in this repo imports it and nothing should: the daily GitHub Action
runs run_philly_daily.py only. If you are an agent asked to "automate the skip
trace" or "add this to the pipeline", stop and confirm with the operator first.


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
import re
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
            n = NoticeData(
                owner_name=owner,
                address=(r.get("Property address") or "").strip(),
                city=(r.get("Property city") or "").strip(),
                state=(r.get("Property state") or "").strip(),
                zip=(r.get("Property zip5") or r.get("Property zip") or "").strip(),
                county=(r.get("Property county") or "").strip(),
            )
            n._export_tags = tags  # lowercased Tags cell, for the already-done guard
            out.append(n)
    return out


def _phone_count(n: NoticeData) -> int:
    return sum(1 for f in PHONE_FIELDS if getattr(n, f, ""))


def _is_entity(name: str) -> bool:
    # Word-boundary matching: bare substring checks flagged person surnames
    # containing a marker ("Aiftinca" -> "inc", missed on the 2026-07-24 run).
    # "redevelopmen" keeps an open right edge to still catch "redevelopment".
    low = name.lower()
    for m in (*ENTITY_MARKERS, *_ENTITY_EXTRA):
        m = m.strip()
        tail = r"" if m == "redevelopmen" else r"\b"
        if re.search(rf"\b{re.escape(m)}{tail}", low):
            return True
    return False


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
    ap.add_argument("--list", default="Priority Skip", dest="list_name",
                    help="Universal DataSift list all priority tiers funnel into (merge + "
                         "score target). Default: 'Priority Skip'")
    ap.add_argument("--run", action="store_true",
                    help="Execute the billed chain (Tracerfy + Enformion merge + Trestle). "
                         "Default is a dry preview with no API calls and no CRM change.")
    ap.add_argument("--no-trestle", action="store_true", help="Skip the Trestle scoring step")
    ap.add_argument("--no-tracerfy", action="store_true", help="Skip the Tracerfy source (Enformion only)")
    ap.add_argument("--no-enformion", action="store_true", help="Skip the Enformion source (Tracerfy only)")
    ap.add_argument("--limit", type=int, default=0, help="Process only the first N records (testing)")
    ap.add_argument("--headed", action="store_true", help="Show the browser during merges")
    a = ap.parse_args()

    notices = _read_reisift_export(a.csv)

    # Owner-only: drop entity/LLC owners (no person), blank names, and records this
    # tool has ALREADY 3-source traced (global guard tag) — so any priority tier can
    # feed the same queue and each property is Tracerfy+Enformion traced only once ever.
    GUARD_TAG = "3source_skiptraced"
    owners: list[NoticeData] = []
    skipped_entity = skipped_done = 0
    for n in notices:
        name = (n.owner_name or "").strip()
        if len(name.split()) < 2:
            continue
        if _is_entity(name):
            skipped_entity += 1
            continue
        if GUARD_TAG in getattr(n, "_export_tags", ""):
            skipped_done += 1
            continue
        owners.append(n)
    if a.limit:
        owners = owners[: a.limit]

    print(f"Priority skip trace  |  CSV: {a.csv}  |  list: {a.list_name}")
    print(f"  records in export      : {len(notices)}")
    print(f"  entity owners skipped  : {skipped_entity}")
    print(f"  already 3-source traced: {skipped_done} (guard tag '{GUARD_TAG}')")
    print(f"  new owners to trace    : {len(owners)}")
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
    # Stamp the global guard tag so this property is skipped on every future run,
    # in any priority tier. Plus a dated tag for reporting.
    #
    # "Courthouse Data" is deliberately NOT stamped here. It marks first-to-market
    # county-sourced data and is what the niche-sequential presets use to rank FTM
    # above bulk -- it is a provenance fact set at INGEST, and skip tracing a record
    # does not change where the record came from. This tool was written for the
    # courthouse/FTM pipeline (where every record genuinely was courthouse data) and
    # the tag rode along; once queue-prep started running it over purchased bulk
    # lists it was falsely promoting bulk records into the FTM bucket.
    merge_tags = [GUARD_TAG, f"3source_skiptrace_{date.today():%Y-%m}", week_tag]

    merge_csvs: list[str] = []  # phones from these get re-delivered below

    # ── Source 2: Tracerfy (mutates owner notices in place) ────────────────────
    if a.no_tracerfy:
        print("\n── Tracerfy skip trace (owner) ── SKIPPED (--no-tracerfy)")
    else:
        print("\n── Tracerfy skip trace (owner) ──")
        tr_stats = batch_skip_trace(owners, lookup_heir_addresses=False)
        tr_found = [n for n in owners if _phone_count(n) > 0]
        print(f"Tracerfy: {len(tr_found)}/{len(owners)} owners with phones, ${tr_stats.get('cost', 0):.2f}")
        if tr_found:
            tr_csv = write_datasift_split_csvs(tr_found)[0]["path"]
            print("  merge CSV:", tr_csv)
            res = asyncio.run(_merge(tr_csv, a.list_name, merge_tags, a.headed, "tracerfy"))
            print("  Tracerfy merge:", "OK" if res.get("success") else f"FAILED: {res.get('message')}")
            merge_csvs.append(tr_csv)

    # ── Source 3: Enformion / Endato (owner) — separate merge so it ACCUMULATES ─
    if a.no_enformion:
        print("\n── Enformion skip trace (owner) ── SKIPPED (--no-enformion)")
    elif not enf_configured():
        print("\n── Enformion skip trace (owner) ──")
        print("  ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD not set — skipping Enformion source.")
    else:
        print("\n── Enformion skip trace (owner) ──")
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
            merge_csvs.append(enf_csv)

    # ── Phone delivery: 'Upload phone numbers by property address' ──────────────
    # Guaranteed phone delivery. The Add-Data merge is supposed to carry its
    # Phone columns, but its phone-question dropdown is click-flaky (overlay
    # intercepts, 2026-08-05) and already-present numbers just dedupe — this
    # purpose-built Update-Data flow writes the found numbers unconditionally.
    delivered_addrs: set | None = None
    if merge_csvs:
        from datetime import datetime as _dt
        from sift_upload_wizard import (build_phones_by_address_csv,
                                        upload_phones_by_address)
        pba_path = OUTDIR / f"phones_by_address_{_dt.now():%Y-%m-%d_%H%M%S}.csv"
        pba, n_ph = build_phones_by_address_csv(merge_csvs, str(pba_path))
        if pba:
            import csv as _csvmod
            with open(pba, newline="", encoding="utf-8") as fh:
                delivered_addrs = {(r.get("Property Street Address") or "").strip()
                                   for r in _csvmod.DictReader(fh)}
            delivered_addrs.discard("")
            if not delivered_addrs:  # empty set would silently skip the wait
                print("  WARNING: no addresses parsed from phones-by-address CSV "
                      "-- stale-export wait disabled for this run")
            async def _deliver():
                from datasift_core import create_browser, login
                async with create_browser(headless=not a.headed) as (_b, _c, page):
                    if not await login(page, config.DATASIFT_EMAIL, config.DATASIFT_PASSWORD):
                        return {"success": False, "message": "DataSift login failed"}
                    return await upload_phones_by_address(
                        page, pba,
                        shot_base=str(OUTDIR / "_priority_skiptrace" / "phones_by_addr"))
            print(f"\n── Phone delivery (phones-by-address, {n_ph} phones) ──")
            dres = asyncio.run(_deliver())
            print("  delivery:", "OK" if dres.get("success")
                  else f"FAILED: {dres.get('message')} — phones CSV kept at {pba}")

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
        # Wait only for addresses the merges actually wrote phones to. Owners no
        # source matched can never show phones, and demanding them stalls the
        # whole batch's scoring forever (2026-08-05: 6/88 no-hit owners made
        # _covers() unsatisfiable -> Trestle aborted -> no tiers, gate zeroed).
        expect_addresses=delivered_addrs,
    ))
    print(f"Trestle: {score.get('phones_scored', 0)} phones on list, "
          f"{score.get('phones_new', 0)} newly scored (${score.get('cost', 0):.2f} billable; "
          f"rest reused free from cache), tiers={score.get('tier_counts', {})} "
          f"(upload: {'OK' if score.get('upload_ok') else 'FAIL'})")


if __name__ == "__main__":
    main()
