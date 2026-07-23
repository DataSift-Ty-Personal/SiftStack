"""SiftMap deed-level buyer sweep: who is ACTUALLY buying in a zip, at what
band, with what portfolio: the dispo-list engine built on the 158 Old State run.

Pipeline per zip:
  1. Sold universe from the Zillow /search band-partition pull (zillow_market_api)
     or a previously saved pull (--sold-json).
  2. Filter to the investor band (--min-price/--max-price) + lookback window.
  3. For each sale: SiftMap autocomplete -> get_detail -> deed sale_history
     (buyer_name, is_cash_sale) + owner_info (portfolio, mailing).
  4. Aggregate by buyer; rank by purchase count, band fit, portfolio.
  5. UNMASK hidden principals: when a buyer entity's mailing address is a
     residence, reverse it through SiftMap owner_info; if that owner is a
     person, that is the principal (the Harper move). Falls back to Enformion
     BusinessV2 officers.

Usage:
  python src/buyer_sweep.py --zip 37914 --months 18
  python src/buyer_sweep.py --zip 37914 --sold-json output/zillow_37914_sold.json --out output/buyers_sweep_37914

Requires the Deal Room _api clients (SiftMapClient + reisift Open API key).
Output: <out>.json (full) + <out>.csv (ranked buyer list).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

_API_CLIENTS = Path(r"C:\Users\Tyrus\OneDrive\Desktop\Deal Room Coaching Call\_api\clients")
sys.path.insert(0, str(_API_CLIENTS))

import config  # noqa: E402

logger = logging.getLogger(__name__)

ENTITY_RX = re.compile(r"\bLLC\b|\bLLP\b|\bINC\b|\bTRUST\b|\bPROPERTIES\b|\bHOLDINGS\b|\bHOMES\b|\bCAPITAL\b|\bINVEST", re.I)


def _num(v) -> float:
    if isinstance(v, str):
        v = re.sub(r"[^\d.]", "", v) or 0
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def load_sold_addresses(args) -> list[tuple[str, str, float]]:
    """(address, sold_date, price) tuples in the target band/window."""
    cutoff = (datetime.now() - timedelta(days=args.months * 30)).strftime("%Y-%m-%d")
    rows: list[tuple[str, str, float]] = []
    if args.sold_json:
        items = json.load(open(args.sold_json, encoding="utf-8"))
        if isinstance(items, dict):
            items = items.get("sold") or items.get("records") or []
        from zillow_market_api import normalize
        listings = [normalize(i) if "streetAddress" in i else None for i in items]
        listings = [l for l in listings if l]
    else:
        from zillow_market_api import ZillowMarketAPI
        listings = ZillowMarketAPI().pull_sold(
            f"{args.city}, {args.state} {args.zip_code}", months_back=args.months,
            houses_only=False)
    seen = set()
    for l in listings:
        if (l.sold_date >= cutoff and args.min_price <= l.price <= args.max_price
                and l.address and l.address not in seen):
            seen.add(l.address)
            rows.append((l.address, l.sold_date, l.price))
    return rows


def sweep(args) -> dict:
    from siftmap_api import SiftMapClient, SiftMapError

    client = SiftMapClient()
    targets = load_sold_addresses(args)
    logger.info("Sweeping %d sold properties in %s (band $%s-$%s, %dmo)",
                len(targets), args.zip_code, f"{args.min_price:,}", f"{args.max_price:,}", args.months)

    records, misses = [], 0
    for i, (addr, sold_date, price) in enumerate(targets, 1):
        try:
            cands = client.autocomplete(f"{addr}, {args.city}, {args.state}")
            house_no = addr.split()[0]
            best = next((c for c in cands if (c.get("address") or "").split()[0] == house_no), None)
            if not best:
                misses += 1
                continue
            detail = client.get_detail(best.get("dataflik_id") or best.get("id"))
            oi = detail.get("owner_info") or {}
            hist = detail.get("sale_history") or []
            latest = hist[0] if hist else {}
            records.append({
                "address": addr, "zillow_date": sold_date, "zillow_price": price,
                "buyer": latest.get("buyer_name") or oi.get("owner_name") or "",
                "sale_date": latest.get("sale_date"), "sale_price": latest.get("sale_price"),
                "cash": latest.get("is_cash_sale"),
                "owner_mail": oi.get("owner_mail_address"),
                "portfolio_n": oi.get("total_properties"),
                "portfolio_value": oi.get("portfolio_value"),
                "equity_avg": oi.get("total_equity_avg"),
            })
            if i % 25 == 0:
                logger.info("  %d/%d (%d misses)", i, len(targets), misses)
        except SiftMapError as e:
            misses += 1
            if "401" in str(e):
                raise
        except Exception:  # noqa: BLE001 - per-property resilience
            misses += 1

    buyers: dict[str, dict] = defaultdict(lambda: {"purchases": [], "cash_n": 0})
    for r in records:
        key = re.sub(r"[^A-Z0-9 ]", "", (r["buyer"] or "").upper()).strip()
        if not key:
            continue
        b = buyers[key]
        b["purchases"].append(r)
        if r.get("cash"):
            b["cash_n"] += 1
        b.update({k: r.get(k) for k in ("owner_mail", "portfolio_n", "portfolio_value", "equity_avg")})

    ranked = []
    for name, b in buyers.items():
        prices = [_num(p["sale_price"]) or p["zillow_price"] for p in b["purchases"]]
        ranked.append({
            "buyer": name,
            "is_entity": bool(ENTITY_RX.search(name)),
            "n_buys": len(b["purchases"]),
            "cash_n": b["cash_n"],
            "band_fit": sum(1 for p in prices if args.min_price <= p <= args.max_price),
            "avg_price": round(sum(prices) / len(prices)) if prices else 0,
            "portfolio_n": b.get("portfolio_n"),
            "portfolio_value": b.get("portfolio_value"),
            "equity_avg": b.get("equity_avg"),
            "owner_mail": b.get("owner_mail"),
            "principal": "", "principal_source": "",
            "buys": [(p["address"], p["sale_date"], p["sale_price"]) for p in b["purchases"]],
        })
    ranked.sort(key=lambda x: (x["n_buys"], x["band_fit"], x["portfolio_n"] or 0), reverse=True)

    if not args.no_unmask:
        _unmask_principals(client, ranked[:args.unmask_top], args)

    return {"records": records, "ranked": ranked,
            "misses": misses, "targets": len(targets)}


def _unmask_principals(client, ranked_entities: list[dict], args) -> None:
    """Resolve humans behind entity buyers: SiftMap reverse-address first
    (mailing address owner), Enformion BusinessV2 officers second."""
    for b in ranked_entities:
        if not b["is_entity"]:
            continue
        mail = b.get("owner_mail") or ""
        # 1. Reverse the mailing address: if a person owns that house, that is
        #    the principal (the Harper move).
        if mail and not mail.upper().startswith("PO BOX"):
            try:
                cands = client.autocomplete(mail)
                house_no = mail.split()[0]
                best = next((c for c in cands if (c.get("address") or "").split()[0] == house_no), None)
                if best:
                    oi = (client.get_detail(best.get("dataflik_id") or best.get("id"))
                          .get("owner_info") or {})
                    owner = oi.get("owner_name") or ""
                    if owner and not ENTITY_RX.search(owner):
                        b["principal"] = owner
                        secondary = oi.get("secondary_owner_names") or []
                        if secondary:
                            b["principal"] += " + " + ", ".join(secondary)
                        b["principal_source"] = "siftmap-reverse-address"
                        continue
            except Exception:  # noqa: BLE001
                pass
        # 2. Enformion BusinessV2 officers.
        try:
            from enformion_business import find_principals
            officers = find_principals(b["buyer"], f"{args.city}, {args.state}")
            if officers:
                b["principal"] = "; ".join(f"{o['name']} ({o['title']})" for o in officers[:2])
                b["principal_source"] = "enformion-businessv2"
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="SiftMap deed-level buyer sweep for a zip")
    ap.add_argument("--zip", dest="zip_code", required=True)
    ap.add_argument("--city", default="Knoxville")
    ap.add_argument("--state", default="TN")
    ap.add_argument("--months", type=int, default=18)
    ap.add_argument("--min-price", type=int, default=25000)
    ap.add_argument("--max-price", type=int, default=170000)
    ap.add_argument("--sold-json", help="Reuse a saved zillow pull instead of hitting the API")
    ap.add_argument("--unmask-top", type=int, default=15, help="Reverse/unmask top N entities")
    ap.add_argument("--no-unmask", action="store_true")
    ap.add_argument("--out", help="Output stem (default output/buyer_sweep_<zip>_<date>)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    result = sweep(args)

    stem = args.out or str(config.OUTPUT_DIR / f"buyer_sweep_{args.zip_code}_{datetime.now():%Y%m%d}")
    Path(stem).parent.mkdir(parents=True, exist_ok=True)
    json.dump(result, open(f"{stem}.json", "w", encoding="utf-8"), indent=1, default=str)
    with open(f"{stem}.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["buyer", "principal", "principal_source", "n_buys", "cash_n", "band_fit",
                    "avg_price", "portfolio_n", "portfolio_value", "equity_avg", "owner_mail", "recent_buys"])
        for b in result["ranked"]:
            w.writerow([b["buyer"], b["principal"], b["principal_source"], b["n_buys"], b["cash_n"],
                        b["band_fit"], b["avg_price"], b["portfolio_n"], b["portfolio_value"],
                        b["equity_avg"], b["owner_mail"],
                        " | ".join(f"{a} {d} ${_num(p):,.0f}" for a, d, p in b["buys"][:4])])

    print(f"resolved {len(result['records'])}/{result['targets']} sales "
          f"({result['misses']} misses) -> {len(result['ranked'])} unique buyers")
    print(f"wrote {stem}.json + .csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
