"""Buyer search: the buyers most likely to buy one property, from the data team's model.

Ported from the DataFlik data team's buyer-search Lambda (Bitbucket dataflik-data-web-service,
commit c9df3c67affd, 2026-02-09) and reviewed 2026-11-23. The scoring model is theirs and is
unchanged: the same ten components, weights, scales, fills and profile-size discount. What
changed is listed in FIXES and marked "FIX n" in the code; `compat=True` puts every change back,
which is how tests/test_buyer_search.py proves this port reproduces the original's scores.

A search, for one property by dataflik_id:
  1. Its facts from `properties` and its 10-number embedding from `properties-embeddings`.
  2. Sales near it in `sales-backtesting`: inside the radius and the period, the buyer a company by
     default, and a similar house (value, beds, size, lot, age within loose bands, same type).
  3. Every buyer on those sales, with how many of them each bought here.
  4. How closely each buyer's purchase profile (`buyers-embeddings`) matches this house.
  5. Each buyer's track record (`buyer-stats-by-county-property-type-pattern`, pattern ALL).
  6. Ten components scaled to 0..1 and summed by weight into a 0..100 score; the best buyers
     above MIN_BUYER_SCORE come back, best first.

Storage is a Backend: the workstation's read-only MCP (backends.py, staff only: the cluster reads every
account's data) or the in-memory cluster in the tests (fakes.py). This module never writes and never
talks to a network itself. Pure Python, no numpy or pandas.

Users do not have the cluster. SiftStack's own version (open_api.py) runs the same pipeline on a user's
Open API key and scores with score_buyers below, so both versions share the model's exact math.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

log = logging.getLogger("buyer_search")

VERSION = "1.0.0"
ORIGIN = "dataflik-data-web-service c9df3c67affd lambda_function.py (2026-02-09)"

# ----------------------------------------------------------------- the data team's model, verbatim
WEIGHTS = {
    # buyer statistics for investor sales
    "total_investor_purchases": 0.25,
    "purchases_per_year": 0.10,
    "locality_percentage": 0.10,
    "median_discount": 0.05,
    "month_active_perc": 0.05,
    "active_years": 0.05,
    # similarity and buyer profile stats
    "similarity": 0.25,
    "buyer_size": 0.10,
    "profile_size": 0.05,
    # buyer purchases in the search radius
    "purchases_radius": 0.05,
}
SCALE_MIN_MAX = {
    "purchases_radius": [3, 20],
    "profile_size": [3, 10],
    "buyer_size": [5, 50],
    "total_investor_purchases": [5, 25],
    "median_discount": [100, 125],
    "purchases_per_year": [5, 20],
    "active_years": [2, 5],
    "month_active_perc": [100, 100],
    "locality_percentage": [100, 100],
}
FILL_NA = {
    "purchases_radius": 1,
    "profile_size": 1,
    "buyer_size": 1,
    "similarity": 0.50,
    "total_investor_purchases": 0,
    "median_discount": 0,
    "purchases_per_year": 0,
    "active_years": 1,
    "month_active_perc": 0,
    "locality_percentage": 0,
}
RADIUS_MILES = 25
PERIOD_DAYS = 730
IS_INVESTOR_SALE = ["true", "false"]
IS_BUYER_CORPORATION = ["true"]
MIN_BUYER_SCORE = 10
TOP_N_BUYERS = 100
MAX_SIZE_SEARCH = 2000
COMPONENTS = tuple(WEIGHTS)

# Production aliases. The original defaulted to the staging cluster's "-qa" indexes; production
# names them differently ("properties-embeddings", "buyers-embeddings"). Each is a setting.
INDEXES = {
    "properties": "properties",
    "properties_embed": "properties-embeddings",
    "sales": "sales-backtesting",
    "buyers_embed": "buyers-embeddings",
    "buyers_stats": "buyer-stats-by-county-property-type-pattern",
}
INDEX_ENV = {   # our setting first, then the name the Lambda used
    "properties": ("BUYER_SEARCH_INDEX_PROPERTIES", "INDEX_PROPERTIES"),
    "properties_embed": ("BUYER_SEARCH_INDEX_PROPERTIES_EMBED", "INDEX_PROPERTIES_EMBED"),
    "sales": ("BUYER_SEARCH_INDEX_SALES", "INDEX_SALES"),
    "buyers_embed": ("BUYER_SEARCH_INDEX_BUYERS_EMBED", "INDEX_BUYERS_EMBED"),
    "buyers_stats": ("BUYER_SEARCH_INDEX_BUYERS_STATS", "INDEX_BUYERS_STATS"),
}

# What this port changes, each measured or read off the production cluster on 2026-11-23.
FIXES = (
    ("1 period on sale_date",
     "The period filter used last_sale_date, which on a sale row is the PREVIOUS sale (when the seller "
     "bought). Around one Knox house it kept 190 of 1,771 company purchases: only houses resold within "
     "two years of the prior sale. The period now applies to sale_date."),
    ("2 the subject county's stats row",
     "Buyer stats come one row per county, and total_investor_purchases is the buyer's national total "
     "repeated on every row, so 'the row with the highest total' was a tie broken at random and "
     "locality_percentage came from an arbitrary county. The row for the subject's county is used, "
     "else the buyer's busiest county (the original TODO: filter by FIPS)."),
    ("3 strict JSON",
     "A buyer missing from the stats or profile index went out as NaN, which JavaScript cannot parse. "
     "Missing values are null."),
    ("4 nothing cut off silently",
     "Every search stopped at 2,000 rows with no sort, so a dense market dropped an arbitrary set. "
     "Searches are sorted (newest sale first) and paged, and a cut is reported in counts."),
    ("5 bounded inputs",
     "Raw OpenSearch filters and weights from the caller went straight into a production query. "
     "clean_params takes named parameters only, inside fixed bounds; raw filters only from a trusted caller."),
    ("6 the current year",
     "The year-built band used a fixed 2025."),
    ("7 lookups by ids query",
     "A GET by id fails on an alias that spans two indexes during a rebuild; an ids query does not."),
    ("8 no crash on odd profile rows",
     "A profile with profile_prop 0 or null, or a null profile_size, raised and failed the request."),
)


class NotFound(Exception):
    """The property is not in the index."""


class Backend(Protocol):
    def get(self, index: str, doc_id: Any, fields: list[str] | dict | None = None) -> dict | None:
        """The document's _source, or None."""

    def search(self, index: str, query: dict, *, sort: list, fields: list[str] | dict | None,
               limit: int, scored: bool = False) -> tuple[list[dict], int]:
        """(hits, total): up to `limit` hits in `sort` order, each {"_id", "_score", "_source"},
        and how many matched in all."""


def indexes_from_env(env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    out = dict(INDEXES)
    for key, names in INDEX_ENV.items():
        for n in names:
            if env.get(n):
                out[key] = env[n]
                break
    return out


# ------------------------------------------------------------------------------ parameters

@dataclass
class Params:
    radius_miles: float = RADIUS_MILES
    period_days: int = PERIOD_DAYS
    is_investor_sale: Any = field(default_factory=lambda: list(IS_INVESTOR_SALE))
    is_buyer_corporation: Any = field(default_factory=lambda: list(IS_BUYER_CORPORATION))
    weights: dict = field(default_factory=lambda: dict(WEIGHTS))
    sales_filters: list = field(default_factory=list)   # trusted callers only; the service never sets it
    limit: int = TOP_N_BUYERS
    min_score: float = MIN_BUYER_SCORE
    max_sales: int = 5000
    max_profiles: int = 5000
    max_stats_rows: int = 5000


BOUNDS = {   # (low, high) for callers from the network; trusted callers get TRUSTED_BOUNDS
    "radius_miles": (0.25, 50.0), "period_days": (30, 1095), "limit": (1, 100), "min_score": (0.0, 100.0),
}
TRUSTED_BOUNDS = {
    "radius_miles": (0.1, 100.0), "period_days": (1, 3650), "limit": (1, 500), "min_score": (0.0, 100.0),
}


def _bool_list(v, name: str):
    if v is None:
        return None
    vals = v if isinstance(v, (list, tuple)) else [v]
    out = []
    for x in vals:
        s = str(x).strip().lower()
        if s not in ("true", "false"):
            raise ValueError("%s takes true and/or false, not %r" % (name, x))
        out.append(s)
    if not out:
        raise ValueError("%s is empty" % name)
    return out


def clean_params(raw: dict | None, *, trusted: bool = False) -> Params:
    """Params from a request, refused on anything unknown or out of bounds (FIX 5).

    The network API is never trusted: no raw filters, and the tighter bounds. The CLI on the
    workstation is trusted and may pass `sales_filters`."""
    raw = dict(raw or {})
    p = Params()
    bounds = TRUSTED_BOUNDS if trusted else BOUNDS
    known = set(asdict(p)) - ({"max_sales", "max_profiles", "max_stats_rows"} if not trusted else set())
    unknown = sorted(k for k in raw if k not in known)
    if unknown:
        raise ValueError("unknown parameter(s): %s" % ", ".join(unknown))
    for key in ("radius_miles", "min_score"):
        if raw.get(key) is not None:
            v = float(raw[key])
            lo, hi = bounds[key]
            if not (lo <= v <= hi) or math.isnan(v):
                raise ValueError("%s must be between %s and %s" % (key, lo, hi))
            setattr(p, key, v)
    for key in ("period_days", "limit"):
        if raw.get(key) is not None:
            v = int(raw[key])
            lo, hi = bounds[key]
            if not (lo <= v <= hi):
                raise ValueError("%s must be between %s and %s" % (key, lo, hi))
            setattr(p, key, v)
    for key in ("is_investor_sale", "is_buyer_corporation"):
        if key in raw:
            setattr(p, key, _bool_list(raw[key], key))
    if raw.get("weights") is not None:
        w = raw["weights"]
        if not isinstance(w, dict) or not w:
            raise ValueError("weights must be a non-empty object of component: weight")
        bad = sorted(k for k in w if k not in WEIGHTS)
        if bad:
            raise ValueError("unknown weight(s): %s (known: %s)" % (", ".join(bad), ", ".join(WEIGHTS)))
        clean = {}
        for k, v in w.items():
            f = float(v)
            if not (0.0 <= f <= 10.0) or math.isnan(f):
                raise ValueError("weight %s must be between 0 and 10" % k)
            clean[k] = f
        p.weights = clean
    if raw.get("sales_filters"):
        if not trusted:
            raise ValueError("sales_filters is not accepted over the network")
        if not isinstance(raw["sales_filters"], list):
            raise ValueError("sales_filters must be a list")
        p.sales_filters = list(raw["sales_filters"])
    for key in ("max_sales", "max_profiles", "max_stats_rows"):
        if trusted and raw.get(key) is not None:
            setattr(p, key, max(1, min(int(raw[key]), 20000)))
    return p


# ------------------------------------------------------------- the original filter builders

def exists_field_in_filter(filt, field_name: str) -> bool:
    """Whether a field name appears anywhere in an OpenSearch filter structure."""
    if isinstance(filt, dict):
        if field_name in filt:
            return True
        return any(exists_field_in_filter(v, field_name) for v in filt.values())
    if isinstance(filt, list):
        return any(exists_field_in_filter(v, field_name) for v in filt)
    return False


def bool_as_list_for_terms_filter(value) -> list[str]:
    if value is None:
        return ["true", "false"]
    if isinstance(value, (list, tuple)):
        return [str(v).lower() for v in value]
    return [str(value).lower()]


def add_filter_for_bool(field_value, field_name: str, filters: list) -> list:
    field_value = bool_as_list_for_terms_filter(field_value)
    if not exists_field_in_filter(filters, field_name):
        filters.append({"terms": {field_name: field_value}})
    return filters


def add_filters_for_similar_properties(doc: dict, filters: list, this_year: int = 2025) -> list:
    """Loose range filters for a similar house (the original, with the year a parameter: FIX 6)."""
    value = doc.get("property_value")
    if value is not None and 0 < value < 99_000_000:
        dev = int(max(50_000, 0.50 * value))
        filters.append({"range": {"property_value": {"gte": max(0, value - dev), "lte": value + dev}}})
    beds = doc.get("bedroom_count")
    if beds is not None and 0 < beds < 99:
        dev = int(max(1, 0.33 * beds))
        filters.append({"range": {"bedroom_count": {"gte": max(0, beds - dev), "lte": beds + dev}}})
    sqft = doc.get("property_sqft")
    if sqft is not None and 0 < sqft < 99_999:
        dev = int(max(200, 0.50 * sqft))
        filters.append({"range": {"property_sqft": {"gte": max(0, sqft - dev), "lte": sqft + dev}}})
    lot = doc.get("area_lot_sf")
    if lot is not None and 0 < lot < 9_999_999:
        dev = int(max(2000, 0.50 * lot))
        filters.append({"range": {"area_lot_sf": {"gte": max(0, lot - dev), "lte": lot + dev}}})
    year = doc.get("year_built")
    if year is not None and 1800 < year < 2999:
        dev = int(max(10, 0.25 * max(this_year - year, 0)))
        filters.append({"range": {"year_built": {"gte": year - dev, "lte": year + dev}}})
    ptype = doc.get("property_type_group")
    if ptype is not None and not exists_field_in_filter(filters, "property_type_group"):
        filters.append({"terms": {"property_type_group": [ptype]}})
    return filters


def add_filter_radius_around_property(doc: dict, radius_miles, filters: list) -> list:
    geo = doc.get("location")
    if geo is not None:
        filters.append({"geo_distance": {"distance": "%smi" % radius_miles, "location": geo}})
    return filters


def add_filter_sales_period(period_days, filters: list, compat: bool = False) -> list:
    if not exists_field_in_filter(filters, "sale_date"):
        # FIX 1: on a sale row last_sale_date is the previous sale; the period is about this sale.
        date_field = "last_sale_date" if compat else "sale_date"
        filters.append({"range": {date_field: {"gte": "now-%dd/d" % int(period_days)}}})
    return filters


# ----------------------------------------------------------------------------- small helpers

SUBJECT_FIELDS = ["dataflik_id", "property_address", "property_city", "property_state", "property_zip", "county",
                  "fips", "property_type_group", "property_value", "bedroom_count", "bath_count", "property_sqft",
                  "living_area_sqft", "area_lot_sf", "year_built", "location"]
DETAIL_FIELDS = ["buyer_full_name", "buyer_state", "buyer_city", "buyer_address", "buyer_zip",
                 "is_buyer_corporation"]
SALE_FIELDS = ["dataflik_id", "buyer_id", "transaction_id", "sale_date", "sale_amount", "is_investor_sale",
               "fips", "property_address", "property_city", "property_zip"] + DETAIL_FIELDS
STATS_FIELDS = ["buyer_id", "fips", "county_name", "total_investor_purchases", "median_discount",
                "purchases_per_year", "active_years", "month_active_perc", "locality_percentage",
                "local_group_investor_purchases", "national_group_investor_purchases", "last_activity_date",
                "idle_months"]
STAT_COMPONENTS = ["total_investor_purchases", "median_discount", "purchases_per_year", "active_years",
                   "month_active_perc", "locality_percentage"]


def _num(v):
    """A number, or None for a missing or NaN value."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _as_int_if_whole(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer() and abs(v) < 2 ** 53:
        return int(v)
    return v


def _day(v) -> str:
    """A sale date as YYYY-MM-DD from epoch milliseconds or an ISO string; "" when unreadable."""
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return _dt.datetime.fromtimestamp(float(v) / 1000, tz=_dt.timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    s = str(v)
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else ""


def _round1(x: float) -> float:
    """numpy's round(x, 1): scale by 10, round half to even, scale back (the original used np.round)."""
    return round(x * 10) / 10


def _nanmax(vals):
    good = [v for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return max(good) if good else math.nan


def subject_summary(doc: dict) -> dict:
    out = {k: doc.get(k) for k in SUBJECT_FIELDS if k != "location"}
    loc = doc.get("location")
    out["location"] = loc if isinstance(loc, str) else None
    return out


def score_buyers(rows: list[dict], weights: dict) -> list[float]:
    """The original's composite score, step for step (lambda_function.py steps 8 and 9).

    `rows` hold each buyer's raw components (None where missing). Returns one score per row."""
    scaled = [dict(r) for r in rows]
    # - transform median_discount (higher means better, adjusted by size, truncated)
    for r in scaled:
        total, md = r.get("total_investor_purchases"), r.get("median_discount")
        if total is None or md is None:
            r["median_discount"] = None
        else:
            adj = min(total / SCALE_MIN_MAX["total_investor_purchases"][0], 1.0)
            r["median_discount"] = min(max(100 - md * adj, 50), 150)
    # - fill missing values
    for col, fill in FILL_NA.items():
        for r in scaled:
            if r.get(col) is None:
                r[col] = fill
    # - scale to [0, 1]
    for col, (lo, hi) in SCALE_MIN_MAX.items():
        col_max = _nanmax([r[col] for r in scaled])
        scale = min(max(lo, col_max), hi)
        if scale == 0:
            scale = 1
        for r in scaled:
            r[col] = min(r[col] / scale, 1.0)
    # - similarity discounted by profile size (less size, less certainty)
    for r in scaled:
        r["similarity"] = r["similarity"] * math.sqrt(r["profile_size"])
    # - weighted sum, in the weights' own order (float addition is order-sensitive)
    sum_w = 0.0
    scores = [0.0] * len(scaled)
    for col, w in weights.items():
        if col in COMPONENTS:
            sum_w += w
            for i, r in enumerate(scaled):
                scores[i] += r[col] * w
    if sum_w > 0:
        scores = [_round1(s / sum_w * 100) for s in scores]
    return scores


# --------------------------------------------------------------------- a street line to an id

_ABBR = {"street": "st", "road": "rd", "drive": "dr", "avenue": "ave", "av": "ave", "lane": "ln", "court": "ct",
         "circle": "cir", "boulevard": "blvd", "place": "pl", "terrace": "ter", "highway": "hwy",
         "parkway": "pkwy", "trail": "trl", "square": "sq", "cove": "cv", "point": "pt", "pointe": "pt",
         "north": "n", "south": "s", "east": "e", "west": "w", "northeast": "ne", "northwest": "nw",
         "southeast": "se", "southwest": "sw"}
_UNIT = re.compile(r"\b(apt|unit|ste|suite|#|lot|bldg)\b.*$", re.I)


def street_key(street: str) -> str:
    """'123 Example Road' and '123 EXAMPLE RD' read the same."""
    s = _UNIT.sub("", (street or "").lower().replace("#", " # "))
    words = re.sub(r"[^a-z0-9 ]", " ", s).split()
    return " ".join(_ABBR.get(w, w) for w in words)


def resolve_address(backend: Backend, street: str, zip5: str, *, index: str = INDEXES["properties"],
                    limit: int = 25) -> list[dict]:
    """The properties at a street line in a ZIP, best match first. Each is
    {"dataflik_id", "property_address", "property_city", "property_zip", "exact"}; `exact` is True
    when the whole street matches after normalizing (Road is Rd, North is N). Searched by house
    number inside the ZIP, then compared here, so a typed suffix never misses the house."""
    z = re.sub(r"\D", "", str(zip5 or ""))[:5]
    m = re.match(r"\s*(\d+[A-Za-z]?)\s+(.+)", street or "")
    if len(z) != 5:
        raise ValueError("a 5-digit ZIP is needed to find the house")
    if not m:
        raise ValueError("the street line must start with the house number")
    number = m.group(1)
    hits, _ = backend.search(index, {"bool": {"filter": [{"term": {"property_zip": z}},
                                                          {"prefix": {"property_address": number + " "}}]}},
                             sort=[{"dataflik_id": {"order": "asc"}}],
                             fields=["dataflik_id", "property_address", "property_city", "property_zip"],
                             limit=limit)
    want = street_key(street)
    want_words = set(want.split()[1:])
    out = []
    for h in hits:
        s = h.get("_source") or {}
        key = street_key(s.get("property_address") or "")
        words = set(key.split()[1:])
        overlap = len(want_words & words) / max(1, len(want_words | words))
        out.append({"dataflik_id": s.get("dataflik_id"), "property_address": s.get("property_address"),
                    "property_city": s.get("property_city"), "property_zip": s.get("property_zip"),
                    "exact": key == want, "overlap": round(overlap, 2)})
    out.sort(key=lambda c: (not c["exact"], -c["overlap"], str(c["property_address"])))
    return [c for c in out if c["exact"] or c["overlap"] >= 0.5]


# ------------------------------------------------------------------------------- the search

def find_buyers(dataflik_id, backend: Backend, params: Params | None = None, *, indexes: dict | None = None,
                compat: bool = False, today: _dt.date | None = None) -> dict:
    """The ranked buyers for one property. Never raises for an ordinary miss: `status` says what
    happened (200 found or nothing to score, 400 bad input or no such property, 500 storage failed)."""
    p = params or Params()
    ix = dict(INDEXES, **(indexes or {}))
    today = today or _dt.date.today()
    lines: list[str] = []

    def note(msg: str, level: int = logging.INFO) -> None:
        lines.append("%s: %s" % (logging.getLevelName(level), msg))
        log.log(level if level >= logging.WARNING else logging.DEBUG, msg)   # progress rides in `log`, not the box's log

    out: dict = {"status": 200, "dataflik_id": None, "buyers": [], "subject": None, "counts": {},
                 "params": {k: v for k, v in asdict(p).items() if k != "sales_filters"},
                 "indexes": ix, "compat": bool(compat), "version": VERSION, "log": lines}

    try:
        did = int(str(dataflik_id).strip())
    except (TypeError, ValueError):
        note("dataflik_id %r is not a number" % (dataflik_id,), logging.ERROR)
        out.update(status=400, error="dataflik_id must be a number")
        return out
    out["dataflik_id"] = did
    note("Received dataflik_id=%s and search parameters." % did)

    # 1. the property
    try:
        doc = backend.get(ix["properties"], did, SUBJECT_FIELDS)
    except Exception as exc:  # noqa: BLE001
        note("Failed to fetch property details from index '%s': %s" % (ix["properties"], exc), logging.ERROR)
        out.update(status=500, error="the property index could not be read")
        return out
    if not doc:
        note("No property with dataflik_id %s in '%s'." % (did, ix["properties"]), logging.ERROR)
        out.update(status=400, error="no property with dataflik_id %s" % did)
        return out
    out["subject"] = subject_summary(doc)
    note("Retrieved property details: %s, %s %s (%s, fips %s, %s)." % (
        doc.get("property_address"), doc.get("property_city"), doc.get("property_zip"), doc.get("county"),
        doc.get("fips"), doc.get("property_type_group")), logging.DEBUG)

    # 2. its embedding
    emb, emb_source = None, None
    try:
        e = backend.get(ix["properties_embed"], did, ["dataflik_id", "emb"])
        emb = (e or {}).get("emb") or None
        if emb:
            emb_source = "property"
            note("Retrieved property embedding (size=%d)." % len(emb))
        else:
            note("Embedding field 'emb' missing for the property.", logging.WARNING)
    except Exception as exc:  # noqa: BLE001
        note("Failed to fetch the property embedding: %s" % exc, logging.WARNING)
    out["subject"]["embedding"] = emb_source

    # 3. similar sold properties
    filters = list(p.sales_filters)
    filters = add_filter_for_bool(p.is_investor_sale, "is_investor_sale", filters)
    filters = add_filter_for_bool(p.is_buyer_corporation, "is_buyer_corporation", filters)
    filters = add_filter_sales_period(p.period_days, filters, compat)
    filters = add_filter_radius_around_property(doc, p.radius_miles, filters)
    filters = add_filters_for_similar_properties(doc, filters, 2025 if compat else today.year)
    out["filters"] = filters
    sale_limit = MAX_SIZE_SEARCH if compat else p.max_sales
    try:
        sales_hits, sales_total = backend.search(
            ix["sales"], {"bool": {"filter": filters}},
            sort=[{"sale_date": {"order": "desc"}}, {"transaction_id": {"order": "asc"}}],
            fields=SALE_FIELDS, limit=sale_limit)
    except Exception as exc:  # noqa: BLE001
        note("Failed to search the sales index (%s): %s" % (ix["sales"], exc), logging.ERROR)
        out.update(status=500, error="the sales index could not be searched")
        return out
    sales = [h.get("_source") or {} for h in sales_hits]
    counts = out["counts"]
    counts.update(sales=len(sales), sales_total=sales_total, sales_truncated=sales_total > len(sales))
    days = sorted(d for d in (_day(s.get("sale_date")) for s in sales) if d)
    if days:   # what the search actually covered: the index holds a fixed window, not the period asked for
        counts.update(sale_date_first=days[0], sale_date_last=days[-1])
    if counts["sales_truncated"]:
        note("Only the newest %d of %d matching sales were read." % (len(sales), sales_total), logging.WARNING)
    if not sales:
        note("No relevant sales found, so no buyers to score.", logging.WARNING)
        return out
    note("Found %d relevant sales." % len(sales))

    # - the sold houses' average embedding when the property has none
    if emb is None:
        ids = sorted({s.get("dataflik_id") for s in sales if s.get("dataflik_id")})
        try:
            ehits, _ = backend.search(ix["properties_embed"], {"bool": {"filter": {"terms": {"dataflik_id": ids}}}},
                                      sort=[{"dataflik_id": {"order": "asc"}}], fields=["dataflik_id", "emb"],
                                      limit=MAX_SIZE_SEARCH if compat else max(1, len(ids)))
            vecs = [h["_source"]["emb"] for h in ehits if (h.get("_source") or {}).get("emb") is not None]
        except Exception as exc:  # noqa: BLE001
            vecs = []
            note("Sold-house embeddings could not be read: %s" % exc, logging.WARNING)
        if vecs:
            n, dim = len(vecs), len(vecs[0])
            acc = [0.0] * dim
            for v in vecs:
                for i in range(dim):
                    acc[i] += v[i]
            emb = [a / n for a in acc]
            emb_source = "sales average"
            out["subject"]["embedding"] = emb_source
            note("Will use average embedding of properties from sales as property embedding.")

    # 4 and 5. unique buyers, purchases in the radius, and each buyer's details off their newest sale
    counts_by: dict[str, int] = {}
    details: dict[str, dict] = {}
    for s in sales:
        b = s.get("buyer_id")
        if b:
            counts_by[b] = counts_by.get(b, 0) + 1
            if b not in details:
                details[b] = {k: s.get(k) for k in DETAIL_FIELDS}
    buyer_ids = list(counts_by)
    counts["buyers"] = len(buyer_ids)
    if not buyer_ids:
        note("Sales found, but no 'buyer_id' fields present in the sales.", logging.WARNING)
        return out
    note("Identified %d unique buyers." % len(buyer_ids))
    rows = {b: {"buyer_id": b, "purchases_radius": n, "profile_size": None, "buyer_size": None,
                "similarity": None, **{c: None for c in STAT_COMPONENTS}} for b, n in counts_by.items()}

    # 6. similarity to each buyer's best-matching profile
    hits: list[dict] = []
    if emb:
        try:
            hits, _ = backend.search(
                ix["buyers_embed"],
                {"script_score": {
                    "query": {"bool": {"filter": [{"terms": {"buyer_id": buyer_ids}}, {"exists": {"field": "emb"}}]}},
                    "script": {"source": "knn_score", "lang": "knn",
                               "params": {"field": "emb", "query_value": emb, "space_type": "cosinesimil"}}}},
                sort=[{"_score": {"order": "desc"}}, {"buyer_profile_id": {"order": "asc"}}],
                fields={"excludes": ["emb"]}, limit=MAX_SIZE_SEARCH if compat else p.max_profiles, scored=True)
        except Exception as exc:  # noqa: BLE001
            note("Failed to calculate similarity to buyers profiles: %s" % exc, logging.WARNING)
    else:
        note("Skip calculation of similarity to buyers as property embedding is missing.", logging.WARNING)
    best: dict[str, tuple] = {}
    for h in hits:
        src = h.get("_source") or {}
        b = src.get("buyer_id")
        if not b:
            continue
        ps = src.get("profile_size", 1)
        ps = math.nan if ps is None else float(ps)
        prop = src.get("profile_prop", 1.0)
        # FIX 8: a 0 or null proportion, or a null size, raised in the original
        bsize = None if (math.isnan(ps) or not prop) else int(ps / float(prop))
        sim = h.get("_score")
        sim = math.nan if sim is None else float(sim)
        adj_key = sim * math.sqrt(min(1.0, ps / SCALE_MIN_MAX["profile_size"][0])) if not math.isnan(ps) else math.nan
        key = -math.inf if math.isnan(adj_key) else adj_key
        if b not in best or key > best[b][0]:
            best[b] = (key, None if math.isnan(ps) else ps, bsize, None if math.isnan(sim) else sim)
    for b, (_, ps, bsize, sim) in best.items():
        if b in rows:
            rows[b].update(profile_size=ps, buyer_size=bsize, similarity=sim)
    ids = set(buyer_ids)
    counts["profiles_found"] = sum(1 for b in best if b in ids)
    note("Retrieved buyer profile stats and similarity scores for %d buyers (%.1f%% coverage)." % (
        counts["profiles_found"], 100.0 * counts["profiles_found"] / len(buyer_ids)))

    # 7. buyer stats
    ptype = doc.get("property_type_group")
    base = [{"term": {"pattern": "ALL"}}]
    if ptype:
        base.append({"terms": {"property_type_group": [ptype]}})
    chosen: dict[str, dict] = {}
    try:
        if compat:
            stat_hits, _ = backend.search(
                ix["buyers_stats"], {"bool": {"filter": base + [{"terms": {"buyer_id": buyer_ids}}]}},
                sort=[{"buyer_id": {"order": "asc"}}, {"fips": {"order": "asc"}}], fields=STATS_FIELDS,
                limit=MAX_SIZE_SEARCH)
            for h in stat_hits:   # the original: the row with the highest national total, ties as they fall
                s = h.get("_source") or {}
                b = s.get("buyer_id")
                t = _num(s.get("total_investor_purchases"))
                cur = chosen.get(b)
                if cur is None or (t is not None and (_num(cur.get("total_investor_purchases")) is None
                                                      or t > _num(cur.get("total_investor_purchases")))):
                    chosen[b] = s
        else:
            # FIX 2: the row for the subject's county; else the nearby county (one where this buyer bought
            # inside the radius) with the most of their purchases. A national buyer has one row per county
            # (one national lender has 747), so only these counties are read.
            subject_fips = doc.get("fips")
            near = sorted({f for f in [subject_fips] + [s.get("fips") for s in sales] if f})
            stat_rank = {"subject": 0, "nearby": 0, "elsewhere": 0}
            if near:
                stat_hits, stat_total = backend.search(
                    ix["buyers_stats"],
                    {"bool": {"filter": base + [{"terms": {"fips": near}}, {"terms": {"buyer_id": buyer_ids}}]}},
                    sort=[{"buyer_id": {"order": "asc"}}, {"local_group_investor_purchases": {"order": "desc"}},
                          {"fips": {"order": "asc"}}],
                    fields=STATS_FIELDS, limit=p.max_stats_rows)
                if stat_total > len(stat_hits):
                    counts["stats_truncated"] = True
                    note("Only %d of %d stats rows were read." % (len(stat_hits), stat_total), logging.WARNING)
                for h in stat_hits:   # sorted busiest county first within each buyer
                    s = h.get("_source") or {}
                    b = s.get("buyer_id")
                    if s.get("fips") == subject_fips:
                        chosen[b] = s
                    else:
                        chosen.setdefault(b, s)
                for s in chosen.values():
                    stat_rank["subject" if s.get("fips") == subject_fips else "nearby"] += 1
            rest = [b for b in buyer_ids if b not in chosen]
            if rest:   # no row in any nearby county: their busiest county anywhere
                stat_hits, stat_total = backend.search(
                    ix["buyers_stats"], {"bool": {"filter": base + [{"terms": {"buyer_id": rest}}]}},
                    sort=[{"buyer_id": {"order": "asc"}}, {"local_group_investor_purchases": {"order": "desc"}},
                          {"fips": {"order": "asc"}}],
                    fields=STATS_FIELDS, limit=min(p.max_stats_rows, 2000))
                for h in stat_hits:
                    s = h.get("_source") or {}
                    if s.get("buyer_id") not in chosen:
                        chosen[s.get("buyer_id")] = s
                        stat_rank["elsewhere"] += 1
            counts["stats_from"] = stat_rank
    except Exception as exc:  # noqa: BLE001
        note("Buyer statistics could not be read: %s" % exc, logging.WARNING)
    for b, s in chosen.items():
        if b in rows:
            for c in STAT_COMPONENTS:
                rows[b][c] = _num(s.get(c))
            rows[b]["stats_fips"] = s.get("fips")
            rows[b]["stats_county"] = s.get("county_name")
            rows[b]["local_group_investor_purchases"] = _num(s.get("local_group_investor_purchases"))
            rows[b]["last_activity_date"] = s.get("last_activity_date")
    counts["stats_found"] = sum(1 for b in chosen if b in ids)
    note("Retrieved statistics for %d buyers (%.1f%% coverage)." % (
        counts["stats_found"], 100.0 * counts["stats_found"] / len(buyer_ids)))

    # 8 and 9. the composite score
    ordered = [rows[b] for b in buyer_ids]
    scores = score_buyers(ordered, p.weights)
    for r, sc in zip(ordered, scores):
        r["score"] = sc
    note("Scores calculated (min=%s | max=%s)." % (min(scores), max(scores)))

    # 10. the response, in the original's shape: details, buyer_id, score, then each weighted component raw
    keep = [r for r in ordered if r["score"] > p.min_score]
    keep.sort(key=lambda r: (-r["score"], r["buyer_id"]))
    keep = keep[:p.limit]
    cols = ["buyer_id", "score"] + [k for k in p.weights if k in COMPONENTS]
    buyers = []
    for r in keep:
        rec = {**details.get(r["buyer_id"], {}), **{c: _as_int_if_whole(r.get(c)) for c in cols}}
        rec["stats_county"] = r.get("stats_county")
        rec["local_purchases_in_county"] = _as_int_if_whole(r.get("local_group_investor_purchases"))
        rec["last_activity_date"] = r.get("last_activity_date")
        buyers.append(rec)
    counts.update(scored=len(ordered), above_min=sum(1 for r in ordered if r["score"] > p.min_score),
                  returned=len(buyers))
    out["buyers"] = buyers
    note("Returning %d buyers." % len(buyers))
    return out
