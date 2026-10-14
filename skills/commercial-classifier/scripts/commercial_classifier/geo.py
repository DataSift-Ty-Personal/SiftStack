"""Parcel polygons: parse, measure, and point-in-polygon.

Pure Python by default (WKT, GeoJSON or ArcGIS rings), shapely when it is
installed. Area math uses each polygon's own latitude, not a hard-coded one.

The `geometry` phase fills a centroid point for any parcel that has a polygon
but no lat/lon, records polygon acres, and flags a situs point that falls
outside its own parcel (a geocode-quality signal).
"""
from __future__ import annotations

import json
import math
import re

from . import store

M_PER_DEG = 111320.0
SQM_PER_ACRE = 4046.86


# ---- parsing ---------------------------------------------------------------
def _ring(text):
    pts = []
    for pair in text.split(","):
        nums = pair.split()
        if len(nums) >= 2:
            try:
                pts.append((float(nums[0]), float(nums[1])))
            except ValueError:
                return None
    return pts if len(pts) >= 3 else None


def parse_wkt(s):
    """WKT POLYGON / MULTIPOLYGON -> [polygon], polygon = [outer, hole, ...]."""
    s = s.strip()
    if s.upper().startswith("SRID="):
        s = s.split(";", 1)[-1].strip()
    m = re.match(r"^(MULTIPOLYGON|POLYGON)\s*(?:Z|M|ZM)?\s*\((.*)\)\s*$", s, re.I | re.S)
    if not m:
        return None
    kind, body = m.group(1).upper(), m.group(2).strip()
    chunks = re.split(r"\)\s*\)\s*,\s*\(\s*\(", body) if kind == "MULTIPOLYGON" else [body]
    polys = []
    for ch in chunks:
        ch = ch.strip().strip("()")
        rings = [_ring(r.strip().strip("()")) for r in re.split(r"\)\s*,\s*\(", ch)]
        rings = [r for r in rings if r]
        if rings:
            polys.append(rings)
    return polys or None


def _signed_area(r):
    a = 0.0
    for (x1, y1), (x2, y2) in zip(r, r[1:] + r[:1]):
        a += x1 * y2 - x2 * y1
    return a / 2.0


def _point_in_ring(x, y, r):
    inside = False
    j = len(r) - 1
    for i in range(len(r)):
        xi, yi = r[i]
        xj, yj = r[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi:
            inside = not inside
        j = i
    return inside


def _esri_rings_to_polys(rings):
    """ArcGIS rings: outer rings clockwise, holes counter-clockwise. Each hole
    goes to the first outer ring that contains its first vertex."""
    outers, holes = [], []
    for r in rings:
        pts = [(float(p[0]), float(p[1])) for p in r if len(p) >= 2]
        if len(pts) < 3:
            continue
        (outers if _signed_area(pts) < 0 else holes).append(pts)
    if not outers and holes:
        outers, holes = holes, []
    polys = [[o] for o in outers]
    for h in holes:
        for p in polys:
            if _point_in_ring(h[0][0], h[0][1], p[0]):
                p.append(h)
                break
    return polys or None


def parse(s):
    """WKT, GeoJSON geometry, or ArcGIS rings JSON -> polygons, else None."""
    if not s:
        return None
    s = str(s).strip()
    if s.startswith("{"):
        try:
            g = json.loads(s)
        except ValueError:
            return None
        if "rings" in g:
            return _esri_rings_to_polys(g["rings"])
        t = g.get("type")
        coords = g.get("coordinates") or []
        if t == "Polygon":
            return [[[(float(p[0]), float(p[1])) for p in r] for r in coords]] or None
        if t == "MultiPolygon":
            return [[[(float(p[0]), float(p[1])) for p in r] for r in poly] for poly in coords] or None
        return None
    return parse_wkt(s)


def to_wkt(polys):
    def ring(r):
        r = list(r)
        if r[0] != r[-1]:
            r = r + [r[0]]
        return "(" + ", ".join("%.7f %.7f" % p for p in r) + ")"
    if len(polys) == 1:
        return "POLYGON (" + ", ".join(ring(r) for r in polys[0]) + ")"
    return "MULTIPOLYGON (" + ", ".join("(" + ", ".join(ring(r) for r in p) + ")" for p in polys) + ")"


# ---- measures --------------------------------------------------------------
def area_deg2(polys):
    total = 0.0
    for p in polys:
        total += abs(_signed_area(p[0])) - sum(abs(_signed_area(h)) for h in p[1:])
    return max(total, 0.0)


def centroid(polys):
    cx = cy = a_sum = 0.0
    for p in polys:
        for i, r in enumerate(p):
            a = _signed_area(r)
            if a == 0:
                continue
            sx = sy = 0.0
            for (x1, y1), (x2, y2) in zip(r, r[1:] + r[:1]):
                cross = x1 * y2 - x2 * y1
                sx += (x1 + x2) * cross
                sy += (y1 + y2) * cross
            w = abs(a) if i == 0 else -abs(a)
            cx += (sx / (6 * a)) * w
            cy += (sy / (6 * a)) * w
            a_sum += w
    if a_sum <= 0:
        r = polys[0][0]
        return sum(x for x, _ in r) / len(r), sum(y for _, y in r) / len(r)
    return cx / a_sum, cy / a_sum


def acres(polys, lat=None):
    if lat is None:
        lat = centroid(polys)[1]
    return area_deg2(polys) * M_PER_DEG ** 2 * math.cos(math.radians(lat)) / SQM_PER_ACRE


def bbox(polys):
    xs = [x for p in polys for r in p for x, _ in r]
    ys = [y for p in polys for r in p for _, y in r]
    return min(xs), min(ys), max(xs), max(ys)


def contains(polys, x, y):
    for p in polys:
        if _point_in_ring(x, y, p[0]) and not any(_point_in_ring(x, y, h) for h in p[1:]):
            return True
    return False


def meters(lat1, lon1, lat2, lon2):
    dx = (lon2 - lon1) * M_PER_DEG * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * M_PER_DEG
    return math.hypot(dx, dy)


def looks_geographic(polys):
    x0, y0, x1, y1 = bbox(polys)
    return -180 <= x0 <= 180 and -90 <= y0 <= 90 and -180 <= x1 <= 180 and -90 <= y1 <= 90


# ---- spatial index -----------------------------------------------------------
class GridIndex:
    """Bounding boxes on a lat/lon grid. Good enough for a county or a state."""

    def __init__(self, cell=0.01):
        self.cell = cell
        self.cells = {}
        self.items = []

    def add(self, item, box):
        idx = len(self.items)
        self.items.append(item)
        x0, y0, x1, y1 = box
        c = self.cell
        ix0, ix1, iy0, iy1 = int(math.floor(x0 / c)), int(math.floor(x1 / c)), int(math.floor(y0 / c)), int(math.floor(y1 / c))
        if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) > 2500:
            ix1, iy1 = ix0, iy0  # absurdly large box: index by its corner only
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                self.cells.setdefault((ix, iy), []).append(idx)

    def near(self, x, y, radius_cells=0):
        c = self.cell
        ix, iy = int(math.floor(x / c)), int(math.floor(y / c))
        out = []
        for dx in range(-radius_cells, radius_cells + 1):
            for dy in range(-radius_cells, radius_cells + 1):
                out.extend(self.cells.get((ix + dx, iy + dy), ()))
        return out


def bbox_from_points(points, trim=0.005, pad=0.005):
    """Trimmed bounding box of (lat, lon) points: drops the 0.5% tails on each
    side so a stray 0,0 or swapped coordinate cannot blow the box up."""
    lats = sorted(p[0] for p in points)
    lons = sorted(p[1] for p in points)
    if not lats:
        return None
    k = int(len(lats) * trim)
    lo, hi = k, max(k, len(lats) - 1 - k)
    return (lats[lo] - pad, lons[lo] - pad, lats[hi] + pad, lons[hi] + pad)  # s, w, n, e


# ---- the geometry phase ------------------------------------------------------
def run(work):
    con = store.connect(work)
    store.require(con, "ingest")
    n_geom = con.execute("SELECT COUNT(*) FROM geom").fetchone()[0]
    store.create_table(con, "geo", [("rid", "TEXT"), ("poly_acres", "REAL"), ("centroid_lat", "REAL"),
                                    ("centroid_lon", "REAL"), ("situs_outside", "TEXT")])
    if not n_geom:
        stats = {"skipped": "no polygon column in this file; business points will match by distance only"}
        store.mark_done(con, "geometry", stats)
        con.close()
        return stats
    situs = {r["rid"]: (r["lat"], r["lon"]) for r in con.execute("SELECT rid, lat, lon FROM records")}
    out, bad, outside, filled, projected = [], 0, 0, 0, 0
    for rid, g in con.execute("SELECT rid, geometry FROM geom").fetchall():
        polys = parse(g)
        if not polys:
            bad += 1
            continue
        if not looks_geographic(polys):
            projected += 1
            continue
        cx, cy = centroid(polys)
        ac = acres(polys, cy)
        lat, lon = situs.get(rid, (None, None))
        flag = ""
        if lat is not None and lon is not None:
            if not contains(polys, lon, lat):
                flag = "Y"
                outside += 1
        else:
            filled += 1
        out.append((rid, round(ac, 4), round(cy, 7), round(cx, 7), flag))
    store.insert_many(con, "geo", ["rid", "poly_acres", "centroid_lat", "centroid_lon", "situs_outside"], out)
    con.execute("UPDATE records SET lat = (SELECT centroid_lat FROM geo WHERE geo.rid = records.rid), "
                "lon = (SELECT centroid_lon FROM geo WHERE geo.rid = records.rid) "
                "WHERE lat IS NULL AND rid IN (SELECT rid FROM geo)")
    con.commit()
    stats = {"polygons": n_geom, "valid": len(out), "unreadable": bad, "not_lat_lon": projected,
             "situs_point_outside_parcel": outside, "points_filled_from_centroid": filled}
    if projected:
        stats["note"] = ("%d polygons are not in latitude/longitude (a projected coordinate system); "
                         "re-download with outSR=4326 to use them" % projected)
    store.mark_done(con, "geometry", stats)
    con.close()
    return stats


def load_polygons(con):
    """rid -> polygons for every parseable geographic polygon."""
    out = {}
    for rid, g in con.execute("SELECT rid, geometry FROM geom"):
        polys = parse(g)
        if polys and looks_geographic(polys):
            out[rid] = polys
    return out

