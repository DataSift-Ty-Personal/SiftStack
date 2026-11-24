"""An in-memory stand-in for the OpenSearch subset the buyer search sends, and invented data for it.

Used by tests/test_buyer_search.py, the agents selftest and the equivalence harness. It answers both
interfaces: engine.Backend (get, search with sort and search_after) and the original Lambda's client
(get(index=, id=, _source=), search(body=, index=)), so the data team's unmodified code can run on it.

Query subset: bool (filter, must, should with minimum_should_match, must_not), term, terms, range (numbers
and now-Nd/d date math), geo_distance on WKT points, exists, ids, prefix, match_all, and script_score with
the k-NN plugin's cosinesimil score, which on the production cluster is (1 + cosine) / 2 (two live vectors
checked 2026-11-23: a local investor's profile scored 0.994165 at cosine 0.988330).

Every name, street, county and id here is invented.
"""
from __future__ import annotations

import copy
import functools
import math
import re

NOW_MS = 1795392000000          # 2026-11-23T00:00:00Z
DAY_MS = 86_400_000
_WKT = re.compile(r"POINT\s*\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)", re.I)


def _point(v):
    if isinstance(v, str):
        m = _WKT.match(v.strip())
        return (float(m.group(1)), float(m.group(2))) if m else None
    if isinstance(v, dict) and "lat" in v and "lon" in v:
        return float(v["lon"]), float(v["lat"])
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return float(v[0]), float(v[1])
    return None


def _miles(a, b) -> float:
    (lon1, lat1), (lon2, lat2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(h))


def _cos01(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return (1.0 + dot / (na * nb)) / 2.0


class FakeCluster:
    def __init__(self, docs: dict, now_ms: int = NOW_MS):
        self.docs = {ix: [dict(d) for d in rows] for ix, rows in docs.items()}
        self.now_ms = now_ms
        self.calls: list[tuple[str, dict]] = []

    # ------------------------------------------------------------------------ evaluation
    def _value(self, v):
        if isinstance(v, str):
            m = re.match(r"now-(\d+)d(/d)?$", v.strip())
            if m:
                ms = self.now_ms - int(m.group(1)) * DAY_MS
                return ms - (ms % DAY_MS) if m.group(2) else ms
        return v

    @staticmethod
    def _eq(doc_v, want) -> bool:
        if isinstance(doc_v, bool) or isinstance(want, bool):
            dv = str(doc_v).lower() if isinstance(doc_v, bool) else str(doc_v).lower()
            return dv == str(want).lower()
        if isinstance(doc_v, (int, float)) and not isinstance(want, bool):
            try:
                return float(doc_v) == float(want)
            except (TypeError, ValueError):
                return False
        return doc_v == want

    def _match(self, src: dict, _id: str, q: dict) -> tuple[bool, float]:
        if not q or "match_all" in q:
            return True, 1.0
        if "bool" in q:
            b = q["bool"]
            for key in ("filter", "must"):
                clauses = b.get(key) or []
                for c in (clauses if isinstance(clauses, list) else [clauses]):
                    if not self._match(src, _id, c)[0]:
                        return False, 0.0
            nots = b.get("must_not") or []
            for c in (nots if isinstance(nots, list) else [nots]):
                if self._match(src, _id, c)[0]:
                    return False, 0.0
            should = b.get("should") or []
            should = should if isinstance(should, list) else [should]
            if should:
                need = int(b.get("minimum_should_match", 1))
                if sum(1 for c in should if self._match(src, _id, c)[0]) < need:
                    return False, 0.0
            return True, 0.0
        if "term" in q:
            (field, want), = q["term"].items()
            want = want.get("value") if isinstance(want, dict) else want
            v = src.get(field)
            vals = v if isinstance(v, list) else [v]
            return any(self._eq(x, want) for x in vals if x is not None), 0.0
        if "terms" in q:
            (field, wants), = q["terms"].items()
            v = src.get(field)
            vals = v if isinstance(v, list) else [v]
            return any(self._eq(x, w) for x in vals if x is not None for w in wants), 0.0
        if "range" in q:
            (field, cond), = q["range"].items()
            v = src.get(field)
            if v is None:
                return False, 0.0
            for op, raw in cond.items():
                if op == "format":
                    continue
                w = self._value(raw)
                if op == "gte" and not v >= w or op == "gt" and not v > w or op == "lte" and not v <= w \
                        or op == "lt" and not v < w:
                    return False, 0.0
            return True, 0.0
        if "geo_distance" in q:
            g = dict(q["geo_distance"])
            dist = float(re.match(r"([\d.]+)", str(g.pop("distance"))).group(1))
            (field, origin), = g.items()
            a, b = _point(src.get(field)), _point(origin)
            return (a is not None and b is not None and _miles(a, b) <= dist), 0.0
        if "exists" in q:
            return src.get(q["exists"]["field"]) is not None, 0.0
        if "ids" in q:
            return str(_id) in {str(x) for x in q["ids"]["values"]}, 0.0
        if "prefix" in q:
            (field, want), = q["prefix"].items()
            want = want.get("value") if isinstance(want, dict) else want
            return str(src.get(field) or "").startswith(str(want)), 0.0
        if "script_score" in q:
            ss = q["script_score"]
            ok, _ = self._match(src, _id, ss.get("query") or {})
            if not ok:
                return False, 0.0
            p = ss["script"]["params"]
            return True, _cos01(src[p["field"]], p["query_value"])
        raise ValueError("FakeCluster does not understand %s" % list(q))

    @staticmethod
    def _project(src: dict, fields):
        if fields is None or fields is True:
            return copy.deepcopy(src)
        if isinstance(fields, dict):
            ex = set(fields.get("excludes") or [])
            inc = fields.get("includes")
            return {k: copy.deepcopy(v) for k, v in src.items() if k not in ex and (not inc or k in inc)}
        return {k: copy.deepcopy(src[k]) for k in fields if k in src}

    @staticmethod
    def _sortvals(hit: dict, sort: list) -> list:
        out = []
        for item in sort:
            (field, spec), = item.items()
            out.append(hit["_score"] if field == "_score" else hit["_source_full"].get(field))
        return out

    @staticmethod
    def _cmp(a: list, b: list, sort: list) -> int:
        for x, y, item in zip(a, b, sort):
            (_, spec), = item.items()
            order = spec.get("order", "asc") if isinstance(spec, dict) else spec
            if x == y:
                continue
            if x is None:
                return 1            # missing sorts last either way
            if y is None:
                return -1
            c = -1 if x < y else 1
            return c if order == "asc" else -c
        return 0

    def _run(self, index: str, body: dict) -> tuple[list[dict], int]:
        self.calls.append((index, copy.deepcopy(body)))
        rows = self.docs.get(index)
        if rows is None:
            raise KeyError("no such index: %s" % index)
        matched = []
        for d in rows:
            src = {k: v for k, v in d.items() if k != "_id"}
            ok, score = self._match(src, str(d["_id"]), body.get("query") or {})
            if ok:
                matched.append({"_index": index, "_id": str(d["_id"]), "_score": score, "_source_full": src})
        sort = body.get("sort") or []
        if sort:
            matched.sort(key=functools.cmp_to_key(
                lambda h1, h2: self._cmp(self._sortvals(h1, sort), self._sortvals(h2, sort), sort)))
            for h in matched:
                h["sort"] = self._sortvals(h, sort)
            after = body.get("search_after")
            if after is not None:
                matched = [h for h in matched if self._cmp(h["sort"], list(after), sort) > 0]
        total = len(matched)
        size = int(body.get("size", 10))
        out = []
        for h in matched[:size]:
            hit = {k: v for k, v in h.items() if k != "_source_full"}
            hit["_source"] = self._project(h["_source_full"], body.get("_source"))
            out.append(hit)
        return out, total

    # ------------------------------------------------------------------- engine.Backend
    def search(self, index: str, query: dict, *, sort: list, fields=None, limit: int = 100, scored: bool = False,
               page_size: int = 100) -> tuple[list[dict], int]:
        out, after, total, first = [], None, 0, True
        while len(out) < limit:
            size = min(page_size, limit - len(out))
            body = {"query": query, "size": size}
            if sort:
                body["sort"] = sort
            if fields is not None:
                body["_source"] = fields
            if after is not None:
                body["search_after"] = after
            hits, t = self._run(index, body)
            if first:
                total, first = t, False
            out.extend(hits)
            if len(hits) < size or not sort:
                break
            after = hits[-1]["sort"]
        return out, max(total, len(out))

    def get(self, index: str, doc_id, fields=None):
        hits, _ = self._run(index, {"query": {"ids": {"values": [str(doc_id)]}}, "size": 1,
                                    **({"_source": fields} if fields is not None else {})})
        return hits[0]["_source"] if hits else None

    def close(self) -> None:
        pass

    # ------------------------------------------------------ the original Lambda's client
    def original_client(self):
        cluster = self

        class Original:
            def get(self, index, id, _source=None):  # noqa: A002
                hits, _ = cluster._run(index, {"query": {"ids": {"values": [str(id)]}}, "size": 1,
                                               **({"_source": _source} if _source else {})})
                if not hits:
                    raise Exception("NotFoundError(404, 'document missing')")
                return hits[0]

            def search(self, body, index):
                hits, _ = cluster._run(index, dict(body))
                return {"hits": {"hits": hits}}

        return Original()


# ------------------------------------------------------------------------ invented data

def _e(*v):
    return [float(x) for x in v]


SUBJECT_ID = 1001
NO_EMBED_ID = 1002
FIPS, NEIGHBOR_FIPS = "47999", "47998"


def _sale(i, buyer, name, *, investor=True, corp=True, sale_days=100, last_days=3000, lon=-84.02, lat=36.02,
          value=210000, beds=3, sqft=1350, lot=9500, year=1978, ptype="single_family_residence", fips=FIPS,
          city="Sampleton", state="TN"):
    return {"_id": str(9000 + i), "transaction_id": 9000 + i, "dataflik_id": 2000 + i, "buyer_id": buyer,
            "buyer_full_name": name, "buyer_city": city, "buyer_state": state,
            "buyer_address": "%d Sample Ave" % (100 + i), "buyer_zip": "37999", "is_buyer_corporation": corp,
            "is_investor_sale": investor, "sale_date": NOW_MS - sale_days * DAY_MS,
            "last_sale_date": NOW_MS - last_days * DAY_MS, "location": "POINT (%s %s)" % (lon, lat),
            "property_value": value, "bedroom_count": beds, "property_sqft": sqft, "area_lot_sf": lot,
            "year_built": year, "property_type_group": ptype, "fips": fips, "sale_amount": int(value * 0.7),
            "property_address": "%d Example St" % (200 + i), "property_city": "Sampleton", "property_zip": "37999"}


def _stats(buyer, name, fips, total, loc, local, md, ppy, active, month, county="Sample"):
    row = {"buyer_id": buyer, "buyer_full_name": name, "fips": fips, "county_name": county, "pattern": "ALL",
           "property_type_group": "single_family_residence", "total_investor_purchases": total,
           "locality_percentage": loc, "local_group_investor_purchases": local, "purchases_per_year": ppy,
           "active_years": active, "month_active_perc": month, "last_activity_date": NOW_MS - 30 * DAY_MS}
    if md is not None:
        row["median_discount"] = md
    return row


def sample_docs() -> dict:
    """A house in an invented county and the market around it.

    For the period fix: s1, s5 and s7 had a previous sale inside two years (the only ones the original's
    last_sale_date filter keeps); every s1-s10 sale itself is inside two years. s11 is 60 miles away, s12's
    buyer is a person, s13 is a condo, s14 sold 900 days ago. b1 and b4 have two county rows with the same
    national total (the original's tie); b3's profile has profile_prop 0 (the original raised on it); b6 has
    neither stats nor a profile (the original's NaN)."""
    sales = [
        _sale(1, "b1", "EXAMPLE HOLDINGS LLC", last_days=300),
        _sale(2, "b1", "EXAMPLE HOLDINGS LLC", sale_days=200),
        _sale(3, "b1", "EXAMPLE HOLDINGS LLC", sale_days=150, fips=NEIGHBOR_FIPS),
        _sale(4, "b2", "SAMPLE HOMES LLC", sale_days=120),
        _sale(5, "b2", "SAMPLE HOMES LLC", sale_days=90, last_days=500),
        _sale(6, "b3", "TEST PROPERTIES LLC", sale_days=60),
        _sale(7, "b4", "DEMO MORTGAGE LLC", investor=False, sale_days=40, last_days=400, city="Example City",
              state="CA"),
        _sale(8, "b5", "CITY OF SAMPLETON", investor=False, sale_days=20),
        _sale(9, "b6", "PLACEHOLDER VENTURES LLC", sale_days=10),
        _sale(10, "b7", "WILLBANKS HOLDINGS LLC", sale_days=5),
        _sale(11, "b8", "FAR AWAY LLC", lon=-85.1, lat=36.0),
        _sale(12, "b9", "PERSON BUYER", corp=False, investor=True),
        _sale(13, "b1", "EXAMPLE HOLDINGS LLC", ptype="condominium"),
        _sale(14, "b3", "TEST PROPERTIES LLC", sale_days=900, last_days=1200),
    ]
    props = [
        {"_id": str(SUBJECT_ID), "dataflik_id": SUBJECT_ID, "property_address": "100 Example St",
         "property_city": "Sampleton", "property_state": "TN", "property_zip": "37999", "county": "Sample",
         "fips": FIPS, "property_type_group": "single_family_residence", "property_value": 200000,
         "bedroom_count": 3, "bath_count": 2, "property_sqft": 1400, "living_area_sqft": 1400,
         "area_lot_sf": 10000.0, "year_built": 1980, "location": "POINT (-84.0 36.0)"},
        {"_id": str(NO_EMBED_ID), "dataflik_id": NO_EMBED_ID, "property_address": "102 Example St",
         "property_city": "Sampleton", "property_state": "TN", "property_zip": "37999", "county": "Sample",
         "fips": FIPS, "property_type_group": "single_family_residence", "property_value": 205000,
         "bedroom_count": 3, "bath_count": 2, "property_sqft": 1380, "living_area_sqft": 1380,
         "area_lot_sf": 9800.0, "year_built": 1979, "location": "POINT (-84.01 36.01)"},
        {"_id": "1003", "dataflik_id": 1003, "property_address": "100 Sample Ave", "property_city": "Sampleton",
         "property_state": "TN", "property_zip": "37999", "county": "Sample", "fips": FIPS,
         "property_type_group": "single_family_residence", "property_value": 150000, "bedroom_count": 2,
         "property_sqft": 1000, "area_lot_sf": 8000.0, "year_built": 1960, "location": "POINT (-84.02 36.0)"},
    ]
    embeds = [{"_id": str(SUBJECT_ID), "dataflik_id": SUBJECT_ID, "state": "TN",
               "emb": _e(3.2, -0.5, 0.5, 0.1, 1.0, -0.1, 0.3, 0.6, -0.2, -0.4)}]
    embeds += [{"_id": str(2000 + i), "dataflik_id": 2000 + i, "state": "TN",
                "emb": _e(3.0 + i / 10, -0.4, 0.4, 0.2, 0.9, 0.0, 0.2, 0.5, -0.1, -0.3)} for i in range(1, 11)]
    profiles = [
        {"_id": "b1-0", "buyer_id": "b1", "buyer_profile_id": "b1-0", "profile_size": 12, "profile_prop": 0.6,
         "emb": _e(3.1, -0.5, 0.6, 0.3, 1.0, -0.2, 0.2, 0.2, -0.2, -0.4)},
        {"_id": "b1-1", "buyer_id": "b1", "buyer_profile_id": "b1-1", "profile_size": 2, "profile_prop": 0.1,
         "emb": _e(1.8, 1.9, 0.5, -0.1, 0.6, 0.1, 0.0, 0.1, -0.1, 0.1)},
        {"_id": "b2-0", "buyer_id": "b2", "buyer_profile_id": "b2-0", "profile_size": 5, "profile_prop": 0.5,
         "emb": _e(2.9, -0.2, 0.3, 0.1, 0.7, 0.1, 0.1, 0.3, 0.0, -0.1)},
        {"_id": "b3-0", "buyer_id": "b3", "buyer_profile_id": "b3-0", "profile_size": 3, "profile_prop": 0.0,
         "emb": _e(2.0, 0.5, 0.1, 0.0, 0.2, 0.0, 0.1, 0.0, 0.0, 0.0)},
        {"_id": "b4-0", "buyer_id": "b4", "buyer_profile_id": "b4-0", "profile_size": 50, "profile_prop": 0.2,
         "emb": _e(3.0, -0.3, 0.5, 0.0, 0.9, -0.1, 0.2, 0.4, -0.1, -0.3)},
        {"_id": "b7-0", "buyer_id": "b7", "buyer_profile_id": "b7-0", "profile_size": 4, "profile_prop": 1.0,
         "emb": _e(2.5, 1.0, 0.2, 0.1, 0.5, 0.0, 0.0, 0.1, 0.0, 0.0)},
    ]
    stats = [
        _stats("b1", "EXAMPLE HOLDINGS LLC", NEIGHBOR_FIPS, 20, 25.0, 5, 12.0, 6, 4, 60, county="Neighbor"),
        _stats("b1", "EXAMPLE HOLDINGS LLC", FIPS, 20, 75.0, 15, 12.0, 6, 4, 60),
        _stats("b2", "SAMPLE HOMES LLC", FIPS, 6, 100.0, 6, 30.0, 3, 2, 50),
        _stats("b3", "TEST PROPERTIES LLC", FIPS, 2, 100.0, 2, -5.0, 1, 1, 100),
        # 901 against 900: on the live index the national total is the same on every county row, and the
        # original's choice between tied rows follows pandas' sort internals; one tie (b1) is enough.
        _stats("b4", "DEMO MORTGAGE LLC", "47001", 901, 0.5, 4, 3.0, 150, 7, 95, county="Faraway"),
        _stats("b4", "DEMO MORTGAGE LLC", FIPS, 900, 0.4, 3, 3.0, 150, 7, 95),
        _stats("b5", "CITY OF SAMPLETON", FIPS, 4, 100.0, 4, None, 1, 3, 20),
        _stats("b7", "WILLBANKS HOLDINGS LLC", FIPS, 9, 90.0, 8, 18.0, 4, 3, 70),
        dict(_stats("b1", "EXAMPLE HOLDINGS LLC", FIPS, 20, 75.0, 15, 12.0, 6, 4, 60), pattern="FLIP"),
    ]
    for i, s in enumerate(stats):
        s["_id"] = "st%d" % i
    return {"properties": props, "properties-embeddings": embeds, "sales-backtesting": sales,
            "buyers-embeddings": profiles, "buyer-stats-by-county-property-type-pattern": stats}


def sample_cluster() -> FakeCluster:
    return FakeCluster(sample_docs())


# ------------------------------------------------------- SiftStack's own version: a fake SiftMap

class FakeSiftMap:
    """The two SiftMap calls open_api makes (search, get_detail), over invented rows and raw deeds.

    search() answers the investor sweep (filters carry the transaction label), the nearby-sales sweep (filters
    without one) and the map lookup (a polygon alone), paged the way deal_buyer_match._paged asks. It counts
    every deed read, so a test can see what was read and what was not."""

    K_TXN = "extra_last_sale_investor_transaction_type"

    def __init__(self, investor: dict, comps: list, parcels: list, details: dict):
        import threading
        self.investor, self.comps, self.parcels, self.details = investor, comps, parcels, details
        self.read: list[str] = []
        self.bodies: list[dict] = []
        self._lock = threading.Lock()

    def search(self, body: dict) -> dict:
        self.bodies.append(copy.deepcopy(body))
        filters = body.get("filters")
        if filters is None:
            rows = self.parcels
        elif self.K_TXN in filters:
            rows = [r for t in filters[self.K_TXN] for r in self.investor.get(t, [])]
        else:
            rows = self.comps
        size = body.get("size")
        start = int(body.get("result_index") or 0) if size else int(body.get("result_index") or 1) - 1
        size = int(size or 250)
        return {"total_results": len(rows), "data": [dict(r) for r in rows[start:start + size]]}

    def get_detail(self, pid) -> dict:
        with self._lock:
            self.read.append(str(pid))
        if str(pid) not in self.details:
            raise RuntimeError("no such property")
        return copy.deepcopy(self.details[str(pid)])


def _row(pid, address, lat, lon, *, beds=3, baths=1, sqft=1250, acres=0.24, value=210000, corp=True):
    return {"id": pid, "address": address, "latitude": lat, "longitude": lon, "bedrooms": beds, "bathrooms": baths,
            "squareFeet": sqft, "lotAcres": acres, "estimatedValue": value, "mlsSold": False, "corporateOwned": corp}


def _deed(owner, sales, *, year=1962, held=0, mail="123 Sample Ave, Sampleton, TN 37999", props=None):
    return {"owner_info": {"owner_name": owner, "owner_mail_address": mail, "total_properties": held,
                           "portfolio_value": held * 150000, "secondary_owner_names": [],
                           "owner_properties": props or []},
            "sale_history": [{"sale_date": d, "sale_price": p, "buyer_name": b, "seller_name": s, "is_cash_sale": c}
                             for d, p, b, s, c in sales],
            "years_built": year}


SIFTMAP_SUBJECT = {"dataflik_id": "5000", "address": "100 Example St, Sampleton, TN 37999", "lat": 36.0,
                   "lon": -84.0, "beds": 3, "baths": 1, "sqft": 1300, "lot_sf": 10890, "year_built": 1960,
                   "value": 200000, "record": ""}


def sample_siftmap() -> FakeSiftMap:
    """A house in an invented town and the investor market around it.

    p1, p2: EXAMPLE HOLDINGS LLC still holds both (a hold label), 11 houses in its portfolio (SiftMap's ceiling).
    p3: SAMPLE HOMES LLC holds it. p4: a $900,000 house, outside the value band, so it is never read.
    r1: DEMO MORTGAGE LLC, a lender (marked, never dropped). f1: a flip exit; the buyer is TEST FLIPS LLC, the
    last-sale seller, who bought it eight months earlier. c1: an unlabeled company buy right next door
    (PLACEHOLDER VENTURES LLC). c2: a person's retail purchase (not a buyer). y1: built 1999, outside the age band
    once its deed is read."""
    investor = {
        "pending": [_row("p1", "110 Example St, Sampleton, TN 37999", 36.004, -84.0),
                    _row("p2", "120 Example St, Sampleton, TN 37999", 36.010, -84.0, beds=4, sqft=1500, value=240000),
                    _row("p3", "130 Example St, Sampleton, TN 37999", 36.020, -84.0),
                    _row("p4", "140 Example St, Sampleton, TN 37999", 36.006, -84.0, value=900000)],
        "rental": [_row("r1", "150 Example St, Sampleton, TN 37999", 36.012, -84.0),
                   _row("y1", "160 Example St, Sampleton, TN 37999", 36.003, -84.0)],
        "flip": [_row("f1", "170 Example St, Sampleton, TN 37999", 36.015, -84.0, corp=False)],
        "wholesale": [], "wholetail": [],
    }
    comps = [_row("c1", "105 Example St, Sampleton, TN 37999", 36.001, -84.0),
             _row("c2", "107 Example St, Sampleton, TN 37999", 36.002, -84.0, corp=False)]
    parcels = [_row("5000", "100 Example St, Sampleton, TN 37999", 36.0005, -84.0, sqft=1300, value=200000,
                    corp=False),
               _row("5001", "102 Example St, Sampleton, TN 37999", 36.0006, -84.0, corp=False),
               _row("5002", "100 Sample Ave, Sampleton, TN 37999", 36.0007, -84.0, corp=False)]
    held = [{"bedrooms": 3, "bathrooms": 1, "estimatedValue": 190000, "lastSaleAmount": 120000,
             "squareFeet": 11000} for _ in range(11)]
    details = {
        "5000": _deed("OWNER OF RECORD", [("2005-06-01", 90000, "OWNER OF RECORD", "PREVIOUS OWNER", False)]),
        "p1": _deed("EXAMPLE HOLDINGS LLC", [("2026-03-10", 140000, "EXAMPLE HOLDINGS LLC", "A SELLER", True)],
                    held=11, props=held),
        "p2": _deed("EXAMPLE HOLDINGS LLC", [("2025-08-20", 150000, "EXAMPLE HOLDINGS LLC", "B SELLER", True)],
                    held=11, props=held),
        "p3": _deed("SAMPLE HOMES LLC", [("2025-11-02", 160000, "SAMPLE HOMES LLC", "C SELLER", False)], held=3),
        "p4": _deed("BIG HOUSE LLC", [("2025-10-01", 850000, "BIG HOUSE LLC", "D SELLER", False)], held=1),
        "r1": _deed("DEMO MORTGAGE LLC", [("2026-01-15", 120000, "DEMO MORTGAGE LLC", "E SELLER", False)], held=11),
        "y1": _deed("NEWER HOMES LLC", [("2026-02-01", 150000, "NEWER HOMES LLC", "F SELLER", True)], year=1999),
        "f1": _deed("A RETAIL FAMILY", [("2026-06-01", 245000, "A RETAIL FAMILY", "TEST FLIPS LLC", False),
                                        ("2025-10-01", 110000, "TEST FLIPS LLC", "G SELLER", True)]),
        "c1": _deed("PLACEHOLDER VENTURES LLC", [("2026-04-01", 130000, "PLACEHOLDER VENTURES LLC", "H SELLER", True)],
                    held=2),
        "c2": _deed("JANE SAMPLE", [("2026-05-01", 215000, "JANE SAMPLE", "I SELLER", False)]),
    }
    return FakeSiftMap(investor, comps, parcels, details)
