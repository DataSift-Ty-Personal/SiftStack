"""Remove the DataFlik bulk lists from our first-to-market records.

Enrichment attaches its own bulk lists (Owner Occupied, High Equity, Senior
Homeowners, Free & Clear ...) to whatever it touches. Those lists drive the BULK
sequential cadence, so a courthouse record carrying them gets pulled into the
wrong flow. Ty: these bulk lists are demo data for the challenge, and the FTM
records should sit in the first-to-market flow only.

THE API CONTRACT, which took three wrong guesses to find:

    POST /api/internal/property/{uuid}/remove-lists/   {"lists": "Owner Occupied"}

  * `lists` is a STRING, not an array. Sending an array returns 201 and does
    NOTHING. That silent success is the dangerous part: batched blindly it would
    have reported 878 records cleaned with zero actually changed.
  * It is the list TITLE, not its uuid. A uuid string is also accepted and also
    does nothing (unless a literal uuid is genuinely on the record).
  * One list per call. Comma-separated values are ignored.
  * `PATCH /property/{uuid}/ {"lists": [...]}` APPENDS, it never removes, and it
    will happily write a raw uuid into the list array.

Every record is read back after its removals, and a record is only counted as
done when the read confirms it.

    python src/strip_bulk_lists.py --dry-run
    python src/strip_bulk_lists.py --commit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datasift_api_upload import Api  # noqa: E402

# The only lists an FTM record should carry.
KEEP = {"probate", "foreclosure"}


def lists_of(d: dict) -> list[str]:
    out = []
    for x in d.get("lists") or []:
        out.append((x.get("title") or x.get("name") or "").strip()
                   if isinstance(x, dict) else str(x).strip())
    return [x for x in out if x]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", default="output/enriched_audit.json")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--state", default="output/strip_done.json")
    a = ap.parse_args()

    rows = json.load(open(a.audit, encoding="utf-8"))
    if a.limit:
        rows = rows[:a.limit]

    done = set()
    if os.path.exists(a.state):
        try:
            done = set(json.load(open(a.state, encoding="utf-8")))
        except Exception:
            done = set()

    api = Api()
    touched = cleaned = skipped = failed = 0
    residual = []

    print(f"[{'COMMIT' if a.commit else 'DRY RUN'}] {len(rows)} records\n")

    for i, r in enumerate(rows, 1):
        if r["uuid"] in done:
            skipped += 1
            continue
        try:
            d = api.call(f"/api/internal/property/{r['uuid']}/")
        except Exception as exc:
            failed += 1
            if failed <= 5:
                print(f"  read fail {r['address']}: {str(exc)[:70]}")
            continue

        cur = lists_of(d)
        drop = [x for x in cur if x.lower() not in KEEP]
        if not drop:
            done.add(r["uuid"])
            continue
        touched += 1

        if not a.commit:
            if touched <= 8:
                print(f"  {r['address'][:26]:28} would drop {drop}")
            continue

        for name in drop:
            try:
                api.call(f"/api/internal/property/{r['uuid']}/remove-lists/",
                         "POST", {"lists": name})
                time.sleep(0.25)
            except Exception as exc:
                if failed <= 8:
                    print(f"  drop fail {r['address'][:22]} / {name}: {str(exc)[:60]}")
                failed += 1

        time.sleep(0.5)
        back = lists_of(api.call(f"/api/internal/property/{r['uuid']}/"))
        left = [x for x in back if x.lower() not in KEEP]
        if left:
            residual.append((r["address"], left))
            print(f"  RESIDUAL {r['address'][:26]:28} still has {left}")
        else:
            cleaned += 1
            done.add(r["uuid"])

        if i % 50 == 0:
            print(f"   {i}/{len(rows)} touched={touched} cleaned={cleaned} "
                  f"residual={len(residual)} failed={failed}", flush=True)
            json.dump(sorted(done), open(a.state, "w"))
        time.sleep(a.sleep)

    if a.commit:
        json.dump(sorted(done), open(a.state, "w"))

    print(f"\nrecords needing a strip : {touched}")
    if a.commit:
        print(f"fully cleaned + verified: {cleaned}")
        print(f"residual lists left     : {len(residual)}")
        print(f"failed calls            : {failed}")
        for addr, left in residual[:10]:
            print(f"   {addr[:28]:30} {left}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
