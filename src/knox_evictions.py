"""Knox County eviction (detainer) docket -> landlord leads.

WHY THIS RUNS DAILY. The Clerk publishes one PDF per court date at a fixed URL
and keeps only a short, IRREGULAR window. Measured 2026-08-19: today, yesterday
and 8/17 were live, 8/12 and 7/29 were live, and 8/05, 8/13 and 8/14 were
already 404, as was everything older. So some history lingers and most does not.
Anything not captured while it is up is gone: there is no archive to backfill
from, unlike foreclosure and probate.

    python src/knox_evictions.py                  # sweep the last 30 days
    python src/knox_evictions.py --days 60
    python src/knox_evictions.py --no-fetch       # re-parse the archive

DETAINERS ARE NOT LABELLED. The county says detainer cases are heard Tuesdays in
the Fifth Sessions Court, and that the detainer docket itself is only posted on
paper at the Clerk's office. What IS online is the civil division's daily
docket, and the detainers sit inside it unmarked. Verified on Tuesday 2026-08-18:
172 cases, of which 62 had plainly landlord plaintiffs (KCDC 17 times, apartment
communities, property managers, rentals). So this classifies by PLAINTIFF rather
than trusting a case-type column that does not exist.

Output `evictions.json` in the shape `knox_ftm_pull.load_landlords` expects. The
landlord name still has to be resolved to parcels, which `knox_lien_resolve`
already does against the open county tax API.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DOCKET_URL = "https://www.knoxcounty.org/civil/pdfs/civil_dockets/DailyDkt%s.pdf"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_CASE = re.compile(r"^(.+?)\s+VS\s+(.+?)$", re.M)
_COURT_DATE = re.compile(r"Court Date:\s*\n\s*(\d{2}/\d{2}/\d{4})")

# Plaintiffs that look like a landlord or property owner.
LANDLORD_HINTS = (
    "APARTMENT", "APARTMENTS", "APTS", "PROPERTIES", "PROPERTY", "REALTY",
    "RENTAL", "RENTALS", "HOMES", "MANAGEMENT", "HOLDINGS", "ESTATES",
    "VILLAGE", "VILLAS", "COMMONS", "RIDGE", "POINTE", "MANOR", "TOWNHOME",
    "TOWNHOMES", "RESIDENTIAL", "HOUSING", "LEASING", "LANDLORD", "FLATS",
    "PLACE", "PARK", "COURT", "TRACE", "CROSSING", "LANDING", "TERRACE",
    "KCDC", "COMMUNITY DEVELOPMENT",
)

# Plaintiffs that match a hint but are definitely not landlord/tenant matters.
# Debt buyers are the big one: they file in volume and their names carry
# "MANAGEMENT" or "PROPERTY". Midland Credit Management alone was 59 of the
# first 293 cases classified, and it is a collector, not a landlord.
NOT_LANDLORD = (
    "DEPT OF LABOR", "DEPARTMENT OF LABOR", "WORKFORCE", "MEDICAL CENTER",
    "HOSPITAL", "FINANCIAL", "FINANCE", "BANK", "CREDIT UNION", "INSURANCE",
    "CAPITAL", "FUNDING", "ACCEPTANCE", "RECOVERY", "COLLECTION", "PORTFOLIO",
    "CATERPILLAR", "VERIZON", "STORAGE", "ROOFING", "CONSTRUCTION",
    "UNIVERSITY OF", "STATE OF TENNESSEE", "CITY OF KNOXVILLE",
    "MIDLAND CREDIT", "MIDLAND MANAGEMENT", "CROWN ASSET", "NCB MANAGEMENT",
    "ACCELERATED INVENTORY", "CASUALTY", "TRAVELERS PROPERTY",
    "COURT REPORTING", "ASSET ACCEPTANCE", "LVNV", "CAVALRY", "RESURGENT",
    "TATTOO", "SUPPLY",
)

# Hints are matched on WORD BOUNDARIES. Substring matching put three people
# named COURTNEY and one named POINTER on the landlord list, because "COURT"
# and "POINTE" appear inside their surnames.
_HINT_RX = None


def _clean(s: str) -> str:
    return " ".join((s or "").split()).strip(" ,")


def looks_like_landlord(plaintiff: str) -> bool:
    """Classify a plaintiff as a landlord for detainer purposes.

    Deliberately a heuristic over a hint list, because the online docket carries
    no case-type column. Precision matters more than recall: a false positive
    sends a non-landlord into the buy box, while a miss only costs one lead in a
    docket that repeats weekly.
    """
    global _HINT_RX
    p = (plaintiff or "").upper()
    if not p or len(p) < 4:
        return False
    if any(bad in p for bad in NOT_LANDLORD):
        return False
    if _HINT_RX is None:
        _HINT_RX = re.compile(
            r"\b(?:" + "|".join(re.escape(h) for h in LANDLORD_HINTS) + r")\b")
    return bool(_HINT_RX.search(p))


def fetch_docket(day: date, scratch: str, timeout: int = 40) -> str | None:
    """Download one court date's docket. Returns the archived path or None."""
    import requests

    arc = os.path.join(scratch, "eviction_archive")
    os.makedirs(arc, exist_ok=True)
    stamp = day.strftime("%Y%m%d")
    path = os.path.join(arc, f"DailyDkt{stamp}.pdf")
    if os.path.exists(path) and os.path.getsize(path) > 2000:
        return path
    try:
        r = requests.get(DOCKET_URL % stamp, headers={"User-Agent": UA}, timeout=timeout)
    except Exception:
        return None
    # A missing date returns the site's 404 page with a 404 status, but check the
    # magic bytes too: a courtesy page served as 200 would otherwise be archived
    # as if it were a docket.
    if r.status_code != 200 or not r.content.startswith(b"%PDF-"):
        return None
    with open(path, "wb") as fh:
        fh.write(r.content)
    return path


def parse_docket(path: str) -> list[dict]:
    """Pull landlord/tenant cases out of one daily docket."""
    from pdfminer.high_level import extract_text

    txt = extract_text(path)
    m = _COURT_DATE.search(txt)
    court_date = ""
    if m:
        try:
            court_date = datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            court_date = m.group(1)

    out = []
    for pm in _CASE.finditer(txt):
        plaintiff = _clean(pm.group(1))
        defendant = _clean(pm.group(2))
        # The attorney column bleeds into the plaintiff when a firm is listed;
        # keep the tail, which is the party itself.
        if not plaintiff or not defendant:
            continue
        if not looks_like_landlord(plaintiff):
            continue
        out.append({
            "source": "eviction",
            "landlord": plaintiff,
            "tenant": defendant,
            "court_date": court_date,
            "filed_date": court_date,
            "docket_pdf": os.path.basename(path),
        })
    return out


def sweep(scratch: str, days: int, end: date | None = None,
          no_fetch: bool = False, sleep: float = 0.4) -> list[dict]:
    end = end or date.today()
    found, got_days, missing = [], 0, 0
    for back in range(days + 1):
        d = end - timedelta(days=back)
        if d.weekday() >= 5:      # court does not sit at the weekend
            continue
        if no_fetch:
            p = os.path.join(scratch, "eviction_archive", f"DailyDkt{d:%Y%m%d}.pdf")
            p = p if os.path.exists(p) else None
        else:
            p = fetch_docket(d, scratch)
            time.sleep(sleep)
        if not p:
            missing += 1
            continue
        got_days += 1
        try:
            cases = parse_docket(p)
        except Exception as exc:
            print(f"  parse failed {os.path.basename(p)}: {type(exc).__name__}: {exc}")
            continue
        found.extend(cases)
        print(f"  {d}  {len(cases):>3} landlord case(s)")
    print(f"\ncourt dates retrieved: {got_days}   unavailable: {missing}")
    return found


def merge(scratch: str, fresh: list[dict]) -> list[dict]:
    """Accumulate forward. There is no archive to re-pull, so never drop."""
    path = os.path.join(scratch, "evictions.json")
    prior = []
    if os.path.exists(path):
        try:
            prior = json.load(open(path, encoding="utf-8"))
        except Exception:
            prior = []
    seen = {}
    for r in prior + fresh:
        k = (r.get("landlord", "").upper(), r.get("tenant", "").upper(),
             r.get("court_date", ""))
        seen[k] = r
    return list(seen.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default=os.environ.get("FTM_SCRATCH", "output/knox"))
    ap.add_argument("--days", type=int, default=30,
                    help="how far back to sweep; retention is irregular so a "
                         "wider sweep occasionally recovers an older date")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    os.makedirs(a.scratch, exist_ok=True)
    print(f"Knox detainer sweep, last {a.days} days -> {a.scratch}")
    fresh = sweep(a.scratch, a.days, no_fetch=a.no_fetch)

    if not fresh:
        print("NO LANDLORD CASES FOUND. Either every docket 404'd or the layout "
              "changed. The PDFs are archived, but this is a lost week if it is "
              "the layout.")
        return 1

    allrec = merge(a.scratch, fresh)
    out = a.out or os.path.join(a.scratch, "evictions.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(allrec, fh, indent=1)

    landlords = {}
    for r in allrec:
        landlords.setdefault(r["landlord"], 0)
        landlords[r["landlord"]] += 1
    print(f"this sweep: {len(fresh)} case(s)   cumulative: {len(allrec)}")
    print(f"distinct landlords: {len(landlords)}")
    for name, n in sorted(landlords.items(), key=lambda x: -x[1])[:8]:
        print(f"   {n:>3}  {name[:56]}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
