"""Repair the owner on records where it is not the personal representative.

TWO BUGS PRODUCED THIS, and the second one was mine.

1. DataSift enrichment matches a record against its bulk data and takes the
   owner of title. On a probate record that is the DECEDENT, so enrichment
   replaced the living PR with a dead person on about 7.5% of records.

2. The first repair attempt posted `owner: {first_name, last_name}` with NO
   owner address. DataSift then linked the property to an EXISTING owner entity
   rather than the intended person, and it picked the wrong one: 17 of 35
   records ended up owned by someone from an unrelated record ("Deborah Estep"
   landed on seven different properties). Including the owner's mailing address
   makes the write bind to the right person. Verified live.

So every write here carries the full owner block, and every write is READ BACK
before the record is counted as fixed. A repair that cannot verify itself is how
the second bug got past the first check.

    python src/repair_pr_owners.py --dry-run
    python src/repair_pr_owners.py --commit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datasift_api_upload import Api  # noqa: E402


def norm(s: str) -> str:
    return " ".join((s or "").lower().replace(",", "").split())


def owner_body(rec: dict) -> dict:
    first = (rec.get("Owner First Name") or "").strip().rstrip(",")
    last = (rec.get("Owner Last Name") or "").strip().rstrip(",")
    owner = {"first_name": first.title(), "last_name": last.title()}
    # The mailing address is what makes DataSift bind to the right owner entity
    # instead of reusing an unrelated one. Omitting it is bug #2 above.
    ms = (rec.get("Mailing Street Address") or "").strip()
    if ms:
        owner["address"] = {
            "street": ms,
            "city": (rec.get("Mailing City") or "").strip(),
            "state": (rec.get("Mailing State") or "").strip(),
            "postal_code": (rec.get("Mailing ZIP Code") or "").strip(),
        }
    return {
        "address": {"street": rec["Property Street Address"], "city": rec["Property City"],
                    "state": rec["Property State"], "postal_code": rec["Property ZIP Code"]},
        "owner": owner,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mailing", default="output/mailing_map.json")
    ap.add_argument("--audit", default="output/enriched_audit.json")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.4)
    a = ap.parse_args()

    mail = json.load(open(a.mailing, encoding="utf-8"))
    audit = json.load(open(a.audit, encoding="utf-8"))
    api = Api()

    # Every record that should have a PR as its contact.
    targets = []
    for row in audit:
        key = f"{row['address'].lower()}|{row['city'].lower()}"
        rec = mail.get(key)
        if not rec:
            continue
        want = norm(f"{rec.get('Owner First Name')} {rec.get('Owner Last Name')}")
        if not want or not (rec.get("Personal Representative") or "").strip():
            continue
        targets.append((row, rec, want))

    print(f"[{'COMMIT' if a.commit else 'DRY RUN'}] {len(targets)} records that should carry a PR\n")

    checked = correct = fixed = still_bad = 0
    bad = []
    for row, rec, want in targets:
        try:
            d = api.call(f"/api/internal/property/{row['uuid']}/")
        except Exception as exc:
            print(f"  read fail {row['address']}: {str(exc)[:70]}")
            continue
        o = d.get("owner") or {}
        now = norm(f"{o.get('first_name')} {o.get('last_name')}")
        checked += 1
        if now == want:
            correct += 1
        elif not a.commit:
            bad.append((row["address"], now, want))
        else:
            api.call("/property/", "POST", owner_body(rec))
            time.sleep(0.8)
            back = api.call(f"/api/internal/property/{row['uuid']}/")
            bo = back.get("owner") or {}
            after = norm(f"{bo.get('first_name')} {bo.get('last_name')}")
            if after == want:
                fixed += 1
            else:
                still_bad += 1
                bad.append((row["address"], after, want))
                print(f"  STILL WRONG {row['address'][:26]:28} now={after[:20]} want={want[:20]}")
        if checked % 50 == 0:
            print(f"   {checked}/{len(targets)} correct={correct} fixed={fixed} "
                  f"still_bad={still_bad}", flush=True)
        time.sleep(a.sleep)

    print(f"\nchecked      : {checked}")
    print(f"already right: {correct}")
    if a.commit:
        print(f"repaired     : {fixed}")
        print(f"STILL WRONG  : {still_bad}")
    else:
        print(f"would repair : {len(bad)}")
        for addr, now, want in bad[:20]:
            print(f"   {addr[:28]:30} now={now[:20]:22} want={want[:20]}")
    return 1 if still_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
