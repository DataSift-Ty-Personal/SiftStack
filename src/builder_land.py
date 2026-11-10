"""Builder land desk: the land acquisition department for small builders, run off the county deeds.

The model (Ty, 2026-11-06, from a wholesaler friend): find the buyers who explicitly target one niche, learn
exactly what each one buys, and build the seller list to match. On land those buyers are builders, and every
purchase they make is on the deeds, so before the first call we already know where each one buys, what lot size,
what they pay and what they build. This module reads that off the opensearch MCP for any county, writes each
builder's buy box, and matches the lots on the ground today to the builders who would buy them. Read-only
everywhere; nothing is pulled into an account.

    python src/builder_land.py --market knox_blount --fips 47093,47009 --phase all
    python src/builder_land.py --market union_oh --fips 39159 --phase all \
        --lots-csv output/vacant_land/union_oh/mail_list.csv
    python src/builder_land.py --phase national       # rank every county by active small land buyers
    python tests/test_builder_land.py                 # zero network

Phases: pull (properties, parallel id-range streams, resumable) -> history (sales-history for the new builds,
the land sold since 2019 and the old houses entities bought since 2021) -> analyze (deed chains, buyers, buy
boxes, supply, matches) -> export (workbook). `--from-dir` imports an earlier pull instead of re-reading the
cluster. Output: output/builder_land/<market>/.

Deed-chain rules, each one silently wrong without it (Knox, 2026-11-06):
- the lot purchase is the JUMP in the chain (a sale followed by one at 2x or more after the build), never
  today's AVM: prices rose ~70% since 2019, so an old new-home sale passes for a lot buy;
- the roll lags construction: a "vacant" lot resold at $150K+ and 3x is a house, not a lot flip;
- family transfers (shared surname) and multi-parcel deeds (over 3x the parcel AVM) are not purchases;
- sales-history has no fips, sale_amount comes back as a STRING and sale_date as epoch ms.
"""
import argparse
import collections
import concurrent.futures as cf
import datetime as dt
import glob
import json
import math
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "builder_land"
REG = ROOT / "output" / "dispo_buyers"
BS_INDEX = "buyer-stats-by-county-property-type-pattern--*"

SKIP_TYPES = ["condo", "townhouse", "warehouse", "multi_family_commercial"]
FIELDS = ["dataflik_id", "fips", "property_type_group", "property_type", "land_use", "county_land_use_code", "year_built",
          "living_area_sqft", "area_lot_acres", "lot_frontage_feet", "lot_type", "property_value", "assessed_value",
          "assessed_land_value", "last_sale_date", "last_sale_price", "last_sale_investor_transaction_type",
          "is_last_sale_intrafamily", "is_builder_owned", "is_corporate_owned", "is_trust_owned", "is_new_construction",
          "owner_occupied", "mailing_address_state", "mailing_address_city", "mailing_full_address", "owner_age",
          "subdivision_name", "neighborhood", "property_zip", "property_city", "property_address", "location",
          "flood_zone", "flood_risk", "full_name", "full_name_2", "mls_data.status", "is_active_listing",
          "has_postal_address", "bedroom_count", "bath_count", "parcel_number",
          "distressor_data.is_estate_sales.is_active", "distressor_data.is_probate_properties.is_active",
          "distressor_data.is_heirs_property.is_active", "distressor_data.is_obituary_property.is_active",
          "distressor_data.is_senior_homeowners.is_active", "distressor_data.is_free_and_clear.is_active",
          "distressor_data.is_vacant_property.is_active", "distressor_data.is_recent_deliquent.is_active",
          "distressor_data.is_inheritance_property.is_active", "distressor_data.is_pre_probate.is_active",
          "distressor_data.is_notice_of_default.is_active"]
SALES_FIELDS = ["dataflik_id", "sale_date", "sale_amount", "buyer_full_name", "buyer_2_full_name", "seller_full_name",
                "seller_2_full_name", "is_cash_sale", "transaction_id"]
STATE_FIPS = {"01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
              "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
              "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
              "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
              "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
              "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY", "72": "PR"}

NEW_BUILD_YEAR = 2016
SINCE = 2021
NOW = dt.datetime.now()
NOW_MS = NOW.timestamp() * 1000
DAY_MS = 86400000
SINCE_MS = dt.datetime(SINCE, 1, 1).timestamp() * 1000
LUX, MID = 750000, 450000          # finished-value bands, calibrated on Knox (override with --lux / --mid)
FEE = 12000                        # Knox infill quick lot flips cleared a median $15,000 (middle half $9-26K)
BOX_TYPES = ("builder", "holder (not built yet)")
# a lot fits a builder who bought within this distance; acreage and tracts are bought across a wider area
RADIUS = {"lot": 600, "acreage": 3000, "tract": 5000}
CLASS_OF = {"urban_infill": "lot", "midrange_rebuild": "lot", "luxury_rebuild": "lot", "leftover_lot": "lot",
            "rural_homesite": "acreage", "development_tract": "tract"}
DEFAULT_ACRES = {"lot": (0.06, 1.0), "acreage": (1.5, 6.0), "tract": (5.0, 300.0)}
MIN_LOT_ACRES = 0.06               # ~2,600 sq ft: below it a vacant parcel is a sliver or a cemetery plot, not a lot
DISTRESS = ["is_estate_sales", "is_probate_properties", "is_heirs_property", "is_obituary_property",
            "is_inheritance_property", "is_pre_probate", "is_notice_of_default", "is_recent_deliquent"]
NICHES = ("luxury_rebuild", "midrange_rebuild", "urban_infill", "leftover_lot", "rural_homesite", "development_tract")
SMALL_MAX = 100                    # national screen: a "small" land buyer has at most this many purchases anywhere

SUFFIX = re.compile(r"\b(LLC|L L C|INC|CORP|CORPORATION|CO|LP|LLP|LTD|PLLC)\b")
ENTITY = re.compile(r"\b(LLC|L L C|INC|CORP|CORPORATION|COMPANY|LP|LLP|LTD|HOMES|BUILDERS?|BUILDING|CONSTRUCTION|"
                    r"CONTRACTING|PROPERTIES|PROPERTY|INVESTMENTS?|GROUP|HOLDINGS|DEVELOPMENT|DEVELOPERS|PARTNERS|"
                    r"REALTY|ENTERPRISES|CAPITAL|VENTURES|LAND|BANK|ASSOCIATION|CHURCH|AUTHORITY|CITY OF|COUNTY|"
                    r"PARTNERSHIP|FUND|HOUSING)\b")
BUILDER_NAME = re.compile(r"\b(HOMES|BUILDERS?|BUILDING|CONSTRUCTION|CONTRACTING|CUSTOM|RENOVATIONS?|CRAFTSMAN)\b")
GOV = re.compile(r"\b(COUNTY|STATE OF|CITY OF|TOWN OF|VILLAGE OF|TOWNSHIP|UNITED STATES|FCU|CREDIT UNION|BANK|MORTGAGE|"
                 r"SAVINGS|HOUSING AUTHORITY|KCDC|UTILITY|UTILITIES|BOARD OF|DEPARTMENT|DEPT|TVA|UNIVERSITY|SCHOOL|"
                 r"CHURCH|MINISTR\w*|CEMETERY|TRUSTEE)\b")
NONPROFIT = re.compile(r"HABITAT|HOUSING SOL|NEIGHBORHOOD HOUSING|HSNG DEV|HOUSING DEV|COMMUNITY DEV|\bCDC\b|FOUNDATION|LAND TRUST|"
                       r"LAND BANK")


# ---------------------------------------------------------------- names, numbers, geometry

def norm(n):
    n = (n or "").upper().replace("&", " AND ")
    n = re.sub(r"[.,'\"]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def key(n):
    return re.sub(r"\s+", " ", SUFFIX.sub("", norm(n))).strip()


def is_entity(n):
    return bool(ENTITY.search(norm(n)))


def same_party(a, b):
    """Deeds write LAST FIRST and the property index writes First Last, so compare token sets."""
    ta = {t for t in key(a).split() if len(t) > 1}
    tb = {t for t in key(b).split() if len(t) > 1}
    return bool(ta and tb) and (ta <= tb or tb <= ta or len(ta & tb) >= 2)


def family(s):
    """Buyer and seller share a surname token: a family transfer, not a purchase."""
    tb = {t for t in key(s.get("buyer_full_name")).split() if len(t) > 2}
    ts = {t for t in key(s.get("seller_full_name")).split() if len(t) > 2}
    return bool(tb & ts) and not (is_entity(s.get("buyer_full_name")) or is_entity(s.get("seller_full_name")))


def _utc(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)


def year(ms):
    return _utc(ms).year


def day(ms):
    return _utc(ms).strftime("%Y-%m-%d") if ms else None


def med(xs):
    xs = [x for x in xs if x]
    return int(statistics.median(xs)) if xs else None


def q(xs, p):
    xs = sorted(x for x in xs if x)
    return xs[int(round(p * (len(xs) - 1)))] if xs else None


def flag(r, k):
    return bool(((r.get("distressor_data") or {}).get(k) or {}).get("is_active"))


def latlon(r):
    m = re.match(r"POINT \(([-\d.]+) ([-\d.]+)\)", r.get("location") or "")
    return (float(m.group(2)), float(m.group(1))) if m else (None, None)


def meters(lat1, lon1, lat2, lon2):
    """Equirectangular distance: exact enough inside a county."""
    x = (lon2 - lon1) * 111320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    y = (lat2 - lat1) * 110540.0
    return math.hypot(x, y)


class Grid:
    """Square cells of `size_m` at the market's own latitude (a degree of longitude shrinks going north)."""

    def __init__(self, lat0, size_m=280):
        self.clat = size_m / 110540.0
        self.clon = size_m / (111320.0 * math.cos(math.radians(lat0)))

    def cell(self, lat, lon):
        return (int(lat // self.clat), int(lon // self.clon))

    def block(self, lat, lon, r=1):
        cy, cx = self.cell(lat, lon)
        return [(cy + dy, cx + dx) for dy in range(-r, r + 1) for dx in range(-r, r + 1)]


class PointIndex:
    """Points bucketed in cells one radius wide, so a 3x3 block always covers the radius."""

    def __init__(self, radius_m, lat0):
        self.r = radius_m
        self.g = Grid(lat0, radius_m)
        self.cells = collections.defaultdict(list)

    def add(self, lat, lon, item):
        self.cells[self.g.cell(lat, lon)].append((lat, lon, item))

    def near(self, lat, lon, r=None):
        r = r or self.r
        for c in self.g.block(lat, lon):
            for a, b, item in self.cells.get(c, ()):
                d = meters(lat, lon, a, b)
                if d <= r:
                    yield d, item


# ---------------------------------------------------------------- settings, deed chains, buyers

def setting(med_yb, new_share, n):
    if n < 15:
        return "rural"
    if med_yb <= 1985 and new_share <= 0.30:
        return "infill_old"
    if med_yb <= 2012 and new_share <= 0.25:
        return "leftover_lot"
    return "production"


def is_house(r):
    return r.get("property_type_group") == "single_family_residence" and (r.get("year_built") or 0) > 1800


def is_land(r):
    return r.get("land_use") == "Vacant" or str(r.get("property_type_group") or "").startswith("land")


def area_index(rows, grid):
    """classify(row) -> (setting, basis, median year built, share built 2010+, homes counted).

    A house's own subdivision when it holds 8+ homes (an old plat keeps its name, a new production phase gets a new
    one), else the 3x3 block of grid cells around it.
    """
    sub = collections.defaultdict(list)
    blk = collections.defaultdict(list)
    for r in rows:
        if not is_house(r):
            continue
        if r.get("subdivision_name"):
            sub[(r["fips"], r["subdivision_name"].strip().upper())].append(r["year_built"])
        if r.get("lat") is not None:
            blk[grid.cell(r["lat"], r["lon"])].append(r["year_built"])

    def classify(r):
        k = (r.get("fips"), (r.get("subdivision_name") or "").strip().upper())
        ybs = sub.get(k) if k[1] else None
        basis = "subdivision"
        if not ybs or len(ybs) < 8:
            basis = "block"
            ybs = [] if r.get("lat") is None else [y for c in grid.block(r["lat"], r["lon"]) for y in blk.get(c, [])]
        n = len(ybs)
        m = statistics.median(ybs) if ybs else 0
        share = sum(1 for y in ybs if y >= 2010) / n if n else 0
        return setting(m, share, n if basis == "block" else max(n, 15)), basis, m, round(share, 3), n
    return classify


def chain(r, sales):
    """The pre-build purchase and the new-home exit, read off the jump in the deed chain.

    1. consecutive priced sales (a, b): a dated Jan 1 (Y-6) to Dec 31 Y, b dated Jan 1 (Y-1) or later, and b at 2x a
       or more (1.5x when a predates the build year: a teardown costs more than a lot)
    2. no jump (the lot buyer still holds the finished house): the latest priced sale in the window at or under half
       the AVM deflated 8% a year back to the sale year
    middleman = the sale before the purchase, inside 365 days, whose buyer is the purchase's seller.
    """
    Y = r["year_built"]
    lo = dt.datetime(Y - 6, 1, 1).timestamp() * 1000
    hi = dt.datetime(Y, 12, 31).timestamp() * 1000
    yb1 = dt.datetime(Y - 1, 1, 1).timestamp() * 1000
    yb0 = dt.datetime(Y, 1, 1).timestamp() * 1000
    avm = r.get("property_value") or 0
    priced = [s for s in sales if (s.get("sale_amount") or 0) >= 5000 and not family(s)
              and not (avm and s["sale_amount"] > 3 * avm)]
    acq = exit_ = None

    def house_priced(b):
        # the exit is a finished house: a lot wholesaled at 1.5x before the build is not the jump
        return b["sale_amount"] >= (0.4 * avm / 1.08 ** max(0, NOW.year - year(b["sale_date"])) if avm else 100000)
    for a, b in zip(priced, priced[1:]):
        ratio = b["sale_amount"] / a["sale_amount"]
        if (lo <= a["sale_date"] <= hi and b["sale_date"] >= yb1 and house_priced(b)
                and (ratio >= 2.0 or (ratio >= 1.5 and a["sale_date"] < yb0))):
            acq, exit_ = a, b
            break
    if not acq:
        for s in priced:
            if lo <= s["sale_date"] <= hi and s["sale_amount"] <= 0.5 * avm / 1.08 ** max(0, NOW.year - year(s["sale_date"])):
                acq = s
        if not acq or any(s["sale_date"] > acq["sale_date"] for s in priced):
            return None      # a later priced sale with no jump is a resale we cannot place
    prior = [s for s in sales if s["sale_date"] < acq["sale_date"]]
    mid = None
    if prior:
        p = prior[-1]
        if (acq["sale_date"] - p["sale_date"]) <= 365 * DAY_MS and key(p.get("buyer_full_name")) == key(acq.get("seller_full_name")):
            mid = p
    return {"acq": acq, "exit": exit_, "mid": mid, "V": max(avm, exit_["sale_amount"] if exit_ else 0)}


def niche_of(d, lux=LUX, mid=MID):
    fin = d.get("exit_price") or d.get("value") or d.get("V") or 0
    if d["setting"] in ("infill_old", "leftover_lot") and (d.get("acres") or 0) >= 1.5:
        return "rural_homesite"      # acreage among old rural homes is a homesite, not a teardown or a subdivision lot
    if d["setting"] == "infill_old":
        return "luxury_rebuild" if fin >= lux else ("urban_infill" if fin < mid else "midrange_rebuild")
    return {"leftover_lot": "leftover_lot", "rural": "rural_homesite"}.get(d["setting"], "production")


def land_niche(r, classify, grid, blk_val, lux=LUX, mid=MID):
    """What kind of buyer a vacant parcel is for, from where it sits and what the houses around it are worth."""
    s0 = classify(r)[0]
    a = r.get("area_lot_acres") or 0
    if a >= 5 or (s0 == "production" and a >= 1.5):
        return "development_tract"   # raw acreage a subdivision builder or developer buys
    if a >= 1.5 or s0 == "rural":
        return "rural_homesite"
    if s0 == "infill_old":
        vals = [v for c in grid.block(r["lat"], r["lon"]) for v in blk_val.get(c, [])] if r.get("lat") is not None else []
        mv = statistics.median(vals) if vals else 0
        # the old houses around an infill lot sell for well under what gets built on it
        return "luxury_rebuild" if mv >= 0.73 * lux else ("urban_infill" if mv < mid * 2 / 3 else "midrange_rebuild")
    if s0 == "leftover_lot":
        return "leftover_lot"
    return "production"


def resale_kind(s, resale):
    """What happened after a vacant-lot purchase, read off the next deed by the same party."""
    if not resale or resale["sale_amount"] < 5000:
        return None, None
    days = round((resale["sale_date"] - s["sale_date"]) / DAY_MS)
    if resale["sale_amount"] >= 150000 and resale["sale_amount"] >= 3 * s["sale_amount"]:
        return "house", days         # a new house the assessor has not put on the roll yet
    if days <= 120:
        return "quick_flip", days    # bought and resold as a lot inside four months: a wholesale or double close
    return "lot_resale", days


def buyer_type(name, bs):
    """Builder, lot wholesaler, holder, lot flipper, nonprofit or government, from what the buyer did with the lots."""
    built = sum(b["status"] == "built" or b["resale_kind"] == "house" for b in bs)
    flipped = sum(b["resale_kind"] in ("quick_flip", "lot_resale") for b in bs)
    quick = sum(b["resale_kind"] == "quick_flip" for b in bs)
    if GOV.search(name):
        return "government or lender"
    if NONPROFIT.search(name):
        return "nonprofit"
    if quick >= 2 and quick >= built:
        return "lot wholesaler"      # double-closes lots more than it builds: a competitor, not an end buyer
    if built or BUILDER_NAME.search(name):
        return "builder"
    if flipped >= max(2, 0.4 * len(bs)):
        return "lot flipper"
    return "holder (not built yet)"


def bulk_flags(buys):
    """A takedown deed carries the deed's TOTAL price on every parcel it covers: flag it, and price each lot as
    its share of the deed so a 10-lot takedown is neither ten lots at the full price nor ten lots with no price."""
    n = collections.Counter((b["key"], b["date"], b["price"]) for b in buys)
    for b in buys:
        k = n[(b["key"], b["date"], b["price"])]
        b["bulk"] = k >= 3 or b.get("multi", False)
        # a deed that also carried parcels outside this list (a subdivision section) cannot be split: no lot price
        b["unit_price"] = None if b.get("multi") else (int(b["price"] / k) if k >= 3 else b["price"])


def events(bs, within_days=None):
    """Separate purchases: one deed date per purchase, so a takedown counts once."""
    return len({b["date"] for b in bs if within_days is None or NOW_MS - b["date"] <= within_days * DAY_MS})


def acre_range(acres, cls):
    """The lot sizes a buyer takes: p10-p90 of what they bought, widened a little; defaults when the record is thin."""
    dlo, dhi = DEFAULT_ACRES[cls]
    acres = [a for a in acres if a]
    if len(acres) >= 3:
        lo, hi = q(acres, .1), q(acres, .9)
        return round(max(0.02, lo * 0.8), 2), round(hi * 1.25, 2), q(acres, .25), q(acres, .75)
    if acres:
        return round(max(dlo * 0.5, min(acres) * 0.5), 2), round(min(dhi, max(acres) * 2), 2), min(acres), max(acres)
    return dlo, dhi, dlo, dhi


def buy_box(bs, builds):
    """One buyer's box, from their own deeds: where, what size, what they paid, what they build and sell."""
    classes = {}
    by = collections.defaultdict(list)
    for b in bs:
        if CLASS_OF.get(b["niche"]):
            by[CLASS_OF[b["niche"]]].append(b)
    for cls, xs in by.items():
        lo, hi, core_lo, core_hi = acre_range([b.get("acres") for b in xs], cls)
        prices = [b["unit_price"] for b in xs if b["unit_price"]]
        classes[cls] = {"n": len(xs), "events": events(xs), "acres_lo": lo, "acres_hi": hi, "core_lo": core_lo, "core_hi": core_hi,
                        "acres_med": med(b.get("acres") for b in xs), "price_p25": q(prices, .25),
                        "price_med": med(prices), "price_p75": q(prices, .75)}
    exits = [d for d in builds if d.get("exit_price")]
    days = [round((d["exit_date"] - d["acq_date"]) / DAY_MS) for d in exits if d.get("exit_date")]
    years = max(0.5, (NOW_MS - SINCE_MS) / (365.25 * DAY_MS))
    return {
        "classes": classes,
        "buys_12m": sum(1 for b in bs if NOW_MS - b["date"] <= 365 * DAY_MS),
        "events": events(bs), "events_24m": events(bs, 730),
        "per_year": round(len(bs) / years, 1),
        "builds": len(builds), "build_sqft": med(d.get("sqft") for d in builds),
        "sells_for": med(d["exit_price"] for d in exits), "lot_to_sale_days": med(days),
        "areas": [a for a, _ in collections.Counter(b["area"] for b in bs if b.get("area")).most_common(3)],
        "zips": [z for z, _ in collections.Counter(b["zip"] for b in bs if b.get("zip")).most_common(4)],
        "from_person_share": round(sum(not b["seller_entity"] for b in bs) / len(bs), 2) if bs else None,
    }


def fit(parcel, cls, near, box):
    """Score how well one parcel fits one buyer's box; None when it does not fit.

    near = [(distance m, buy)] of this buyer's purchases within the class radius. A parcel with no acreage on
    record cannot be checked on size, so it fits on location alone and earns no size credit.
    """
    cb = box["classes"].get(cls)
    if not cb or cb["events"] < 2 or not near:
        return None      # one purchase of a kind is an event, not a buy box
    a = parcel.get("acres") or 0
    if a and not (cb["acres_lo"] <= a <= cb["acres_hi"]):
        return None
    r = RADIUS[cls]
    dmin = min(d for d, _ in near)
    last = max(b["date"] for _, b in near)
    age = NOW_MS - last
    score = 50 * (1 - dmin / r) + 8 * min(len(near), 5) + (15 if age <= 365 * DAY_MS else 8 if age <= 730 * DAY_MS else 0)
    if a and cb["core_lo"] <= a <= cb["core_hi"]:
        score += 10
    return {"dist": round(dmin), "nearby": len(near), "last_nearby": day(last), "score": round(score),
            "prices": [b["unit_price"] for _, b in near if b["unit_price"]]}


def reference_price(matches, area_prices):
    """What a builder pays for this parcel: what the fitting builders paid nearby, else every niche buy nearby."""
    ps = [p for m in matches for p in m["prices"]]
    if len(ps) >= 3:
        return med(ps), "fitting builders' buys nearby (%d)" % len(ps)
    if len(area_prices) >= 3:
        return med(area_prices), "all niche lot buys nearby (%d)" % len(area_prices)
    return None, None


def offer(ref, fee):
    """Offer to the seller = what the builder pays minus our fee; None when the fee would eat most of the lot."""
    if not ref:
        return None
    o = ref - fee
    return int(round(o / 500) * 500) if o >= 0.4 * ref else None


# ---------------------------------------------------------------- work dir and loaders

def work_dir(args):
    w = OUT / args.market
    w.mkdir(parents=True, exist_ok=True)
    return w


def _load(p, default=None):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return default


def _dump(obj, p):
    tmp = str(p) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, p)


def logger(w):
    def log(msg):
        line = "%s %s" % (dt.datetime.now().strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        with open(w / "run.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return log


def source_dirs(w):
    """The market's own folder plus any earlier pulls it names in sources.json (a region built from county pulls
    reads them in place instead of copying a gigabyte of rows)."""
    return [w] + [OUT / m for m in (_load(w / "sources.json", []) or [])]


def load(w):
    """Every pulled row once, with lat/lon; a stream stopped mid-line leaves a broken last line, which is skipped."""
    seen = {}
    for f in sorted(f for d in source_dirs(w) for f in glob.glob(str(d / "props_*.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            for x in fh:
                try:
                    r = json.loads(x)
                except ValueError:
                    continue
                seen[r["dataflik_id"]] = r
    rows = list(seen.values())
    for r in rows:
        r["lat"], r["lon"] = latlon(r)
    return rows


def load_sales(w):
    by = collections.defaultdict(list)
    seen = set()
    for f in sorted(f for d in source_dirs(w) for f in glob.glob(str(d / "sales" / "*.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            for x in fh:
                try:
                    s = json.loads(x)
                except ValueError:
                    continue
                try:
                    s["sale_amount"] = int(float(s.get("sale_amount") or 0))     # the index returns it as a string
                except ValueError:
                    s["sale_amount"] = 0
                k = (s.get("dataflik_id"), s.get("transaction_id"), s.get("sale_date"), s["sale_amount"])
                if not s.get("sale_date") or k in seen:
                    continue
                seen.add(k)
                by[s["dataflik_id"]].append(s)
    for v in by.values():
        v.sort(key=lambda s: (s["sale_date"], s.get("transaction_id") or 0))
    return by


# ---------------------------------------------------------------- phase: pull

def _query(fips):
    return {"bool": {"filter": [{"terms": {"fips": fips}}], "must_not": [{"terms": {"property_type_group": SKIP_TYPES}}]}}


def _resume_id(path):
    """The last complete row's id, after cutting off a half-written last line."""
    if not path.exists():
        return None
    data = path.read_bytes()
    cut = data.rfind(b"\n") + 1
    if cut < len(data):
        path.write_bytes(data[:cut])
    for ln in reversed(data[:cut].splitlines()):
        try:
            return json.loads(ln)["dataflik_id"]
        except (ValueError, KeyError):
            continue
    return None


def balanced_edges(buckets, n, lo, hi):
    """Stream boundaries with about equal rows each, from an id histogram [(bucket start, rows)]. Ids are not spread
    evenly across a county, so equal id ranges leave one stream holding most of the rows."""
    total = sum(c for _, c in buckets)
    edges, cum, want = [lo], 0, 1
    for start, c in buckets:
        if want < n and cum >= want * total / n and start > edges[-1]:
            edges.append(start)
            want += 1
        cum += c
    return edges + [hi]


def id_buckets(c, base, lo, hi, total, n, levels=3):
    """An id histogram fine enough to split: ids cluster (Franklin OH put 276,000 of 370,000 rows in two of 400 buckets),
    so any bucket holding more than half a stream's share gets its own histogram, up to `levels` deep."""
    def hist(a, b, parts):
        span = max(1, (b - a) // parts)
        q = {"bool": {"filter": base["bool"]["filter"] + [{"range": {"dataflik_id": {"gte": a, "lt": b}}}],
                      "must_not": base["bool"]["must_not"]}}
        h = c.search({"query": q, "aggs": {"h": {"histogram": {"field": "dataflik_id", "interval": span, "min_doc_count": 1}}}}, 0)
        return [(max(a, int(x["key"])), x["doc_count"], span) for x in h["aggregations"]["h"]["buckets"]]
    buckets = hist(lo, hi, 400)
    for _ in range(levels):
        big = [x for x in buckets if x[1] > total / (2 * n) and x[2] > 1]
        if not big:
            break
        out = []
        for x in buckets:
            out.extend(hist(x[0], x[0] + x[2], 200) if x in big else [x])
        buckets = sorted(out)
    return [(a, cnt) for a, cnt, _ in buckets]


def _pull_stream(w, i, base, lo, hi, log):
    from opensearch_mcp import OpenSearchMCP
    path, done = w / ("props_%d.jsonl" % i), w / ("props_%d.done" % i)
    if done.exists():
        return 0
    last = _resume_id(path)
    start = lo if last is None else last + 1
    qy = {"bool": {"filter": base["bool"]["filter"] + [{"range": {"dataflik_id": {"gte": start, "lt": hi}}}],
                   "must_not": base["bool"]["must_not"]}}
    c, n, t = OpenSearchMCP(), 0, time.time()
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for row in c.page(qy, [{"dataflik_id": "asc"}], FIELDS, 10 ** 8):
                fh.write(json.dumps(row) + "\n")
                n += 1
                if n % 10000 == 0:
                    log("pull stream %d: %d rows %.0fs" % (i, n, time.time() - t))
    finally:
        c.close()
    done.write_text(str(n))
    log("pull stream %d done: %d rows %.0fs" % (i, n, time.time() - t))
    return n


def _county_names(fips):
    from opensearch_mcp import OpenSearchMCP
    c, names = OpenSearchMCP(), {}
    try:
        for f in fips:
            try:
                h = c.search({"query": {"term": {"fips": f}}, "_source": ["county_name"]}, 1, BS_INDEX)["hits"]["hits"]
                names[f] = h[0]["_source"].get("county_name") if h else f
            except RuntimeError:
                names[f] = f
    finally:
        c.close()
    return names


def phase_pull(args):
    w = work_dir(args)
    log = logger(w)
    fips = args.fips
    meta = _load(w / "meta.json", {})
    meta.update({"market": args.market, "fips": fips, "pulled": NOW.strftime("%Y-%m-%d")})
    if not meta.get("county_names"):
        meta["county_names"] = _county_names(fips)
    meta["name"] = args.name or meta.get("name") or " and ".join(
        meta["county_names"].get(f, f) for f in fips) + ", " + "/".join(sorted({STATE_FIPS.get(f[:2], "") for f in fips}))
    _dump(meta, w / "meta.json")
    if args.from_dir:
        n = 0
        with open(w / "props_import.jsonl", "w", encoding="utf-8") as out:
            seen = set()
            for f in sorted(glob.glob(os.path.join(args.from_dir, "*.jsonl"))):
                base = os.path.basename(f)
                if base.startswith(("sales", "mls", "props_")):
                    continue
                with open(f, encoding="utf-8") as fh:
                    for x in fh:
                        try:
                            r = json.loads(x)
                        except ValueError:
                            continue
                        if r.get("fips") in fips and r["dataflik_id"] not in seen:
                            seen.add(r["dataflik_id"])
                            out.write(json.dumps(r) + "\n")
                            n += 1
        log("imported %d properties from %s" % (n, args.from_dir))
        return 0
    from opensearch_mcp import OpenSearchMCP
    base = _query(fips)
    c = OpenSearchMCP()
    try:
        total = c.count(base)

        def edge(order):
            return c.search({"query": base, "sort": [{"dataflik_id": order}], "_source": ["dataflik_id"]}, 1)["hits"]["hits"][0]["_source"]["dataflik_id"]
        lo, hi = edge("asc"), edge("desc") + 1
        edges = meta.get("edges") if glob.glob(str(w / "props_*.jsonl")) else None   # a resume keeps its boundaries
        if not edges:
            edges = balanced_edges(id_buckets(c, base, lo, hi, total, max(1, args.streams)), max(1, args.streams), lo, hi)
    finally:
        c.close()
    meta.update({"parcels": total, "edges": edges})
    _dump(meta, w / "meta.json")
    n = len(edges) - 1
    log("pull %s: %d parcels in %d streams" % (",".join(fips), total, n))
    with cf.ThreadPoolExecutor(n) as ex:
        got = sum(ex.map(lambda i: _pull_stream(w, i, base, edges[i], edges[i + 1], log), range(n)))
    rows = len(load(w))
    log("pull done: %d new rows this run, %d parcels on disk of %d counted" % (got, rows, total))
    if rows < 0.98 * total:
        log("WARNING: fewer parcels on disk than the count; re-run --phase pull to resume")
        return 1
    return 0


# ---------------------------------------------------------------- phase: history

def history_ids(rows):
    """Ids whose deed chain we need: every new build, every vacant parcel sold since 2019, and every old house an
    entity bought since 2021 (teardowns in progress show up there)."""
    nb = [r for r in rows if is_house(r) and r["year_built"] >= NEW_BUILD_YEAR]
    land = [r for r in rows if is_land(r) and (r.get("last_sale_date") or 0) >= 1546300800000]
    held = [r for r in rows if is_house(r) and r["year_built"] <= 1985 and (r.get("is_builder_owned") or r.get("is_corporate_owned"))
            and (r.get("last_sale_date") or 0) >= 1609459200000]
    return sorted({r["dataflik_id"] for r in nb + land + held})


def _sales_stream(w, batches, ids, log):
    from opensearch_mcp import OpenSearchMCP
    sort = [{"dataflik_id": "asc"}, {"sale_date": "asc"}, {"transaction_id": "asc"}]
    c, n = OpenSearchMCP(), 0
    try:
        for b in batches:
            dest = w / "sales" / ("batch_%05d.jsonl" % b)
            if dest.exists():
                continue
            qy = {"bool": {"filter": [{"terms": {"dataflik_id": ids[b * 500:(b + 1) * 500]}}],
                           "must_not": [{"term": {"is_deleted": True}}]}}
            tmp = str(dest) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for row in c.page(qy, sort, SALES_FIELDS, 10 ** 7, "sales-history"):
                    fh.write(json.dumps(row) + "\n")
                    n += 1
            os.replace(tmp, dest)
    finally:
        c.close()
    return n


def phase_history(args):
    w = work_dir(args)
    log = logger(w)
    rows = load(w)
    if not rows:
        raise SystemExit("no properties on disk: run --phase pull first")
    ids = history_ids(rows)
    _dump(ids, w / "hist_ids.json")
    (w / "sales").mkdir(exist_ok=True)
    if args.from_dir:
        want, n = set(ids), 0
        with open(w / "sales" / "batch_import.jsonl", "w", encoding="utf-8") as out:
            for f in sorted(glob.glob(os.path.join(args.from_dir, "sales*.jsonl"))):
                with open(f, encoding="utf-8") as fh:
                    for x in fh:
                        try:
                            s = json.loads(x)
                        except ValueError:
                            continue
                        if s.get("dataflik_id") in want:
                            out.write(x if x.endswith("\n") else x + "\n")
                            n += 1
        log("imported %d sales for %d ids from %s" % (n, len(ids), args.from_dir))
        return 0
    nb = (len(ids) + 499) // 500
    k = max(1, args.streams)
    log("history: %d ids in %d batches over %d streams" % (len(ids), nb, k))
    t = time.time()
    with cf.ThreadPoolExecutor(k) as ex:
        got = sum(ex.map(lambda i: _sales_stream(w, list(range(i, nb, k)), ids, log), range(k)))
    have = len(glob.glob(str(w / "sales" / "batch_0*.jsonl")))
    log("history done: %d sales rows this run, %d of %d batches on disk, %.0fs" % (got, have, nb, time.time() - t))
    return 0 if have >= nb else 1


# ---------------------------------------------------------------- phase: analyze

def build_deals(nb, sales):
    deals = []
    for r in nb:
        c = chain(r, sales.get(r["dataflik_id"], []))
        if not c:
            continue
        a, e, m = c["acq"], c["exit"], c["mid"]
        deals.append({
            "id": r["dataflik_id"], "fips": r["fips"], "setting": r["setting"], "yb": r["year_built"],
            "address": r.get("property_address"), "city": r.get("property_city"), "zip": r.get("property_zip"),
            "area": r.get("neighborhood") or r.get("property_zip"), "lat": r.get("lat"), "lon": r.get("lon"),
            "acres": r.get("area_lot_acres"), "sqft": r.get("living_area_sqft"), "value": r.get("property_value"), "V": c["V"],
            "acq_date": a["sale_date"], "acq_year": year(a["sale_date"]), "acq_price": a["sale_amount"],
            "builder": norm(a.get("buyer_full_name")), "builder_key": key(a.get("buyer_full_name")),
            "builder_entity": is_entity(a.get("buyer_full_name")), "seller_entity": is_entity(a.get("seller_full_name")),
            "exit_date": e["sale_date"] if e else None, "exit_price": e["sale_amount"] if e else None,
            "middleman": norm(m.get("buyer_full_name")) if m else None,
            "mid_markup": (a["sale_amount"] - m["sale_amount"]) if m and (m.get("sale_amount") or 0) >= 5000 else None,
            # still held by the person who bought the lot: they built their own home (a builder's client, not a builder)
            "end_user": (not e) and (not is_entity(a.get("buyer_full_name"))) and same_party(a.get("buyer_full_name"), r.get("full_name")),
        })
    return deals


def collect_buys(deals, land, sales):
    """Every niche purchase since 2021: lots that already carry the new house, plus lots still vacant (a new build's
    deed chain cannot see a lot bought last year and not yet built on)."""
    buys = []
    for d in deals:
        if d["niche"] != "production" and d["acq_date"] >= SINCE_MS:
            buys.append({"niche": d["niche"], "key": d["builder_key"], "name": d["builder"], "entity": d["builder_entity"],
                         "date": d["acq_date"], "price": d["acq_price"], "status": "built", "fips": d["fips"], "id": d["id"],
                         "area": d["area"], "zip": d["zip"], "address": "%s, %s" % (d["address"], d["city"]),
                         "acres": d["acres"], "seller_entity": d["seller_entity"], "exit": d["exit_price"],
                         "lat": d["lat"], "lon": d["lon"], "resale_kind": "house" if d["exit_price"] else None,
                         "resale_days": None, "resale_markup": None})
    for r in land:
        n = r["_niche"]
        if n == "production":
            continue
        ss = sales.get(r["dataflik_id"], [])
        for i, s in enumerate(ss):
            if s["sale_date"] < SINCE_MS or s["sale_amount"] < 5000 or s["sale_amount"] > 5000000 or family(s):
                continue
            if CLASS_OF.get(n) == "lot" and 0 < (r.get("area_lot_acres") or 0) < MIN_LOT_ACRES:
                continue
            bk = key(s.get("buyer_full_name"))
            resale = next((t for t in ss[i + 1:] if key(t.get("seller_full_name")) == bk), None)
            kind, days = resale_kind(s, resale)
            buys.append({"niche": n, "key": bk, "name": norm(s.get("buyer_full_name")), "entity": is_entity(s.get("buyer_full_name")),
                         "date": s["sale_date"], "price": s["sale_amount"], "status": "vacant", "fips": r["fips"],
                         "id": r["dataflik_id"], "area": place(r)[2], "zip": place(r)[1],
                         "address": "%s, %s" % (r.get("property_address"), r.get("property_city")), "acres": r.get("area_lot_acres"),
                         "seller_entity": is_entity(s.get("seller_full_name")), "exit": None, "lat": r.get("lat"), "lon": r.get("lon"),
                         "resale_kind": kind, "resale_days": days,
                         # priced at 3x+ this parcel's value: the deed carried other parcels too
                         "multi": s["sale_amount"] >= 100000 and s["sale_amount"] > 3 * max(r.get("property_value") or 0, 1),
                         "resale_markup": (resale["sale_amount"] - s["sale_amount"]) if kind in ("quick_flip", "lot_resale") else None})
    bulk_flags(buys)
    return buys


def place(r):
    """(city, zip, area). A parcel with no postal address carries the taxing district as its city, matched to a
    same-named town elsewhere (Franklin OH: Columbus becomes Columbus Grove 45830), so it gets no place at all."""
    if r.get("has_postal_address") is False:
        return None, None, None
    return r.get("property_city"), r.get("property_zip"), r.get("neighborhood") or r.get("property_zip")


def seller_reason(r, repeat_keys, recent_years=2):
    """Why an owner is not someone we mail (None = mailable)."""
    owner = r.get("full_name") or ""
    if not owner:
        return "no owner name"
    if r.get("is_corporate_owned") or r.get("is_builder_owned") or is_entity(owner) or key(owner) in repeat_keys:
        return "owned by an entity or a builder"
    if r.get("is_active_listing") or (r.get("mls_data") or {}).get("status") in ("Active", "Pending", "Contingent"):
        return "listed on the MLS"
    sold = r.get("last_sale_date") or 0
    if sold and (NOW_MS - sold) < recent_years * 365 * DAY_MS:
        return "bought in the last 2 years"
    if r.get("lot_type") == "Landlocked":
        return "landlocked"
    return None


def owner_flags(r, home_state):
    sold = r.get("last_sale_date") or 0
    tenure = round((NOW_MS - sold) / (365.25 * DAY_MS), 1) if sold else None
    # on land, absentee and free-and-clear are near universal, so they carry no signal
    lm = {"out_of_state": bool(r.get("mailing_address_state")) and r.get("mailing_address_state") != home_state,
          "estate": any(flag(r, k) for k in DISTRESS[:6]), "long_tenure": bool(tenure and tenure >= 20),
          "delinquent": flag(r, "is_recent_deliquent"), "senior": flag(r, "is_senior_homeowners"),
          "trust": bool(r.get("is_trust_owned"))}
    return tenure, [k for k, v in lm.items() if v], any(flag(r, k) for k in DISTRESS) or flag(r, "is_vacant_property")


def phase_analyze(args):
    w = work_dir(args)
    log = logger(w)
    meta = _load(w / "meta.json", {})
    names = meta.get("county_names") or {}
    rows = load(w)
    sales = load_sales(w)
    if not rows or not sales:
        raise SystemExit("properties or sales history missing: run --phase pull and --phase history first")
    lux, mid = args.lux, args.mid
    lat0 = statistics.median([r["lat"] for r in rows if r.get("lat") is not None])
    grid = Grid(lat0)
    classify = area_index(rows, grid)
    home_state = STATE_FIPS.get(args.fips[0][:2], "")
    log("analyze: %d parcels, %d with sales history" % (len(rows), len(sales)))

    nb = []
    for r in rows:
        if is_house(r) and r["year_built"] >= NEW_BUILD_YEAR:
            r["setting"] = classify(r)[0]
            nb.append(r)
    deals = build_deals(nb, sales)
    for d in deals:
        d["niche"] = niche_of(d, lux, mid)
    blk_val = collections.defaultdict(list)
    for r in rows:
        if r.get("lat") is not None and is_house(r) and r.get("property_value"):
            blk_val[grid.cell(r["lat"], r["lon"])].append(r["property_value"])
    land = [r for r in rows if is_land(r)]
    for r in land:
        r["_niche"] = land_niche(r, classify, grid, blk_val, lux, mid)
    buys = collect_buys(deals, land, sales)

    # buyers with 2+ niche buys since 2021, across niches (a builder buying infill and leftover lots is one buyer)
    groups = collections.defaultdict(list)
    for b in buys:
        if b["key"]:
            groups[b["key"]].append(b)
    deals_by = collections.defaultdict(list)
    for d in deals:
        deals_by[d["builder_key"]].append(d)
    buyers = []
    for k, bs in groups.items():
        if len(bs) < 2:
            continue
        name = collections.Counter(b["name"] for b in bs).most_common(1)[0][0]
        qf = [b for b in bs if b["resale_kind"] == "quick_flip"]
        buyers.append({"key": k, "name": name, "type": buyer_type(name, bs), "entity": bs[0]["entity"],
                       "buys": len(bs), "events": events(bs), "built": sum(b["status"] == "built" or b["resale_kind"] == "house" for b in bs),
                       "vacant": sum(b["status"] == "vacant" for b in bs), "quick_flips": len(qf),
                       "quick_flip_markup": med(b["resale_markup"] for b in qf),
                       "main_niche": collections.Counter(b["niche"] for b in bs).most_common(1)[0][0],
                       "niche_mix": ", ".join("%s %d" % x for x in collections.Counter(b["niche"] for b in bs).most_common()),
                       "last_buy": day(max(b["date"] for b in bs)), "last_ms": max(b["date"] for b in bs),
                       "counties": sorted({names.get(b["fips"], b["fips"]) for b in bs}),
                       "sample": [b["address"] for b in sorted(bs, key=lambda b: -b["date"])[:3]]})
    buyers.sort(key=lambda b: -b["buys"])
    btype = {b["key"]: b["type"] for b in buyers}
    reach = registry_status()

    boxes = {}
    for b in buyers:
        if (b["type"] in BOX_TYPES and b["buys"] >= args.min_buys and b["events"] >= 2
                and NOW_MS - b["last_ms"] <= args.active_months * 30.4 * DAY_MS):
            bx = buy_box(groups[b["key"]], deals_by.get(b["key"], []))
            bx.update({k: b[k] for k in ("key", "name", "type", "buys", "built", "vacant", "main_niche", "niche_mix",
                                          "last_buy", "counties", "sample")})
            bx["reach"] = reach.get(b["key"], "not in the dispo registry")
            boxes[b["key"]] = bx
    log("buyers 2+: %d, active boxes (%d+ buys, bought in %d months): %d" % (len(buyers), args.min_buys, args.active_months, len(boxes)))

    # spatial indexes: the box builders' own buys, and every niche buy (for a price when the fitting builders are thin)
    idx = {c: PointIndex(RADIUS[c], lat0) for c in RADIUS}
    all_idx = {c: PointIndex(RADIUS[c], lat0) for c in RADIUS}
    for b in buys:
        cls = CLASS_OF.get(b["niche"])
        if cls is None or b.get("lat") is None:
            continue
        all_idx[cls].add(b["lat"], b["lon"], b)
        if b["key"] in boxes:
            idx[cls].add(b["lat"], b["lon"], b)
    prod_idx = PointIndex(3000, lat0)
    for d in deals:
        if d["niche"] == "production" and d["acq_date"] >= SINCE_MS and d.get("lat") is not None and d["builder_entity"]:
            prod_idx.add(d["lat"], d["lon"], d)
    prod_blocks = collections.Counter()
    for r in nb:
        if r["setting"] == "production" and r["year_built"] >= 2019 and r.get("lat") is not None:
            prod_blocks[grid.cell(r["lat"], r["lon"])] += 1

    def matches_for(p, cls):
        near = collections.defaultdict(list)
        for d, b in idx[cls].near(p["lat"], p["lon"]):
            near[b["key"]].append((d, b))
        out = []
        for k, hs in near.items():
            m = fit(p, cls, hs, boxes[k])
            if m:
                m["key"], m["name"] = k, boxes[k]["name"]
                out.append(m)
        out.sort(key=lambda m: -m["score"])
        return out

    # supply: the lots on the ground today
    csv_lots = read_lots_csv(args.lots_csv) if args.lots_csv else None
    repeat = set(btype)
    lots, td, tracts, drop = [], [], [], collections.Counter()
    td_blocks = collections.defaultdict(list)
    for d in deals:
        if d.get("lat") is not None and d["niche"] in ("luxury_rebuild", "midrange_rebuild", "urban_infill") and d["acq_year"] >= 2019:
            td_blocks[grid.cell(d["lat"], d["lon"])].append(d.get("exit_price") or d["V"])
    for r in rows:
        if r.get("lat") is None:
            continue
        ext = csv_lots.get(r["dataflik_id"]) if csv_lots is not None else None
        if csv_lots is not None and ext is None:
            continue
        if ext and r.get("_niche") is None:
            r["_niche"] = land_niche(r, classify, grid, blk_val, lux, mid)      # the county screen called it a lot
        land_row = r.get("_niche") is not None
        if not land_row and not (is_house(r) and r["year_built"] <= 1975):
            continue
        why = None if ext else seller_reason(r, repeat)
        if why:
            if land_row:
                drop[why] += 1
            continue
        tenure, lflags, distressed = owner_flags(r, home_state)
        city, zp, area = place(r)
        if r.get("has_postal_address") is False:
            lflags.append("no postal address")
        base = {"id": r["dataflik_id"], "county": names.get(r["fips"], r["fips"]), "address": r.get("property_address"),
                "city": city, "zip": zp, "area": area,
                "owner": r.get("full_name"), "mailing": r.get("mailing_full_address"), "mail_state": r.get("mailing_address_state"),
                "value": r.get("property_value"), "acres": r.get("area_lot_acres"), "tenure_years": tenure,
                "flags": lflags, "on_distress_list": distressed, "flood": r.get("flood_risk"), "ext": ext,
                "lat": r["lat"], "lon": r["lon"]}
        if land_row:
            n = r["_niche"]
            if n == "production" and not ext:
                drop["lot inside a new production subdivision"] += 1
                continue
            cls = CLASS_OF.get(n, "lot")
            a = r.get("area_lot_acres") or 0
            if cls == "tract":
                if not (5 <= a <= 300) and not ext:
                    continue
                ms_ = matches_for(base, cls)
                prod = sum(prod_blocks.get(c, 0) for c in grid.block(r["lat"], r["lon"], 3))
                if prod < 15 and len(ms_) < 2 and not ext:
                    continue     # near no new subdivision and wanted by fewer than two tract buyers
                pa = [b["unit_price"] / b["acres"] for _, b in all_idx["tract"].near(r["lat"], r["lon"]) if b.get("acres") and b["unit_price"]]
                pb = collections.Counter(x["builder"] for _, x in prod_idx.near(r["lat"], r["lon"]))
                tracts.append({**base, "segment": n, "matches": ms_[:8], "n_fit": len(ms_), "production_homes_2km": prod,
                               "per_acre_ref": med(pa), "per_acre_n": len(pa),
                               "production_builders": [b for b, _ in pb.most_common(3)]})
                continue
            if a and not (MIN_LOT_ACRES <= a <= 6) and not ext:
                continue
            ms_ = matches_for(base, cls)
            area_p = [b["unit_price"] for _, b in all_idx[cls].near(r["lat"], r["lon"]) if b["unit_price"]]
            ref, basis = reference_price(ms_, area_p)
            lots.append({**base, "segment": n, "cls": cls, "matches": ms_[:8], "n_fit": len(ms_), "ref": ref,
                         "ref_basis": basis, "offer": offer(ref, args.fee)})
        elif not ext:
            sold = r.get("last_sale_date") or 0
            vals = [v for c in grid.block(r["lat"], r["lon"]) for v in td_blocks.get(c, []) if v]
            v = r.get("property_value") or 0
            if len(vals) < 2 or not v or v > 0.55 * statistics.median(vals) or (sold and NOW_MS - sold < 2 * 365 * DAY_MS):
                continue
            ms_ = matches_for(base, "lot")
            if not ms_:
                continue
            built = statistics.median(vals)
            td.append({**base, "year_built": r["year_built"], "sqft": r.get("living_area_sqft"), "zone_rebuilds": len(vals),
                       "zone_new_value": int(built), "value_gap": round(v / built, 2), "matches": ms_[:8], "n_fit": len(ms_),
                       "segment": "luxury_rebuild" if built >= lux else ("urban_infill" if built < mid else "midrange_rebuild")})
    for b in boxes.values():
        b["matched_lots"] = 0
        b["matched_core"] = 0
        b["matched_teardowns"] = 0
        b["matched_tracts"] = 0
    for kind, items in (("matched_lots", lots), ("matched_teardowns", td), ("matched_tracts", tracts)):
        for x in items:
            for m in x["matches"]:
                boxes[m["key"]][kind] += 1
    for x in lots:
        for m in x["matches"]:
            cb = boxes[m["key"]]["classes"].get(x["cls"], {})
            if x.get("acres") and cb and cb["core_lo"] <= x["acres"] <= cb["core_hi"]:
                boxes[m["key"]]["matched_core"] += 1

    for x in lots + td + tracts:
        for mm in x["matches"]:
            mm.pop("prices", None)       # used for the price reference above; not worth carrying to disk
    summary = niche_summary(args.fips, names, deals, buys, land, nb, btype, lux, mid)
    qf = [b for b in buys if b["resale_kind"] == "quick_flip" and CLASS_OF.get(b["niche"]) == "lot"]
    competitors = [b for b in buyers if b["type"] == "lot wholesaler"]
    res = {
        "meta": {**meta, "analyzed": NOW.strftime("%Y-%m-%d"), "parcels_on_disk": len(rows), "new_builds": len(nb),
                 "deals": len(deals), "buys_since_2021": len(buys), "fee": args.fee, "lux": lux, "mid": mid,
                 "min_buys": args.min_buys, "active_months": args.active_months, "radius": RADIUS,
                 "lots_csv": args.lots_csv, "home_state": home_state},
        "summary": summary,
        "boxes": sorted(boxes.values(), key=lambda b: (-b["events_24m"], -b["built"], -b["buys"])),
        "buyers": [{k: v for k, v in b.items() if k != "last_ms"} for b in buyers],
        "competitors": competitors,
        "quick_flips": {"n": len(qf), "sellers": len({b["key"] for b in qf}), "buy_median": med(b["price"] for b in qf),
                        "markup_median": med(b["resale_markup"] for b in qf), "markup_p25": q([b["resale_markup"] for b in qf], .25),
                        "markup_p75": q([b["resale_markup"] for b in qf], .75), "days_median": med(b["resale_days"] for b in qf)},
        "lots": sorted([x for x in lots if x["n_fit"] or x["ext"]], key=lambda x: (-x["n_fit"], -(x["matches"][0]["score"] if x["matches"] else 0))),
        "lots_unmatched": sum(1 for x in lots if not x["n_fit"]),
        "teardowns": sorted(td, key=lambda x: (-x["n_fit"], x["value_gap"])),
        "tracts": sorted(tracts, key=lambda x: (-x["n_fit"], -x["production_homes_2km"])),
        "dropped": dict(drop),
    }
    _dump(res, w / "analysis.json")
    log("boxes %d | lots fitting a builder %d (3+ builders %d), unmatched %d | teardowns %d | tracts %d | competitors %d" % (
        len(boxes), sum(1 for x in lots if x["n_fit"]), sum(1 for x in lots if x["n_fit"] >= 3), res["lots_unmatched"],
        len(td), len(tracts), len(competitors)))
    return 0


def niche_summary(fips, names, deals, buys, land, nb, btype, lux, mid):
    built_per_year = collections.Counter()
    for r in nb:
        if 2019 <= r["year_built"] <= 2025:
            built_per_year[(r["fips"], niche_of({"setting": r["setting"], "acres": r.get("area_lot_acres"),
                                                 "value": r.get("property_value")}, lux, mid))] += 1
    vac_n = collections.Counter((r["fips"], r["_niche"]) for r in land)
    out = []
    for f in fips:
        for n in NICHES:
            ds = [d for d in deals if d["fips"] == f and d["niche"] == n and d["acq_year"] >= 2019]
            bs = [b for b in buys if b["fips"] == f and b["niche"] == n]
            if not ds and not bs and not vac_n[(f, n)]:
                continue
            bk = collections.Counter(b["key"] for b in bs)
            mix = collections.Counter(btype.get(b["key"], "one-time buyer") for b in bs)
            out.append({
                "county": names.get(f, f), "niche": n, "built_per_year": round(built_per_year[(f, n)] / 7, 1),
                "vacant_lots_now": vac_n[(f, n)], "buys_since_2021": len(bs), "buyers": len(bk),
                "repeat_buyers_3plus": sum(1 for v in bk.values() if v >= 3),
                "share_by_repeat_buyers": round(sum(v for v in bk.values() if v >= 3) / len(bs), 3) if bs else None,
                "builder_share": round(mix.get("builder", 0) / len(bs), 3) if bs else None,
                "median_buy": med(b["price"] for b in bs if not b["bulk"]),
                "median_exit": med(d["exit_price"] for d in ds),
                "bought_from_person_share": round(sum(not b["seller_entity"] for b in bs) / len(bs), 3) if bs else None,
                "end_user_share": round(sum(d["end_user"] for d in ds) / len(ds), 3) if ds else None,
            })
    return out


def read_lots_csv(path):
    """A vacant_land mail list (or any CSV with a 'DataSift id' column): restrict the supply to those lots and carry
    their buildability verdict and offer band across."""
    import csv
    keep = ("Rank", "Tier", "Buildability", "Verdict", "Segment", "Sewer proxy", "Flood %", "Offer low", "Offer high")
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                i = int(float(row.get("DataSift id") or ""))
            except ValueError:
                continue
            out[i] = {k: row.get(k) for k in keep if k in row}
    return out


def registry_status():
    """buyer key -> reachability in the dispo buyer registry (output/dispo_buyers), when it exists here."""
    reg, phones = _load(REG / "registry.json", []), _load(REG / "phones.json", {})
    ks = {}
    for b in reg or []:
        status = "phones on file" if b.get("buyer_key") in (phones or {}) else "in registry, no phones"
        for n in [b.get("name")] + list(b.get("aliases") or []):
            if n:
                ks[key(n)] = status
    return ks


# ---------------------------------------------------------------- phase: national

BS_FIELDS = ["fips", "county_name", "buyer_id", "buyer_full_name", "buyer_city", "buyer_state", "pattern",
             "local_group_investor_purchases", "total_investor_purchases", "idle_months"]
# public bodies, associations and nonprofits buy land too, and none of them buys a lot from us
JUNK = re.compile(GOV.pattern + "|" + NONPROFIT.pattern + r"|ASSOCIAT|HOMEOWNER|PROPERTY OWNERS|\bPOA\b|\bHOA\b|DISTRICT|"
                  r"ELECTRIC|SANCTUAR|CONSERVANCY|COOPERATIVE|RAILROAD|\bWATER (AUTH|DIST|CO|UTIL)")


BUILDER_WILDCARDS = ["*HOMES*", "*BUILDER*", "*CONSTRUCTION*", "*CONTRACTING*", "*CUSTOM*", "*BUILDING*"]


def national_rank(by_pat, new_homes=None, named=None):
    """County table. The builder signal is NAMED small builders buying single-family parcels (a builder's lot counts as
    single family once the house is on the roll): Knox 29 (Ball Homes, Volhomes, Palmetto Homes, Worley Builders),
    Union OH 3, Marion FL 39 (the Marion Oaks scattered-lot builders). It misses builders without a builder name
    (Braden Family Properties), so it ranks markets and does not size them.

    The land patterns measure the other side. Land FLIP is land bought and resold as LAND, because a lot that became a
    house leaves the land class: land resellers (the Florida and Arizona platted-lot sellers), not builders. Land
    WHOLESALE is the lot wholesalers (Knox: SMP Holdings), the direct competition.
    """
    new_homes, named = new_homes or {}, named or {}
    cs = {}

    def county(f, name=None):
        if f not in cs:
            nb = named.get(f) or {}
            cs[f] = {"fips": f, "state": STATE_FIPS.get(f[:2], ""), "county": name, "builders": nb.get("n", 0),
                     "builder_buys": nb.get("buys", 0), "top_builders": nb.get("top", []), "resellers": 0,
                     "small_buyers": 0, "small_lots": 0, "active_buyers": 0, "lots": 0, "wholesalers": 0,
                     "new_homes": new_homes.get(f), "_r": [], "_w": []}
        elif name and not cs[f]["county"]:
            cs[f]["county"] = name
        return cs[f]
    for f in named:
        county(f, named[f].get("county"))
    for r in by_pat.get("ALL", []):
        if JUNK.search(norm(r.get("buyer_full_name"))):
            continue
        c, n = county(r["fips"], r.get("county_name")), r.get("local_group_investor_purchases") or 0
        c["active_buyers"] += 1
        c["lots"] += n
        if (r.get("total_investor_purchases") or 0) <= SMALL_MAX:
            c["small_buyers"] += 1
            c["small_lots"] += n
    for pat, k, lst in (("FLIP", "resellers", "_r"), ("WHOLESALE", "wholesalers", "_w")):
        for r in by_pat.get(pat, []):
            if JUNK.search(norm(r.get("buyer_full_name"))):
                continue
            c = county(r["fips"], r.get("county_name"))
            c[k] += 1
            c[lst].append(r)

    def names(rs, k):
        rs = sorted(rs, key=lambda r: -(r.get("local_group_investor_purchases") or 0))[:k]
        return ["%s (%d)" % (r["buyer_full_name"], r["local_group_investor_purchases"]) for r in rs]
    ranked = sorted(cs.values(), key=lambda x: (-x["builders"], -(x["new_homes"] or 0), -x["small_buyers"]))
    for i, x in enumerate(ranked, 1):
        x["rank"] = i
        x["top_resellers"], x["top_wholesalers"] = names(x.pop("_r"), 4), names(x.pop("_w"), 4)
    return ranked


def named_builders(c):
    """fips -> small builder-named buyers of single-family parcels: count, purchases, the largest by name. One query."""
    qy = {"bool": {"filter": [{"term": {"property_type_group": "single_family_residence"}}, {"term": {"pattern": "ALL"}},
                              {"range": {"local_group_investor_purchases": {"gte": 3}}}, {"range": {"idle_months": {"lte": 12}}},
                              {"range": {"total_investor_purchases": {"lte": 3 * SMALL_MAX}}}],
                   "should": [{"wildcard": {"buyer_full_name": w}} for w in BUILDER_WILDCARDS], "minimum_should_match": 1}}
    aggs = {"f": {"terms": {"field": "fips", "size": 5000}, "aggs": {
        "buys": {"sum": {"field": "local_group_investor_purchases"}},
        "county": {"terms": {"field": "county_name", "size": 1}},
        "top": {"terms": {"field": "buyer_full_name", "size": 6, "order": {"m": "desc"}},
                "aggs": {"m": {"max": {"field": "local_group_investor_purchases"}}}}}}}
    r = c.search({"query": qy, "aggs": aggs}, 0, BS_INDEX)
    out = {}
    for bk in r["aggregations"]["f"]["buckets"]:
        top = [t for t in bk["top"]["buckets"] if not JUNK.search(norm(t["key"]))]
        cn = bk["county"]["buckets"]
        out[bk["key"]] = {"n": bk["doc_count"], "buys": int(bk["buys"]["value"]), "county": cn[0]["key"] if cn else None,
                          "top": ["%s (%d)" % (t["key"], t["m"]["value"]) for t in top]}
    return out


def _bs_rows(pattern, min_local):
    from opensearch_mcp import OpenSearchMCP
    qy = {"bool": {"filter": [{"term": {"property_type_group": "land_residential"}}, {"term": {"pattern": pattern}},
                              {"range": {"local_group_investor_purchases": {"gte": min_local}}},
                              {"range": {"idle_months": {"lte": 12}}}]}}
    c = OpenSearchMCP()
    try:
        return list(c.page(qy, [{"fips": "asc"}, {"buyer_id": "asc"}], BS_FIELDS, 10 ** 7, BS_INDEX))
    finally:
        c.close()


def phase_national(args):
    """Rank every county by the small builders who buy land there: where to open the next desk.

    buyer-stats counts a buyer's purchases by the parcel's CURRENT class, so a lot that already carries a house counts
    as single family, not land: fast builders undercount and the screen ranks markets, it does not size them.
    """
    from opensearch_mcp import OpenSearchMCP
    d = OUT / "national"
    d.mkdir(parents=True, exist_ok=True)
    log = logger(d)
    spec = {"ALL": 3, "FLIP": 2, "WHOLESALE": 2}
    by_pat = {}
    todo = [p for p in spec if args.refresh or _load(d / ("bs_%s.json" % p)) is None]
    for p in spec:
        if p not in todo:
            by_pat[p] = _load(d / ("bs_%s.json" % p))
    if todo:
        log("national: pulling buyer-stats land rows for %s" % ", ".join(todo))
        with cf.ThreadPoolExecutor(len(todo)) as ex:
            for p, rows in zip(todo, ex.map(lambda p: _bs_rows(p, spec[p]), todo)):
                by_pat[p] = rows
                _dump(rows, d / ("bs_%s.json" % p))
                log("national: %s %d rows" % (p, len(rows)))
    new_homes = _load(d / "new_homes.json")
    if new_homes is None or args.refresh:
        c = OpenSearchMCP()
        try:
            nbq = {"bool": {"filter": [{"term": {"property_type_group": "single_family_residence"}},
                                       {"range": {"year_built": {"gte": NOW.year - 3}}}]}}
            r = c.search({"query": nbq, "aggs": {"f": {"terms": {"field": "fips", "size": 5000}}}}, 0)
            new_homes = {bk["key"]: bk["doc_count"] for bk in r["aggregations"]["f"]["buckets"]}
            _dump(new_homes, d / "new_homes.json")
        except RuntimeError as e:
            log("new-home counts skipped: %s" % str(e)[:200])
            new_homes = {}
        finally:
            c.close()
    named = _load(d / "named_builders.json")
    if named is None or args.refresh:
        c = OpenSearchMCP()
        try:
            named = named_builders(c)
            _dump(named, d / "named_builders.json")
        finally:
            c.close()
    ranked = national_rank(by_pat, new_homes, named)
    _dump({"run": NOW.strftime("%Y-%m-%d"), "small_max": SMALL_MAX, "counties": ranked}, d / "national.json")
    log("national: %d counties, %d with a named small builder buying" % (len(ranked), sum(1 for x in ranked if x["builders"])))
    return export_national(d, ranked)


# ---------------------------------------------------------------- export

def _book():
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, Side
    from openpyxl.utils import get_column_letter
    NAVY, LINE = "0A1130", Side(style="thin", color="C9CED8")
    wb = Workbook()
    head = Font(bold=True, color=NAVY)

    def sheet(title, header, rows, widths=None, money=(), pct=(), num=(), wrap=(), first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title
        ws.append(header)
        for cell in ws[1]:
            cell.font = head
            cell.border = Border(bottom=Side(style="medium", color=NAVY))
            cell.alignment = Alignment(wrap_text=True, vertical="bottom")
        for r in rows:
            ws.append([("" if v is None else v) for v in r])
        for ci, h in enumerate(header, 1):
            col = get_column_letter(ci)
            ws.column_dimensions[col].width = (widths or {}).get(h, min(40, max(10, len(h) + 2)))
            for cell in ws[col][1:]:
                if h in money:
                    cell.number_format = '"$"#,##0'
                elif h in pct:
                    cell.number_format = "0%"
                elif h in num:
                    cell.number_format = "#,##0"
                if h in wrap:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                cell.border = Border(bottom=LINE)
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
        return ws
    return wb, sheet


def _save(wb, path):
    """Write to a _PENDING_ name and swap: Excel holds an exclusive lock on an open workbook."""
    tmp = path.parent / ("_PENDING_" + path.name)
    wb.save(tmp)
    try:
        os.replace(tmp, path)
    except PermissionError:
        print("workbook is open in Excel; saved as %s" % tmp)
        return tmp
    return path


def money(x):
    return "$%s" % format(int(x), ",") if x else "n/a"


def n_(x):
    return format(int(x or 0), ",")


HOLDING = {"TRUST", "TR", "ESTATE", "REVOCABLE", "LIVING", "TRUSTEE"}     # a trust name is not LAST FIRST
SMALL_WORDS = {"OF", "AND", "AT", "THE", "IN"}
UPPER = {"LLC", "INC", "LP", "LLP", "LTD", "PLLC", "II", "III", "IV", "TN", "OH", "USA", "DR", "BN", "NS", "JK", "GDP", "SMP"}


def pretty(name):
    """Deed names are upper case and people are LAST FIRST: 'GUESS NICHOLAS' reads as Nicholas Guess."""
    toks = (name or "").split()
    if toks and not is_entity(name) and not set(toks) & HOLDING and 2 <= len(toks) <= 4:
        toks = toks[1:] + toks[:1]
    return " ".join(t if t in UPPER or (len(t) <= 3 and not t.isalpha()) else t.lower() if i and t in SMALL_WORDS
                    else t.capitalize() for i, t in enumerate(toks))


def acres_txt(lo, hi):
    return "%.2f-%.2f" % (lo, hi)


def area_name(a):
    """Knox neighborhoods carry the city ('Beaumont - Knoxville'); the city adds nothing on a call."""
    return re.sub(r"\s+-\s+[^-]+$", "", str(a or "")).strip()


def main_class(b):
    usable = [c for c in ("lot", "acreage", "tract") if c in b["classes"] and b["classes"][c]["events"] >= 2]
    return usable[0] if usable else next(iter(b["classes"]), "lot")


def pitch(b):
    """The opening line on the first call: their own record, then what we found for them."""
    cls = main_class(b)
    cb = b["classes"].get(cls, {})
    what = {"lot": "lots", "acreage": "acreage homesites", "tract": "tracts"}[cls]
    where = ", ".join(area_name(x) for x in b["areas"][:2]) or "this county"
    paid = " at around %s" % money(cb["price_med"]) if cb.get("price_med") and cls != "tract" else ""
    n = b["matched_tracts"] if cls == "tract" else b["matched_lots"]
    found = ("We found %s %s that fit what you buy (%s acres, near your own), owned by individuals and not listed. Want us "
             "to go get them?" % (n_(n), what, acres_txt(cb.get("acres_lo", 0), cb.get("acres_hi", 0))) if n else
             "Tell us exactly what you want next and we will go find it.")
    return "You've bought %d %s here since %d, mostly in %s%s. %s" % (cb.get("n") or b["buys"], what, SINCE, where, paid, found)


def verdict(a):
    """One line: is this a builder land desk market, from how many repeat lot builders and fitting lots it has."""
    lot_b = [b for b in a["boxes"] if (b["classes"].get("lot") or {}).get("events", 0) >= 2]
    fit = sum(1 for x in a["lots"] if x["n_fit"])
    tract_b = [b for b in a["boxes"] if (b["classes"].get("tract") or {}).get("events", 0) >= 2]
    eu = [s for s in a["summary"] if s["niche"] in ("rural_homesite", "leftover_lot") and (s.get("end_user_share") or 0) >= 0.5]
    if len(lot_b) >= 20 and fit >= 300:
        return ("Yes. %d builders keep buying lots here and %s lots owned by individuals fit at least one of them: enough to "
                "run a builder land desk." % (len(lot_b), n_(fit)))
    if len(lot_b) >= 5 and fit >= 50:
        return ("Thin. %d repeat lot builders and %s fitting lots: workable as a side lane next to a stronger market, not as "
                "a desk of its own." % (len(lot_b), n_(fit)))
    tail = ""
    if eu:
        tail = " Most lots here end up as one owner's home (%s)." % "; ".join(
            "%s: %d%% still owned by the lot buyer" % (s["niche"].replace("_", " "), round(100 * s["end_user_share"])) for s in eu)
    lane = (" The builder lane is tracts: %s buy acreage here." % ", ".join(pretty(b["name"]) for b in tract_b[:4])) if tract_b else ""
    return ("No. Only %d builder%s bought lots here repeatedly and %s lot%s fit one of them, so a builder land desk has little "
            "to sell to. Sell these lots retail (list them) instead.%s%s" % (
                len(lot_b), "" if len(lot_b) == 1 else "s", n_(fit), "" if fit == 1 else "s", tail, lane))


def overview_rows(a, nat):
    m, boxes, lots, qf = a["meta"], a["boxes"], a["lots"], a["quick_flips"]
    fit_lots = [x for x in lots if x["n_fit"]]
    demand = sum(b["buys_12m"] for b in boxes)
    pw = sum(b["buys"] for b in boxes if b["from_person_share"] is not None)
    pp = sum(b["from_person_share"] * b["buys"] for b in boxes if b["from_person_share"] is not None) / pw if pw else None
    refs = [x["ref"] for x in fit_lots if x.get("ref")]
    top = ", ".join("%s (%d)" % (pretty(b["name"]), b["buys"]) for b in sorted(boxes, key=lambda b: -b["buys"])[:5])
    tcount = collections.Counter(b["type"] for b in boxes)
    rows = [
        ("Market", "%s (FIPS %s), run %s" % (m.get("name"), ", ".join(m["fips"]), m["analyzed"])),
        ("Verdict", verdict(a)),
        ("What the business is", "A land desk for small builders. Every builder's purchases are on the county deeds, so before the "
         "first call we already know where each one buys, what lot size, what they pay and what they build (the Builder buy "
         "boxes sheet). We find the owners of lots that fit, contract the lot, and assign it or double close to the builder "
         "for a fee. The builder gets lots without hunting for them; the seller gets a cash offer on land nobody else asked about."),
        ("Builders to sell to", "%d buyers bought %d or more lots here since %d, on at least two separate deeds, and bought "
         "again in the last %d months. %d have already built on or sold a house from their lots; %d have not built yet (often "
         "builders whose houses are not on the county roll yet). Largest: %s." % (
             len(boxes), m["min_buys"], SINCE, m["active_months"], tcount.get("builder", 0),
             tcount.get("holder (not built yet)", 0), top)),
        ("How much they buy", "Together they bought %s lots in the last 12 months; %s of their purchases since %d came "
         "straight from an individual, which is the seller we market to." % (n_(demand), "%d%%" % round(100 * pp) if pp is not None else "n/a", SINCE)),
        ("Lots that fit them today", "%s lots owned by individuals, not listed and not sold in 2 years, fit at least one builder's "
         "box (within %d m of a lot that builder already bought, in their size range); %s fit 3 or more builders, which is "
         "the most liquid inventory. %s more vacant parcels fit nobody." % (
             n_(len(fit_lots)), m["radius"]["lot"], n_(sum(1 for x in fit_lots if x["n_fit"] >= 3)), n_(a["lots_unmatched"]))),
        ("What a lot pays", "Builders paid a median %s for the lots that fit (from their own deeds nearby). At a %s fee the "
         "offer to the seller is the builder's price minus the fee (Lot list sheet)." % (money(med(refs)), money(m["fee"]))),
    ]
    if qf["n"]:
        rows.append(("The spread is measured, not assumed", "%d lots here were bought and resold as lots inside four months by %d "
                     "different sellers: median markup %s (middle half %s to %s) on a %s median buy, median %s days." % (
                         qf["n"], qf["sellers"], money(qf["markup_median"]), money(qf["markup_p25"]), money(qf["markup_p75"]),
                         money(qf["buy_median"]), qf["days_median"])))
    if a["competitors"]:
        rows.append(("Competition already doing it", "%d buyers resell lots more often than they build: %s. They prove the spread "
                     "and they are mailing the same owners." % (len(a["competitors"]), ", ".join(
                         pretty(c["name"]) for c in sorted(a["competitors"], key=lambda c: -c["quick_flips"])[:5]))))
    if a["teardowns"] or a["tracts"]:
        rows.append(("Second and third lanes", "%s old houses worth 55%% or less of the new homes around them sit near a builder's "
                     "own lots (Teardowns sheet; confirm the builder takes a house to demolish). %s tracts of 5 to 300 acres sit "
                     "beside new production subdivisions or a tract buyer's own purchases (Development tracts sheet)." % (
                         n_(len(a["teardowns"])), n_(len(a["tracts"])))))
    if nat:
        mine = [c for c in nat["counties"] if c["fips"] in m["fips"]]
        if mine:
            rows.append(("Where it ranks nationally", "; ".join(
                "%s ranks %d of %s counties (%d named small builders buying, %d lot wholesaler%s)" % (
                    c.get("county") or c["fips"], c["rank"], n_(len(nat["counties"])), c["builders"], c["wholesalers"],
                    "" if c["wholesalers"] == 1 else "s") for c in mine)
                + ". The National screen workbook ranks every county the same way."))
    rows += [
        ("How it runs", "1) Call the builders on the buy box sheet, starting with the most active, and confirm the box with the "
         "Builder intake questions; the pitch column is the opening. 2) Filter Lots by builder to that builder: that is their "
         "tailored seller list. 3) Mail, text and call those owners with a cash offer. 4) Contract with the assignment disclosed, "
         "send the lot to the builder, assign or double close. 5) Ask what they want next quarter and re-run this module."),
        ("Before the first call", "Builder contact details are the gap: the Reachable column shows who is already in our dispo "
         "registry with phones. The rest need their records pulled into DataSift for skip trace and their LLC principals resolved."),
        ("Read the caveats", "The county roll lags construction, LLCs rotate per house, and every number here is from deeds and the "
         "property index, not from a builder. The Sources and caveats sheet lists every rule."),
    ]
    return rows


INTAKE = [
    ("Which neighborhoods or streets do you build in, and which do you avoid?", "Defines the area the seller list is cut to; most builders work 2-4 neighborhoods."),
    ("What lot width, depth and size do you need? Corner lots, slope, alley access?", "Zoning sets a minimum frontage and lot size for a house; a lot that cannot be permitted is not a deal."),
    ("Do you need public sewer, or will you build on septic?", "Decides most rural and edge-of-town lots."),
    ("What do you pay for a lot in each area, and what is your all-in maximum?", "Their own number sets the offer; the deeds only show what they paid before."),
    ("Will you take a lot with a house on it? Who handles demolition and what does it cost you?", "Separates teardown buyers from lot-only buyers and prices the demolition."),
    ("Do you build duplexes or townhomes where the zoning allows them?", "A lot that allows two or more units is worth more to the right builder."),
    ("Do you split wide lots? What is the minimum width to split?", "Wide old lots are often two lots to a builder and one lot to the owner."),
    ("What disqualifies a lot: flood zone, easements, power lines, steep grade, HOA, historic overlay?", "Drops dead lots before we spend a stamp."),
    ("What do you build there and what does it sell for?", "Confirms the finished value we use to judge which old houses are teardowns."),
    ("How fast do you close, cash or loan, how much earnest money, how long an inspection period?", "Sets the terms we can promise a seller."),
    ("How many lots do you want per quarter, and who decides and signs?", "Sizes the list and tells us who to call when a lot comes in."),
    ("How do you want deals sent, and are you fine with an assignment or a double close?", "Some states require written disclosure of our interest (Tennessee: Public Chapter 72, 2025)."),
]


def phase_export(args):
    w = work_dir(args)
    a = _load(w / "analysis.json")
    if not a:
        raise SystemExit("run --phase analyze first")
    nat = _load(OUT / "national" / "national.json")
    m = a["meta"]
    wb, sheet = _book()

    sheet("Overview", ["Item", "Detail"], overview_rows(a, nat), {"Item": 30, "Detail": 120}, wrap=("Detail",), first=True)

    hdr = ["Rank", "Builder", "Type", "Main niche", "Buys since %d" % SINCE, "Last 12 months", "Per year", "Last buy",
           "Areas", "ZIPs", "Lot size (acres)", "Paid p25", "Paid median", "Paid p75", "Builds (sq ft)", "Sells for",
           "Lot to sale (days)", "From individuals", "Lots that fit now", "In their core size", "Teardowns that fit",
           "Tracts that fit", "Reachable", "Niche mix", "Opening line"]
    rows = []
    for i, b in enumerate(a["boxes"], 1):
        cb = b["classes"].get(main_class(b)) or {}
        rows.append([i, pretty(b["name"]), b["type"], b["main_niche"], b["buys"], b["buys_12m"], b["per_year"], b["last_buy"],
                     ", ".join(area_name(x) for x in b["areas"]), ", ".join(map(str, b["zips"])),
                     acres_txt(cb.get("acres_lo", 0), cb.get("acres_hi", 0)) if cb else None,
                     cb.get("price_p25"), cb.get("price_med"), cb.get("price_p75"), b["build_sqft"], b["sells_for"],
                     b["lot_to_sale_days"], b["from_person_share"], b["matched_lots"], b["matched_core"],
                     b["matched_teardowns"], b["matched_tracts"], b["reach"], b["niche_mix"], pitch(b)])
    sheet("Builder buy boxes", hdr, rows, {"Builder": 34, "Areas": 30, "Niche mix": 34, "Opening line": 90, "Reachable": 22},
          money=("Paid p25", "Paid median", "Paid p75", "Sells for"), pct=("From individuals",), num=("Builds (sq ft)",),
          wrap=("Opening line",))

    def ext(x, k):
        return (x.get("ext") or {}).get(k)
    with_ext = bool(m.get("lots_csv"))
    hdr = ["Rank", "Segment", "Address", "City", "ZIP", "Area", "Acres", "Builders who fit", "Best fit", "Other fits",
           "Builders pay (nearby)", "Price basis", "Offer to seller", "Owner", "Mailing address", "Owner state",
           "Years held", "Land flags", "On a distress list", "Flood risk", "DataSift id"]
    if with_ext:
        hdr += ["Lot tier", "Buildability", "Verdict", "Lot offer low", "Lot offer high"]
    rows = []
    for i, x in enumerate(a["lots"], 1):
        ms_ = x["matches"]
        r = [i, x["segment"], x["address"], x["city"], x["zip"], x["area"], x["acres"], x["n_fit"],
             pretty(ms_[0]["name"]) if ms_ else None, "; ".join(pretty(mm["name"]) for mm in ms_[1:4]), x.get("ref"), x.get("ref_basis"),
             x.get("offer"), x["owner"], x["mailing"], x["mail_state"], x["tenure_years"], ", ".join(x["flags"]),
             "yes" if x["on_distress_list"] else "", x["flood"], x["id"]]
        if with_ext:
            r += [ext(x, "Tier"), ext(x, "Buildability"), ext(x, "Verdict"), ext(x, "Offer low"), ext(x, "Offer high")]
        rows.append(r)
    sheet("Lot list", hdr, rows, {"Address": 28, "Best fit": 30, "Other fits": 44, "Owner": 28, "Mailing address": 40,
                                  "Price basis": 30}, money=("Builders pay (nearby)", "Offer to seller"))

    hdr = ["Builder", "Fit rank", "Address", "City", "ZIP", "Area", "Acres", "Meters to their nearest lot",
           "Their lots within %d m" % RADIUS["lot"], "Their last buy nearby", "Fit score", "Other builders who fit",
           "Builders pay (nearby)", "Offer to seller", "Owner", "Mailing address", "Land flags", "DataSift id"]
    per = collections.defaultdict(list)
    for x in a["lots"]:
        for mm in x["matches"]:
            per[mm["key"]].append((mm, x))
    rows = []
    for b in a["boxes"]:
        for j, (mm, x) in enumerate(sorted(per.get(b["key"], []), key=lambda t: -t[0]["score"])[:args.per_builder], 1):
            rows.append([pretty(b["name"]), j, x["address"], x["city"], x["zip"], x["area"], x["acres"], mm["dist"], mm["nearby"],
                         mm["last_nearby"], mm["score"], x["n_fit"] - 1, x.get("ref"), x.get("offer"), x["owner"],
                         x["mailing"], ", ".join(x["flags"]), x["id"]])
    sheet("Lots by builder", hdr, rows, {"Builder": 32, "Address": 28, "Owner": 28, "Mailing address": 40},
          money=("Builders pay (nearby)", "Offer to seller"))

    hdr = ["Rank", "Segment", "Address", "City", "ZIP", "Area", "Built", "Sq ft", "Acres", "Value", "New homes nearby (median)",
           "Value vs new", "Builders who fit", "Best fit", "Other fits", "Owner", "Mailing address", "Years held", "Owner flags",
           "On a distress list", "DataSift id"]
    rows = [[i, x["segment"], x["address"], x["city"], x["zip"], x["area"], x["year_built"], x["sqft"], x["acres"], x["value"],
             x["zone_new_value"], x["value_gap"], x["n_fit"], pretty(x["matches"][0]["name"]) if x["matches"] else None,
             "; ".join(pretty(mm["name"]) for mm in x["matches"][1:4]), x["owner"], x["mailing"], x["tenure_years"],
             ", ".join(x["flags"]), "yes" if x["on_distress_list"] else "", x["id"]] for i, x in enumerate(a["teardowns"], 1)]
    sheet("Teardowns", hdr, rows, {"Address": 28, "Best fit": 30, "Other fits": 44, "Owner": 28, "Mailing address": 40},
          money=("Value", "New homes nearby (median)"), pct=("Value vs new",))

    hdr = ["Rank", "Address", "City", "ZIP", "Acres", "New production homes within ~2 km", "Tract buyers who fit",
           "Production builders active within 3 km", "Tract buys nearby per acre (median)", "Tract buys priced", "Owner",
           "Mailing address", "Years held", "Owner flags", "Flood risk", "DataSift id"]
    rows = [[i, x["address"], x["city"], x["zip"], x["acres"], x["production_homes_2km"],
             "; ".join(pretty(mm["name"]) for mm in x["matches"][:4]), "; ".join(pretty(b) for b in x["production_builders"]),
             x["per_acre_ref"], x["per_acre_n"], x["owner"], x["mailing"], x["tenure_years"], ", ".join(x["flags"]),
             x["flood"], x["id"]] for i, x in enumerate(a["tracts"], 1)]
    sheet("Development tracts", hdr, rows, {"Address": 28, "Tract buyers who fit": 40, "Production builders active within 3 km": 44,
                                            "Owner": 28, "Mailing address": 40}, money=("Tract buys nearby per acre (median)",))

    hdr = ["Buyer", "Lots bought", "Resold inside 4 months", "Median markup", "Built", "Main niche", "Last buy", "Recent lots"]
    rows = [[pretty(c["name"]), c["buys"], c["quick_flips"], c["quick_flip_markup"], c["built"], c["main_niche"], c["last_buy"],
             "; ".join(c["sample"])] for c in sorted(a["competitors"], key=lambda c: -c["quick_flips"])]
    sheet("Competitors", hdr, rows, {"Buyer": 34, "Recent lots": 80}, money=("Median markup",))

    econ(wb, a)
    sheet("Builder intake", ["#", "Question", "Why it matters"], [[i, qq, why] for i, (qq, why) in enumerate(INTAKE, 1)],
          {"Question": 70, "Why it matters": 80}, wrap=("Question", "Why it matters"))
    hdr = ["County", "Niche", "New homes a year (2019-2025)", "Vacant parcels now", "Purchases since %d" % SINCE, "Buyers",
           "Buyers with 3+", "Share bought by 3+ buyers", "Share bought by builders", "Median paid", "Median new home",
           "Bought from an individual", "Owned by the lot buyer"]
    rows = [[s["county"], s["niche"], s["built_per_year"], s["vacant_lots_now"], s["buys_since_2021"], s["buyers"],
             s["repeat_buyers_3plus"], s["share_by_repeat_buyers"], s["builder_share"], s["median_buy"], s["median_exit"],
             s["bought_from_person_share"], s["end_user_share"]] for s in a["summary"]]
    sheet("Niches", hdr, rows, money=("Median paid", "Median new home"),
          pct=("Share bought by 3+ buyers", "Share bought by builders", "Bought from an individual", "Owned by the lot buyer"))
    sheet("Sources and caveats", ["Item", "Detail"], caveats(a), {"Item": 30, "Detail": 120}, wrap=("Detail",))

    slug = "_".join(p.upper() if len(p) <= 2 else p.title() for p in args.market.split("_"))
    path = _save(wb, w / ("%s_Builder_Land_Desk_%s.xlsx" % (slug, NOW.strftime("%Y%m%d"))))
    print("wrote %s" % path)
    return 0


def econ(wb, a):
    """Live formulas: change a blue input and every output moves. The response rates are placeholders to replace
    with the first 90 days of real results, not measurements."""
    from openpyxl.styles import Alignment, Font
    ws = wb.create_sheet("Economics")
    fit_lots = sum(1 for x in a["lots"] if x["n_fit"])
    demand = sum(b["buys_12m"] for b in a["boxes"])
    blue, bold = Font(color="316AFF"), Font(bold=True, color="0A1130")
    rows = [
        ("Measured", None, None),
        ("Lots that fit a builder today", fit_lots, "From the Lot list sheet."),
        ("Lots the box builders bought in the last 12 months", demand, "From the Builder buy boxes sheet."),
        ("Inputs (blue: assumptions to replace with real results)", None, None),
        ("Lots marketed a month", min(fit_lots, 400), "How many owners get a first touch each month."),
        ("Touches per lot", 4, "Mail, text and call, over about 60 days."),
        ("Cost per touch", 0.65, "Blended mail and text."),
        ("Owners who answer", 0.03, "Assumption. Land owners who were never asked tend to answer more than distress lists; measure it."),
        ("Answers that sign a contract", 0.15, "Assumption."),
        ("Contracts a builder takes", 0.7, "Assumption; the rest fail the builder's checks (sewer, survey, title)."),
        ("Fee per lot", a["meta"]["fee"], "The spread we keep, assignment or double close."),
        ("Outputs", None, None),
        ("Lots closed a month", "=B6*B9*B10*B11", None),
        ("Fee income a month", "=B14*B12", None),
        ("Marketing a month", "=B6*B7*B8", None),
        ("Marketing per closed lot", '=IF(B14>0,B16/B14,"")', None),
        ("Net a month before people", "=B15-B16", None),
        ("Net a year before people", "=B18*12", None),
        ("Sanity checks", None, None),
        ("Share of the builders' yearly buying we would supply", '=IF(B4>0,B14*12/B4,"")', "Above about 25% the plan outruns the demand."),
        ("Months to give every fitting lot a first touch", '=IF(B6>0,B3/B6,"")', None),
    ]
    ws.append(["Line", "Value", "Note"])
    for r in rows:
        ws.append(list(r))
    for i, (label, val, _) in enumerate(rows, 2):
        if val is None:
            ws.cell(i, 1).font = bold
        if 6 <= i <= 12:
            ws.cell(i, 2).font = blue
    fmt = {3: "#,##0", 4: "#,##0", 6: "#,##0", 7: "0", 8: '"$"#,##0.00', 9: "0%", 10: "0%", 11: "0%", 12: '"$"#,##0',
           14: "0.0", 15: '"$"#,##0', 16: '"$"#,##0', 17: '"$"#,##0', 18: '"$"#,##0', 19: '"$"#,##0', 21: "0%", 22: "0.0"}
    for r, f in fmt.items():
        ws.cell(r, 2).number_format = f
    for c in ws[1]:
        c.font = bold
    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 90
    for row in ws.iter_rows(min_row=2):
        row[2].alignment = Alignment(wrap_text=True, vertical="top")


def caveats(a):
    m = a["meta"]
    return [
        ("Property universe", "DataSift property index through the opensearch MCP (read-only): %s parcels on disk for FIPS %s. "
         "Condos, townhouses, warehouses and commercial multifamily are left out." % (format(m["parcels_on_disk"], ","), ", ".join(m["fips"]))),
        ("Deed history", "sales-history for %s ids: every house built since %d, every vacant parcel sold since 2019 and every "
         "old house an entity bought since 2021." % (format(m["new_builds"], ","), NEW_BUILD_YEAR)),
        ("How a new house is placed", "Its own subdivision when it has 8+ homes, else the ~840 m block around it: an old "
         "neighborhood (median year built 1985 or older, 30% or less built since 2010), a lot left in a built-out subdivision, "
         "scattered rural homes, or a new production subdivision. 1.5+ acres counts as a rural homesite."),
        ("How the lot purchase is found", "The jump in the deed chain: a priced sale followed by one at 2x or more after the build "
         "(1.5x when the first sale predates the build year). Family transfers, sales under $5,000 and multi-parcel deeds are "
         "ignored. Today's AVM is never the yardstick, because prices rose sharply since 2019."),
        ("Buyer types", "Builder: built on a lot, or resold one as a house, or carries a builder name. Lot wholesaler: resold 2+ "
         "lots inside 120 days, more often than it built. Holder: bought 2+ lots, not built or resold yet (often a builder whose "
         "houses are not on the roll yet). Government, lenders and nonprofits are labeled and get no buy box."),
        ("Buy box", "From the builder's own purchases since %d: lot size p10-p90 widened 20-25%% (defaults when fewer than 3 lots "
         "carry an acreage), price p25-p75 without takedown deeds, areas and ZIPs by count, finished size and resale price from "
         "the houses they built. Active = bought %d+ lots and bought again in the last %d months." % (SINCE, m["min_buys"], m["active_months"])),
        ("Fit", "A lot fits a builder when one of that builder's own lots is within %d m (acreage %d m, tracts %d m) and its size "
         "is inside their range. Score = closeness, how many of their lots are nearby, how recently, and whether the size is in "
         "their core (p25-p75)." % (m["radius"]["lot"], m["radius"]["acreage"], m["radius"]["tract"])),
        ("Offer", "Builders pay = the median the fitting builders paid for lots nearby (3+ prices), else every niche lot buy nearby. "
         "Offer to seller = that minus the %s fee; no offer when the fee would be more than 60%% of the lot." % money(m["fee"])),
        ("Seller rules", "Owner is a person or a trust (no entities, no builders, no repeat lot buyers), not listed, not sold in the "
         "last 2 years, not landlocked. Teardown: built 1975 or earlier, worth 55%% or less of the new homes within ~420 m, near a "
         "builder's own lots. Excluded: %s." % "; ".join("%s %s" % (format(v, ","), k) for k, v in sorted(a["dropped"].items(), key=lambda t: -t[1]))),
        ("Known undercounts", "The county roll lags construction, so the newest houses can still read as vacant lots. A lot split "
         "off a bigger parcel has no sale of its own. Builders who rotate a new LLC per house, or buy in a personal name, look "
         "like separate one-time buyers. Prices are from deeds; some states do not disclose prices, which empties the price columns."),
        ("Price bands", "Luxury rebuild = finished at %s+, mid-range %s to %s, urban infill under %s (calibrated on Knox; pass "
         "--lux and --mid for another market)." % (money(m["lux"]), money(m["mid"]), money(m["lux"]), money(m["mid"]))),
        ("Reachable", "Matched by deed name to output/dispo_buyers/registry.json. A builder missing there still needs a phone: pull "
         "its parcels into DataSift for skip trace and resolve the LLC principal."),
        ("Run", "%s. Module src/builder_land.py, re-run any time; nothing was written to any account." % m["analyzed"]),
    ]


def export_national(d, ranked):
    wb, sheet = _book()
    nh = "New homes %d+" % (NOW.year - 3)
    rows = [[x["rank"], x["state"], x.get("county"), x["fips"], x["builders"], x["builder_buys"], x.get("new_homes"),
             x["small_buyers"], x["resellers"], x["wholesalers"], "; ".join(x["top_builders"]),
             "; ".join(x["top_wholesalers"]), "; ".join(x["top_resellers"])] for x in ranked]
    sheet("National screen", ["Rank", "State", "County", "FIPS", "Small builders buying (named)", "Their purchases", nh,
                              "Small active land buyers", "Land resellers", "Lot wholesalers", "Largest small builders",
                              "Lot wholesalers (local buys)", "Land resellers (local buys)"], rows,
          {"Largest small builders": 80, "Lot wholesalers (local buys)": 50, "Land resellers (local buys)": 60, "County": 18},
          num=("Their purchases", nh), first=True)
    by_state = collections.defaultdict(lambda: [0, 0, 0, 0, 0])
    for x in ranked:
        s = by_state[x["state"]]
        s[0] += 1
        s[1] += x["builders"]
        s[2] += x.get("new_homes") or 0
        s[3] += x["resellers"]
        s[4] += x["wholesalers"]
    sheet("By state", ["State", "Counties", "Small builders buying (named)", nh, "Land resellers", "Lot wholesalers"],
          sorted([[k] + v for k, v in by_state.items()], key=lambda r: -r[2]), num=(nh,))
    sheet("How to read it", ["Item", "Detail"], [
        ("What it ranks", "Every US county by its small builders: buyers with a builder name (Homes, Builders, Construction, "
         "Contracting, Custom, Building) that bought 3+ single-family parcels in the county, bought in the last 12 months and "
         "made %d or fewer purchases anywhere. A builder's lot counts as single family once the house is on the roll, so this "
         "is the pool a builder land desk sells to. Ties break on new homes built since %d." % (3 * SMALL_MAX, NOW.year - 3)),
        ("What it misses", "Builders without a builder name (on Knox, Braden Family Properties and Definity Investments, the two "
         "largest), and it counts some house flippers that carry a Homes name. It ranks markets; it does not count every builder. Run src/builder_land.py on a county for that: on Knox "
         "the deeds show 110 active lot and tract buyers where this column shows 29."),
        ("Land resellers", "Buyers that bought land and resold it as land. A lot that became a house leaves the land class, so "
         "this is land flipping (the Florida and Arizona platted-lot sellers), not building. A high count means a retail land "
         "market with lots of competition for sellers."),
        ("Lot wholesalers", "Buyers whose land purchases this index marks as wholesale (assigned or double closed). On Knox that "
         "is SMP Holdings, the same wholesaler the deed analysis found."),
        ("Source", "buyer-stats-by-county-property-type-pattern (DataSift, through the opensearch MCP, read-only); new homes from "
         "the property index. Public bodies, utilities, associations, nonprofits, banks and land trusts are dropped by name."),
        ("Run", NOW.strftime("%Y-%m-%d")),
    ], {"Item": 24, "Detail": 120}, wrap=("Detail",))
    path = _save(wb, d / ("National_Builder_Land_Screen_%s.xlsx" % NOW.strftime("%Y%m%d")))
    print("wrote %s" % path)
    return 0


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--market", default="knox_blount", help="slug for the output folder")
    ap.add_argument("--fips", default="47093,47009", help="comma-separated county FIPS forming one market")
    ap.add_argument("--name", help="market label (default: county names from the cluster)")
    ap.add_argument("--phase", default="all", choices=["pull", "history", "analyze", "export", "all", "national"])
    ap.add_argument("--from-dir", help="import an earlier pull (properties *.jsonl and sales*.jsonl) instead of reading the cluster")
    ap.add_argument("--streams", type=int, default=4, help="parallel MCP streams for pull and history")
    ap.add_argument("--min-buys", type=int, default=3)
    ap.add_argument("--active-months", type=int, default=24)
    ap.add_argument("--fee", type=int, default=FEE)
    ap.add_argument("--lux", type=int, default=LUX)
    ap.add_argument("--mid", type=int, default=MID)
    ap.add_argument("--lots-csv", help="restrict the supply to a vacant_land mail list (needs a 'DataSift id' column)")
    ap.add_argument("--per-builder", type=int, default=75, help="rows per builder on the Lots by builder sheet")
    ap.add_argument("--refresh", action="store_true", help="national: re-pull the buyer-stats rows instead of the cache")
    args = ap.parse_args(argv)
    args.fips = [f.strip() for f in args.fips.split(",") if f.strip()]
    if args.phase == "national":
        return phase_national(args)
    steps = ["pull", "history", "analyze", "export"] if args.phase == "all" else [args.phase]
    for s in steps:
        rc = globals()["phase_" + s](args)
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
