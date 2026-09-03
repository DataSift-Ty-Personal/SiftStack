"""Restore the 'Priority 1' tag after it was stripped account-wide.

WHAT HAPPENED (2026-08-11): a rollback intended to remove 'Priority 1' from ONE
record used POST /api/internal/property/remove-tags/ with the properties list at
the TOP LEVEL of the body. That endpoint ignores a top-level `properties` filter
and applies account-wide, so it stripped the tag from every record that had it.
Priority 1 went from at least 4,386 down to 3,507.

THE SHAPE THAT ACTUALLY SCOPES: `properties` must be nested INSIDE query.must,
and the tag must be passed as a TITLE, not a uuid:

    {"query": {"must": {"property_type": "clean", **signal, "properties": [...]},
               "ordering": ["-list_count"]},
     "tags": ["Priority 1"]}

That is the only form the records-filter INDEX sees, which is the same reason a
plain PATCH {tags: [...]} updates a record but leaves it invisible to its own
cadence.

Priority 1 is the union of four signals, copied verbatim from
_api/_priority_tag.py (the script that owns these tags):
  AI 90+, Absentee+LowIncome, BadCredit+FreeClear, Absentee+BadCredit
all property_type=clean and excluding the 'Not Single Family' tag.

This reruns that union incrementally: it tags only records that match a signal
and do NOT currently carry Priority 1, which is exactly a restore. Idempotent.

  python src/restore_priority1.py --dry
  python src/restore_priority1.py --commit
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from obituary_opportunity import Reader  # noqa: E402

TIER = "Priority 1"
CHUNK = 400
PAGE = 250


def aslist(r):
    if isinstance(r, dict):
        return r["results"] if "results" in r else []
    return r or []


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--account", default="datasift-apikey")
    args = ap.parse_args()

    rd = Reader(args.account, gap=0.35)
    lists = {(l.get("title") or "").lower(): l["uuid"]
             for l in aslist(rd._call("/api/internal/list/?limit=999"))}
    tags = {(t.get("title") or "").lower(): t["uuid"]
            for t in aslist(rd._call("/api/internal/tag/?limit=999"))}
    tier_uuid = tags.get(TIER.lower())
    nsf = tags.get("not single family")
    if not tier_uuid:
        sys.exit(f"'{TIER}' tag not found")

    def L(n):
        return lists[n.lower()]

    def ai(lo, hi):
        return {"investor_score": [lo, hi]}

    def combo(*ns):
        return {"all_lists": [L(n) for n in ns]}

    SIGNALS = [("AI 90+", ai(90, 100)),
               ("Absentee+LowIncome", combo("Absentee Owners", "Low Income")),
               ("BadCredit+FreeClear", combo("Low Credit Score", "Free & Clear")),
               ("Absentee+BadCredit", combo("Absentee Owners", "Low Credit Score"))]

    def cnt(must):
        return rd._call("/api/internal/property/", method="POST", override="GET",
                        body={"limit": 1, "offset": 0, "query": {"must": must}}).get("count")

    def select(sig):
        """Records matching this signal that do NOT already carry the tier tag."""
        must = {"property_type": "clean", **sig}
        excl = [tier_uuid] + ([nsf] if nsf else [])
        must["must_not"] = {"any_tags": excl}
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

    before = cnt({"any_tags": [tier_uuid], "property_type": "clean"})
    print(f"{TIER} currently on {before} clean records\n")

    total = 0
    for label, sig in SIGNALS:
        ids = select(sig)
        print(f"  {label:24} {len(ids):5} records match but are UNTAGGED")
        if not (args.commit and not args.dry):
            continue
        done = 0
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i + CHUNK]
            body = {"query": {"must": {"property_type": "clean", **sig,
                                       "properties": chunk},
                              "ordering": ["-list_count"]},
                    "tags": [TIER]}
            try:
                r = rd._call("/api/internal/property/add-tags/", method="POST", body=body)
                done += (r.get("count") or 0) if isinstance(r, dict) else 0
            except Exception as e:
                print(f"      chunk skipped: {str(e)[:90]}")
            time.sleep(1.0)
        total += done
        print(f"  {label:24} tagged {done}")

    if args.commit and not args.dry:
        print(f"\n  submitted {total}; the index catches up async over 1-2 minutes")
    else:
        print("\n  DRY RUN, nothing written. Rerun with --commit.")


if __name__ == "__main__":
    main()
