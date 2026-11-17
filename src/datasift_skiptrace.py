"""Skip trace via the DataSift internal API.

THE SCOPING PARAMETER IS IGNORED. `POST /api/internal/property/skip-trace/`
accepts a body but does not honour `properties`: a call carrying a single
property uuid skip traced 53,322 records, the whole account. Measured live
2026-08-19 against the stats endpoint:

    total_properties  12,920 -> 66,242   (delta 53,322)
    value_spent       0.0

So treat this endpoint as ALL OR NOTHING. There is no known way to skip trace a
subset over the API; that needs the browser wizard (Send To > Skip Trace on a
filtered selection).

WHY THAT IS TOLERABLE HERE, and would not be elsewhere: this account is on the
unlimited skip trace plan, so an account-wide run costs $0.00. `stats()` reports
`value_spent` and `saved_by_unlimited_skiptracing`, and this module REFUSES to
run if value_spent is non-zero, because on a metered plan the same call would
bill for every record in the account.

    python src/datasift_skiptrace.py --stats            # read only
    python src/datasift_skiptrace.py --run              # account-wide, guarded

WHY IT MATTERS: every FTM preset requires `phone: 1` and `skiptraced: 1`.
Records that are correctly tagged and listed still enter no cadence until they
carry a phone, and notices never contain one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datasift_api_upload import Api  # noqa: E402

STATS = "/api/internal/activity/skiptrace/stats/"
RUN = "/api/internal/property/skip-trace/"


def stats(api: Api) -> dict:
    return api.call(STATS)


def show(s: dict) -> None:
    print(f"  properties skip traced : {s.get('total_properties'):,}")
    print(f"  owners                 : {s.get('total_owners'):,}")
    print(f"  phones + emails        : {s.get('both'):,}")
    print(f"  phones only            : {s.get('phones_only'):,}")
    print(f"  emails only            : {s.get('emails_only'):,}")
    print(f"  no result              : {s.get('no_result'):,}")
    print(f"  value spent            : ${s.get('value_spent', 0):,.2f}")
    print(f"  saved by unlimited     : ${s.get('saved_by_unlimited_skiptracing', 0):,.2f}")


def run(api: Api, confirm_unlimited: bool = True) -> dict:
    """Trigger a skip trace. This is ACCOUNT-WIDE, see the module docstring."""
    before = stats(api)
    spent = float(before.get("value_spent") or 0)
    saved = float(before.get("saved_by_unlimited_skiptracing") or 0)

    if confirm_unlimited and (spent > 0 or saved <= 0):
        raise RuntimeError(
            f"Refusing to run: this looks like a METERED skip trace plan "
            f"(value_spent={spent}, saved_by_unlimited={saved}). The endpoint "
            f"ignores scoping and would bill for every record in the account."
        )

    resp = api.call(RUN, "POST", {})
    time.sleep(5)
    after = stats(api)
    return {
        "requested": resp.get("number_of_records") if isinstance(resp, dict) else None,
        "cost": resp.get("cost") if isinstance(resp, dict) else None,
        "before": before.get("total_properties"),
        "after": after.get("total_properties"),
        "delta": (after.get("total_properties") or 0) - (before.get("total_properties") or 0),
        "value_spent": after.get("value_spent"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true", help="read the counters and exit")
    ap.add_argument("--run", action="store_true",
                    help="trigger skip trace. ACCOUNT-WIDE; the endpoint ignores scoping")
    ap.add_argument("--allow-metered", action="store_true",
                    help="override the unlimited-plan guard. Think first.")
    a = ap.parse_args()

    api = Api()
    if a.stats or not a.run:
        print("DataSift skip trace, current state:")
        show(stats(api))
        if not a.run:
            print("\nNothing triggered. Use --run to skip trace (account-wide).")
        return 0

    print("Triggering skip trace. NOTE: the API ignores scoping, so this covers "
          "the whole account.")
    res = run(api, confirm_unlimited=not a.allow_metered)
    print(json.dumps(res, indent=1))
    if res["delta"] <= 0:
        print("\nNo change in the counter. Either everything was already traced "
              "or the run did not take.")
        return 1
    print(f"\ntraced {res['delta']:,} more properties, cost ${res['value_spent']:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
