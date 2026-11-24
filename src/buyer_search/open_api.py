"""SiftStack's own buyer search: the data team's method, on what a user's own DataSift key can read.

The data team's model (engine.py) reads the production property cluster: learned embeddings and a
national buyer-stats index nobody outside DataSift can reach. A SiftStack user has their Open API key,
so this version rebuilds the same pipeline from documented SiftMap reads and keeps the model's scoring
exactly (engine.score_buyers: the same ten components, weights, scales and fills, pinned to the original
in the tests):

  1. The house: its DataSift record (map point, beds, baths, size, year, value) or a SiftMap address search.
  2. Sales near it: SiftMap POST /properties/search/ in a box around the house, every investor transaction
     label plus the arm's length sales right around it (deal_buyer_match.phase_sweep, the reviewed sweep).
  3. The model's similar-house bands (value, beds, size, lot) on the search rows BEFORE any deed is read, so
     the read budget goes to houses like this one, nearest first; the age band once the deed gives the year.
  4. Who bought them: capped deed reads (deal_buyer_match.phase_hydrate; Acceptable use forbids SiftMap detail
     pulls in volume without adding the records). A hold label means the owner is the buyer; a flip means the
     buyer is the last-sale SELLER (deal_buyer_match._observations, which also finds unlabeled company buys
     and unlabeled flips in the deeds).
  5. Each buyer's ten components rebuilt from those deeds (COMPONENT_NOTES), then the model's own score.

The answer has the engine's shape (buyers with the same component fields), so the client, the CLI and the
Slack workflow treat both versions alike. Reads only: nothing is written anywhere but the work folder.
"""
from __future__ import annotations

import argparse
import logging
import math
import re
from datetime import date
from pathlib import Path
from statistics import median

from . import engine

log = logging.getLogger("buyer_search.open_api")

RADIUS_MI = 5.0     # the model searches 25 miles against a national index; here every buyer costs a deed read
MONTHS = 24         # the model's 730 days
READ_CAP = 150      # deed reads one search may make: the deal workflows' run cap
COMP_MI = 1.0       # arm's length sales read for unlabeled company buys and flips right around the house
COMP_SHARE = 4      # at most READ_CAP / COMP_SHARE of the reads go to those, nearest first
SQFT_PER_ACRE = 43_560
SOURCE = "siftmap"

COMPONENT_NOTES = {
    "purchases_radius": "similar houses this buyer bought inside the radius and the period (the model's own definition)",
    "total_investor_purchases": "the properties the buyer owns now (SiftMap's owner portfolio, which stops at 11, so 11 "
                                "reads 11 or more) when a house swept here is still theirs, else the purchases seen "
                                "here (model: their national investor purchases)",
    "purchases_per_year": "purchases seen here per year of the period (model: per year, nationally)",
    "locality_percentage": "purchases seen here as a share of the buyer's total (model: share in this county)",
    "median_discount": "median of 1 minus price paid over today's estimated value, in percent, on the houses bought "
                       "here and the ones they hold (model: median discount)",
    "month_active_perc": "months with a purchase over the months from their first to their last purchase seen here",
    "active_years": "years from their first to their last purchase seen here, at least 1",
    "similarity": "how close their average house is to this one, over the similar houses they bought here and up "
                  "to 11 they hold, on beds, baths, size, lot, value and age where known (exp of minus half the "
                  "mean squared z-distance; model: cosine on learned embeddings, (1+cos)/2)",
    "buyer_size": "the same as total investor purchases (model: purchases across all their profiles)",
    "profile_size": "the houses in that profile (model: houses in their best matching profile)",
}
PRICE_NOTE = ("A guess, not a score part. Each of the buyer's deeds in this search becomes a price for this house: a "
              "flip's buy over its resale, times what this house is worth fixed up (the ARV given, else its estimated "
              "value); any other purchase's price over the house's value (today's estimate, or its own sale in the "
              "three years before when that was higher), times this house's estimated value. A deed counts at most "
              "at 100%. The area's typical (the median of those prices over the buyers to call) counts as one more "
              "deed beside their own, so one odd deal cannot swing it and a buyer with no usable price gets the "
              "typical. To the nearest $1,000. The houses they hold are left out because SiftMap gives no sale "
              "dates for them")


# ------------------------------------------------------------------------- the house

def subject_from_record(s: dict) -> dict:
    """quick_screen.subject_of(record) -> this module's subject."""
    return {"dataflik_id": str(s.get("dataflik_id") or "").strip() or None, "address": s.get("street") or "",
            "city": s.get("city") or "", "state": s.get("state") or "", "zip": s.get("zip") or "",
            "county": s.get("county") or "", "lat": s.get("lat") or None, "lon": s.get("lon") or None,
            "beds": s.get("beds") or None, "baths": s.get("baths") or None, "sqft": s.get("sqft") or None,
            "lot_sf": None, "year_built": s.get("year_built") or None, "value": s.get("estimate_value") or None,
            "record": s.get("uuid") or ""}


def _house_number(text: str) -> str:
    m = re.match(r"\s*(\d+[A-Za-z]?)\b", text or "")
    return m.group(1).upper() if m else ""


CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def census_geocode(address: str):
    """(lat, lon) from the US Census one-line geocoder (free, keyless), or None."""
    import json
    import urllib.parse
    import urllib.request
    q = urllib.parse.urlencode({"address": address, "benchmark": "Public_AR_Current", "format": "json"})
    req = urllib.request.Request(CENSUS_URL + "?" + q, headers={"Accept": "application/json",
                                                               "User-Agent": "siftstack-buyer-search/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode() or "{}")
    found = (data.get("result") or {}).get("addressMatches") or []
    if not found:
        return None
    c = found[0].get("coordinates") or {}
    return float(c["y"]), float(c["x"])


def subject_from_siftmap(client, address: str, *, geocode=None) -> tuple[dict | None, str]:
    """A house that is not a DataSift record: the Census geocoder places the address, then a SiftMap
    polygon search about 100 meters around that point returns the parcels there, and only a row with the
    same house number and the same street is taken.

    SiftMap's own address search is no route for this: `addresses: [{"type": "address", ...}]` is in the
    published reference but the server ignores it (2026-11-23: every query returned all 159,790,494
    properties, Prattville AL first), which is why siftmap_standalone.search_address finds nothing."""
    want = _house_number(address)
    if not want:
        return None, "the address must start with the house number"
    try:
        point = (geocode or census_geocode)(address)
    except Exception as exc:  # noqa: BLE001
        return None, "the address could not be placed on the map: %s" % str(exc)[:120]
    if not point:
        return None, "the Census geocoder does not know %s; give the city and ZIP, or the record" % address
    lat, lon = point
    want_street = set(engine.street_key(address.split(",")[0]).split()[1:])
    best, best_key = None, None
    # The geocoder interpolates along the street, so its point can sit a block from the parcel (5123 Example
    # Rd mapped 160 meters north of its parcel): widen in steps and stop at the first box that holds the house.
    for miles in (0.1, 0.2, 0.35):
        for page in range(4):
            rows = client.search({"polygon": _dbm().box(lat, lon, miles), "size": 100, "result_index": page * 100,
                                  "with_boundaries": False}).get("data") or []
            for r in rows:
                line = str(r.get("address") or "")
                if _house_number(line) != want:
                    continue
                words = set(engine.street_key(line.split(",")[0]).split()[1:])
                overlap = len(want_street & words) / max(1, len(want_street | words))
                if overlap < 0.5:
                    continue
                key = (-overlap, _dbm().miles_between(lat, lon, r.get("latitude"), r.get("longitude")))
                if best_key is None or key < best_key:
                    best, best_key = r, key
            if len(rows) < 100:
                break
        if best:
            break
    if not best:
        return None, "SiftMap has no house numbered %s on that street near where %s maps" % (want, address)
    lot = best.get("lotAcres")
    return {"dataflik_id": str(best.get("id") or "") or None, "address": str(best.get("address") or address),
            "city": "", "state": best.get("state") or "", "zip": "", "county": best.get("county") or "",
            "lat": best.get("latitude"), "lon": best.get("longitude"), "beds": best.get("bedrooms"),
            "baths": best.get("bathrooms"), "sqft": best.get("squareFeet"),
            "lot_sf": round(float(lot) * SQFT_PER_ACRE) if lot else None, "year_built": None,
            "value": best.get("estimatedValue"), "record": ""}, ""


# --------------------------------------------------------- the model's similar-house bands

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def bands(subject: dict, this_year: int) -> dict:
    """The model's loose bands (engine.add_filters_for_similar_properties), as (low, high) per field."""
    out = {}
    v = _num(subject.get("value"))
    if v is not None and 0 < v < 99_000_000:
        d = int(max(50_000, 0.50 * v))
        out["value"] = (max(0, v - d), v + d)
    b = _num(subject.get("beds"))
    if b is not None and 0 < b < 99:
        d = int(max(1, 0.33 * b))
        out["beds"] = (max(0, b - d), b + d)
    s = _num(subject.get("sqft"))
    if s is not None and 0 < s < 99_999:
        d = int(max(200, 0.50 * s))
        out["sqft"] = (max(0, s - d), s + d)
    lot = _num(subject.get("lot_sf"))
    if lot is not None and 0 < lot < 9_999_999:
        d = int(max(2000, 0.50 * lot))
        out["lot_sf"] = (max(0, lot - d), lot + d)
    y = _num(subject.get("year_built"))
    if y is not None and 1800 < y < 2999:
        d = int(max(10, 0.25 * max(this_year - y, 0)))
        out["year_built"] = (y - d, y + d)
    return out


def row_fields(row: dict) -> dict:
    """A swept row (deal_buyer_match._paged) in the subject's units."""
    lot = _num(row.get("lot"))
    return {"value": _num(row.get("value")), "beds": _num(row.get("beds")), "sqft": _num(row.get("sqft")),
            "lot_sf": lot * SQFT_PER_ACRE if lot is not None else None, "baths": _num(row.get("baths"))}


def in_bands(fields: dict, bnds: dict, keys=("value", "beds", "sqft", "lot_sf")) -> bool:
    """As the model's range filters: a house missing a banded field is out."""
    for k in keys:
        if k in bnds:
            v = fields.get(k)
            lo, hi = bnds[k]
            if v is None or not (lo <= v <= hi):
                return False
    return True


# ---------------------------------------------------------------------- components

FEATURES = ("beds", "baths", "sqft", "lot_sf", "value", "year_built")


def profile_match(subject: dict, houses: list[dict], spread: dict) -> float | None:
    """exp(-mean z^2 / 2) between this house and the average house a buyer bought, over the features both
    carry, each in units of its spread across the similar houses swept. None with fewer than three."""
    zs = []
    for f in FEATURES:
        s = _num(subject.get(f))
        vals = [_num(h.get(f)) for h in houses]
        vals = [x for x in vals if x is not None]
        sd = spread.get(f)
        if s is None or not vals or not sd:
            continue
        zs.append(((s - sum(vals) / len(vals)) / sd) ** 2)
    if len(zs) < 3:
        return None
    return math.exp(-sum(zs) / len(zs) / 2)


def _spread(houses: list[dict]) -> dict:
    out = {}
    for f in FEATURES:
        vals = [x for x in (_num(h.get(f)) for h in houses) if x is not None]
        if len(vals) >= 2:
            m = sum(vals) / len(vals)
            sd = math.sqrt(sum((x - m) ** 2 for x in vals) / (len(vals) - 1))
            out[f] = sd or None
    return out


def parse_mail(line: str) -> dict:
    """SiftMap's one-string mailing address -> street, city, state, zip (the dispo stack's own splitter)."""
    from enformion_business import split_mail
    street, city, state, zip5 = split_mail(line or "")
    return {"buyer_address": street, "buyer_city": city, "buyer_state": state, "buyer_zip": zip5}


def _month(d: str) -> tuple | None:
    m = re.match(r"(\d{4})-(\d{2})", d or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


SHARE_RANGE = (0.2, 2.0)    # under a fifth of the worth is a paper deed, twice it carried other parcels
PRIOR_SALE_DAYS = 3 * 365   # the house's own sale this long before the purchase still says what it was worth
AREA_WEIGHT = 1.0           # the area's typical counts as one more deed in every buyer's guess


def _days_between(a: str, b: str) -> int:
    try:
        return abs((date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days)
    except ValueError:
        return 10 ** 6


def priced_deed(o: dict, houses: dict, det: dict | None = None) -> dict | None:
    """One purchase priced against what the house was worth: {"share", "price", "worth", "basis"}, or None when
    either number is missing or the deed is nominal or carried other parcels.

    A flip carries both ends on its deeds months apart, so the worth is the resale ("resale"). Otherwise it is
    today's estimated value ("value"), or the house's own last sale in the three years before the purchase when
    that was higher ("prior sale"): an estimate that lags the market reads low, and a buyer who paid what the
    house had just sold for read as paying 130% of its worth on a live run (2026-11-23)."""
    dbm = _dbm()
    price = _num(o.get("price"))
    if not (price and price >= dbm.NOMINAL):
        return None
    if o.get("role") == "flipped" and (_num(o.get("exit_price")) or 0) >= dbm.NOMINAL:
        worth, basis = _num(o["exit_price"]), "resale"
    else:
        worth, basis = _num((houses.get(o["id"]) or {}).get("value")), "value"
        bought = str(o.get("date") or "")[:10]
        prior = next((s for s in ((det or {}).get(o["id"]) or {}).get("sale_history") or []   # newest first
                      if bought and str(s.get("date") or "")[:10] and str(s.get("date"))[:10] < bought
                      and (_num(s.get("price")) or 0) >= dbm.NOMINAL), None)
        if (prior and _days_between(str(prior["date"]), bought) <= PRIOR_SALE_DAYS
                and _num(prior["price"]) > (worth or 0)):
            worth, basis = _num(prior["price"]), "prior sale"
    if not worth:
        return None
    share = price / worth
    if not SHARE_RANGE[0] <= share < SHARE_RANGE[1]:
        return None
    return {"share": share, "price": price, "worth": worth, "basis": basis}


def _unpriced_reason(obs: list[dict], houses: dict, nominal: float) -> str:
    """Why none of a buyer's deeds could be priced, in words for the Slack line."""
    pairs = [(_num(o.get("price")) or 0, _num((houses.get(o["id"]) or {}).get("value")) or 0) for o in obs]
    if any(v and p >= SHARE_RANGE[1] * v for p, v in pairs):
        return "their deed covered several houses"
    if all(p < nominal for p, _ in pairs):
        return "their deed shows no price"
    return "no usable price on their deeds"


def is_subject(rid, address, subject: dict) -> bool:
    """Whether a swept sale is the house being searched: its SiftMap id, else the same street line."""
    sid = str(subject.get("dataflik_id") or "")
    if sid and str(rid or "") == sid:
        return True
    mine = str(subject.get("address") or "").split(",")[0]
    return bool(mine) and engine.street_key(str(address or "").split(",")[0]) == engine.street_key(mine)


def _round_k(v: float) -> int:
    return int(round(v / 1000.0)) * 1000


def _deals(b: dict) -> list[tuple[float, str]]:
    """A buyer's priced deeds as (share counted at most at 100%, basis)."""
    shares, bases = b.get("paid_shares") or [], b.get("paid_bases") or []
    return [(min(s, 1.0), bases[i] if i < len(bases) else "value") for i, s in enumerate(shares) if s]


def price_guesses(result: dict, arv=None) -> dict:
    """Each buyer's guess at what they would pay for this house (`likely_price`, and `likely_share` of its value).

    Every deed of theirs in this search becomes a price for THIS house on its own terms (priced_deed, each counted
    at most at 100%): a flip's share of its resale times what this house is worth fixed up (the ARV given, else its
    estimated value), any other purchase's share of a house's value times this house's estimated value. The two
    must not be mixed: a holder who paid 87% of a house as it stood would otherwise read as paying 87% of the ARV,
    which on a gut-rehab (3014 Sampleville, 11-23: $142,000 estimated, $225,000 ARV) priced the buyers at $195,000.
    The area's typical, the median of those prices over the buyers we would call, counts as one more deed beside
    each buyer's own, so one odd deal cannot swing a guess and a buyer with no usable price gets the typical.

    The houses they hold are left out on purpose: SiftMap gives no sale dates for them, so a purchase from 2012
    would read as a deep discount. result["pricing"] says what the guesses were priced against and what the
    buyers we would call paid together. Run after client.annotate, so the set-aside buyers stay out of the pool."""
    value = _num((result.get("subject") or {}).get("value"))
    fixed = _num(arv) or value

    def implied(deals):
        out = []
        for share, basis in deals:
            target = fixed if basis == "resale" else (value or fixed)
            if target:
                out.append(share * target)
        return out

    buyers = result.get("buyers") or []
    targets = [b for b in buyers if not b.get("not_a_target")]
    pool = [p for b in targets for p in implied(_deals(b))]
    area = median(pool) if pool else None
    guesses = []
    for b in buyers:
        own = implied(_deals(b))
        if area is not None:
            est = (sum(own) + AREA_WEIGHT * area) / (len(own) + AREA_WEIGHT)
        else:
            est = sum(own) / len(own) if own else None
        b["likely_price"] = _round_k(est) if est else None
        b["likely_share"] = round(100.0 * est / value, 1) if est and value else None
        if b["likely_price"] and not b.get("not_a_target"):
            guesses.append(b["likely_price"])
    held = [s for b in targets for s, basis in _deals(b) if basis != "resale"]
    flips = [s for b in targets for s, basis in _deals(b) if basis == "resale"]
    result["pricing"] = {
        "value": value, "arv": _num(arv),
        "usual_price": _round_k(area) if area else None,
        "holder_share": round(100.0 * median(held), 1) if held else None, "holder_deals": len(held),
        "flip_share": round(100.0 * median(flips), 1) if flips else None, "flip_deals": len(flips),
        "priced_purchases": len(pool),
        "buyers_priced": sum(1 for b in targets if b.get("paid_shares")),
        "low": min(guesses) if guesses else None, "high": max(guesses) if guesses else None,
        "how": PRICE_NOTE,
    }
    return result


def buyer_rows(subject: dict, observations: list[dict], det: dict, houses: dict, months: int) -> list[dict]:
    """One row per buyer with the ten raw components (None where the deeds cannot say)."""
    dbm = _dbm()
    by: dict[str, list[dict]] = {}
    for o in observations:
        by.setdefault(dbm.norm_name(o["buyer"]), []).append(o)
    spread = _spread(list(houses.values()))
    rows = []
    for key, obs in by.items():
        names = [o["buyer"] for o in obs]
        name = max(set(names), key=names.count)
        portfolio, mail, held = None, "", []
        for o in obs:
            d = det.get(o["id"]) or {}
            # the buyer still owns it (a hold label, or an unlabeled company buy): the owner portfolio is theirs
            if o.get("role") in ("holder", "entity buy") and dbm.norm_name(d.get("owner_name") or o["buyer"]) == key:
                n = _num(d.get("portfolio_n"))
                if n is not None and (portfolio is None or n > portfolio):
                    portfolio = n
                    held = [p for p in d.get("owner_properties") or [] if isinstance(p, dict)]
                mail = mail or str(d.get("owner_mail") or "")
        seen = len(obs)
        total = max(portfolio or 0, seen)
        days = sorted(str(o.get("date") or "")[:10] for o in obs if o.get("date"))
        mths = sorted({m for m in (_month(x) for x in days) if m})
        if mths:
            span = (mths[-1][0] - mths[0][0]) * 12 + mths[-1][1] - mths[0][1] + 1
            month_pct = round(100.0 * len(mths) / span, 2)
            active = max(1, round(span / 12))
        else:
            month_pct, active = None, None
        disc = []
        for o in obs:
            price, value = _num(o.get("price")), _num((houses.get(o["id"]) or {}).get("value"))
            if price and value and price >= dbm.NOMINAL:
                disc.append(100.0 * (1 - price / value))
        for p in held:     # what they paid for the houses they still hold
            paid, value = _num(p.get("paid")), _num(p.get("value"))
            if paid and value and paid >= dbm.NOMINAL:
                disc.append(100.0 * (1 - paid / value))
        priced = [(o, p) for o in obs for p in (priced_deed(o, houses, det),) if p]
        unpriced = "" if priced else _unpriced_reason(obs, houses, dbm.NOMINAL)
        # their profile: the similar houses bought here plus the houses they hold (beds, baths, value)
        bought = [houses[o["id"]] for o in obs if o["id"] in houses]
        bought += [{"beds": p.get("beds"), "baths": p.get("baths"), "value": p.get("value")} for p in held]
        rows.append({
            "buyer_id": key, "buyer_full_name": name, **parse_mail(mail),
            "is_buyer_corporation": bool(dbm._is_entity(name)),
            "purchases_radius": seen, "total_investor_purchases": total,
            "purchases_per_year": round(seen / (months / 12.0), 2),
            "locality_percentage": round(100.0 * seen / total, 2) if total else None,
            "median_discount": round(median(disc), 2) if disc else None,
            "month_active_perc": month_pct, "active_years": active,
            "similarity": profile_match(subject, bought, spread), "buyer_size": total, "profile_size": len(bought),
            "portfolio": portfolio, "portfolio_11_or_more": bool(portfolio and portfolio >= 11),
            "last_purchase": days[-1] if days else None,
            "cash_share": round(sum(1 for o in obs if o.get("cash")) / seen, 2),
            "paid_shares": [round(p["share"], 4) for _, p in priced],
            "paid_bases": [p["basis"] for _, p in priced],
            "price_share": round(100.0 * median(p["share"] for _, p in priced), 1) if priced else None,
            "priced_purchases": len(priced),
            "median_paid": round(median(p["price"] for _, p in priced)) if priced else None,
            "priced_deeds": [{"address": o.get("address"), "date": str(o.get("date") or "")[:10], "price": p["price"],
                              "worth": p["worth"], "basis": p["basis"]}
                             for o, p in sorted(priced, key=lambda x: str(x[0].get("date") or ""), reverse=True)[:5]],
            "unpriced": unpriced,
            "roles": sorted({o.get("role") or "" for o in obs}),
            "bought_here": [{"address": o.get("address"), "date": str(o.get("date") or "")[:10],
                             "price": o.get("price"), "role": o.get("role"),
                             "miles": round(float(o.get("dist") or 0), 2)}
                            for o in sorted(obs, key=lambda x: str(x.get("date") or ""), reverse=True)[:5]],
        })
    return rows


# ------------------------------------------------------------------------- the search

def _dbm():
    import deal_buyer_match as dbm   # loads the SiftMap client; imported where it is used
    return dbm


def find_buyers(subject: dict, *, client, work: Path, params: engine.Params | None = None,
                radius_mi: float = RADIUS_MI, months: int = MONTHS, read_cap: int = READ_CAP,
                comp_mi: float = COMP_MI, page_size: int | None = None, today: date | None = None) -> dict:
    """The ranked buyers for one house from SiftMap reads on the caller's own key. Never raises for an
    ordinary miss: `status` says what happened (200 ranked or nothing found, 400 bad input, 502 SiftMap)."""
    dbm = _dbm()
    p = params or engine.Params()
    today = today or date.today()
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def note(msg: str) -> None:
        lines.append(msg)
        log.debug(msg)

    out = {"status": 200, "source": SOURCE, "dataflik_id": subject.get("dataflik_id"), "buyers": [],
           "subject": {k: subject.get(k) for k in ("address", "city", "state", "zip", "county", "beds", "baths",
                                                     "sqft", "lot_sf", "year_built", "value", "record")},
           "counts": {}, "params": {"radius_miles": radius_mi, "months": months, "read_cap": read_cap,
                                    "limit": p.limit, "min_score": p.min_score},
           "components": COMPONENT_NOTES, "version": engine.VERSION, "log": lines}
    if not (subject.get("lat") and subject.get("lon")):
        out.update(status=400, error="the house has no map point, so there is no area to search")
        return out
    radius_mi = max(0.25, min(float(radius_mi), 10.0))
    a = argparse.Namespace(lat=float(subject["lat"]), lon=float(subject["lon"]), comp_mi=min(comp_mi, radius_mi),
                           comp_months=months, buyer_mi=radius_mi, buyer_months=months, county=None,
                           page_size=page_size, subject_id=subject.get("dataflik_id") or None,
                           detail_cap=max(0, int(read_cap)), sqft=float(subject.get("sqft") or 0))
    out["params"]["radius_miles"] = radius_mi
    try:
        dbm.phase_sweep(a, work, client=client)
    except SystemExit as exc:          # the sweep's own gate: nothing at all in the box
        note(str(exc))
        out["counts"].update(sales_seen=0)
        return out
    except Exception as exc:  # noqa: BLE001  (SiftMap refusing or down: said, not raised)
        out.update(status=502, error="the SiftMap search failed: %s" % str(exc)[:200])
        return out
    sw = dbm._load(work / "sweep.json", {})
    bnd = bands(subject, today.year)
    inv_all = sum(len(r) for r in sw.get("investor", {}).values())
    keep = lambda r: in_bands(row_fields(r), bnd)  # noqa: E731
    inv = {t: [r for r in rows if keep(r)] for t, rows in (sw.get("investor") or {}).items()}
    comps = sorted((r for r in sw.get("comps") or [] if keep(r)),
                   key=lambda r: dbm.miles_between(a.lat, a.lon, r.get("lat"), r.get("lon")))
    comps = comps[:max(10, a.detail_cap // COMP_SHARE)]
    similar = sum(len(r) for r in inv.values())
    dbm._save(work / "sweep_all.json", sw)
    dbm._save(work / "sweep.json", dict(sw, investor=inv, comps=comps))
    counts = out["counts"]
    counts.update(sales_seen=inv_all + len(sw.get("comps") or []), investor_sales_seen=inv_all,
                  similar=similar, similar_comps=len(comps))
    note("%d investor sales within %.1f mi in %d months, %d like this house; %d nearby sales kept for "
         "unlabeled buys" % (inv_all, radius_mi, months, similar, len(comps)))
    if not similar and not comps:
        return out
    (work / "unread.json").unlink(missing_ok=True)      # phase_hydrate writes it only when the cap bites
    try:
        dbm.phase_hydrate(a, work, client=client)
    except SystemExit as exc:
        out.update(status=502, error=str(exc).replace("GATE FAILED: ", "")[:200])
        return out
    except Exception as exc:  # noqa: BLE001
        out.update(status=502, error="the SiftMap deed reads failed: %s" % str(exc)[:200])
        return out
    det = dbm._load(work / "details.json", {})
    unread = (dbm._load(work / "unread.json", {}) or {}).get("unread") or []
    wanted = {r["id"] for r in comps} | {r["id"] for rows in inv.values() for r in rows}
    # deeds on hand for THIS search (read now, or cached in the work folder by an earlier one)
    counts.update(deeds_read=sum(1 for i in wanted if i in det and not det[i].get("miss")), deeds_unread=len(unread))
    if subject.get("dataflik_id") and not subject.get("year_built"):
        y = _num((det.get(str(subject["dataflik_id"])) or {}).get("year_built"))
        if y:
            subject = dict(subject, year_built=int(y))
            out["subject"]["year_built"] = int(y)
            bnd = bands(subject, today.year)
    sw_f = dict(sw, investor=inv, comps=comps)
    # The house's own deed is no buyer's purchase nearby: 3014 Sampleville's own August sale listed the company selling
    # it as a buyer to call and put its $91,250 into the pricing (2026-11-23).
    obs = [o for o in dbm._observations(a, sw_f, det) if not is_subject(o.get("id"), o.get("address"), subject)]
    houses = {}
    for r in comps + [r for rows in inv.values() for r in rows]:
        f = row_fields(r)
        f["year_built"] = _num((det.get(r["id"]) or {}).get("year_built"))
        houses[r["id"]] = f
    lo_day = date.fromordinal(today.toordinal() - int(months * 30.44)).isoformat()
    kept = []
    for o in obs:
        y = (houses.get(o["id"]) or {}).get("year_built")
        if "year_built" in bnd and (y is None or not (bnd["year_built"][0] <= y <= bnd["year_built"][1])):
            continue
        if max(str(o.get("date") or ""), str(o.get("exit_date") or "")) < lo_day:
            continue
        kept.append(o)
    counts.update(observations=len(obs), observations_kept=len(kept))
    days = sorted(str(o.get("date") or "")[:10] for o in kept if o.get("date"))
    if days:
        counts.update(sale_date_first=days[0], sale_date_last=days[-1])
    rows = buyer_rows(subject, kept, det, houses, months)
    counts["buyers"] = len(rows)
    if not rows:
        note("No buyer bought a house like this one nearby in the deeds read.")
        return out
    scores = engine.score_buyers(rows, p.weights)
    for r, s in zip(rows, scores):
        r["score"] = s
    keep_rows = sorted((r for r in rows if r["score"] > p.min_score), key=lambda r: (-r["score"], r["buyer_id"]))
    counts.update(scored=len(rows), above_min=len(keep_rows))
    out["buyers"] = [{k: engine._as_int_if_whole(v) if isinstance(v, float) else v for k, v in r.items()}
                     for r in keep_rows[:p.limit]]
    counts["returned"] = len(out["buyers"])
    note("Returning %d of %d buyers." % (counts["returned"], len(rows)))
    return out
