"""Stage A input builder for the obituary deep-prospecting run.

Takes the ranked obituary opportunity list and emits the SmartSkip bulk-skip CSV
for the top N records, plus a traceability audit. SmartSkip needs a clean
First + Last + mailing address; an LLC or trust cannot be name-traced at all and
has to be routed to Enformion BusinessV2 instead, so those are split out here
rather than being paid for and coming back empty.

Usage:
  python src/obituary_dp_input.py --top 60
  python src/obituary_dp_input.py --top 60 --out output/dp/smartskip_input.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from obituary_opportunity import CACHE, assemble, parse_date  # noqa: E402

SPLIT_MARKERS = (" and ", " & ", " aka ", " a/k/a ", " c/o ", " c o ", " or ",
                 " et al", " etal", " trustee", " ttee")
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "md", "dds", "esq"}
# Matched on whole words only. A substring test flags "Ralph" as an entity
# because "lp" lives inside it, which is how two real people nearly got routed
# to a business-search API that cannot find them.
ENTITY_WORDS = {"llc", "inc", "corp", "corporation", "trust", "tr", "revocable",
                "revo", "properties", "holdings", "company", "co", "partners",
                "lp", "llp", "ltd", "foundation", "church", "bank", "ministries",
                "association", "estate", "trustees", "ttee"}


def clean_name(raw: str) -> tuple[str, str]:
    """A possibly-messy owner string into ONE clean (First, Last).

    Same rules as enformion_ftm.clean_owner_name: keep only the primary owner,
    drop suffixes, middle initials and punctuation, because a co-owner string
    like 'James G Key Jack' traces as nobody.
    """
    s = " " + (raw or "").strip().lower() + " "
    cut = len(s)
    for m in SPLIT_MARKERS:
        i = s.find(m)
        if i != -1:
            cut = min(cut, i)
    s = re.sub(r"[.,`()/]", " ", s[:cut])
    toks = [t for t in s.split() if t and t not in SUFFIXES and len(t) > 1]
    if len(toks) < 2:
        toks = [t for t in s.split() if t and t not in SUFFIXES]
    if len(toks) < 2:
        return "", ""
    return toks[0].title(), toks[-1].title()


def is_entity(r) -> bool:
    """True only for a real business, trust or estate entity.

    owner.type is authoritative when reisift parsed the deed cleanly. When it
    says 'incomplete' the name is an unparsed county blob that usually holds two
    human co-owners, so fall back to a whole-word entity test on the text.
    """
    t = (r.get("owner_type") or "").strip().lower()
    if t in ("trust", "company", "entity", "business", "organization"):
        return True
    if t == "person":
        return False
    name = (r.get("owner_company") or r.get("owner_name") or "").lower()
    words = set(re.findall(r"[a-z]+", name))
    return bool(words & ENTITY_WORDS)


def person_name(raw_first: str, raw_last: str) -> tuple[str, str, str]:
    """(first, last, confidence) from reisift's parsed owner fields.

    reisift sometimes packs 'MIDDLE SURNAME' into first_name and leaves the given
    name in last_name, so first='D Lowe' last='John' is really John D Lowe. Detect
    it by looking at the trailing word of first_name: if it is a real word and is
    NOT a variant of last_name, the fields are reversed. The variant test is what
    keeps first='Mark Jr Helmbol' last='Helmboldt' from being flipped, since the
    trailing fragment there is just a truncated copy of the surname.
    """
    toks = [t for t in re.sub(r"[.,`()/]", " ", (raw_first or "").lower()).split()
            if t and t not in SUFFIXES]
    tail = toks[-1] if len(toks) > 1 and len(toks[-1]) > 1 else ""
    last_l = (raw_last or "").strip().lower()
    if tail and last_l:
        same = tail.startswith(last_l[:5]) or last_l.startswith(tail[:5])
        if not same:
            return raw_last.strip().title(), tail.title(), "check"
    first, last = clean_name(f"{raw_first} {raw_last}")
    return first, last, "high"


def parse_blob(raw: str) -> tuple[str, str, str]:
    """Parse an unparsed county owner blob into (first, last, confidence).

    Knox recorder convention is LAST FIRST MIDDLE, and multiple co-owners are
    concatenated, so take the FIRST owner only: token 0 is the surname and
    token 1 is the given name. 'Rutherford Hillard & Alma G' is Hillard
    Rutherford. Marked low confidence because the convention is not guaranteed
    and a wrong name is worse than no name.
    """
    s = " " + (raw or "").strip().lower() + " "
    cut = len(s)
    for m in SPLIT_MARKERS:
        i = s.find(m)
        if i != -1:
            cut = min(cut, i)
    s = re.sub(r"[.,`()/&]", " ", s[:cut])
    toks = [t for t in s.split() if t and t not in SUFFIXES and len(t) > 1]
    if len(toks) < 2:
        return "", "", "none"
    return toks[1].title(), toks[0].title(), "low"


def build(qualified, top_n):
    """Split the top N into SmartSkip-traceable rows and everything that is not."""
    rows, entities, unusable = [], [], []
    for i, r in enumerate(qualified[:top_n], 1):
        if is_entity(r):
            entities.append({"rank": i, **_thin(r)})
            continue

        # A cleanly parsed person is the trustworthy path.
        if (r.get("owner_type") or "").lower() == "person" and r.get("owner_first") \
                and r.get("owner_last"):
            first, last, conf = person_name(r["owner_first"], r["owner_last"])
        else:
            first, last, conf = parse_blob(r.get("owner_company") or r.get("owner_name") or "")
        if not first or not last:
            unusable.append({"rank": i, "reason": "no parseable first and last name",
                             **_thin(r)})
            continue
        # Mailing address is required by the API. Fall back to the property
        # address, which is correct for an owner-occupied decedent home.
        mail_street = r.get("mail_street") or r.get("street")
        if not mail_street:
            unusable.append({"rank": i, "reason": "no mailing or property street", **_thin(r)})
            continue
        rows.append({
            "Rank": i,
            "Name Confidence": conf,
            "Sift UUID": r["uuid"],
            "First Name": first,
            "Last Name": last,
            "Mailing Address": mail_street,
            "Mailing City": r.get("mail_city") or r.get("city") or "",
            "Mailing State": r.get("state") or "TN",
            "Mailing Zip": r.get("mail_zip") or r.get("zip") or "",
            "Property Address": r.get("street") or "",
            "Property City": r.get("city") or "",
            "Property State": r.get("state") or "TN",
            "Property Zip": r.get("zip") or "",
        })
    return rows, entities, unusable


def _thin(r):
    return {"address": r.get("street"), "city": r.get("city"), "zip": r.get("zip"),
            "owner": r.get("owner_name"), "company": r.get("owner_company"),
            "score": r.get("score"), "uuid": r.get("uuid")}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--min-months", type=int, default=3)
    ap.add_argument("--today", default=None)
    ap.add_argument("--outdir", default="output/dp")
    args = ap.parse_args()

    today = parse_date(args.today) or date.today()
    qualified, _, meta = assemble(Path(args.cache), args.min_months, today)
    rows, entities, unusable = build(qualified, args.top)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    csv_path = outdir / "smartskip_input.csv"
    cols = ["Rank", "Name Confidence", "Sift UUID", "First Name", "Last Name", "Mailing Address",
            "Mailing City", "Mailing State", "Mailing Zip",
            "Property Address", "Property City", "Property State", "Property Zip"]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    audit = {"built": today.isoformat(), "top_n": args.top,
             "traceable": len(rows), "entities": entities, "unusable": unusable}
    (outdir / "stage_a_audit.json").write_text(json.dumps(audit, indent=1, default=str),
                                               encoding="utf-8")

    print(f"top {args.top} of {meta['qualified']} qualified")
    print(f"  SmartSkip traceable : {len(rows)}  -> {csv_path}")
    print(f"  entity owners       : {len(entities)}  (route to Enformion BusinessV2)")
    print(f"  unusable names      : {len(unusable)}")
    for e in entities:
        print(f"     entity  #{e['rank']:2} {str(e['address'])[:28]:28} {e['owner'] or e['company']}")
    for u in unusable:
        print(f"     unusable #{u['rank']:2} {str(u['address'])[:28]:28} "
              f"{u['owner']!r}  ({u['reason']})")
    print(f"\n  estimated SmartSkip cost at $0.15/hit: ${0.15*len(rows):.2f} "
          f"(billed only on rows that return data)")


if __name__ == "__main__":
    main()
