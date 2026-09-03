"""Incremental Priority 1 / Priority 2 tag stamping for ty+2 (Doors per Deal).

Generalizes restore_priority1.py (kept as the 2026-08-11 incident record) to BOTH
tiers, on the no-expiry Api-Key path (no JWT, no impersonation). Signal table copied
verbatim from Deal Room _api/_priority_tag.py, the script that owns these tags.

Tags only records that match a signal and do NOT already carry the tier tag, so a
rerun is a no-op. The add-tags contract that reaches the records-filter INDEX:
properties nested INSIDE query.must, tag passed as a TITLE.

  python src/priority_tags.py --dry
  python src/priority_tags.py --commit
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from obituary_opportunity import Reader  # noqa: E402

CHUNK = 400
PAGE = 250


def aslist(r):
    if isinstance(r, dict):
        return r.get("results") or r.get("data") or []
    return r or []


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--account", default="datasift-apikey")
    args = ap.parse_args()
    commit = args.commit and not args.dry

    rd = Reader(args.account, gap=0.35)
    lists = {(l.get("title") or "").lower(): l["uuid"]
             for l in aslist(rd._call("/api/internal/list/?limit=999"))}
    tags = {(t.get("title") or "").lower(): t["uuid"]
            for t in aslist(rd._call("/api/internal/tag/?limit=10000"))}
    nsf = tags.get("not single family")

    def L(n):
        return lists[n.lower()]

    def ai(lo, hi):
        return {"investor_score": [lo, hi]}

    def combo(*ns):
        return {"all_lists": [L(n) for n in ns]}

    TIERS = {
        "Priority 1": [("AI 90+", ai(90, 100)),
                       ("Absentee+LowIncome", combo("Absentee Owners", "Low Income")),
                       ("BadCredit+FreeClear", combo("Low Credit Score", "Free & Clear")),
                       ("Absentee+BadCredit", combo("Absentee Owners", "Low Credit Score"))],
        "Priority 2": [("AI 70-89", ai(70, 89)),
                       ("BadCredit+LowIncome", combo("Low Credit Score", "Low Income")),
                       ("Absentee+TaxDelinquent", combo("Absentee Owners", "Tax Delinquent")),
                       ("TaxDelinquent+Senior", combo("Tax Delinquent", "Senior Homeowners")),
                       ("FreeClear+TaxDelinquent", combo("Free & Clear", "Tax Delinquent"))],
    }

    def cnt(must):
        return rd._call("/api/internal/property/", method="POST", override="GET",
                        body={"limit": 1, "offset": 0, "query": {"must": must}}).get("count")

    def select(sig, tier_uuid):
        must = {"property_type": "clean", **sig,
                "must_not": {"any_tags": [tier_uuid] + ([nsf] if nsf else [])}}
        out, off = [], 0
        while True:
            r = rd._call("/api/internal/property/", method="POST", override="GET",
                         body={"limit": PAGE, "offset": off, "query": {"must": must}})
            d = aslist(r)
            out += [x["uuid"] for x in d if x.get("uuid")]
            if len(d) < PAGE:
                break
            off += PAGE
        return out

    grand = 0
    for tier, sigs in TIERS.items():
        tier_uuid = tags.get(tier.lower())
        if not tier_uuid:
            sys.exit(f"{tier!r} tag not found on this account")
        print(f"\n== {tier} (currently {cnt({'any_tags': [tier_uuid]})}) ==")
        for label, sig in sigs:
            ids = select(sig, tier_uuid)
            print(f"  {label:26} {len(ids):5} match but are UNTAGGED")
            if not commit:
                continue
            done = 0
            for i in range(0, len(ids), CHUNK):
                chunk = ids[i:i + CHUNK]
                body = {"query": {"must": {"property_type": "clean", **sig,
                                           "properties": chunk},
                                  "ordering": ["-list_count"]},
                        "tags": [tier]}
                try:
                    r = rd._call("/api/internal/property/add-tags/", method="POST", body=body)
                    done += (r.get("count") or 0) if isinstance(r, dict) else 0
                except Exception as e:
                    print(f"      chunk skipped: {str(e)[:90]}")
                time.sleep(1.0)
            grand += done
            print(f"  {label:26} tagged {done}")

    if commit:
        print(f"\n  submitted {grand}; the index catches up async over 1-2 minutes")
    else:
        print("\n  DRY RUN, nothing written. Rerun with --commit.")


if __name__ == "__main__":
    main()
