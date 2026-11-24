"""Enformion (Endato) Person Search re-skip for the records on a DataSift list, then the numbers merged onto those
records and, if asked, scored and tiered. A third phone source on top of DataSift's own trace and Tracerfy, on the
account's own Open API key with your own Enformion keys (ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD in .env).

For each owner: Person Search by NAME + PROPERTY ADDRESS (Enformion refuses a name alone), then that person's
phone numbers. By default the records come from the account itself: every record on the list (Foreclosure unless
--list says otherwise) whose owner has no phone yet; --all takes every record; --csv traces a file's rows instead.
A company, trust or estate owner is left out (a person search cannot find a company).

  python src/enformion_ftm.py                       # dry: who would be searched and the most it can cost
  python src/enformion_ftm.py --limit 2 --commit    # a cheap live test on two records
  python src/enformion_ftm.py --commit --score      # search, merge the numbers, Trestle-score them into tiers
  python src/enformion_ftm.py --addr "100 Example" --all --commit

Billed per match (a miss is free): about $0.10 at the DataSift affiliate rate, about $0.35 at rack rate; the dry
run prices every search at the rack rate. THE MERGE GOES OVER THE API (build plan 5.5): api_intake.merge_phones
finds each record by address, adds only the numbers its owner lacks, counts the trace on the owner, and reads the
first record back before the rest. It never creates a record. The browser wizard is behind --browser (or
FTM_UPLOAD_BROWSER=1), for the team's own workstation only. --finish is the old name for --commit.
"""
import argparse
import asyncio
import csv
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
from enformion_heir import person_search, first_match  # noqa: E402  (canonical library client)

OUTDIR = SRC.parent / "output"
RACK_PER_MATCH = 0.35
ENTITY_MARKERS = ("llc", "inc", "trust", "corp", "company", " co ", "estate of",
                  "properties", "holdings", "ltd", " lp", "bank", "association")


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


def merge(merge_csv, tags: list, *, browser: bool = False, headed: bool = False, merger=None,
          list_name: str = "Foreclosure") -> dict:
    """Merge the found phones into the existing records (build plan 5.5).

    Over the API by default: each record is found by address, only numbers the owner lacks are
    added, the trace is counted on the owner, and the first record is read back before the rest.
    The browser wizard is the fallback (--browser or FTM_UPLOAD_BROWSER=1)."""
    import api_intake

    if browser or api_intake.browser_wanted():
        from sift_upload_wizard import run_upload   # staff-only: the old Playwright wizard, imported only when used
        (OUTDIR / "_enformion_merge").mkdir(parents=True, exist_ok=True)
        return {"route": "browser", "result": asyncio.run(run_upload(
            merge_csv, list_name, tags, existing_list=True, do_finish=True, headless=not headed,
            shot_base=str(OUTDIR / "_enformion_merge" / "run")))}
    with open(merge_csv, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    res = (merger or api_intake.merge_phones)(rows, commit=True, source_tag="Enformion",
                                             list_name=list_name, tags=tags)
    return {"route": "api", "result": res}


def pick(rows: list[dict], addr_filters: list[str]) -> tuple[list, list]:
    """(row, first, last, address) for each person-owned row; the rest named with why they were left out."""
    picked, skipped = [], []
    for r in rows:
        addr = (r.get("address") or "").strip()
        if not addr or (addr_filters and not any(f in addr.lower() for f in addr_filters)):
            continue
        first, last = owner_first_last(r)
        company = (r.get("company") or "").strip()
        if not first or not last:
            skipped.append((addr, company or "no person's name"))
        elif any(m in f" {first} {last} ".lower() for m in ENTITY_MARKERS):
            skipped.append((addr, "a company or trust: %s %s" % (first, last)))
        else:
            picked.append((r, first, last, addr))
    return picked, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", dest="list_name", default="Foreclosure", help="the DataSift list whose records are searched")
    ap.add_argument("--csv", default="", help="search the rows of this file instead of the account's list")
    ap.add_argument("--all", action="store_true", help="every record on the list (default: only those with no phone)")
    ap.add_argument("--no-phone-only", action="store_true", help="the default; kept so older commands still run")
    ap.add_argument("--addr", default="", help="comma list of address pieces to limit to (a re-run on named records)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--commit", "--finish", dest="commit", action="store_true",
                    help="run the searches (billed per match) and merge the numbers onto the records")
    ap.add_argument("--score", action="store_true", help="with --commit: Trestle-score the new numbers into dial tiers")
    ap.add_argument("--max-usd", type=float, default=25.0, help="refuse a run that could cost more than this at rack rate")
    ap.add_argument("--browser", action="store_true",
                    help="staff only: merge through the browser upload wizard instead of the API")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args(argv)

    import datasift_api_upload as up
    api = None if a.csv and not a.commit else up.Api()
    if a.csv:
        with open(a.csv, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    else:
        import api_intake
        rows = api_intake.account_targets(api, a.list_name, no_phone_only=not a.all)
    picked, skipped = pick(rows, [s.strip().lower() for s in a.addr.split(",") if s.strip()])
    if a.limit:
        picked = picked[: a.limit]
    source = a.csv or "list %r (%s)" % (a.list_name, "every record" if a.all else "records with no phone")
    print("Enformion: %d people to search from %s, %d left out; at most $%.2f at rack rate" % (
        len(picked), source, len(skipped), len(picked) * RACK_PER_MATCH))
    for addr, why in skipped[:10]:
        print("  left out: %s (%s)" % (addr, why))
    if not picked:
        print("Nothing to search.")
        return 0
    if len(picked) * RACK_PER_MATCH > a.max_usd:
        raise SystemExit("REFUSING: %d searches could cost $%.2f, over --max-usd %.2f (use --limit)" % (
            len(picked), len(picked) * RACK_PER_MATCH, a.max_usd))
    if not a.commit:
        print("DRY RUN. Re-run with --commit to search and merge%s." % (" and score" if a.score else ""))
        return 0

    found = []
    for r, first, last, addr in picked:
        data = person_search(first, last, city=(r.get("city") or "").strip(),
                             state=(r.get("state") or "TN").strip(), zip_code=(r.get("zip") or "").strip()[:5])
        phones = enf_phones(first_match(data))
        print(f"  {addr[:30]:30} {first} {last:14} -> {len(phones)} numbers")
        if phones:
            n = NoticeData(owner_name=f"{first} {last}", address=addr, city=(r.get("city") or "").strip(),
                           state=(r.get("state") or "TN").strip(), zip=(r.get("zip") or "").strip()[:5],
                           notice_type="foreclosure", county=(r.get("county") or "").strip())
            for i, fld in enumerate(PHONE_FIELDS):
                if i < len(phones):
                    setattr(n, fld, phones[i])
            found.append(n)
    total_ph = sum(len([1 for f in PHONE_FIELDS if getattr(n, f, "")]) for n in found)
    print(f"\n{len(found)}/{len(picked)} people matched, {total_ph} numbers found.")
    if not found:
        return 0
    merge_csv = write_datasift_split_csvs(found)[0]["path"]
    print("merge file:", merge_csv)
    iy, iw, _ = date.today().isocalendar()
    tags = ["FTM", "foreclosure", "has_auction", "Courthouse Data", f"{iy}-W{iw:02d}"] \
        if a.list_name.lower() == "foreclosure" else [f"{iy}-W{iw:02d}"]
    out = merge(merge_csv, tags, browser=a.browser, headed=a.headed, list_name=a.list_name)
    print("merge (%s):" % out["route"], {k: v for k, v in out["result"].items() if k != "records"})
    if out["route"] == "api" and out["result"].get("stopped"):
        print("!! " + out["result"]["stopped"])
        return 1
    uuids = (out["result"].get("records") or []) if out["route"] == "api" else []
    if a.score and uuids:
        import phone_tiers
        phone_tiers.tier_records(api, uuids, commit=True)
    elif uuids:
        print('Next: python src/phone_tiers.py --list "%s" --commit   (Trestle tiers for the %d records that got '
              'numbers; only numbers without a tier are scored)' % (a.list_name, len(uuids)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
