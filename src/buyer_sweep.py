"""SiftMap deed-level buyer sweep: who is ACTUALLY buying in a zip, at what
band, with what portfolio: the dispo-list engine built on the 158 Old State run.

Pipeline per zip:
  1. Sold universe from the Zillow /search band-partition pull (zillow_market_api)
     or a previously saved pull (--sold-json).
  2. Filter to the investor band (--min-price/--max-price) + lookback window.
  3. For each sale: SiftMap address search -> detail -> deed sale_history
     (buyer_name, is_cash_sale) + owner_info (portfolio, mailing).
  4. Aggregate by buyer; rank by purchase count, band fit, portfolio.
  5. UNMASK hidden principals: when a buyer entity's mailing address is a
     residence, reverse it through SiftMap owner_info; if that owner is a
     person, that is the principal (the Harper move, `reverse_address` below).
     Falls back to Enformion BusinessV2 officers, kept only when they are in the
     buyer's own state.

Usage:
  python src/buyer_sweep.py --zip 37914 --months 18
  python src/buyer_sweep.py --zip 37914 --sold-json output/zillow_37914_sold.json --out output/buyers_sweep_37914

Runs on the Open API key alone (REISIFT_API_KEY). It used to import the Deal Room
SiftMap client from a path on one desktop and to find each property through
SiftMap's autocomplete. The agents brain has neither that checkout nor the right
to autocomplete in volume (DataSift's Acceptable use names it, 2026-11), so both
calls now go through MapClient: the documented address search, then the detail.
Output: <out>.json (full) + <out>.csv (ranked buyer list).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import config  # noqa: E402
from agents.autopilot.sources import same_street  # noqa: E402
from enformion_business import _is_human, rank_officers  # noqa: E402

logger = logging.getLogger(__name__)

ENTITY_RX = re.compile(r"\bLLC\b|\bLLP\b|\bINC\b|\bTRUST\b|\bPROPERTIES\b|\bHOLDINGS\b|\bHOMES\b|\bCAPITAL\b|\bINVEST", re.I)
PO_BOX_RX = re.compile(r"\bP\.?\s?O\.?\s+BOX\b|\bPOST OFFICE BOX\b", re.I)
MAP_BASE = "https://map.reisift.io"


class MapError(RuntimeError):
    pass


class MapClient:
    """SiftMap over the Open API key: the documented address search and the property detail.

    Two things it deliberately does not do. It never calls the autocomplete route, which the
    published Acceptable use names as prohibited in volume (the search does the same job). And it
    never falls back to a token minted from a password when the key is refused: the brain has no
    password, a user's install never will, and a refused key is an error to read, not a reason to
    log in some other way.

    `opener` stands in for urllib.request.urlopen in tests; `min_interval` paces this client below
    the account's SiftMap limit (120 a minute, shared with every other key-based tool).
    """

    def __init__(self, api_key: str | None = None, base: str = MAP_BASE, min_interval: float = 0.75,
                 max_retries: int = 4, opener=None, sleep=time.sleep) -> None:
        self._key = api_key if api_key is not None else os.getenv("REISIFT_API_KEY", "")
        self._base = base.rstrip("/")
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep
        self._last = 0.0
        self.calls = 0

    def _request(self, path: str, method: str = "GET", body: dict | None = None, timeout: float = 45.0) -> dict:
        if not self._key:
            raise MapError("REISIFT_API_KEY is not set; SiftMap needs the Open API key")
        headers = {"accept": "application/json", "content-type": "application/json",
                   "authorization": "Api-Key " + self._key}
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(self._max_retries + 1):
            gap = time.time() - self._last
            if gap < self._min_interval:
                self._sleep(self._min_interval - gap)
            self._last = time.time()
            self.calls += 1
            req = urllib.request.Request(self._base + path, data=data, headers=headers, method=method)
            try:
                with self._open(req, timeout=timeout) as resp:
                    raw = resp.read()
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                txt = ""
                try:
                    txt = e.read().decode("utf-8", "replace")[:300]
                except Exception:  # noqa: BLE001
                    pass
                if e.code == 429 and attempt < self._max_retries:
                    wait = 5.0 * (attempt + 1)
                    ra = e.headers.get("Retry-After") if e.headers else None
                    try:
                        wait = float(ra) if ra else wait
                    except ValueError:
                        pass
                    self._sleep(min(wait, 90.0))
                    continue
                if e.code in (500, 502, 503, 504) and attempt < self._max_retries:
                    self._sleep(2.0 * (attempt + 1))
                    continue
                raise MapError("HTTP %s on %s %s: %s" % (e.code, method, path.split("?")[0], txt))
            except urllib.error.URLError as e:
                if attempt < self._max_retries:
                    self._sleep(2.0 * (attempt + 1))
                    continue
                raise MapError("network error on %s %s: %s" % (method, path.split("?")[0], e))
        raise MapError("exhausted retries on %s %s" % (method, path))

    def search(self, body: dict) -> dict:
        return self._request("/properties/search/", "POST", body) or {}

    def get_detail(self, property_id) -> dict:
        return self._request("/properties/detail/%s/" % property_id) or {}

    def find_address(self, address: str, size: int = 5) -> dict | None:
        """The one property at this street address, or None. The search is free text and returns
        near misses (the house next door, the same number on a parallel street), so a row counts
        only when its house number and street name are the address's own."""
        rows = (self.search({"addresses": [{"type": "address", "search": address}], "size": size})
                .get("data") or [])
        return match_row(rows, address)


def street_of(row_address: str) -> str:
    """'123 Main St, Knoxville, TN 37917' -> '123 Main St' (a search row carries one string). A
    semicolon ends the street too: Enformion writes '4300 Example Rd; Knoxville, TN', and cutting
    only at the comma glued the city onto the street (the split_mail lesson in dispo_buyers)."""
    return re.split(r"[,;]", row_address or "")[0].strip()


_UNIT_TAIL = re.compile(r"\s+(?:STE|SUITE|UNIT|APT|BLDG|FL|FLOOR|PMB|RM|#)\b.*$", re.I)


def drop_unit(address: str) -> str:
    """The unit taken off the STREET only: '9 Example Pkwy Ste 4, Knoxville, TN 37917' ->
    '9 Example Pkwy, Knoxville, TN 37917'. The registry's strip_unit cuts everything after the unit
    marker, city and ZIP included, and a search for a bare street line then matches that street in
    every county."""
    parts = re.split(r"([,;])", address or "", maxsplit=1)
    return _UNIT_TAIL.sub("", parts[0]).strip() + "".join(parts[1:])


def _zip_of(text: str) -> str:
    tail = (text or "")[len(street_of(text)):]
    m = re.findall(r"\b(\d{5})(?:-\d{4})?\b", tail)
    return m[-1] if m else ""


def _city_of(text: str) -> str:
    parts = [p.strip() for p in re.split(r"[,;]", text or "")]
    return parts[1].upper() if len(parts) > 2 else ""


def match_row(rows: list, address: str) -> dict | None:
    """The row at this street address. House number and street name must be the address's own, and
    when the address carries a ZIP (or else a city) the row must carry the same one: "123 Main St"
    exists in every county, and the owner of the wrong one is a stranger."""
    want = street_of(address) or address
    zip_, city = _zip_of(address), _city_of(address)
    for r in rows or []:
        if not (isinstance(r, dict) and r.get("id")):
            continue
        got = r.get("address") or ""
        if not same_street(want, street_of(got)):
            continue
        if zip_ and _zip_of(got) and _zip_of(got) != zip_:
            continue
        if not zip_ and city and _city_of(got) and _city_of(got) != city:
            continue
        return r
    return None


def _owner_of(oi: dict) -> str:
    raw = (oi.get("owner_name")
           or " ".join(p for p in [oi.get("first_name"), oi.get("last_name")] if p)
           or oi.get("name") or "")
    return re.sub(r"\s+", " ", str(raw)).strip()


def reverse_address(mail: str, client) -> dict:
    """The Harper move: who owns the house a company's mail goes to.

    An LLC's mailing address is usually somebody's home, and the person who owns that home is
    usually the person behind the LLC. It costs nothing (the map is already paid for), so it runs
    before the paid business search. Measured on the buyer registry: 1,173 principals named this way
    for $0, and they skip trace at 95% because the trace address is, by construction, their own.

    Returns {"principal", "source", "miss", "property_id", "address"}. A miss says why in words."""
    out = {"principal": "", "source": "", "miss": "", "property_id": "", "address": ""}
    mail = (mail or "").strip()
    if not mail:
        out["miss"] = "no mailing address"
        return out
    if PO_BOX_RX.search(mail):
        out["miss"] = "PO box mailing address"     # a PO box reverses to nothing; it is not a residence
        return out
    row = client.find_address(mail)
    if not row:
        out["miss"] = "no property at the mailing address"
        return out
    out["property_id"], out["address"] = str(row.get("id") or ""), str(row.get("address") or "")
    oi = (client.get_detail(row["id"]) or {}).get("owner_info") or {}
    owner = _owner_of(oi)
    if owner and _is_human(owner):
        out["principal"], out["source"] = owner, "siftmap-reverse-address"
        return out
    for s in oi.get("secondary_owner_names") or []:
        if _is_human(str(s)):
            out["principal"], out["source"] = str(s), "siftmap-secondary-owner"
            return out
    out["miss"] = ("the mailing address belongs to a company too" if owner
                   else "no owner on file at the mailing address")
    return out


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


def sweep(args, client: MapClient | None = None) -> dict:
    client = client or MapClient()
    targets = load_sold_addresses(args)
    logger.info("Sweeping %d sold properties in %s (band $%s-$%s, %dmo)",
                len(targets), args.zip_code, f"{args.min_price:,}", f"{args.max_price:,}", args.months)

    records, misses = [], 0
    for i, (addr, sold_date, price) in enumerate(targets, 1):
        try:
            best = client.find_address(f"{addr}, {args.city}, {args.state} {args.zip_code}")
            if not best:
                misses += 1
                continue
            detail = client.get_detail(best["id"])
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
        except MapError as e:
            misses += 1
            if "401" in str(e) or "not set" in str(e):
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
    (mailing address owner), Enformion BusinessV2 officers second.

    A BusinessV2 officer is named only when the officer is in the buyer's own state: the search
    matches company names nationally, and an officer of a same-named company two states away is a
    different person, not a weaker answer (measured: 2 of 12 entities had any Tennessee officer)."""
    for b in ranked_entities:
        if not b["is_entity"]:
            continue
        try:
            got = reverse_address(b.get("owner_mail") or "", client)
            if got["principal"]:
                b["principal"], b["principal_source"] = got["principal"], got["source"]
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            from enformion_business import find_principals
            officers = [o for o in find_principals(b["buyer"], f"{args.city}, {args.state}")
                        if _is_human(o.get("name") or "")]
            officers = rank_officers(b["buyer"], officers, args.state)
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
    ap.add_argument("--min-interval", type=float, default=0.75,
                    help="seconds between SiftMap calls (the account allows 120 a minute across every tool)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    result = sweep(args, MapClient(min_interval=args.min_interval))

    if result["targets"] and not result["records"]:
        logger.error("Resolved 0 of %d sales. That is an AUTH or COVERAGE failure, not an empty "
                     "market: check REISIFT_API_KEY before trusting this file.", result["targets"])

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
