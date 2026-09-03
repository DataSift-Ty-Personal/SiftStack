"""Clean SmartSkip Campaign-format mail file for the obituary heirs.

Mirrors the SmartSkip campaign export, trimmed to what actually carries data on
this run: 5 phone slots and 5 email slots are used, Middle Name and Deceased are
empty on every row, so the empty columns are dropped rather than shipped blank.

Two deliberate departures from the raw export, both because this file gets
mailed rather than researched:

  1. The Mailing Address columns hold the USPS CASS-standardized address, not
     the aggregator's version. Every row was validated, so "124 W Hendron
     Chapel Rd" ships as "124 Hendron Chapel Rd, Knoxville TN 37920-9456" with
     the ZIP+4 the post office actually wants.
  2. Owner names are cleaned. reisift stores some as "Mark Jr Helmbol
     Helmboldt"; that is fine in a database and wrong on an envelope.

Associates, neighbours, non-heir relatives, unresolved houses and trust-owned
houses are all absent. What remains is the people to mail.

Usage:
  python src/obituary_mail_clean.py
  python src/obituary_mail_clean.py --heirs-only   # drop the living-owner rows
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

CAMPAIGN_COLS = [
    "Input Name", "First Name", "Last Name",
    "Mailing Address Full", "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
    "Property Address Full", "Property Address", "Property City", "Property State", "Property Zip",
    "Age", "Relationship", "Possible Type",
] + [f"Phone {i} {k}" for i in range(1, 6) for k in ("number", "type", "connected")]   + [f"Email {i}" for i in range(1, 6)]

TAIL_COLS = [
    "Role", "Relation To Owner", "Letter Type",
    "People On This Record", "Person N Of",
    "Opportunity Score", "Property Value", "Vacant", "Months Since Death",
]

RELATION_PLAIN = {
    "Child": "son or daughter", "Spouse": "spouse", "Parent": "parent",
    "Sibling": "brother or sister", "In-law": "in-law",
    "Other Relative": "relative, unlabelled", "Unknown": "relative, unlabelled",
    "": "owner of record",
}


def clean_person_name(first, last):
    """Trim a database owner string down to something you can put on an envelope.

    reisift packs middle names, suffixes and duplicated surname fragments into
    the name fields, so "Mark Jr Helmbol" / "Helmboldt" has to collapse to
    "Mark Helmboldt" before it goes out.
    """
    SUFFIX = {"jr", "sr", "ii", "iii", "iv", "md", "dds"}
    ftoks = [t for t in re.split(r"[ .,]+", (first or "").strip()) if t]
    ltoks = [t for t in re.split(r"[ .,]+", (last or "").strip()) if t]
    lastname = ltoks[-1] if ltoks else ""
    keep = []
    for t in ftoks:
        if t.lower() in SUFFIX:
            continue
        # drop a truncated echo of the surname, e.g. "Helmbol" before "Helmboldt"
        if lastname and len(t) > 3 and (lastname.lower().startswith(t.lower()[:5])
                                        or t.lower().startswith(lastname.lower()[:5])):
            continue
        keep.append(t)
    firstname = keep[0] if keep else (ftoks[0] if ftoks else "")
    return firstname.title(), (lastname or "").title()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default="output/Obituary_Mail_Campaign.csv")
    ap.add_argument("--out", default="output/Obituary_Heirs_Mail_List.csv")
    ap.add_argument("--heirs-only", action="store_true",
                    help="drop the living-owner rows, mail only relatives")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.src, encoding="utf-8")))
    out_rows = []
    for r in rows:
        if args.heirs_only and r["Role"] == "owner":
            continue
        first, last = clean_person_name(r["First Name"], r["Last Name"])

        # The USPS address is what gets mailed. Split it back into the campaign
        # columns so the file stays a drop-in for the same merge.
        usps = (r.get("USPS Address") or "").strip()
        street, city, state, zipc = r["Mailing Address"], r["Mailing City"], r["Mailing State"], r["Mailing Zip"]
        if usps and "," in usps:
            street, lastline = usps.split(",", 1)
            street = street.strip()
            m = re.match(r"\s*(.+?)\s+([A-Z]{2})\s+([\d-]+)\s*$", lastline.strip())
            if m:
                city, state, zipc = m.group(1), m.group(2), m.group(3)
        full = ", ".join(x for x in [street, city, state, zipc] if x)

        o = {c: (r.get(c) or "") for c in CAMPAIGN_COLS}
        o.update({
            "First Name": first, "Last Name": last,
            "Mailing Address Full": full, "Mailing Address": street,
            "Mailing City": city, "Mailing State": state, "Mailing Zip": zipc,
            "Relationship": "Owner" if r["Role"] == "owner" else "Relative",
            "Role": r["Role"],
            "Relation To Owner": RELATION_PLAIN.get(r["Possible Type"], r["Possible Type"] or "owner of record"),
            "Letter Type": r["Letter Type"],
            "People On This Record": r["People On This Record"],
            "Person N Of": r["Person N Of"],
            "Opportunity Score": r["Opportunity Score"],
            "Property Value": r["Property Value"],
            "Vacant": r["Vacant"], "Months Since Death": r["Months Since Death"],
        })
        out_rows.append(o)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CAMPAIGN_COLS + TAIL_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)

    houses = len({r["Property Address"] for r in out_rows})
    print(f"wrote {out}")
    print(f"  {len(out_rows)} people to mail across {houses} houses")
    print(f"  {len(CAMPAIGN_COLS)} campaign columns + {len(TAIL_COLS)} context = "
          f"{len(CAMPAIGN_COLS)+len(TAIL_COLS)}")
    print(f"  roles: {dict(Counter(r['Role'] for r in out_rows))}")
    print(f"  relation mix: {dict(Counter(r['Relation To Owner'] for r in out_rows))}")


if __name__ == "__main__":
    main()
