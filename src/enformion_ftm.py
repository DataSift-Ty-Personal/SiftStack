"""Layer 3b - Enformion (Endato) Person Search skip-trace for FTM foreclosure records,
then merge the found numbers back into reisift. A THIRD phone source stacked on top of
DataSift (free, at upload) + Tracerfy ($0.02/rec), to maximize reachable numbers.

For each FTM owner: Person Search by NAME + PROPERTY ADDRESS (the address anchor; name
alone is rejected by Enformion) -> pull their phone numbers -> write a DataSift merge CSV
(address + Phone 1-9) -> Add-Data upsert by ADDRESS into the existing 'Foreclosure' list.
reisift MERGES phones, so this accumulates on top of whatever DataSift/Tracerfy already
found. After merging, re-run score_ftm_phones + run_phone_tag_upload to tier the new
numbers (Trestle decides mobile/landline + dial tier, so the MMS mobile-only filter works).

Billed per match (misses free, ~$0.25-0.35/match). DRY by default.

  python src/enformion_ftm.py                  # trace ALL FTM (single-family) records, write merge CSV (no upload)
  python src/enformion_ftm.py --limit 2        # cheap smoke test (2 records)
  python src/enformion_ftm.py --finish         # also merge found phones into reisift
  python src/enformion_ftm.py --no-phone-only  # only records that still have no phones
"""
import argparse
import asyncio
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
import config  # noqa: E402,F401  (loads .env -> Enformion creds for the library client)
from notice_parser import NoticeData  # noqa: E402
from tracerfy_skip_tracer import PHONE_FIELDS  # noqa: E402
from datasift_formatter import write_datasift_split_csvs  # noqa: E402
from sift_upload_wizard import run_upload  # noqa: E402
from enformion_heir import person_search, first_match  # noqa: E402  (canonical library client)

OUTDIR = SRC.parent / "output"
ROUTING = OUTDIR / "ftm_phone_routing.json"
ENTITY_MARKERS = ("llc", "inc", "trust", "corp", "company", " co ", "estate of",
                  "properties", "holdings", "ltd", " lp", "bank", "association")


def latest_master() -> str:
    cands = sorted(OUTDIR.glob("foreclosure_master_active_*_sfr.csv"))
    return str(cands[-1]) if cands else str(OUTDIR / "foreclosure_master_active.csv")


def enf_phones(person: dict) -> list[str]:
    """Unique 10-digit phones from a matched person."""
    out = []
    for p in (person or {}).get("phoneNumbers") or []:
        if isinstance(p, dict):
            num = re.sub(r"\D", "", str(p.get("phoneNumber") or p.get("number") or ""))
            if len(num) == 11 and num.startswith("1"):
                num = num[1:]
            if len(num) == 10 and num not in out:
                out.append(num)
    return out


SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
# cut the owner string at the first co-owner / alias / care-of marker -> primary owner only
SPLIT_MARKERS = (" and ", " & ", " aka ", " a/k/a ", " c/o ", " et al", " et ux", " or ")


def clean_owner_name(raw: str) -> tuple[str, str]:
    """Parse a possibly-messy foreclosure-notice owner string into ONE clean
    (First, Last). Enformion 400s on commas / AND / AKA / suffixes, so:
    keep only the primary owner, drop suffixes + middle initials + punctuation."""
    s = " " + (raw or "").strip().lower() + " "
    cut = len(s)
    for m in SPLIT_MARKERS:
        i = s.find(m)
        if i != -1:
            cut = min(cut, i)
    s = re.sub(r"[.,`()/]", " ", s[:cut])  # keep intra-name hyphen/apostrophe
    toks = [t for t in s.split() if t and t not in SUFFIXES and len(t) > 1]
    if len(toks) < 2:  # don't over-drop a legitimately short name
        toks = [t for t in s.split() if t and t not in SUFFIXES]
    if len(toks) < 2:
        return "", ""
    return toks[0].title(), toks[-1].title()


def owner_first_last(r: dict) -> tuple[str, str]:
    raw = (r.get("full_name") or r.get("owner_name") or "").strip()
    if not raw:
        raw = f"{(r.get('first_name') or '').strip()} {(r.get('last_name') or '').strip()}".strip()
    return clean_owner_name(raw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="", help="FTM master CSV (default: latest *_sfr.csv)")
    ap.add_argument("--addr", default="", help="comma list of address substrings to limit to (e.g. a re-run on specific records)")
    ap.add_argument("--no-phone-only", action="store_true", help="only records still with no phones")
    ap.add_argument("--finish", action="store_true", help="merge found phones into reisift")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()

    csv_path_in = a.csv or latest_master()
    rows = list(csv.DictReader(open(csv_path_in, encoding="utf-8")))
    print(f"master: {csv_path_in}  ({len(rows)} rows)")

    empty = set()
    if ROUTING.exists():
        for v in json.loads(ROUTING.read_text(encoding="utf-8")).values():
            if not v.get("phones"):
                empty.add((v.get("addr") or "").strip().lower())

    addr_filters = [s.strip().lower() for s in a.addr.split(",") if s.strip()]
    picked = []
    for r in rows:
        addr = (r.get("address") or "").strip()
        if not addr:
            continue
        if addr_filters and not any(f in addr.lower() for f in addr_filters):
            continue
        if a.no_phone_only and addr.lower() not in empty:
            continue
        first, last = owner_first_last(r)
        if not first or not last:
            print(f"  skip (no person name): {addr!r}")
            continue
        if any(m in f" {first} {last} ".lower() for m in ENTITY_MARKERS):
            print(f"  skip (entity owner): {addr!r} ({first} {last})")
            continue
        picked.append((r, first, last, addr))
    if a.limit:
        picked = picked[: a.limit]

    print(f"\nEnformion Person Search on {len(picked)} FTM records "
          f"(~${len(picked) * 0.35:.2f} max, billed per match):\n")
    found = []
    for r, first, last, addr in picked:
        data = person_search(first, last, city=(r.get("city") or "").strip(),
                             state=(r.get("state") or "TN").strip(),
                             zip_code=(r.get("zip") or "").strip())
        phones = enf_phones(first_match(data))
        print(f"  {addr[:30]:30} {first} {last:14} -> {len(phones)} phones {phones[:6]}")
        if phones:
            n = NoticeData(owner_name=f"{first} {last}", address=addr,
                           city=(r.get("city") or "").strip(),
                           state=(r.get("state") or "TN").strip(),
                           zip=(r.get("zip") or "").strip(),
                           notice_type="foreclosure", county=(r.get("county") or "").strip())
            for i, fld in enumerate(PHONE_FIELDS):
                if i < len(phones):
                    setattr(n, fld, phones[i])
            found.append(n)

    hits = sum(1 for _ in found)
    total_ph = sum(len([1 for f in PHONE_FIELDS if getattr(n, f, "")]) for n in found)
    print(f"\n{hits}/{len(picked)} records matched, {total_ph} phones found.")
    if not found:
        print("No phones found - nothing to merge.")
        return

    csv_infos = write_datasift_split_csvs(found)
    merge_csv = csv_infos[0]["path"]
    print("merge CSV:", merge_csv)

    if a.finish:
        iy, iw, _ = date.today().isocalendar()
        tags = ["FTM", "foreclosure", "has_auction", "Courthouse Data", f"{iy}-W{iw:02d}"]
        (OUTDIR / "_enformion_merge").mkdir(parents=True, exist_ok=True)
        res = asyncio.run(run_upload(merge_csv, "Foreclosure", tags, existing_list=True,
                                     do_finish=True, headless=not a.headed,
                                     shot_base=str(OUTDIR / "_enformion_merge" / "run")))
        print("merge upload:", res)
        print("\nNEXT: re-run score_ftm_phones.py --commit + run_phone_tag_upload.py --finish "
              "to score/tier the new numbers (then the MMS builder picks up the new mobiles).")
    else:
        print("\nDRY - pass --finish to merge the found phones into reisift.")


if __name__ == "__main__":
    main()
