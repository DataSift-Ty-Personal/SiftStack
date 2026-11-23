"""MLS offers on the published DataSift API, with the account's own Open API key.

The engine (mls_offers.py) prices every active listing in a market: a cash offer from what investors
pay nearby, a subject-to offer on a good-rate loan, seller finance on a free and clear house, and the
listing agent to send it to. This module feeds it from the DOCUMENTED routes a DataSift user's key
can call, inside DataSift's acceptable use (developers.datasift.ai):

  1. screen     SiftMap search, which is allowed in volume: every active single-family listing in the
                market with its list price, value, equity percent, size and place; days on market from
                the extra_days_on_market filter (within 30, 90, 365 days); distress flags from
                distressors_include.
  2. buyers     SiftMap search again, split into last-sale price bands with
                extra_last_sale_value_min/max: what investors paid (pending, rental, wholetail,
                wholesale) and what flips resold for, over 24 months. A search row carries no sale
                price, so each sale is priced at its band's midpoint ($10,000 bands under $300,000).
  3. shortlist  the best N listings on what the search shows.
  4. add        OPTIONAL, and the only step that spends the record allowance: the quote first, then the
                shortlisted listings not yet in the account are added to the list "MLS Offers" with the
                tag "MLS Listing". DataSift allows full property details in volume only for records in
                your own account, so a listing has to be added before its details are read.
  5. detail     SiftMap detail of every shortlisted listing in the account: the listing agent (name,
                office, phone), the listing history (price cuts, relists, days on market), the original
                loans (amount, record date, due date, type), year built, scores, distress flags.
  6. mortgage   each open loan modeled from public data (mortgage_model.py: HMDA actual rates by
                census tract, Freddie Mac weekly rates, the Census geocoder; free, no key).
Then the engine's own offers and export phases run unchanged. It sends nothing.

    python src/mls_offers.py --source api --county "Knox, TN" --phase all
    python src/mls_offers.py --source api --county "Knox, TN" --phase all --add --commit
    python src/mls_offers.py --source api --zips 37914,37917 --phase all --shortlist 40

Keys: REISIFT_API_KEY (the DataSift Open API key) is required; OPENWEBNINJA_API_KEY is optional
(the Zillow check: still for sale, remarks, broker phone, rent estimate; paid per call).
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import mls_offers as mo

logger = logging.getLogger("mls_offers")

HERE = Path(__file__).resolve().parent
COUNTY_FILES = (HERE / "data" / "us_counties.csv", HERE.parent / "data" / "us_counties.csv")
DATASIFT_API = "https://apiv2.reisift.io"

# The whole-world viewport: a search intersects its addresses with the map polygon.
WORLD = [{"lon": -17.89461189115002, "lat": 72.08452694723852},
         {"lon": -17.89461189115002, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": 72.08452694723852}]

LISTED = {"type_single_family": True, "extra_mls_status": ["listed"]}
DOM_WINDOWS = (30, 90, 365)
DOM_GUESS = {"0-30": 15, "31-90": 60, "91-365": 200, "over 365": 400}   # days, until the detail is read
SCREEN_FLAGS = ("is_vacant_property", "is_probate_properties", "is_pre_probate", "is_inheritance_property",
                "is_heirs_property", "is_notice_of_default", "is_lis_pendens", "is_notice_of_foreclosure",
                "is_recent_deliquent", "is_tired_landlord", "is_oos_owners", "is_divorce_property",
                "is_bankruptcy_property", "is_zombie_property", "is_estate_sales", "is_free_and_clear")
ACQ_KINDS = ["pending", "rental", "wholetail", "wholesale"]
BAND_STARTS = ((10_000, 100_000), (100_001, 200_000), (200_001, 300_000), (300_001, 500_000),
               (500_001, 1_000_000), (1_000_001, 5_000_000))
PAGE = 250
SEARCH_CAP = 9_000          # past a 10,000-row offset SiftMap reorders its pages; stay well under it
_RUN_LOCK = threading.Lock()

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}
_COUNTY_WORDS = re.compile(r"\b(county|parish|borough|census area|municipality|city and borough|planning region)\b")


# --------------------------------------------------------------------------------------------- market

def _norm(s: str) -> str:
    s = _COUNTY_WORDS.sub(" ", (s or "").lower().replace(".", "").replace("saint ", "st "))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def _state(s: str) -> str:
    t = (s or "").strip()
    if len(t) == 2 and t.isalpha():
        return t.upper()
    return STATE_NAMES.get(t.lower(), "")


def counties_table() -> list[dict]:
    for p in COUNTY_FILES:
        if p.exists():
            with open(p, encoding="utf-8") as fh:
                return list(csv.DictReader(fh))
    raise RuntimeError("The county table (data/us_counties.csv) is missing from this install.")


def resolve_market(county: str = "", fips: str = "", zips: str = "", name: str = "") -> dict:
    """A market from county names ("Knox, TN", several separated by ";"), FIPS codes or ZIP codes.
    Raises ValueError with a plain sentence when one cannot be found."""
    table = counties_table()
    by_fips = {r["fips"]: r for r in table}
    states = {r["state"] for r in table}
    found: list[dict] = []
    for f in [x.strip() for x in re.split(r"[,; ]+", fips or "") if x.strip()]:
        f = f.zfill(5)
        if f not in by_fips:
            raise ValueError(f"No US county has the FIPS code {f}.")
        found.append(by_fips[f])
    for spec in [x.strip() for x in (county or "").split(";") if x.strip()]:
        if "," not in spec:
            raise ValueError(f'Name the county with its state, like "Knox, TN" (got "{spec}").')
        cname, st_raw = [x.strip() for x in spec.rsplit(",", 1)]
        st = _state(st_raw)
        if st not in states:
            raise ValueError(f'"{st_raw}" is not a US state; use the two-letter code, like "Knox, TN".')
        key = _norm(cname)
        hits = [r for r in table if r["state"] == st and key in (_norm(r["name"]), _norm(r["county"]))]
        if not hits:
            raise ValueError(f"No county named {cname} in {st}.")
        found.append(hits[0])
    zs = []
    for z in [x.strip() for x in re.split(r"[,; ]+", zips or "") if x.strip()]:
        if not re.fullmatch(r"\d{5}", z):
            raise ValueError(f'"{z}" is not a five-digit ZIP code.')
        zs.append(z)
    seen, counties = set(), []
    for r in found:
        if r["fips"] not in seen:
            seen.add(r["fips"])
            counties.append({"fips": r["fips"], "name": r["name"], "state": r["state"]})
    if not counties and not zs:
        raise ValueError('Name a market: a county with its state ("Knox, TN"), FIPS codes, or ZIP codes.')
    slug = name or "-".join([f"{c['name']}-{c['state']}" for c in counties] + (["zips"] + zs[:3] if zs else []))
    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")[:60] or "market"
    return {"name": slug, "counties": counties, "zips": zs, "fips": [c["fips"] for c in counties]}


def county_address(c: dict) -> dict:
    """SiftMap's county address: the rich object plus the thin search pair, `search` the BARE name
    ("Knox"; "Knox County, TN" finds nothing and reads like an empty market)."""
    title = f"{c['name']} County, {c['state']}"
    return {"state": c["state"], "title": title, "value": title, "county": c["name"], "searchType": "county",
            "counties": [{"fips": c["fips"], "county_name": c["name"]}], "search": c["name"], "type": "county"}


def addresses(market: dict) -> list[dict]:
    return [county_address(c) for c in market.get("counties") or []] + \
           [{"search": z, "type": "zip"} for z in market.get("zips") or []]


def label(addr: dict) -> str:
    return addr.get("title") or ("ZIP " + str(addr.get("search")))


# --------------------------------------------------------------------------------------------- search

def make_client(api_key: str = ""):
    from siftmap_standalone import SiftMapClient
    key = (api_key or os.environ.get("REISIFT_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("REISIFT_API_KEY is not set. Put your DataSift Open API key in .env or the "
                           "environment (developers.datasift.ai explains how an account admin creates one).")
    # 0.75 s apart is 80 calls a minute: the pace DataSift asks a key-based tool to sustain. The limit itself
    # (120 a minute, 5,000 an hour) is shared by every tool on the account, and a 429 is retried after its wait.
    return SiftMapClient(key, min_interval=0.75)


def _search(client, addr: dict, filters: dict, index: int = 1, size: int = PAGE) -> dict:
    return client.search({"result_index": index, "size": size, "with_boundaries": False, "filters": filters,
                          "addresses": [addr], "polygon": WORLD}) or {}


def search_all(client, addr: dict, filters: dict, lo: Optional[int] = None, hi: Optional[int] = None,
               notes: Optional[list] = None) -> list[dict]:
    """Every row of one search. result_index is a ROW OFFSET (1, 251, 501, ...); past about 10,000 rows
    the server reorders its pages, so a bigger search is split on the estimated value and each half paged."""
    f = dict(filters)
    if lo is not None:
        f["value_min"] = lo
    if hi is not None:
        f["value_max"] = hi
    first = _search(client, addr, f)
    total = int(first.get("total_results") or 0)
    if total > SEARCH_CAP:
        a, b = (lo or 0), (hi or 50_000_000)
        if b - a < 2:
            raise RuntimeError(f"{label(addr)}: one value window holds over {SEARCH_CAP:,} rows")
        if notes is not None and lo is None and hi is None:
            notes.append(f"{label(addr)} has {total:,} rows, more than one search pages; it was split by "
                         "estimated value, which leaves out rows with no value.")
        mid = (a + b) // 2
        return search_all(client, addr, filters, a, mid) + search_all(client, addr, filters, mid + 1, b)
    rows = list(first.get("data") or [])
    idx = 1 + PAGE
    while len(rows) < total:
        page = _search(client, addr, f, idx).get("data") or []
        if not page:
            break
        rows.extend(page)
        idx += PAGE
    return rows


def ids_of(client, market: dict, filters: dict) -> set:
    out = set()
    for addr in addresses(market):
        out |= {str(r.get("id")) for r in search_all(client, addr, filters) if r.get("id") is not None}
    return out


def split_address(text: str) -> tuple[str, str, str, str]:
    """"1133 Example Ave, Knoxville, TN 37917" -> (street, city, state, zip)."""
    parts = [p.strip() for p in str(text or "").split(",")]
    street = parts[0] if parts else ""
    city = parts[1] if len(parts) > 2 else ""
    tail = parts[-1] if len(parts) > 1 else ""
    m = re.match(r"([A-Za-z]{2})\s*(\d{5})?", tail)
    return street, city, (m.group(1).upper() if m else ""), (m.group(2) or "" if m else "")


def bucket_of(i: str, dom_sets: dict) -> str:
    if i in dom_sets.get(30, set()):
        return "0-30"
    if i in dom_sets.get(90, set()):
        return "31-90"
    if i in dom_sets.get(365, set()):
        return "91-365"
    return "over 365"


def slim_search_row(r: dict, dom_sets: dict, flag_sets: dict) -> dict:
    i = str(r.get("id"))
    street, city, state, zip5 = split_address(r.get("address"))
    b = bucket_of(i, dom_sets)
    return {
        "id": i, "fips": r.get("fips"), "address": street, "city": city.title(), "state": state or r.get("state") or "",
        "zip": zip5, "value": r.get("estimatedValue"), "list_price": r.get("mlsListingPrice"),
        "sqft": r.get("squareFeet"), "beds": r.get("bedrooms"), "baths": r.get("bathrooms"),
        "lot_acres": r.get("lotAcres"), "lat": r.get("latitude"), "lon": r.get("longitude"),
        "corporate": bool(r.get("corporateOwned")), "absentee": bool(r.get("absenteeOwner")),
        "equity_pct": r.get("equityPercent"), "saved": bool(r.get("saved")), "saved_uuid": r.get("saved_uuid"),
        "dom_bucket": b, "dom": DOM_GUESS[b], "flags": sorted(k for k, s in flag_sets.items() if i in s),
        "has_address": bool(re.match(r"^[1-9]", street)), "owner": "", "trust": False, "year": None,
        "tax": None, "rent": None, "loans": [], "total_balance": 0, "total_payment": 0, "agent": "",
        "agent_phone": "", "office": "", "detailed": False, "source": "api",
    }


# --------------------------------------------------------------------------------------------- 1. screen

def phase_screen(market: dict, pol, client, out: Path, notes: list) -> dict:
    """Every active single-family listing in the market, from search alone."""
    base = dict(LISTED, extra_mls_listing_price_max=pol.max_list)
    raw: dict[str, dict] = {}
    for addr in addresses(market):
        got = search_all(client, addr, base, notes=notes)
        if not got:
            notes.append(f"SiftMap shows no active single-family listings under ${pol.max_list:,} in {label(addr)}.")
        for r in got:
            if r.get("id") is not None:
                raw[str(r["id"])] = r
    dom_sets = {w: ids_of(client, market, dict(base, extra_days_on_market=f"{w}d")) for w in DOM_WINDOWS}
    flag_sets = {k: ids_of(client, market, dict(base, distressors_include=k, distressors_include_min_match=1))
                 for k in SCREEN_FLAGS}
    rows = [slim_search_row(r, dom_sets, flag_sets) for r in raw.values()]
    doc = {"pulled": str(date.today()), "source": "api", "market": market, "rows": rows,
           "counts": {"listings": len(rows), "in_account": sum(1 for r in rows if r["saved"]),
                      **{f"dom_{k}": sum(1 for r in rows if r["dom_bucket"] == k) for k in DOM_GUESS},
                      **{f: len(s) for f, s in flag_sets.items() if s}}}
    mo.save_json(out / "listings.json", doc)
    logger.info("screen: %d active single-family listings, %d already in the account", len(rows), doc["counts"]["in_account"])
    return doc


# --------------------------------------------------------------------------------------------- 2. buyers

def leaf_width(lo: int) -> int:
    return 10_000 if lo < 300_000 else 25_000 if lo < 1_000_000 else 250_000


def banded(client, addr: dict, filters: dict, lo: int, hi: int, out: list) -> None:
    """Rows of one last-sale price band, split until each band is narrow enough to price its sales."""
    f = dict(filters, extra_last_sale_value_min=lo, extra_last_sale_value_max=hi)
    first = _search(client, addr, f)
    total = int(first.get("total_results") or 0)
    if total == 0:
        return
    # Split down to the leaf width even for one sale: a lone sale priced at the middle of a $500,000 band
    # would be off by up to $250,000 and drag a neighbor's median.
    if hi - lo + 1 > leaf_width(lo):
        mid = (lo + hi) // 2
        banded(client, addr, filters, lo, mid, out)
        banded(client, addr, filters, mid + 1, hi, out)
        return
    rows = list(first.get("data") or [])
    idx = 1 + PAGE
    while len(rows) < min(total, SEARCH_CAP):
        page = _search(client, addr, f, idx).get("data") or []
        if not page:
            break
        rows.extend(page)
        idx += PAGE
    out.extend((r, lo, hi) for r in rows)


def slim_evidence(r: dict, lo: int, hi: int, kind: str) -> Optional[dict]:
    """One investor sale priced at its band's midpoint, or None when it cannot price a house: no place or
    size, or a deed at twice the parcel's value or more (a portfolio deed that carried other parcels)."""
    lat, lon, sqft, value = r.get("latitude"), r.get("longitude"), r.get("squareFeet"), r.get("estimatedValue")
    if lat is None or lon is None or not sqft or sqft < 400:
        return None
    price = round((lo + hi) / 2)
    if value and price >= 2 * value:
        return None
    psf = price / sqft
    if psf < 10 or psf > 600:
        return None
    street, _city, _st, zip5 = split_address(r.get("address"))
    return {"id": str(r.get("id")), "lat": lat, "lon": lon, "price": price, "psf": round(psf, 1), "type": kind,
            "sqft": sqft, "value": value, "band": [lo, hi], "buyer": "", "buyer_key": None,
            "address": street, "zip": zip5}


def phase_buyers(market: dict, client, out: Path, months: int = 24) -> dict:
    since = (date.today() - timedelta(days=int(months * 30.4))).isoformat()
    base = {"type_single_family": True, "extra_last_sale_date_min": since}
    rows: dict[str, dict] = {}
    seen = 0
    for kind, types in (("Investor", ACQ_KINDS), ("Flip", ["flip"])):
        f = dict(base, extra_last_sale_investor_transaction_type=types)
        for addr in addresses(market):
            got: list = []
            for lo, hi in BAND_STARTS:
                banded(client, addr, f, lo, hi, got)
            seen += len(got)
            for r, lo, hi in got:
                e = slim_evidence(r, lo, hi, kind)
                if e:
                    rows[e["id"] + ":" + kind] = e
    ev = list(rows.values())
    doc = {"read": str(date.today()), "months": months, "sales": seen, "kept": len(ev), "tied": 0, "rows": ev,
           "source": "api", "price_note": "each sale is priced at the midpoint of its last-sale price band"}
    mo.save_json(out / "buyer_evidence.json", doc)
    logger.info("buyers: %d investor sales and flip exits in %d months, %d usable", seen, months, len(ev))
    return doc


# --------------------------------------------------------------------------------------------- 3. shortlist

def preliminary(r: dict, pol, ev: list, today: date) -> dict:
    """Score, buyer market and cash offer from what the search shows, before any detail is read."""
    h = mo.summarize_history([], r.get("list_price"), today)
    h["dom"] = r.get("dom")
    score, why = mo.motivation(r, h, None, pol)
    bm = mo.buyer_market(r, ev, set()) if ev else {}
    c = mo.cash_offer(r, score, pol, bm)
    eq = r.get("equity_pct")
    creative = (eq is not None and eq <= 60) or "is_free_and_clear" in (r.get("flags") or [])
    return {"score": score, "why": "; ".join(why), "cash_prelim": c.get("cash_offer"),
            "watch_prelim": c.get("watch_offer"), "creative_potential": creative,
            "potential": bool(c.get("cash_offer") or c.get("watch_offer")) or creative}


def phase_shortlist(pol, out: Path, n: int = 60, today: Optional[date] = None) -> dict:
    today = today or date.today()
    data = mo.load_json(out / "listings.json")
    if not data:
        raise RuntimeError("run the screen phase first")
    ev = (mo.load_json(out / "buyer_evidence.json") or {}).get("rows") or []
    cands = []
    for r in data["rows"]:
        if mo.screen(r, pol, None, today):
            continue
        cands.append({**r, **preliminary(r, pol, ev, today)})
    cands.sort(key=lambda x: (not x["potential"], -x["score"]))
    top = cands[:max(0, n)]
    picked = {r["id"] for r in top}
    # Listings already in the account cost nothing to read, so a few more of those ride along.
    extra = [r for r in cands[n:] if r["saved"] and r["potential"]][:max(0, n)]
    rows = top + extra
    doc = {"built": str(today), "n": n, "ids": [r["id"] for r in rows],
           "rows": [{k: r.get(k) for k in ("id", "address", "city", "list_price", "score", "why", "cash_prelim",
                                           "watch_prelim", "creative_potential", "saved", "saved_uuid")} for r in rows],
           "candidates": len(cands), "extra_in_account": len(extra)}
    mo.save_json(out / "shortlist.json", doc)
    logger.info("shortlist: %d of %d candidates (%d more already in the account)", len(top), len(cands), len(extra))
    return doc


# --------------------------------------------------------------------------------------------- 4. add

def add_body(ids: list, list_name: str, tag: str) -> dict:
    """add-properties-by-query with an ids-typed address list (the route verified on 30,000 records); the
    quote route is asked about exactly the same body, so a quote and the add it prices never differ."""
    return {"auto_add_enabled": False, "lists": [list_name], "tags": [tag], "replace_owners": False,
            "query": {"result_index": 1, "with_boundaries": False, "filters": {},
                      "addresses": [{"search": str(i), "type": "ids"} for i in ids], "polygon": WORLD}}


def key_owner(api_key: str = "") -> str:
    """The email of the user the Open API key belongs to (GET /api/internal/user/, documented)."""
    key = (api_key or os.environ.get("REISIFT_API_KEY") or "").strip()
    req = urllib.request.Request(DATASIFT_API + "/api/internal/user/",
                                 headers={"Authorization": "Api-Key " + key, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            js = json.loads(r.read() or b"{}")
        return str((js or {}).get("email") or "")
    except (urllib.error.URLError, ValueError, OSError):
        return ""


def search_by_ids(client, ids: list) -> list:
    body = {"result_index": 1, "size": max(1, len(ids)), "with_boundaries": False, "filters": {},
            "addresses": [{"search": str(i), "type": "ids"} for i in ids], "polygon": WORLD}
    return client.search(body).get("data") or []


def poll_saved(client, ids: list, wait_min: float, every: float = 20.0) -> dict:
    """id -> saved_uuid for whichever ids show as saved before the deadline (adds land asynchronously)."""
    deadline = time.time() + wait_min * 60
    landed, pending = {}, [str(i) for i in ids]
    while pending:
        for k in range(0, len(pending), 100):
            for h in search_by_ids(client, pending[k:k + 100]):
                if h.get("saved") and h.get("saved_uuid"):
                    landed[str(h.get("id"))] = h["saved_uuid"]
        pending = [i for i in pending if i not in landed]
        if not pending or time.time() > deadline:
            break
        time.sleep(every)
    return landed


def phase_add(client, out: Path, *, commit: bool = False, list_name: str = "MLS Offers", tag: str = "MLS Listing",
              expect_email: str = "", wait_min: float = 10.0, max_add: int = 500, notes: Optional[list] = None) -> dict:
    """Quote, then (only with commit) add the shortlisted listings not yet in the account."""
    notes = notes if notes is not None else []
    sl = mo.load_json(out / "shortlist.json") or {}
    data = mo.load_json(out / "listings.json") or {}
    rows = {r["id"]: r for r in data.get("rows") or []}
    todo = [i for i in sl.get("ids") or [] if i in rows and not rows[i].get("saved")][:max_add]
    res = {"to_add": len(todo), "quote": None, "added": 0, "pending": 0, "committed": bool(commit)}
    if not todo:
        return res
    quote = client._request("/properties/add-properties-by-query/quote/", "POST", add_body(todo, list_name, tag))
    res["quote"] = quote
    usage = (quote or {}).get("property_usage") or {}
    logger.info("add quote: %s new, %s billable, cost %s, allowance left %s", (quote or {}).get("new"),
                (quote or {}).get("billable"), (quote or {}).get("cost"), usage.get("allowance_left"))
    if not commit:
        notes.append(f"{len(todo)} shortlisted listings are not in your account; adding them would use "
                     f"{(quote or {}).get('billable', len(todo))} records of your allowance. Re-run with --add --commit to add them.")
        return res
    owner = key_owner()
    if expect_email and owner.lower() != expect_email.strip().lower():
        raise RuntimeError(f"The API key belongs to {owner or 'an unknown user'}, not {expect_email}; nothing was added.")
    if float((quote or {}).get("shortfall") or 0) > 0 or int((quote or {}).get("blocked_count") or 0) > 0:
        raise RuntimeError("The quote shows a shortfall or blocked records; nothing was added. "
                           "Check the record allowance in DataSift.")
    for k in range(0, len(todo), 250):
        client._request("/properties/add-properties-by-query/", "POST", add_body(todo[k:k + 250], list_name, tag))
    landed = poll_saved(client, todo, wait_min)
    for i, u in landed.items():
        rows[i]["saved"], rows[i]["saved_uuid"] = True, u
    mo.save_json(out / "listings.json", data)
    res.update(added=len(landed), pending=len(todo) - len(landed), account=owner)
    if res["pending"]:
        notes.append(f"{res['pending']} added listings had not landed after {wait_min:g} minutes; "
                     "run the detail phase again later to read them.")
    logger.info("add: %d added to %r for %s, %d still landing", len(landed), list_name, owner or "the key's account",
                res["pending"])
    return res


# --------------------------------------------------------------------------------------------- rent

RENT_STARTS = ((0, 1_499), (1_500, 2_999), (3_000, 5_999), (6_000, 50_000))


def rent_leaf(lo: int) -> int:
    return 100 if lo < 3_000 else 250 if lo < 6_000 else 1_000


def rent_bands(client, ids: list, lo: int, hi: int, out: dict) -> None:
    """Which of `ids` carry an estimated rent inside [lo, hi], split until the band is a leaf. Neither a search
    row nor a detail carries the rent itself, but the extra_suggested_rent filter does (proven by count)."""
    if not ids:
        return
    f = {"extra_suggested_rent_min": lo, "extra_suggested_rent_max": hi}
    body = {"result_index": 1, "size": PAGE, "with_boundaries": False, "filters": f,
            "addresses": [{"search": str(i), "type": "ids"} for i in ids], "polygon": WORLD}
    r = client.search(body) or {}
    hits = [str(x.get("id")) for x in r.get("data") or []]
    if not hits:
        return
    if hi - lo + 1 > rent_leaf(lo):
        mid = (lo + hi) // 2
        rent_bands(client, hits, lo, mid, out)
        rent_bands(client, hits, mid + 1, hi, out)
        return
    for i in hits:
        out[i] = round((lo + hi) / 2)


def phase_rent(client, out: Path, ids: list) -> dict:
    """The estimated monthly rent of each listed id, within $50 under $3,000 ($125 above)."""
    rents: dict[str, int] = {}
    for k in range(0, len(ids), PAGE):
        chunk = [str(i) for i in ids[k:k + PAGE]]
        for lo, hi in RENT_STARTS:
            rent_bands(client, chunk, lo, hi, rents)
    data = mo.load_json(out / "listings.json") or {}
    for r in data.get("rows") or []:
        if r["id"] in rents and not r.get("rent"):
            r["rent"] = rents[r["id"]]
    mo.save_json(out / "listings.json", data)
    logger.info("rent: an estimate for %d of %d listings", len(rents), len(ids))
    return rents


# --------------------------------------------------------------------------------------------- 5. detail

def _date(s) -> Optional[date]:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _ms(d: Optional[date]) -> Optional[int]:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000) if d else None


def slim_detail(d: dict) -> dict:
    agent = d.get("mls_agent") or {}
    oi = d.get("owner_info") or {}
    flags = [k for k, v in (d.get("distressor_data") or {}).items() if isinstance(v, dict) and v.get("is_active")]
    return {
        "agent": agent.get("agent_name") or "", "office": agent.get("agent_office") or "",
        "agent_phone": mo.fmt_phone(agent.get("agent_phone")), "dom": d.get("mls_days_on_market"),
        "list_price": d.get("mls_listing_price"),
        "mls_history": [{k: h.get(k) for k in ("status_date", "status", "price", "days_listed", "agent_name")}
                        for h in d.get("mls_history") or [] if isinstance(h, dict)],
        "mortgage_history": [{k: m.get(k) for k in ("record_date", "loan_type", "amount", "due_date", "lender_name")}
                             for m in d.get("mortgage_history") or [] if isinstance(m, dict)],
        "sale_history": [{k: s.get(k) for k in ("sale_date", "sale_price", "is_cash_sale")}
                         for s in (d.get("sale_history") or [])[:6] if isinstance(s, dict)],
        "year": d.get("years_built"), "tax": d.get("tax_amount"), "ai_off": d.get("investor_off_market_score"),
        "ai_on": d.get("investor_on_market_score"), "realtor": d.get("realtor_score"),
        "owner": oi.get("owner_name") or "", "owner_2": "; ".join(str(x) for x in oi.get("secondary_owner_names") or []),
        "fips": d.get("fips"), "flags": sorted(flags), "last_sale_price": d.get("last_sale_price"),
        "sqft": d.get("living_square_feet") or d.get("building_square_feet"),
    }


def history_rows(mls_history: list) -> list[dict]:
    """The detail's listing events in the shape mls_offers.summarize_history reads."""
    out = []
    for i, h in enumerate(mls_history or []):
        sd = _date(h.get("status_date"))
        if not sd:
            continue
        dl = h.get("days_listed")
        start = sd - timedelta(days=int(dl)) if isinstance(dl, (int, float)) and dl >= 0 else None
        out.append({"recording_date": _ms(sd), "listing_date": _ms(start), "status": h.get("status"),
                    "sale_price": h.get("price"), "agent_name": h.get("agent_name"),
                    "listing_update_id": f"{sd.isoformat()}-{i:03d}"})
    return out


def phase_detail(client, out: Path, cap: int = 300, notes: Optional[list] = None, today: Optional[date] = None) -> dict:
    """SiftMap detail of every shortlisted listing that is in the account (and only those)."""
    notes = notes if notes is not None else []
    today = today or date.today()
    sl = mo.load_json(out / "shortlist.json") or {}
    data = mo.load_json(out / "listings.json") or {}
    rows = {r["id"]: r for r in data.get("rows") or []}
    details = mo.load_json(out / "details.json", {}) or {}
    read = skipped = failed = 0
    for i in (sl.get("ids") or [])[:cap]:
        r = rows.get(i)
        if not r:
            continue
        if not r.get("saved"):
            skipped += 1
            continue
        if i not in details:
            try:
                details[i] = slim_detail(client.get_detail(i) or {})
                read += 1
            except Exception as exc:  # noqa: BLE001 - one unreadable listing never stops the run
                failed += 1
                logger.warning("detail %s: %s", i, str(exc)[:160])
                continue
    hist = {}
    for i, d in details.items():
        r = rows.get(i)
        if not r:
            continue
        hrows = history_rows(d.get("mls_history"))
        s = mo.summarize_history(hrows, d.get("list_price") or r.get("list_price"), today)
        if s.get("dom") is None and d.get("dom") is not None:
            s["dom"] = d["dom"]
        hist[i] = s
        r.update({k: d[k] for k in ("agent", "office", "agent_phone", "owner", "owner_2", "year", "tax", "ai_off",
                                    "ai_on", "realtor") if d.get(k) not in (None, "")})
        r["flags"] = sorted(set(r.get("flags") or []) | set(d.get("flags") or []))
        if d.get("list_price"):
            r["list_price"] = d["list_price"]
        if not r.get("sqft") and d.get("sqft"):
            r["sqft"] = d["sqft"]
        r["dom"] = s.get("dom") or r.get("dom")
        r["listed_since"] = s.get("run_start") or ""
        r["detailed"] = True
    mo.save_json(out / "details.json", details)
    mo.save_json(out / "history.json", {"read": str(today), "source": "api", "summary": hist})
    mo.save_json(out / "listings.json", data)
    if skipped:
        notes.append(f"{skipped} shortlisted listings are not in your account, so their details were not read; "
                     "add them with --add --commit, or work them from the More listings sheet.")
    if failed:
        notes.append(f"{failed} listing details could not be read; run the detail phase again.")
    logger.info("detail: %d read this run, %d not in the account, %d failed", read, skipped, failed)
    return {"read": read, "skipped": skipped, "failed": failed, "detailed": len(hist)}


# --------------------------------------------------------------------------------------------- 6. mortgage

def loan_kind(raw: str) -> str:
    t = (raw or "").lower()
    if any(w in t for w in ("credit line", "line of credit", "heloc", "revolving")):
        return "Credit Line"
    if "construction" in t:
        return "Construction"
    if "commercial" in t:
        return "Commercial"
    if re.search(r"\bfha\b", t):
        return "FHA"
    if re.search(r"\bva\b", t) or "veteran" in t:
        return "VA"
    if any(w in t for w in ("usda", "rural", "farmers home")):
        return "USDA"
    if any(w in t for w in ("seller", "private", "land contract")):
        return "Other"
    return "Conventional"


def open_loans(mortgage_history: list, sale_history: list, today: Optional[date] = None) -> list[dict]:
    """The loans still open on a house, judged from its recorded loans: those recorded since the owner bought
    it, not yet due; the newest term loan replaces older ones (a refinance pays them off), a second recorded
    the same week stays with it; credit lines stay when recorded with or after it; construction only until due."""
    today = today or date.today()
    bought = _date((sale_history or [{}])[0].get("sale_date")) if sale_history else None
    loans = []
    for m in mortgage_history or []:
        rec, due, amt = _date(m.get("record_date")), _date(m.get("due_date")), m.get("amount")
        if not rec or not amt or amt <= 0:
            continue
        if bought and rec < bought - timedelta(days=10):
            continue
        if due and due <= today:
            continue
        loans.append({"recorded": rec, "due": due, "amount": float(amt), "kind": loan_kind(m.get("loan_type")),
                      "raw_type": m.get("loan_type") or "", "lender": m.get("lender_name") or ""})
    term = [l for l in loans if l["kind"].lower() not in mo.SHORT_LIENS]
    keep = []
    if term:
        newest = max(l["recorded"] for l in term)
        keep = [l for l in term if (newest - l["recorded"]).days <= 10]
        first = min(l["recorded"] for l in keep)
        keep += [l for l in loans if l["kind"] == "Credit Line" and l["recorded"] >= first - timedelta(days=10)]
    else:
        keep = [l for l in loans if l["kind"] == "Credit Line"]
    keep += [l for l in loans if l["kind"] in ("Construction", "Commercial")]
    return sorted(keep, key=lambda l: -l["amount"])


def phase_mortgage(market: dict, out: Path, today: Optional[date] = None, workers: int = 4) -> dict:
    """Each open loan on the detailed listings modeled from public data. Without an index estimate to fall
    back on, a loan with no HMDA match is priced at Freddie Mac's rate in its lock week plus a spread
    calibrated on the HMDA matches themselves."""
    import mortgage_model as mm
    from concurrent.futures import ThreadPoolExecutor
    today = today or date.today()
    details = mo.load_json(out / "details.json", {}) or {}
    data = mo.load_json(out / "listings.json") or {}
    rows = {r["id"]: r for r in data.get("rows") or []}
    jobs = []
    for i, d in details.items():
        r = rows.get(i)
        if not r:
            continue
        loans = open_loans(d.get("mortgage_history"), d.get("sale_history"), today)
        r["loans"] = [{"type": l["kind"], "rate": None, "balance": l["amount"], "payment": None,
                       "recorded": l["recorded"].isoformat(), "private": l["kind"] == "Other"} for l in loans]
        r["total_balance"] = sum(l["amount"] for l in loans)
        if loans and r.get("lat") is not None:
            jobs.append((i, r, loans))
    if not jobs:
        mo.save_json(out / "listings.json", data)
        doc = {"built": str(today), "source": "api", "stats": {"loans": 0, "modeled": 0}, "loans": {}}
        mo.save_json(out / "mortgage_model.json", doc)
        return doc
    fips = sorted({str(c) for c in market.get("fips") or []} | {str(d.get("fips")) for d in details.values() if d.get("fips")})
    mm.fetch_pmms()
    for c in fips:
        for y in mm.HMDA_YEARS:
            try:
                mm.fetch_hmda(c, y)
            except (SystemExit, Exception) as exc:  # noqa: BLE001 - a missing year only loses matches
                logger.warning("HMDA %s %s: %s", c, y, str(exc)[:120])
    model = mm.Model(fips, mm.Rates(mm.fetch_pmms()))
    tcache_p = mm.CACHE / "tracts.json"
    tcache = mo.load_json(tcache_p, {}) or {}
    pts = [(r["lat"], r["lon"]) for _, r, _ in jobs if f"{r['lat']:.6f},{r['lon']:.6f}" not in tcache]
    if pts:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda p: mm.tract_ids(p[0], p[1], tcache), pts))
        mo.save_json(tcache_p, tcache)
    matches = []
    for _i, r, loans in jobs:
        tr = tcache.get(f"{r['lat']:.6f},{r['lon']:.6f}", {})
        for l in loans:
            if l["kind"].lower() in mm.UNMODELED:
                continue
            m1 = model.loan(l["amount"], l["recorded"], l["due"], l["kind"], tr, today=today)
            if m1 and m1["rate_source"] == "HMDA match":
                matches.append({"recorded": l["recorded"], "typ": mm.TYPE_CODE.get(l["kind"].lower(), 1),
                                "term": m1["term_months"], "rate": m1["rate"], "index_rate": None})
    check = model.choose_fallback(matches) if matches else {"chosen": "pmms", "note": "no HMDA matches to calibrate on"}
    modeled, stats = {}, {"loans": 0, "modeled": 0, "hmda_match": 0, "hmda_close": 0, "fallback": 0}
    for i, r, loans in jobs:
        tr = tcache.get(f"{r['lat']:.6f},{r['lon']:.6f}", {})
        for l in loans:
            stats["loans"] += 1
            rec = {"recorded": l["recorded"].isoformat(), "type": l["kind"], "index_rate": None,
                   "index_balance": None, "index_payment": None, "lender": l["lender"],
                   "due": l["due"].isoformat() if l["due"] else ""}
            m2 = None if l["kind"].lower() in mm.UNMODELED else model.loan(l["amount"], l["recorded"], l["due"],
                                                                              l["kind"], tr, today=today)
            if m2:
                stats["modeled"] += 1
                src = m2["rate_source"]
                stats["hmda_match" if src == "HMDA match" else "hmda_close" if src.startswith("HMDA match") else "fallback"] += 1
                rec.update(m2)
            modeled.setdefault(i, []).append(rec)
    d_, r30 = model.rates.latest()
    doc = {"built": str(today), "source": "api", "market_rate": {"date": str(d_), "rate30": r30},
           "hmda_loans": model.hmda_loans, "stats": stats, "check": check,
           "calibrated_spreads": {str(k): v for k, v in model.dated.items()}, "loans": modeled}
    mo.save_json(out / "mortgage_model.json", doc)
    mo.save_json(out / "listings.json", data)
    logger.info("mortgage: %s", stats)
    return doc


# --------------------------------------------------------------------------------------------- the whole run

def _top(offers: dict, n: int = 10) -> list[dict]:
    out = []
    for r in (offers or {}).get("rows", [])[:n]:
        out.append({"address": r.get("address"), "city": r.get("city"), "list_price": r.get("list_price"),
                    "cash_offer": r.get("cash_offer"), "lane": r.get("lane") or ("Cash" if r.get("cash_offer") else ""),
                    "creative_price": r.get("creative_price"), "agent": r.get("agent"),
                    "agent_phone": r.get("agent_phone"), "score": r.get("score"), "why": r.get("why")})
    return out


def run(market: dict, *, out: Optional[Path] = None, shortlist: int = 60, add: bool = False, commit: bool = False,
        zillow: bool = False, zillow_limit: int = 0, months: int = 24, policy: Optional[dict] = None,
        list_name: str = "MLS Offers", tag: str = "MLS Listing", client=None, log: Optional[Callable] = None,
        sender: str = "", expect_email: str = "", today: Optional[date] = None) -> dict:
    """One full run for a market (resolve_market's dict). Writes every artifact into `out`
    (default output/mls_offers/<market name>/) and returns the workbook, the CSV, counts and notes.
    Adds records only when add AND commit; reads Zillow only when zillow (paid per call)."""
    log = log or (lambda msg: logger.info("%s", msg))
    sender = sender or mo.default_sender()           # MLS_OFFERS_SENDER, as on the command line
    zillow_calls = 0
    out = Path(out) if out else mo.OUT / market["name"]
    out.mkdir(parents=True, exist_ok=True)
    pol = mo.Policy(**(policy or {}))
    client = client or make_client()
    notes: list[str] = []
    with _RUN_LOCK:
        saved_out = mo.OUT
        mo.OUT = out.parent
        key = out.name
        try:
            log("Screening active listings")
            scr = phase_screen(market, pol, client, out, notes)
            log("Pricing what investors paid nearby")
            bys = phase_buyers(market, client, out, months)
            sl = phase_shortlist(pol, out, shortlist, today)
            # Always quote (free, nothing stored): the reply says what adding would use. Add only on both flags.
            log("Quoting the add" + (" and adding the shortlist" if add and commit else ""))
            addres = phase_add(client, out, commit=bool(add and commit), list_name=list_name, tag=tag,
                               expect_email=expect_email, notes=notes)
            log("Reading listing details")
            det = phase_detail(client, out, notes=notes, today=today)
            phase_rent(client, out, sl.get("ids") or [])
            log("Modeling the sellers' loans")
            mmod = phase_mortgage(market, out, today) if det["detailed"] else {"stats": {}}
            offers = mo.phase_offers(key, pol, today)
            if zillow and zillow_limit:
                log("Checking the offers on Zillow")
                mo.phase_zillow(key, zillow_limit)
                zillow_calls = mo.ZILLOW_READS["last"]
                offers = mo.phase_offers(key, pol, today)
            wb = mo.phase_export(key, sender)
            csv_path = next(iter(sorted(out.glob("agent_outreach_*.csv"), key=lambda p: p.stat().st_mtime,
                                        reverse=True)), None)
        finally:
            mo.OUT = saved_out
    rows = offers.get("rows") or []
    excl = offers.get("excluded") or []
    counts = {"listings": scr["counts"]["listings"], "in_account": scr["counts"]["in_account"],
              "shortlisted": len(sl.get("ids") or []), "added": addres.get("added", 0),
              "detailed": det.get("detailed", 0), "offers": len(rows), "zillow_calls": zillow_calls,
              "cash": sum(1 for r in rows if r.get("cash_offer")),
              "subject_to": sum(1 for r in rows if (r.get("lane") or "").startswith("Subject-to")),
              "seller_finance": sum(1 for r in rows if r.get("lane") == "Seller finance"),
              "price_watch": sum(1 for r in excl if r.get("watch_offer")),
              "screened_only": sum(1 for r in excl if (r.get("reason_group") or "").startswith("screened only")),
              "investor_sales": bys.get("kept", 0), "loans_modeled": (mmod.get("stats") or {}).get("modeled", 0)}
    return {"market": market["name"], "out": str(out), "workbook": str(wb) if wb else None,
            "csv": str(csv_path) if csv_path else None, "counts": counts, "quote": addres.get("quote"),
            "notes": notes, "top": _top(offers)}
