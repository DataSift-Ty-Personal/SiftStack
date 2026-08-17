"""Audit every backfilled record, restore PR contacts, and score the buy box.

One pass over the records so the three questions get answered off the same
snapshot rather than three drifting ones:

  1. Is enrichment actually finished?
  2. Which probate records lost their personal representative to it?
  3. Which properties clear the buy box now that there is data to judge?

    python src/probate_audit_restore.py --audit                  # read only
    python src/probate_audit_restore.py --audit --restore-prs    # + put PRs back

WHY THE PR RESTORE IS NEEDED. DataSift enrichment matches a record against its
bulk data and takes the owner of title from it. On a probate record the owner of
title is the DECEDENT, so enrichment quietly replaces the living personal
representative with a dead person. Measured at about 7.5% of PR records.
Re-posting the PR puts it back and pushes the enrichment-supplied name into
secondary owners, which is where it belongs; enrichment data is untouched.

READS ARE UPSERTS. `POST /property/` is the only address-to-uuid route, and it
CREATES a record if none exists. That silently resurrected records deleted
earlier in the day. Only ever call it for addresses that are meant to exist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datasift_api_upload import Api  # noqa: E402

# Buy box (Ty): single family only, AVM $1 to $700,000. The $1 floor is
# deliberate: condemned and tax-distressed stock routinely falls under $100K.
VALUE_MIN = 1.0
VALUE_MAX = 700_000.0


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def norm(s: str) -> str:
    return " ".join((s or "").lower().replace(",", "").split())


def audit_one(api: Api, rec: dict) -> dict:
    p = api.call("/property/", "POST", {"address": {
        "street": rec["Property Street Address"], "city": rec["Property City"],
        "state": rec["Property State"], "postal_code": rec["Property ZIP Code"]}})
    uuid = p.get("uuid")
    d = api.call(f"/api/internal/property/{uuid}/")
    o = d.get("owner") or {}
    avm = num(d.get("estimate_value"))
    eq = num(d.get("equity_percent"))
    st = (d.get("structure_type") or "").strip()

    enriched = any(d.get(k) for k in ("estimate_value", "year", "sqft", "bedrooms"))
    owner_now = norm(f"{o.get('first_name')} {o.get('last_name')}")
    pr_want = norm(f"{rec.get('Owner First Name')} {rec.get('Owner Last Name')}")
    decedent = norm(rec.get("Decedent Name"))

    single = (not st) or ("single" in st.lower()) or ("sfr" in st.lower())
    in_box = bool(avm is not None and VALUE_MIN <= avm <= VALUE_MAX and single)

    return {
        "uuid": uuid,
        "address": rec["Property Street Address"],
        "city": rec["Property City"],
        "county": rec.get("County", ""),
        "type": rec.get("Notice Type", ""),
        "enriched": enriched,
        "avm": avm,
        "equity_percent": eq,
        "structure_type": st,
        "year": d.get("year"),
        "sqft": num(d.get("sqft")),
        "beds": d.get("bedrooms"),
        "vacant": (d.get("address") or {}).get("vacant"),
        "investor_score": d.get("investor_score"),
        "mls": d.get("mls"),
        "last_sold": str(d.get("last_sold") or ""),
        "owner_now": owner_now,
        "pr_expected": pr_want,
        "decedent": decedent,
        "pr_lost": bool(pr_want and owner_now != pr_want),
        "pr_is_decedent": bool(decedent and owner_now == decedent),
        "in_buy_box": in_box,
        "dataflik": bool(d.get("dataflik_id")),
    }


def restore_pr(api: Api, rec: dict) -> bool:
    first = (rec.get("Owner First Name") or "").strip().rstrip(",")
    last = (rec.get("Owner Last Name") or "").strip().rstrip(",")
    if not first and not last:
        return False
    api.call("/property/", "POST", {
        "address": {"street": rec["Property Street Address"], "city": rec["Property City"],
                    "state": rec["Property State"], "postal_code": rec["Property ZIP Code"]},
        "owner": {"first_name": first.title(), "last_name": last.title()},
    })
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", default="output/all_records.json",
                    help="every backfilled record (address + our fields)")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--restore-prs", action="store_true",
                    help="re-post the PR wherever enrichment overwrote it")
    ap.add_argument("--out", default="output/enriched_audit.json")
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    recs = json.load(open(a.records, encoding="utf-8"))
    if a.limit:
        recs = recs[:a.limit]

    api = Api()
    rows, restored, failed = [], 0, 0
    t0 = time.time()
    for i, r in enumerate(recs, 1):
        try:
            row = audit_one(api, r)
            if a.restore_prs and row["pr_lost"] and row["pr_expected"]:
                if restore_pr(api, r):
                    row["restored"] = True
                    restored += 1
            rows.append(row)
        except Exception as exc:
            failed += 1
            if failed <= 5:
                print(f"  FAIL {r.get('Property Street Address')}: {str(exc)[:90]}")
        if i % 50 == 0:
            enr = sum(1 for x in rows if x["enriched"])
            print(f"   {i}/{len(recs)}  enriched={enr}  pr_lost={sum(1 for x in rows if x['pr_lost'])}"
                  f"  restored={restored}  failed={failed}  {time.time()-t0:.0f}s", flush=True)
            json.dump(rows, open(a.out, "w"), default=str)
        time.sleep(a.sleep)

    json.dump(rows, open(a.out, "w"), default=str)
    n = len(rows)
    enr = sum(1 for x in rows if x["enriched"])
    print(f"\n=== {n} records ===")
    print(f"enriched            : {enr}/{n}  ({100*enr/max(n,1):.1f}%)")
    print(f"PR lost to enrich   : {sum(1 for x in rows if x['pr_lost'])}")
    print(f"  of which decedent : {sum(1 for x in rows if x['pr_is_decedent'])}")
    print(f"restored            : {restored}")
    print(f"in buy box          : {sum(1 for x in rows if x['in_buy_box'])}")
    print(f"failed              : {failed}")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
