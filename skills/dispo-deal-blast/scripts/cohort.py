#!/usr/bin/env python3
"""Cohort and price-band calculator for a dispo blast. Standard library only.

Answers the two questions you need before sending anything:

  1. Which buyers should get this deal, given their real purchase history?
  2. How many are you actually reaching, and who did you drop and why?

The band rule here is the one that matters and the one people get wrong. See
the "asymmetric" note in SKILL.md: above a buyer's range is affordability,
below it is only interest, so the floor and the ceiling are NOT symmetric.

Input is a JSON list of buyers:

    [{"name": "...", "price_min": 90000, "price_max": 130000,
      "last_buy": "2025-12-29", "n_buys": 3, "phone": "..."}, ...]

Usage:
    python cohort.py buyers.json --price 104000
    python cohort.py buyers.json --price 104000 --floor-mult 4 --months 12
"""
import argparse
import datetime
import json
import sys


def parse_date(s):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(str(s)[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def fmt(n):
    return "$%s" % format(int(n or 0), ",")


def in_band(buyer, price, floor_mult, tol):
    """Asymmetric: the floor is DIVIDED, the ceiling gets the tolerance.

    A buyer with no band is KEPT. Missing history means we never resolved it,
    not that they buy nothing, and silently dropping the unknown is how a
    cohort quietly shrinks to whatever happened to hydrate.
    """
    lo, hi = buyer.get("price_min"), buyer.get("price_max")
    if not lo and not hi:
        return True, "band unknown, kept"
    floor = (lo or 0) / float(floor_mult or 1)
    ceil = (hi or lo or 0) * (1 + tol)
    if price < floor:
        return False, "below their range (buys %s and up)" % fmt(lo)
    if price > ceil:
        return False, "above their range (tops out at %s)" % fmt(hi)
    return True, ""


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("buyers", help="JSON list of buyers")
    ap.add_argument("--price", type=int, required=True, help="the asking price")
    ap.add_argument("--floor-mult", type=float, default=4.0,
                    help="keep a buyer whose cheapest purchase is up to N times the ask")
    ap.add_argument("--tolerance", type=float, default=0.35,
                    help="how far above their ceiling still counts")
    ap.add_argument("--months", type=int, default=12,
                    help="0 to disable the recency gate")
    a = ap.parse_args()

    buyers = json.load(open(a.buyers, encoding="utf-8"))
    if not isinstance(buyers, list):
        sys.exit("expected a JSON list of buyers")

    cutoff = None
    if a.months:
        cutoff = datetime.date.today() - datetime.timedelta(days=30 * a.months)

    kept, stale, out = [], [], []
    for b in buyers:
        if cutoff:
            d = parse_date(b.get("last_buy"))
            if d and d < cutoff:
                stale.append(b)
                continue
        ok, why = in_band(b, a.price, a.floor_mult, a.tolerance)
        (kept if ok else out).append((b, why))

    print("buyers in file      : %d" % len(buyers))
    if cutoff:
        print("dropped, not active : %d (no purchase since %s)"
              % (len(stale), cutoff))
    print("out of band         : %d" % len(out))
    print("COHORT              : %d" % len(kept))

    covers = sum(1 for b in buyers
                 if b.get("price_min") and b.get("price_max")
                 and b["price_min"] <= a.price <= b["price_max"])
    never = sum(1 for b in buyers
                if b.get("price_min") and b["price_min"] > a.price)
    print("")
    print("band covers %s outright : %d" % (fmt(a.price), covers))
    print("have never bought that cheap : %d" % never)
    if never > covers:
        print("  NOTE: this deal is priced below where most of this list plays.")

    if out:
        print("")
        print("dropped, with the reason:")
        for b, why in out[:10]:
            print("   %-34s %s" % (str(b.get("name"))[:34], why))
        if len(out) > 10:
            print("   ... and %d more" % (len(out) - 10))


if __name__ == "__main__":
    main()
