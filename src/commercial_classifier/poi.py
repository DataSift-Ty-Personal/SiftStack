"""Evidence pass: match business points to parcels, then confirm, correct, or
queue for review.

  CONFIRM  a property-level business (a gas station, a hotel, a self-storage
           facility) agrees with our class: confidence floor 0.85.
  CORRECT  a property-level business disagrees, our label is weak (< 0.70)
           and not from a source table or a person, and the point sits INSIDE
           the parcel polygon: relabel, provenance 'imputed'.
  QUEUE    every other disagreement goes to the review queue with the
           business named. A source-table label is never auto-flipped.

Tenant-level businesses (a restaurant inside a mall) are listed as evidence and
never drive a decision. Without polygons, points match the nearest parcel within
40 m; that can confirm or queue, never correct, because point matching proved
fragile on the Knox build.
"""
from __future__ import annotations

import collections
import csv
import math
import os

from . import geo, io_tables, store, taxonomy

POI_COLS = ["rid", "businesses", "kind", "expect_class", "expect_sub", "evidence", "mode",
            "before_class", "before_sub", "before_conf"]
NEVER_FLIP = ("sourced", "verified", "learned-human")
_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def category_map():
    m = {}
    for name, key in (("poi_overture.csv", "category"), ("poi_osm.csv", "tag")):
        with open(os.path.join(_DATA, name), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                m[r[key]] = (r["asset_class"], r["sub_type"], r["strength"])
    return m


def expect(cmap, category, hierarchy=""):
    """(class, sub, strength) a business category implies, or None."""
    cat = (category or "").strip().lower()
    if cat in cmap:
        return cmap[cat]
    for level in reversed([h for h in (hierarchy or "").split(">") if h]):
        if level in cmap:
            return cmap[level]
    if "place_of_worship" in cat or "church" in cat:
        return ("Specialty", "Religious Facility", "hard")
    if "restaurant" in cat:
        return ("Restaurant", "", "soft")
    if "school" in cat:
        return ("Specialty", "Schools", "soft")
    return None


def _shapely_index(polys_by_rid):
    import shapely
    from shapely.geometry import MultiPolygon, Polygon
    rids, geoms = [], []
    for rid, polys in polys_by_rid.items():
        try:
            parts = [Polygon(p[0], p[1:]) for p in polys]
            g = parts[0] if len(parts) == 1 else MultiPolygon(parts)
            if not g.is_valid:
                g = g.buffer(0)
        except Exception:
            continue
        rids.append(rid)
        geoms.append(g)
    return rids, geoms, shapely.STRtree(geoms)


def match_polygons(places, polys_by_rid):
    """[(place_index, rid)] for every place inside a parcel polygon."""
    pairs = []
    if io_tables.has_module("shapely"):
        import shapely
        rids, _, tree = _shapely_index(polys_by_rid)
        pts = shapely.points([p["lon"] for p in places], [p["lat"] for p in places])
        pi, ti = tree.query(pts, predicate="within")
        return [(int(a), rids[int(b)]) for a, b in zip(pi, ti)]
    idx = geo.GridIndex(0.005)
    for rid, polys in polys_by_rid.items():
        idx.add((rid, polys), geo.bbox(polys))
    for i, p in enumerate(places):
        for j in idx.near(p["lon"], p["lat"]):
            rid, polys = idx.items[j]
            if geo.contains(polys, p["lon"], p["lat"]):
                pairs.append((i, rid))
    return pairs


def match_points(places, situs, radius_m=40.0):
    """[(place_index, rid)] for the nearest parcel point within radius_m."""
    cell = 0.001
    idx = geo.GridIndex(cell)
    for rid, (lat, lon) in situs.items():
        idx.add(rid, (lon, lat, lon, lat))
    rc = int(math.ceil(radius_m / (geo.M_PER_DEG * cell))) + 1
    pairs = []
    for i, p in enumerate(places):
        best, bd = None, radius_m
        for j in idx.near(p["lon"], p["lat"], rc):
            rid = idx.items[j]
            lat, lon = situs[rid]
            d = geo.meters(p["lat"], p["lon"], lat, lon)
            if d <= bd:
                best, bd = rid, d
        if best:
            pairs.append((i, best))
    return pairs


def run(work, radius_m=40.0):
    con = store.connect(work)
    store.require(con, "classify", "places")
    store.create_table(con, "poi", [(c, "REAL" if c == "before_conf" else "TEXT") for c in POI_COLS])
    places = [dict(r) for r in con.execute("SELECT * FROM places WHERE lat IS NOT NULL AND lon IS NOT NULL")]
    if not places:
        stats = {"skipped": store.stats_of(con, "places").get("skipped", "no business points")}
        store.mark_done(con, "poi", stats)
        con.close()
        return stats
    polys = geo.load_polygons(con)
    if polys:
        mode = "polygon"
        pairs = match_polygons(places, polys)
    else:
        mode = "point"
        situs = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT rid, lat, lon FROM records WHERE lat IS NOT NULL AND lon IS NOT NULL")}
        pairs = match_points(places, situs, radius_m)
    cmap = category_map()
    business = collections.defaultdict(list)
    hard = collections.defaultdict(list)
    for pi, rid in pairs:
        p = places[pi]
        if len(business[rid]) < 4:
            business[rid].append("%s (%s)" % (p["name"], p["category"]))
        exp = expect(cmap, p["category"], p.get("hierarchy"))
        if exp and exp[2] == "hard" and (p.get("confidence") or 0) >= 0.5:
            hard[rid].append((exp[0], exp[1], p["name"]))
    cls = {}
    for r in con.execute("SELECT rid, asset_class, sub_type, confidence, provenance, method FROM cls"):
        if r["rid"] in business:
            cls[r["rid"]] = dict(r)
    out, updates = [], []
    counts = collections.Counter()
    for rid, biz in business.items():
        c = cls.get(rid)
        if not c:
            continue
        kind = ""
        ac = st = ""
        evidence = ""
        if rid in hard:
            vote = collections.Counter((a, s) for a, s, _ in hard[rid])
            (ac, st), _ = vote.most_common(1)[0]
            evidence = " | ".join(n for a, s, n in hard[rid] if (a, s) == (ac, st))[:200]
            cur_ac, cur_st, cur_conf = c["asset_class"], c["sub_type"] or "", c["confidence"] or 0
            agree = cur_ac == ac and (not st or not cur_st or cur_st == st)
            if agree:
                if cur_conf < 0.85:
                    kind = "confirm"
                    updates.append((cur_ac, cur_st or st, 0.85, c["provenance"], c["method"] + "+poi-confirm",
                                    taxonomy.commercial_target(cur_ac, cur_st or st), rid))
            elif mode == "polygon" and cur_conf < 0.70 and c["provenance"] not in NEVER_FLIP:
                kind = "correct"
                ac, st = taxonomy.validate(ac, st)
                updates.append((ac, st, 0.8, "imputed", "tier3c-%s-correct" % places[0]["source"],
                                taxonomy.commercial_target(ac, st), rid))
            else:
                kind = "queue"
        counts[kind or "evidence-only"] += 1
        out.append((rid, " | ".join(biz), kind, ac, st, evidence, mode, c["asset_class"], c["sub_type"], c["confidence"]))
    store.insert_many(con, "poi", POI_COLS, out)
    if updates:
        con.executemany("UPDATE cls SET asset_class=?, sub_type=?, confidence=?, provenance=?, method=?, "
                        "commercial_target=? WHERE rid=?",
                        updates)
        con.commit()
    stats = {"mode": mode, "places": len(places), "place_parcel_matches": len(pairs),
             "parcels_with_business": len(business), "confirm": counts["confirm"],
             "correct": counts["correct"], "queue": counts["queue"]}
    store.mark_done(con, "poi", stats)
    con.close()
    return stats
