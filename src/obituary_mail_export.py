"""Emit the obituary mail list in SmartSkip Campaign (vertical) format.

The raw SmartSkip campaign export is every relative plus every neighbour: 783
rows for 58 houses. That is the research artifact, not a mail list. This keeps
the exact same 59 columns so it drops into the same mail-merge, but the rows are
only the people we actually decided to mail, and it appends a stats block on the
right so each row carries its own record context.

Who survives into the file, by situation (see obituary_campaign.py):
  estate_*           the heirs, spouse and children, by TN intestacy
  widowed_owner      the Subject row (the living owner) plus the adult children
  owner_parent_died  the Subject row only, nothing passed to anybody
  living_owner       the Subject row plus adult children
  unresolved         nothing
  entity_owner       nothing, a trust cannot be mailed

Associates (neighbours) are never mailed. Research drops, like Betty Burchell's
third son who does not appear in her obituary, are honoured here too.

Usage:
  python src/obituary_mail_export.py
  python src/obituary_mail_export.py --out output/Obituary_Mail_Campaign.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from obituary_opportunity import CACHE, assemble  # noqa: E402
from obituary_mail_validate import norm  # noqa: E402

# The SmartSkip campaign header, in order. Kept verbatim so the file is a
# drop-in for whatever mail merge already consumes a SmartSkip export.
SS_COLS = [
    "Input Name", "First Name", "Last Name", "Mailing Address Full", "Mailing Address",
    "Middle Name", "Mailing City", "Mailing State", "Mailing Zip",
    "Property Address Full", "Property Address", "Property City", "Property State",
    "Property Zip", "Age", "Relationship", "Possible Type", "Deceased",
] + [f"Phone {i} {k}" for i in range(1, 9) for k in ("number", "type", "connected")] \
  + [f"Email {i}" for i in range(1, 18)]

# The stats block, appended to the right of the SmartSkip columns.
STAT_COLS = [
    "Mail To", "Role", "Why This Person", "Letter Type", "Situation",
    "People On This Record", "Person N Of", "Relatives Found", "Relatives Mailed",
    "Opportunity Score", "Tier", "Property Value", "Equity %", "Equity $",
    "Beds", "Baths", "SqFt", "Year Built", "Vacant", "Months Since Death",
    "Tax Owed", "Active Liens", "Owner Of Record", "Owner Alive",
    "Decedent Verified", "Obituary Source", "Address Status", "USPS Address",
    "Phone Count", "Best Phone", "Email Count", "Record UUID",
]


def key(first, last):
    return (re.sub(r"[^a-z]", "", (first or "").lower()),
            re.sub(r"[^a-z]", "", (last or "").lower()))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vertical", default="output/dp/smartskip_vertical.csv")
    ap.add_argument("--roster", default="output/dp/mail_roster_final.csv")
    ap.add_argument("--cache", default=str(CACHE))
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--out", default="output/Obituary_Mail_Campaign.csv")
    args = ap.parse_args()

    ss_rows = list(csv.DictReader(open(args.vertical, encoding="utf-8-sig")))
    roster = list(csv.DictReader(open(args.roster, encoding="utf-8")))
    qualified, _, _ = assemble(Path(args.cache), 3, date(2026, 8, 7))
    crm = {norm(r["street"]): r for r in qualified[:args.top]}

    # index the SmartSkip rows by (input name, person)
    ss_index = {}
    subjects = {}
    rel_counts = Counter()
    for r in ss_rows:
        inp = r["Input Name"].strip()
        if r["Relationship"] == "Subject":
            subjects[inp] = r
        elif r["Relationship"] == "Relative":
            rel_counts[inp] += 1
        ss_index[(inp, key(r["First Name"], r["Last Name"]))] = r

    # group the roster by property so we can number people within a record
    by_prop = defaultdict(list)
    for p in roster:
        by_prop[p["property"]].append(p)

    out_rows, unmatched = [], []
    for prop, people in by_prop.items():
        mailing = [p for p in people
                   if p["situation"] not in ("unresolved", "entity_owner")
                   and p["mail_status"] == "deliverable"]
        if not mailing:
            continue
        m = crm.get(norm(prop), {})
        n_total = len(mailing)
        for i, p in enumerate(sorted(mailing, key=lambda x: (x["who"] != "owner", x["name"])), 1):
            inp = p["record"].strip()
            if p["who"] == "owner":
                src = subjects.get(inp)
            else:
                src = ss_index.get((inp, key(*(p["name"].split(" ", 1) + [""])[:2])))
                if src is None:  # names like "MARY ANN SHULTZ" split differently
                    parts = p["name"].split()
                    if len(parts) >= 2:
                        src = ss_index.get((inp, key(parts[0], parts[-1])))
            if src is None:
                unmatched.append((inp, p["name"], p["who"]))
                src = {}

            row = {c: (src.get(c) or "") for c in SS_COLS}
            # A roster row always wins on identity: research corrections and the
            # CRM owner record are more trustworthy than the raw export.
            if not row["Input Name"]:
                row["Input Name"] = inp
            if not row["First Name"] and not row["Last Name"]:
                parts = p["name"].split()
                row["First Name"] = parts[0] if parts else p["name"]
                row["Last Name"] = " ".join(parts[1:]) if len(parts) > 1 else ""
            if not row["Mailing Address Full"] and p.get("street"):
                row["Mailing Address"] = p["street"]
                row["Mailing City"], row["Mailing State"] = p.get("city", ""), p.get("state", "")
                row["Mailing Zip"] = p.get("zip", "")
                row["Mailing Address Full"] = ", ".join(
                    x for x in [p.get("street"), p.get("city"), p.get("state"), p.get("zip")] if x)
            if not row["Property Address Full"]:
                row["Property Address"] = prop
                row["Property City"] = p.get("property_city", "")
                row["Property State"] = "TN"
                row["Property Zip"] = p.get("property_zip", "")
                row["Property Address Full"] = ", ".join(
                    x for x in [prop, p.get("property_city"), "TN", p.get("property_zip")] if x)

            phones = [row[f"Phone {i} number"] for i in range(1, 9) if row[f"Phone {i} number"]]
            emails = [row[f"Email {i}"] for i in range(1, 18) if row[f"Email {i}"]]

            row.update({
                "Mail To": p["name"],
                "Role": p["who"],
                "Why This Person": p["reason"],
                "Letter Type": {
                    "estate_verified": "Estate letter, heirs confirmed",
                    "estate_presumed": "Estate letter, decedent unconfirmed, no condolence",
                    "widowed_owner": "Owner letter, NOT an estate letter",
                    "owner_parent_died": "Owner letter, nothing passed to anybody",
                    "living_owner": "Plain we-buy-houses, no mention of a death",
                }.get(p["situation"], ""),
                "Situation": p["situation"],
                "People On This Record": n_total,
                "Person N Of": f"{i} of {n_total}",
                "Relatives Found": rel_counts.get(inp, 0),
                "Relatives Mailed": sum(1 for x in mailing if x["who"] != "owner"),
                "Opportunity Score": p.get("opportunity_score"),
                "Tier": m.get("tier", ""),
                "Property Value": m.get("estimate_value"),
                "Equity %": m.get("equity_pct"),
                "Equity $": m.get("equity_dollars"),
                "Beds": m.get("beds"), "Baths": m.get("baths"), "SqFt": m.get("sqft"),
                "Year Built": m.get("year_built"),
                "Vacant": "yes" if m.get("vacant") else "",
                "Months Since Death": p.get("months_since_death"),
                "Tax Owed": m.get("tax_amount"), "Active Liens": m.get("lien_amount"),
                "Owner Of Record": m.get("owner_name", ""),
                "Owner Alive": "yes" if p["situation"] in (
                    "widowed_owner", "owner_parent_died", "living_owner") else "no",
                "Decedent Verified": "yes" if p["situation"] in (
                    "estate_verified", "widowed_owner", "owner_parent_died") else "no",
                "Obituary Source": "",
                "Address Status": p["mail_status"],
                "USPS Address": p.get("usps_address", ""),
                "Phone Count": len(phones),
                "Best Phone": phones[0] if phones else "",
                "Email Count": len(emails),
                "Record UUID": m.get("uuid", ""),
            })
            out_rows.append(row)

    # stamp the verified obituary sources
    research = json.loads(Path("output/dp/stage_c_research.json").read_text(encoding="utf-8"))
    src_by_rec = {f["record"]: (f.get("source") or "") for f in research["findings"]}
    for r in out_rows:
        r["Obituary Source"] = src_by_rec.get(r["Input Name"], "")

    out_rows.sort(key=lambda r: (-(float(r["Opportunity Score"] or 0)),
                                 r["Property Address"], r["Role"] != "owner"))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SS_COLS + STAT_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)

    print(f"wrote {out}")
    print(f"  {len(out_rows)} people across {len({r['Property Address'] for r in out_rows})} houses")
    print(f"  columns: {len(SS_COLS)} SmartSkip + {len(STAT_COLS)} stats = "
          f"{len(SS_COLS)+len(STAT_COLS)}")
    print(f"  roles: {dict(Counter(r['Role'] for r in out_rows))}")
    print(f"  with a phone: {sum(1 for r in out_rows if r['Phone Count'])}"
          f" | with an email: {sum(1 for r in out_rows if r['Email Count'])}")
    if unmatched:
        print(f"  WARNING {len(unmatched)} roster rows had no SmartSkip source row "
              f"(identity filled from the roster): {unmatched[:6]}")


if __name__ == "__main__":
    main()
