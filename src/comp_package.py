"""One-command comp package builder (the "158 Old State" document, generalized).

Pulls the subject + sold/active comps from the OpenWeb Ninja Zillow /search API
(via zillow_market_api), optionally clips them to a drawn boundary (bbox and/or
street regex), buckets comps by condition, builds a DUAL-TRACK ARV (same-bed
base case + higher-bed reconfig upside), runs the 4-tier rehab scenarios
(cosmetic / mid / full gut), computes wholesale MAO math off the CONSERVATIVE
track, matches local buyer-prospecting data by zip, and writes a branded Excel
workbook.

Dual-track ARV rule (Ty, 2026-07-21): a subject whose bedroom count is below
the comp set lives in a lower value band than the per-bedroom adjustment
implies. Base ARV comes ONLY from same-bed renovated comps; the higher-bed
track is a labeled upside that requires a verified reconfig, and underwriting
always uses the base track.

Usage:
  python src/comp_package.py --address "158 Old State Rd" --city Knoxville --zip 37914 \
      --beds 2 --baths 1 --sqft 1946 --year-built 1938 \
      --bbox "35.996,36.016,-83.895,-83.840" \
      --streets "old state|nash rd|seahorn|holston dr|bona|grata|silva|chilhowee"
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import logging
import math
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

import config
from comp_analyzer import fetch_subject_property
from rehab_estimator import estimate_rehab
from zillow_market_api import MarketListing, ZillowMarketAPI, filter_bbox, filter_streets

logger = logging.getLogger(__name__)

NAVY, BLUE, GREEN, GOLD = "0A1130", "316AFF", "1B9E5A", "B8860B"

GUT_ALLOWANCE_PER_SQFT = 15.0   # demo / drywall / insulation / reconfig framing
SOFT_COST_PCT = 0.13            # 3% permits + 10% contingency


# ── Condition bucketing ───────────────────────────────────────────────

def classify(listing: MarketListing) -> str:
    """Bucket a sold comp by condition using sold price vs Zestimate."""
    if listing.zestimate:
        ratio = listing.price / listing.zestimate
        if ratio >= 0.90:
            return "RENOVATED/RETAIL"
        if ratio <= 0.70:
            return "DISTRESSED"
        return "AVERAGE"
    return "UNKNOWN"


# ── Dual-track ARV ────────────────────────────────────────────────────

def dual_track_arv(subject_beds: int, subject_sqft: int,
                   sold: list[MarketListing]) -> dict:
    """Base ARV from same-bed renovated comps; upside from higher-bed band."""
    retail = [l for l in sold if classify(l) == "RENOVATED/RETAIL" and l.sqft and l.price]

    def track(comps: list[MarketListing], clamp_to_band: bool) -> dict:
        if not comps:
            return {}
        ppsf = statistics.median(l.ppsf for l in comps)
        est = ppsf * subject_sqft
        prices = sorted(l.price for l in comps)
        lo = prices[max(0, len(prices) // 4)]
        hi = prices[min(len(prices) - 1, (3 * len(prices)) // 4)]
        if clamp_to_band:
            # Bed-count bands cap regardless of size: an oversized subject
            # still sells to the same buyer pool, so never project past the
            # band's MEDIAN price. Analyst adjusts within the band for
            # micro-position (west vs east end of a pocket).
            est = min(est, statistics.median(prices))
        else:
            # Upside track: cap at the 75th percentile of the higher-bed band.
            est = min(est, hi)
        return {"arv": round(est / 5000) * 5000, "band": (lo, hi),
                "ppsf": round(ppsf), "n": len(comps)}

    same_bed = [l for l in retail if l.beds == subject_beds]
    base_comps = same_bed
    base = track(same_bed, clamp_to_band=True)
    base_flag = ""
    if len(same_bed) < 3:
        wider = [l for l in retail if abs(l.beds - subject_beds) <= 1]
        base_comps = wider
        base = track(wider, clamp_to_band=True)
        if base:
            base["arv"] = round(base["arv"] * 0.90 / 5000) * 5000
            base_flag = "THIN same-bed comp set - widened +/-1 bed and discounted 10%"

    upside_comps = [l for l in retail if l.beds > subject_beds
                    and (not subject_sqft or 0.6 <= (l.sqft / subject_sqft) <= 1.4)]
    upside = track(upside_comps, clamp_to_band=False)

    return {"base": base, "base_flag": base_flag, "upside": upside,
            "base_comps": base_comps if base else []}


# ── Rehab scenarios ───────────────────────────────────────────────────

def rehab_scenarios(sqft: int, beds: int, baths: float, year_built: int) -> dict:
    """Cosmetic (wholetail) / mid (systems, no envelope) / full gut."""
    def total(est, drop=(), extra=0.0):
        rooms = [r for r in est.rooms if r.category not in drop]
        subtotal = sum(r.total for r in rooms) + extra
        return round(subtotal * (1 + SOFT_COST_PCT))

    cosmetic = estimate_rehab("", sqft, beds, baths, year_built,
                              tier=2, scope="wholetail", region="knoxville")
    full = estimate_rehab("", sqft, max(beds, 3), max(baths, 2.0), year_built,
                          tier=2, scope="full", region="knoxville")
    gut_extra = GUT_ALLOWANCE_PER_SQFT * sqft
    return {
        "cosmetic": total(cosmetic),
        "mid": total(full, drop=("Roof", "Windows", "Foundation/Structural")),
        "full_gut": total(full, extra=gut_extra),
    }


# ── Buyer matching ────────────────────────────────────────────────────

def local_buyers(zip_code: str) -> list[dict]:
    """Latest buyer-prospecting export rows tied to the subject zip."""
    paths = sorted(glob.glob(str(config.OUTPUT_DIR / "buyers_datasift_*.csv")))
    if not paths:
        return []
    rows = []
    with open(paths[-1], newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            blob = " ".join(str(v) for v in row.values())
            if zip_code and zip_code in blob:
                rows.append(row)
    return rows


# ── Free comps: SiftMap deed sales by distance ────────────────────────
# Ty's standing rule (2026-07-23): pull the sold universe from SiftMap's deeds
# by distance FIRST. A deed carries the as-is, investor and cash purchases the
# MLS never shows, and the read is free; the Zillow pull is a paid supplement.
# The rows come back as MarketListing objects so the ARV math above (classify,
# dual_track_arv) and post_walkthrough's tight ARV run on them unchanged.

K_TXN = "extra_last_sale_investor_transaction_type"
# The investor transaction labels SiftMap puts on a last sale (the dispo
# registry's ALL_TYPES). The filter takes a LIST; a comma string is refused.
INVESTOR_TXN_TYPES = ("pending", "wholesale", "wholetail", "rental", "flip")
NOMINAL_SALE = 10_000          # a deed under this is paper (family, estate, quitclaim), not a sale
# Candidates inside the ARV size band and within a bedroom are read first, so a
# capped read spends itself on comps the tight ARV can actually use.
READ_FIRST_SIZE = (0.70, 1.35)


def box(lat: float, lon: float, miles: float) -> list[dict]:
    """A closed square ring around a point: the polygon SiftMap's search takes."""
    dlat = miles / 69.0
    dlon = miles / (69.0 * math.cos(math.radians(lat)))
    return [{"lat": lat + dlat, "lon": lon - dlon}, {"lat": lat + dlat, "lon": lon + dlon},
            {"lat": lat - dlat, "lon": lon + dlon}, {"lat": lat - dlat, "lon": lon - dlon},
            {"lat": lat + dlat, "lon": lon - dlon}]


def miles_between(lat1, lon1, lat2, lon2) -> float:
    """Great-circle miles; 99 when either point is missing, so it sorts last."""
    if not all(isinstance(v, (int, float)) for v in (lat1, lon1, lat2, lon2)) or not (lat1 and lat2):
        return 99.0
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def since(months: int, today: date | None = None) -> str:
    return ((today or date.today()) - timedelta(days=int(months * 30.44))).isoformat()


def split_address(full: str) -> tuple[str, str, str, str]:
    """'123 Main St, Knoxville, TN 37914' -> (street, city, state, zip). SiftMap
    search rows carry the whole address as one string."""
    parts = [p.strip() for p in str(full or "").split(",")]
    street = parts[0] if parts else ""
    city = parts[1] if len(parts) > 2 else ""
    tail = parts[-1] if len(parts) > 1 else ""
    m = re.match(r"([A-Za-z]{2})\s*(\d{5})?", tail)
    return street, city, (m.group(1).upper() if m else ""), ((m.group(2) or "") if m else "")


def _street_key(street: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(street or "").lower())


def newest_deed(detail: dict) -> dict:
    """The newest deed on a SiftMap detail (sale_history is newest first), in
    the shape the dispo registry and deal_buyer_match store."""
    sh = (detail or {}).get("sale_history") or []
    if not sh:
        return {}
    s = sh[0]
    return {"date": str(s.get("sale_date") or "")[:10], "price": s.get("sale_price"),
            "buyer": (s.get("buyer_name") or "").strip(), "seller": (s.get("seller_name") or "").strip(),
            "cash": bool(s.get("is_cash_sale"))}


def siftmap_sold(subject: dict, client, *, radius_mi: float = 1.0, months: int = 12,
                 detail_cap: int = 12, page_size: int = 100, cache: dict | None = None,
                 today: date | None = None, polygon: list[dict] | None = None,
                 max_rows: int | None = None, streets=None) -> dict:
    """Sold single-family comps around the subject, priced off the deed.

    One search finds every single-family sale in the area inside the window (the
    search rows carry no price), then the newest deed is read for the closest
    candidates, at most `detail_cap` new reads a run. The area is `polygon` (a
    closed ring of {"lat", "lon"}, a drawn boundary) when given, else a box of
    `radius_mi` around the subject. The search is not ordered by distance, so
    `max_rows` pages past the first page (a busy square mile holds more than 100
    sales in 18 months, and the closest may sit on page two). Acceptable use forbids
    SiftMap detail pulls in volume without adding the records, so the cap is
    the point, not a tuning knob. `cache` (id -> {"deed", "at"}) is reused and
    filled in place; a cached deed older than the window is read again, because
    the house is only in the window now if it sold again since. `streets` (the
    pasted street list, a pattern searched in the street line, as the clip does)
    decides which rows may be read at all: applied after the reads, the cap went
    to the closest houses in the box, the clip then dropped every one of them for
    sitting on another street, and a busy pocket came back with no comps.

    Returns {"sold": [MarketListing], "records": [deed rows in the buyer sweep
    shape], "found", "read", "cached", "not_read", "off_streets", "dropped":
    {reason: n}, "why": "" or the plain reason nothing usable came back}.
    """
    out = {"sold": [], "records": [], "found": 0, "read": 0, "cached": 0, "not_read": 0, "off_streets": 0,
           "dropped": {}, "why": "", "radius_mi": radius_mi, "months": months}
    pat = re.compile(streets, re.I) if isinstance(streets, str) and streets.strip() else (streets or None)
    lat, lon = subject.get("lat"), subject.get("lon")
    if not lat or not lon:
        out["why"] = "the record has no map point, so there is no area to search"
        return out
    cut = since(months, today)
    body = {"polygon": polygon or box(float(lat), float(lon), radius_mi),
            "filters": {"type_single_family": True, "extra_last_sale_date_min": cut,
                        "extra_is_last_sale_interfamily": False, "extra_last_sale_value_min": 1000},
            "size": int(page_size), "result_index": 0}
    found = client.search(body) or {}
    rows = list(found.get("data") or [])
    out["found"] = int(found.get("total_results") or len(rows))
    limit = min(out["found"], int(max_rows or page_size))
    # Count only rows not seen yet: if the server's offsets overlap pages by a row, counting the
    # repeat would stop the loop one page early and quietly drop the last sales.
    ids = {str(r.get("id")) for r in rows}
    while len(ids) < limit:
        page = (client.search(dict(body, result_index=len(rows))) or {}).get("data") or []
        new = [r for r in page if str(r.get("id")) not in ids]
        if not new:
            break
        rows += new
        ids.update(str(r.get("id")) for r in new)
    me_id = str(subject.get("dataflik_id") or "")
    me_street = _street_key(subject.get("address") or subject.get("street") or "")
    sq, beds = float(subject.get("sqft") or 0), int(subject.get("beds") or 0)

    def drop(reason: str) -> None:
        out["dropped"][reason] = out["dropped"].get(reason, 0) + 1

    cands, seen = [], set()
    for r in rows:
        rid = str(r.get("id") or "")
        street, city, state, zip5 = split_address(r.get("address"))
        # A row seen twice (pages that overlap by one) would count one sale twice in every median.
        if not rid or rid in seen or (me_id and rid == me_id) or (me_street and _street_key(street) == me_street):
            continue
        seen.add(rid)
        if pat is not None and not pat.search(street or ""):
            out["off_streets"] += 1
            continue
        d = miles_between(float(lat), float(lon), r.get("latitude"), r.get("longitude"))
        ratio = (float(r.get("squareFeet") or 0) / sq) if sq else 1.0
        near = READ_FIRST_SIZE[0] <= ratio <= READ_FIRST_SIZE[1] and abs(int(r.get("bedrooms") or 0) - beds) <= 1
        cands.append((0 if near else 1, d, rid, r, (street, city, state, zip5)))
    cands.sort(key=lambda c: (c[0], c[1]))

    cache = cache if cache is not None else {}
    failures = 0
    for _, dist, rid, r, (street, city, state, zip5) in cands:
        hit = cache.get(rid) or {}
        deed = hit.get("deed") if str((hit.get("deed") or {}).get("date") or "") >= cut else None
        if deed is not None:
            out["cached"] += 1
        elif out["read"] < max(0, int(detail_cap)):
            out["read"] += 1
            try:
                deed = newest_deed(client.get_detail(rid))
            except Exception as exc:  # noqa: BLE001  (one bad read is a miss, not a failed screen)
                failures += 1
                drop("deed read failed")
                logger.warning("deed read failed for %s: %s", rid, exc)
                continue
            cache[rid] = {"deed": deed, "at": (today or date.today()).isoformat()}
        else:
            out["not_read"] += 1
            continue
        price = float(deed.get("price") or 0)
        if not deed:
            drop("no deed on file")
            continue
        if price < NOMINAL_SALE:
            drop("deed under $10,000 (not arm's length)")
            continue
        if str(deed.get("date") or "") < cut:
            drop("newest deed is older than the window")
            continue
        out["sold"].append(MarketListing(
            zpid="sm%s" % rid, address=street, city=city, state=state, zip_code=zip5,
            latitude=float(r.get("latitude") or 0), longitude=float(r.get("longitude") or 0),
            beds=int(r.get("bedrooms") or 0), baths=float(r.get("bathrooms") or 0),
            sqft=int(float(r.get("squareFeet") or 0)), home_type="SINGLE_FAMILY",
            home_status="RECENTLY_SOLD", price=price, sold_date=deed["date"],
            zestimate=float(r.get("estimatedValue") or 0), lot_acres=float(r.get("lotAcres") or 0),
            raw={"source": "siftmap deed", "id": rid, "miles": round(dist, 2)}))
        out["records"].append({"address": street, "buyer": deed.get("buyer") or "", "cash": bool(deed.get("cash")),
                               "sale_date": deed["date"], "sale_price": price})
    if out["read"] and failures == out["read"] and not out["cached"]:
        out["why"] = "every deed read failed, which is an access problem (REISIFT_API_KEY), not an empty market"
    elif not out["found"]:
        out["why"] = "no single-family sale %s in the last %d months" % (
            "inside the boundary" if polygon else "within %.2f mi" % radius_mi, months)
    elif pat is not None and out["off_streets"] and not cands:
        out["why"] = "%d sale(s) in the area, none on the streets asked for" % out["found"]
    elif not out["sold"]:
        out["why"] = "%d sale(s) found but none with a usable deed price" % out["found"]
    return out


def investor_activity(client, lat: float, lon: float, *, radius_mi: float = 1.0, months: int = 6,
                      today: date | None = None) -> int:
    """Single-family houses near a point whose last sale SiftMap labels an
    investor transaction inside the window. Zero means a dead area: a deal there
    is hard to sell, because nobody who buys this kind of house is buying here
    (build plan 2.8). One search, no detail reads."""
    body = {"polygon": box(float(lat), float(lon), radius_mi),
            "filters": {"type_single_family": True, K_TXN: list(INVESTOR_TXN_TYPES),
                        "extra_last_sale_date_min": since(months, today)},
            "size": 1, "result_index": 0}
    r = client.search(body) or {}
    return int(r.get("total_results") or len(r.get("data") or []))


# ── Pasted inputs: a boundary, a street list, county card notes ───────
# What a person pastes in Slack, read the way the CLI flags read it: --bbox or a
# drawn polygon, --streets, and the county card overrides (--beds --baths --sqft
# --year-built), which beat Zillow and the record. Pure functions: no network.

BOUNDARY_HELP = ("I could not read that boundary. Paste a box as lat_min,lat_max,lon_min,lon_max, "
                 "a list of lat,lon points, or the coordinates from a Google My Maps KML or GeoJSON export.")
STREETS_HELP = ("I could not read that street list. Give street names separated by |, "
                "like old state|nash rd|seahorn.")
_NUM_RX = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _sign(v: float) -> int:
    return 1 if v > 0 else (-1 if v < 0 else 0)


def _ring_ok(points: list) -> bool:
    return (len({(round(a, 7), round(b, 7)) for a, b in points}) >= 3
            and all(-90 <= a <= 90 and -180 <= b <= 180 for a, b in points))


def _open_ring(points: list) -> list:
    """Drop the closing point a ring repeats, so each corner is listed once."""
    return points[:-1] if len(points) > 3 and points[0] == points[-1] else points


def _geojson_ring(obj):
    """The first list of [lon, lat] pairs anywhere in a GeoJSON value, as (lat, lon)."""
    if isinstance(obj, dict):
        for key in ("geometry", "features", "geometries", "coordinates"):
            if key in obj:
                ring = _geojson_ring(obj[key])
                if ring:
                    return ring
        return None
    if isinstance(obj, list) and obj:
        if all(isinstance(p, list) and len(p) >= 2 and all(isinstance(v, (int, float)) for v in p[:2]) for p in obj):
            return [(float(p[1]), float(p[0])) for p in obj]
        for item in obj:
            ring = _geojson_ring(item)
            if ring:
                return ring
    return None


def _polygon(points: list, source: str) -> tuple[dict | None, str]:
    points = _open_ring(points)
    if not _ring_ok(points):
        return None, BOUNDARY_HELP
    return {"kind": "polygon", "points": points, "bbox": None, "source": source}, ""


def _bbox(lat_lo: float, lat_hi: float, lon_lo: float, lon_hi: float, source: str) -> tuple[dict | None, str]:
    if not (-90 <= lat_lo < lat_hi <= 90 and -180 <= lon_lo < lon_hi <= 180):
        return None, BOUNDARY_HELP
    corners = [(lat_lo, lon_lo), (lat_lo, lon_hi), (lat_hi, lon_hi), (lat_hi, lon_lo)]
    return {"kind": "bbox", "points": corners, "bbox": (lat_lo, lat_hi, lon_lo, lon_hi), "source": source}, ""


def parse_boundary(text: str) -> tuple[dict | None, str]:
    """A pasted boundary -> ({"kind", "points", "bbox", "source"}, "") or (None, why).

    Shapes it reads:
      * a box: lat_min,lat_max,lon_min,lon_max (the --bbox form), or two corners lat,lon lat,lon;
      * a list of lat,lon points, closed or not, apart by spaces, semicolons, new lines or commas;
      * Google My Maps KML: a <coordinates> block, or the bare lon,lat,altitude tuples;
      * GeoJSON: a Feature, a FeatureCollection, a geometry, or the bare coordinates array.
    KML and GeoJSON write longitude first; plain points are read as lat,lon unless every pair is
    plainly lon,lat (a longitude past 90, or a negative first value beside a positive second).
    Points come back as (lat, lon). Empty text is no boundary, not an error. Anything else gets
    one sentence naming these shapes, because a boundary guessed wrong silently moves the ARV.
    """
    t = (text or "").strip()
    if not t:
        return None, ""
    if t[:1] in "[{":
        try:
            ring = _geojson_ring(json.loads(t))
        except ValueError:
            ring = None
        if ring:
            return _polygon(ring, "geojson")
        if t[:1] == "{":
            return None, BOUNDARY_HELP
    tag = re.search(r"<coordinates>(.*?)</coordinates>", t, re.S | re.I)
    body = tag.group(1) if tag else t
    if re.search(r"[A-Za-z]", body):
        return None, BOUNDARY_HELP
    if tag:
        pts = []
        for tok in body.split():
            vals = [float(v) for v in _NUM_RX.findall(tok)]
            if len(vals) >= 2:
                pts.append((vals[1], vals[0]))
        return _polygon(pts, "kml")
    nums = [float(v) for v in _NUM_RX.findall(body)]
    if len(nums) >= 9 and len(nums) % 3 == 0 and all(v == 0 for v in nums[2::3]):
        return _polygon([(nums[i + 1], nums[i]) for i in range(0, len(nums), 3)], "kml")
    if len(nums) == 4:
        a, b, c, d = nums
        if _sign(a) == _sign(b) and _sign(c) == _sign(d) and a < b and c < d:
            return _bbox(a, b, c, d, "bbox")
        if _sign(a) == _sign(c) and _sign(b) == _sign(d):
            return _bbox(min(a, c), max(a, c), min(b, d), max(b, d), "corners")
        return None, BOUNDARY_HELP
    if len(nums) >= 6 and len(nums) % 2 == 0:
        pairs = [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]
        lon_first = any(abs(p[0]) > 90 for p in pairs) or all(p[0] < 0 < p[1] for p in pairs)
        return _polygon([(q, p) for p, q in pairs] if lon_first else pairs, "points")
    return None, BOUNDARY_HELP


def boundary_ring(boundary: dict) -> list[dict]:
    """A parsed boundary as the closed ring of {"lat", "lon"} SiftMap's search takes."""
    pts = list(boundary["points"])
    return [{"lat": a, "lon": o} for a, o in pts + pts[:1]]


def parse_streets(text: str) -> tuple[str, str]:
    """A street list or regex -> (pattern, "") or ("", why). Names apart by | are the --streets
    form already; commas, semicolons or new lines between names are read as | too."""
    t = (text or "").strip()
    if not t:
        return "", ""
    if "|" not in t and re.search(r"[,;\n]", t):
        t = "|".join(x.strip() for x in re.split(r"[,;\n]+", t) if x.strip())
    try:
        re.compile(t, re.I)
    except re.error:
        return "", STREETS_HELP
    return t, ""


_SQFT_RX = re.compile(r"(\d{1,2},\d{3}|\d{3,5})\s*(?:sq\.?\s*f(?:ee)?t\.?|square\s*f(?:ee|oo)t|sqft|sf)\b", re.I)
_YEAR_RX = re.compile(r"\b(?:year\s*built|yr\.?\s*built|built(?:\s+in)?|yb)\s*:?\s*(1[89]\d{2}|20\d{2})\b", re.I)
_SLASH_RX = re.compile(r"(?<![\d/.])([1-9])\s*/\s*([1-9](?:\.5)?)(?![\d/])")
_BEDS_RX = re.compile(r"\b(\d{1,2})\s*-?\s*(?:bed(?:room)?s?|bdrms?|bds?|brs?)\b", re.I)
_BATHS_RX = re.compile(r"\b(\d{1,2}(?:\.5)?)\s*-?\s*(?:bath(?:room)?s?|bths?|ba)\b", re.I)
_BARE_YEAR_RX = re.compile(r"(?<![\d$,.])(1[89]\d{2}|20\d{2})(?![\d,])")


def parse_card_notes(text: str, this_year: int | None = None) -> dict:
    """County card facts in free text -> the --beds/--baths/--sqft/--year-built overrides, plus the
    rest of what was said as `context`.

    Reads "2/1, 1946 sqft, built 1938" and "3 bed 2 bath 1,400 sf 1962" alike. Square footage is
    read first so its number is never taken for a year; a bare four-digit year counts only when it
    is the one such number left. A value outside a plausible range is dropped, never clamped, and
    anything it cannot place stays in `context` word for word.
    """
    t = text or ""
    year_hi = (this_year or date.today().year) + 1
    out: dict = {}
    spans: list = []

    def free(span) -> bool:
        return not any(span[0] < e and s < span[1] for s, e in spans)

    for m in _SQFT_RX.finditer(t):
        v = int(m.group(1).replace(",", ""))
        if "sqft" not in out and 200 <= v <= 20000 and free(m.span()):
            out["sqft"] = v
            spans.append(m.span())
    for m in _YEAR_RX.finditer(t):
        v = int(m.group(1))
        if "year_built" not in out and 1800 <= v <= year_hi and free(m.span()):
            out["year_built"] = v
            spans.append(m.span())
    for m in _SLASH_RX.finditer(t):
        if "beds" not in out and "baths" not in out and free(m.span()):
            out["beds"], out["baths"] = int(m.group(1)), float(m.group(2))
            spans.append(m.span())
    for rx, key, cast, lo, hi in ((_BEDS_RX, "beds", int, 1, 20), (_BATHS_RX, "baths", float, 0.5, 20)):
        for m in rx.finditer(t):
            v = cast(float(m.group(1)))
            if key not in out and lo <= v <= hi and free(m.span()):
                out[key] = v
                spans.append(m.span())
    if "year_built" not in out:
        # A bare year counts only beside another card fact ("1,400 sf 1962"): "the roof was
        # replaced in 2022" names a year that is not the year the house was built.
        def beside(m) -> bool:
            for s, e in spans:
                gap = t[e:m.start()] if e <= m.start() else t[m.end():s]
                if re.fullmatch(r"[\s,;|/]*", gap):
                    return True
            return False
        bare = [m for m in _BARE_YEAR_RX.finditer(t) if free(m.span()) and 1800 <= int(m.group(1)) <= year_hi]
        if len(bare) == 1 and beside(bare[0]):
            out["year_built"] = int(bare[0].group(1))
            spans.append(bare[0].span())
    rest = t
    for s, e in sorted(spans, reverse=True):
        rest = rest[:s] + " " + rest[e:]
    rest = re.sub(r"\s+", " ", rest)
    rest = re.sub(r"(?:\s*[,;|/]\s*){2,}", ", ", rest).strip(" ,;|./")
    out["context"] = rest if len(re.findall(r"[A-Za-z]", rest)) >= 3 else ""
    return out


# ── Workbook ──────────────────────────────────────────────────────────

def _header(ws, row, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = Font(bold=True, color="FFFFFF", size=10)
        cell.fill = PatternFill("solid", fgColor=NAVY)


def _title(ws, text, sub=""):
    ws.cell(row=1, column=1, value=text).font = Font(bold=True, size=14, color=NAVY)
    if sub:
        ws.cell(row=2, column=1, value=sub).font = Font(size=10, color="666666")


def _widths(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def build_workbook(subject: dict, sold: list[MarketListing], active: list[MarketListing],
                   arv: dict, rehab: dict, buyers: list[dict], out_path: str) -> str:
    wb = Workbook()
    base, upside = arv.get("base") or {}, arv.get("upside") or {}
    base_arv = base.get("arv", 0)

    ws = wb.active
    ws.title = "Summary"
    _title(ws, f"{subject['address']} - Comp Package",
           f"Built {datetime.now():%m/%d/%Y}. Boundary-filtered API comps. "
           "Underwrite off the BASE (same-bed) track only.")
    mao = {k: {"mao70": round(base_arv * 0.70 - v), "mao75": round(base_arv * 0.75 - v)}
           for k, v in rehab.items()}
    rows = [
        ("SUBJECT", ""),
        ("Specs", f"{subject['sqft']:,} sqft | {subject['beds']}bd/{subject['baths']}ba | "
                  f"built {subject['year_built']}"),
        ("", ""),
        ("THE FOUR NUMBERS", ""),
        ("ARV - BASE (same-bed)", f"${base_arv:,.0f}" + (
            f" (band ${base.get('band', (0, 0))[0]:,.0f}-${base.get('band', (0, 0))[1]:,.0f}, "
            f"{base.get('n', 0)} comps @ ${base.get('ppsf', 0)}/sf median)" if base else " - NO COMPS")),
        ("ARV - UPSIDE (reconfig)", f"${upside.get('arv', 0):,.0f} "
         f"({upside.get('n', 0)} higher-bed comps) - only after a walkthrough verifies the "
         "layout converts" if upside else "n/a"),
        ("Full gut", f"${rehab['full_gut']:,.0f}"),
        ("Mid reno", f"${rehab['mid']:,.0f}"),
        ("Cosmetic", f"${rehab['cosmetic']:,.0f}"),
        ("", ""),
        ("WHOLESALE MATH (off BASE ARV)", ""),
        ("Full gut", f"MAO 70%: ${mao['full_gut']['mao70']:,.0f} | 75%: ${mao['full_gut']['mao75']:,.0f}"),
        ("Mid reno", f"MAO 70%: ${mao['mid']['mao70']:,.0f} | 75%: ${mao['mid']['mao75']:,.0f}"),
        ("Cosmetic", f"MAO 70%: ${mao['cosmetic']['mao70']:,.0f} | 75%: ${mao['cosmetic']['mao75']:,.0f}"),
    ]
    if arv.get("base_flag"):
        rows.append(("Flag", arv["base_flag"]))
    for i, r in enumerate(rows):
        ws.cell(row=4 + i, column=1, value=r[0])
        ws.cell(row=4 + i, column=2, value=r[1])
        if r[0] and not r[1]:
            ws.cell(row=4 + i, column=1).font = Font(bold=True, color=BLUE)
    _widths(ws, [28, 105])

    ws = wb.create_sheet("Sold Comps")
    _title(ws, "Sold comps (boundary-filtered)", "Bucketed by sold price vs Zestimate")
    hdr = ["Address", "Sold", "Price", "Bd", "Ba", "SqFt", "$/SF", "Bucket", "Zestimate", "URL"]
    for j, h in enumerate(hdr):
        ws.cell(row=4, column=j + 1, value=h)
    _header(ws, 4, len(hdr))
    order = {"RENOVATED/RETAIL": 0, "AVERAGE": 1, "UNKNOWN": 2, "DISTRESSED": 3}
    for i, l in enumerate(sorted(sold, key=lambda x: (order.get(classify(x), 9), x.sold_date), reverse=False)):
        bucket = classify(l)
        vals = [l.address, l.sold_date, l.price, l.beds, l.baths, l.sqft,
                round(l.ppsf) if l.ppsf else None, bucket, l.zestimate or None, l.detail_url]
        for j, v in enumerate(vals):
            ws.cell(row=5 + i, column=j + 1, value=v)
        ws.cell(row=5 + i, column=3).number_format = "$#,##0"
        ws.cell(row=5 + i, column=9).number_format = "$#,##0"
        color = {"RENOVATED/RETAIL": GREEN, "DISTRESSED": GOLD}.get(bucket)
        if color:
            ws.cell(row=5 + i, column=8).font = Font(bold=True, color=color, size=10)
    _widths(ws, [24, 11, 11, 5, 5, 7, 7, 18, 11, 45])

    ws = wb.create_sheet("Active")
    _title(ws, "Active listings (boundary-filtered)")
    hdr = ["Address", "List Price", "Bd", "Ba", "SqFt", "$/SF", "DOM", "URL"]
    for j, h in enumerate(hdr):
        ws.cell(row=4, column=j + 1, value=h)
    _header(ws, 4, len(hdr))
    for i, l in enumerate(active):
        vals = [l.address, l.price, l.beds, l.baths, l.sqft,
                round(l.ppsf) if l.ppsf else None, l.days_on_zillow, l.detail_url]
        for j, v in enumerate(vals):
            ws.cell(row=5 + i, column=j + 1, value=v)
        ws.cell(row=5 + i, column=2).number_format = "$#,##0"
    _widths(ws, [24, 12, 5, 5, 7, 7, 6, 45])

    ws = wb.create_sheet("Rehab Scenarios")
    _title(ws, f"Rehab scenarios - {subject['sqft']:,} sqft, built {subject['year_built']}",
           "SiftStack rehab engine, Knoxville multiplier, tier 2, incl. 13% soft costs. "
           f"Full gut adds ${GUT_ALLOWANCE_PER_SQFT:.0f}/sf demo-drywall allowance.")
    for i, (label, cost) in enumerate([("Cosmetic (wholetail, keeps bed count)", rehab["cosmetic"]),
                                       ("Mid reno (kitchen/baths/systems, no envelope)", rehab["mid"]),
                                       ("Full gut (everything + allowance)", rehab["full_gut"])]):
        ws.cell(row=4 + i, column=1, value=label)
        c = ws.cell(row=4 + i, column=2, value=cost)
        c.number_format = "$#,##0"
        ws.cell(row=4 + i, column=3, value=f"${cost / subject['sqft']:,.0f}/sf" if subject["sqft"] else "")
    _widths(ws, [46, 14, 10])

    if buyers:
        ws = wb.create_sheet("Buyer Targets")
        _title(ws, "Local buyer matches (latest buyer-prospecting export, zip-filtered)")
        hdr = ["Buyer", "Tags", "Notes"]
        for j, h in enumerate(hdr):
            ws.cell(row=4, column=j + 1, value=h)
        _header(ws, 4, len(hdr))
        for i, b in enumerate(buyers):
            ws.cell(row=5 + i, column=1, value=b.get("owner_name", ""))
            ws.cell(row=5 + i, column=2, value=b.get("tags", ""))
            ws.cell(row=5 + i, column=3, value=b.get("notes", ""))
        _widths(ws, [36, 30, 60])

    wb.save(out_path)
    return out_path


# ── CLI ───────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Build a boundary-filtered comp package workbook")
    ap.add_argument("--address", required=True)
    ap.add_argument("--city", default="Knoxville")
    ap.add_argument("--state", default="TN")
    ap.add_argument("--zip", dest="zip_code", required=True)
    ap.add_argument("--beds", type=int, help="Override (county card beats Zillow)")
    ap.add_argument("--baths", type=float)
    ap.add_argument("--sqft", type=int)
    ap.add_argument("--year-built", type=int)
    ap.add_argument("--months", type=int, default=12, help="Sold lookback (default 12)")
    ap.add_argument("--bbox", help="lat_min,lat_max,lon_min,lon_max boundary box")
    ap.add_argument("--streets", help="Regex of in-boundary street names")
    ap.add_argument("--out", help="Output .xlsx path")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    subject_data = fetch_subject_property(args.address, args.city, args.state, args.zip_code)
    subject = {
        "address": f"{args.address}, {args.city}, {args.state} {args.zip_code}",
        "beds": args.beds or (subject_data.bedrooms if subject_data else 3),
        "baths": args.baths or (subject_data.bathrooms if subject_data else 2.0),
        "sqft": args.sqft or (subject_data.sqft if subject_data else 0),
        "year_built": args.year_built or (subject_data.year_built if subject_data else 0),
    }
    logger.info("Subject: %s | %s sqft %sbd/%sba built %s", subject["address"],
                subject["sqft"], subject["beds"], subject["baths"], subject["year_built"])

    api = ZillowMarketAPI()
    location = f"{args.city}, {args.state} {args.zip_code}"
    sold = api.pull_sold(location, months_back=args.months)
    active = api.pull_active(location)

    if args.bbox:
        lat_min, lat_max, lon_min, lon_max = (float(x) for x in args.bbox.split(","))
        sold = filter_bbox(sold, lat_min, lat_max, lon_min, lon_max)
        active = filter_bbox(active, lat_min, lat_max, lon_min, lon_max)
    if args.streets:
        pattern = re.compile(args.streets, re.I)
        sold = filter_streets(sold, pattern)
        active = filter_streets(active, pattern)
    logger.info("Boundary-filtered: %d sold, %d active", len(sold), len(active))
    if not sold:
        logger.error("No sold comps inside boundary - widen the boundary or months")
        return 1

    arv = dual_track_arv(subject["beds"], subject["sqft"], sold)
    rehab = rehab_scenarios(subject["sqft"], subject["beds"], subject["baths"],
                            subject["year_built"])
    buyers = local_buyers(args.zip_code)

    safe = "".join(c if c.isalnum() or c in " -" else "_" for c in args.address)[:40].strip().replace(" ", "_")
    out = args.out or str(Path.cwd() / f"{safe}_Comp_Package.xlsx")
    build_workbook(subject, sold, active, arv, rehab, buyers, out)

    base = arv.get("base") or {}
    print(f"\nARV base: ${base.get('arv', 0):,.0f} | upside: ${(arv.get('upside') or {}).get('arv', 0):,.0f}")
    print(f"Rehab: cosmetic ${rehab['cosmetic']:,.0f} | mid ${rehab['mid']:,.0f} | gut ${rehab['full_gut']:,.0f}")
    print(f"Workbook: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
