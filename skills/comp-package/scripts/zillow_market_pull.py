"""Self-contained Zillow /search puller (requests only, no repo dependencies).

Pulls sold + active listings for a location from the OpenWeb Ninja Real-Time
Zillow Data API, beating the 41-row cap via adaptive min_price/max_price band
partitioning. Writes normalized JSON for downstream comp analysis.

Usage:
  set OPENWEBNINJA_API_KEY=your_key            (or export on mac/linux)
  python zillow_market_pull.py --location "Knoxville, TN 37914" --months 12 \
      --out market_37914.json

Optional boundary filters:
  --bbox "lat_min,lat_max,lon_min,lon_max"
  --streets "old state|nash rd|seahorn"        (regex, case-insensitive)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://api.openwebninja.com/realtime-zillow-data"
RESULT_CAP = 41
MIN_BAND_WIDTH = 4000
BANDS = [(1_000, 100_000), (100_001, 160_000), (160_001, 210_000),
         (210_001, 250_000), (250_001, 290_000), (290_001, 340_000),
         (340_001, 420_000), (420_001, 3_000_000)]


def search(key: str, location: str, home_status: str, **params) -> list[dict]:
    for attempt in range(1, 4):
        try:
            resp = requests.get(f"{BASE}/search",
                                params={"location": location, "home_status": home_status, **params},
                                headers={"x-api-key": key}, timeout=60)
            resp.raise_for_status()
            return resp.json().get("data") or []
        except (requests.RequestException, ValueError) as exc:
            print(f"  warn: {exc} (attempt {attempt}/3)", file=sys.stderr)
            time.sleep(attempt)
    return []


def normalize(item: dict) -> dict:
    def num(v):
        if isinstance(v, str):
            v = "".join(c for c in v if c.isdigit() or c == ".") or 0
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    sold = item.get("dateSold")
    sold_date = (datetime.fromtimestamp(sold / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                 if isinstance(sold, (int, float)) and sold else "")
    return {
        "zpid": str(item.get("zpid") or ""),
        "address": item.get("streetAddress") or "",
        "zip": str(item.get("zipcode") or ""),
        "lat": num(item.get("latitude")),
        "lon": num(item.get("longitude")),
        "beds": int(num(item.get("beds") or item.get("bedrooms"))),
        "baths": num(item.get("baths") or item.get("bathrooms")),
        "sqft": int(num(item.get("livingArea") or item.get("area"))),
        "home_type": item.get("homeType") or "",
        "price": num(item.get("unformattedPrice") or item.get("price")),
        "sold_date": sold_date,
        "zestimate": num(item.get("zestimate")),
        "rent_zestimate": num(item.get("rentZestimate")),
        "days_on_zillow": int(num(item.get("daysOnZillow"))),
        "url": item.get("detailUrl") or "",
    }


def pull_sold(key: str, location: str, months: int) -> list[dict]:
    collected: dict[str, dict] = {}

    def band(lo: int, hi: int, depth: int = 0) -> None:
        data = search(key, location, "RECENTLY_SOLD", min_price=lo, max_price=hi)
        print(f"{'  ' * depth}band {lo:,}-{hi:,}: {len(data)}")
        if len(data) >= RESULT_CAP and hi - lo > MIN_BAND_WIDTH:
            mid = (lo + hi) // 2
            band(lo, mid, depth + 1)
            band(mid + 1, hi, depth + 1)
        else:
            for item in data:
                collected[str(item.get("zpid"))] = item
        time.sleep(0.8)

    for lo, hi in BANDS:
        band(lo, hi)

    cutoff = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")
    rows = [normalize(i) for i in collected.values()]
    return sorted((r for r in rows if r["sold_date"] >= cutoff),
                  key=lambda r: r["sold_date"], reverse=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--location", required=True)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--bbox")
    ap.add_argument("--streets")
    ap.add_argument("--out", default="market_pull.json")
    args = ap.parse_args()

    key = os.environ.get("OPENWEBNINJA_API_KEY", "")
    if not key:
        print("error: set OPENWEBNINJA_API_KEY", file=sys.stderr)
        return 1

    sold = pull_sold(key, args.location, args.months)
    active = [normalize(i) for i in search(key, args.location, "FOR_SALE")]

    if args.bbox:
        lat_min, lat_max, lon_min, lon_max = (float(x) for x in args.bbox.split(","))
        keep = lambda r: lat_min <= r["lat"] <= lat_max and lon_min <= r["lon"] <= lon_max
        sold, active = [r for r in sold if keep(r)], [r for r in active if keep(r)]
    if args.streets:
        pat = re.compile(args.streets, re.I)
        sold = [r for r in sold if pat.search(r["address"])]
        active = [r for r in active if pat.search(r["address"])]

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"pulled": datetime.now().strftime("%Y-%m-%d"),
                   "location": args.location, "sold": sold, "active": active}, fh, indent=1)
    print(f"\nsold: {len(sold)} | active: {len(active)} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
