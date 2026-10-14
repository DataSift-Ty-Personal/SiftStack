"""Business points for the evidence pass: who actually operates on a parcel.

Sources, best first:
  overture  Overture Maps Places, free, no key, read straight from public S3
            with duckdb (pip install duckdb). The newest release is discovered
            at run time, because Overture retires old releases and a pinned one
            stops resolving within months.
  osm       OpenStreetMap through Overpass, standard library only, slower.
  file      your own CSV with name, category, lat, lon (and optionally
            confidence), e.g. a business-license export.

The bounding box comes from the parcels themselves, trimmed so one stray
coordinate cannot widen it to half a continent.
"""
from __future__ import annotations

import csv
import json
import os
import time
import urllib.parse
import urllib.request

from . import geo, io_tables, store

OVERTURE_BUCKET = "s3://overturemaps-us-west-2/release"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
PLACE_COLS = ["pid", "name", "category", "hierarchy", "lat", "lon", "confidence", "source"]


class PlacesError(RuntimeError):
    pass


def parcel_bbox(con):
    pts = [(r[0], r[1]) for r in con.execute("SELECT lat, lon FROM records WHERE lat IS NOT NULL AND lon IS NOT NULL")]
    return geo.bbox_from_points(pts) if pts else None


def _duck():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2'; SET enable_progress_bar=false;")
    return con


def latest_release(con=None):
    con = con or _duck()
    rows = con.execute(
        "SELECT DISTINCT regexp_extract(file, 'release/([^/]+)/', 1) AS rel "
        "FROM glob('%s/*/theme=places/type=place/*') ORDER BY rel" % OVERTURE_BUCKET).fetchall()
    rels = [r[0] for r in rows if r[0]]
    if not rels:
        raise PlacesError("no Overture places release found on S3")
    return rels[-1]


def fetch_overture(bbox, release=None):
    if not io_tables.has_module("duckdb"):
        raise PlacesError("Overture needs duckdb (pip install duckdb); falling back to OpenStreetMap")
    s, w, n, e = bbox
    con = _duck()
    rel = release or latest_release(con)
    src = "read_parquet('%s/%s/theme=places/type=place/*', hive_partitioning=1)" % (OVERTURE_BUCKET, rel)
    cols = {r[0] for r in con.execute("DESCRIBE SELECT * FROM %s" % src).fetchall()}
    if "taxonomy" in cols:
        cat, hier = "taxonomy.primary", "array_to_string(taxonomy.hierarchy, '>')"
    elif "categories" in cols:
        cat, hier = "categories.primary", "''"
    else:
        cat, hier = "basic_category", "''"
    status = "AND coalesce(operating_status, 'open') <> 'permanently_closed'" if "operating_status" in cols else ""
    sql = ("SELECT names.primary AS name, coalesce(%s, %s) AS category, %s AS hierarchy, "
           "bbox.ymin AS lat, bbox.xmin AS lon, confidence FROM %s "
           "WHERE bbox.xmin BETWEEN %f AND %f AND bbox.ymin BETWEEN %f AND %f AND names.primary IS NOT NULL %s"
           % (cat, "basic_category" if "basic_category" in cols else "NULL", hier, src, w, e, s, n, status))
    rows = con.execute(sql).fetchall()
    return rel, [{"name": r[0], "category": r[1] or "", "hierarchy": r[2] or "", "lat": r[3], "lon": r[4],
                  "confidence": r[5], "source": "overture"} for r in rows]


def _osm_tags():
    with open(os.path.join(geo_data(), "poi_osm.csv"), encoding="utf-8") as f:
        return [r["tag"] for r in csv.DictReader(f)]


def geo_data():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _overpass(query):
    last = None
    for attempt in range(4):
        url = OVERPASS[attempt % len(OVERPASS)]
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": query}).encode(),
                                         headers={"User-Agent": "siftstack-commercial-classifier/1.0"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())
        except Exception as e:  # network errors, 429, 504
            last = e
            time.sleep(15 * (attempt + 1))
    raise PlacesError("Overpass failed after 4 tries: %s" % last)


def fetch_osm(bbox, cache_dir):
    s, w, n, e = bbox
    tags = _osm_tags()
    os.makedirs(cache_dir, exist_ok=True)
    out = []
    step = 0.5
    y = s
    while y < n:
        x = w
        while x < e:
            tb = (y, x, min(y + step, n), min(x + step, e))
            cache = os.path.join(cache_dir, "osm_%.3f_%.3f.json" % (tb[0], tb[1]))
            if os.path.exists(cache):
                with open(cache, encoding="utf-8") as f:
                    data = json.load(f)
            else:
                box = "%f,%f,%f,%f" % tb
                parts = "".join('node["%s"="%s"](%s);way["%s"="%s"](%s);' % (k, v, box, k, v, box)
                                for k, v in (t.split("=") for t in tags))
                data = _overpass("[out:json][timeout:120];(%s);out center tags;" % parts)
                with open(cache, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                time.sleep(2)
            for el in data.get("elements", []):
                t = el.get("tags", {})
                lat = el.get("lat") or (el.get("center") or {}).get("lat")
                lon = el.get("lon") or (el.get("center") or {}).get("lon")
                if lat is None:
                    continue
                for tag in tags:
                    k, v = tag.split("=")
                    if t.get(k) == v:
                        out.append({"name": t.get("name", ""), "category": tag, "hierarchy": "", "lat": lat,
                                    "lon": lon, "confidence": 0.6, "source": "osm"})
                        break
            x += step
        y += step
    return out


def load_file(path):
    rows = io_tables.read_csv_dicts(path)
    out = []
    for r in rows:
        low = {k.lower().strip(): v for k, v in r.items()}
        try:
            lat, lon = float(low.get("lat") or low.get("latitude")), float(low.get("lon") or low.get("longitude") or low.get("lng"))
        except (TypeError, ValueError):
            continue
        out.append({"name": low.get("name", ""), "category": (low.get("category") or "").strip().lower(),
                    "hierarchy": "", "lat": lat, "lon": lon,
                    "confidence": float(low.get("confidence") or 0.6), "source": "file"})
    return out


def run(work, source="auto", release=None, places_file=None):
    con = store.connect(work)
    store.require(con, "classify")
    store.create_table(con, "places", [(c, "INTEGER" if c == "pid" else "REAL" if c in ("lat", "lon", "confidence")
                                        else "TEXT") for c in PLACE_COLS], pk="pid")
    bbox = parcel_bbox(con)
    stats = {}
    rows = []
    if places_file:
        rows, stats["source"] = load_file(places_file), "file:" + os.path.basename(places_file)
    elif not bbox:
        stats["skipped"] = "no parcel has a latitude/longitude or a polygon, so there is nowhere to look"
    else:
        stats["bbox_swne"] = [round(v, 4) for v in bbox]
        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        if area > 25:
            stats["warning"] = "bounding box covers %.0f square degrees; the pull will be large" % area
        tried = []
        if source in ("auto", "overture"):
            try:
                rel, rows = fetch_overture(bbox, release)
                stats["source"], stats["release"] = "overture", rel
            except Exception as e:
                tried.append("overture: %s" % str(e)[:200])
                if source == "overture":
                    stats["error"] = tried[-1]
        if not rows and source in ("auto", "osm") and "error" not in stats:
            try:
                rows = fetch_osm(bbox, os.path.join(work, "places_cache"))
                stats["source"] = "osm"
            except Exception as e:
                tried.append("osm: %s" % str(e)[:200])
        if tried:
            stats["tried"] = tried
    store.insert_many(con, "places", PLACE_COLS,
                      ((i, r["name"], r["category"], r["hierarchy"], r["lat"], r["lon"], r["confidence"], r["source"])
                       for i, r in enumerate(rows)))
    stats["places"] = len(rows)
    if not rows and "skipped" not in stats:
        stats["skipped"] = "no business points fetched (%s)" % "; ".join(stats.get("tried", [])) if stats.get("tried") else "no business points in the area"
    store.mark_done(con, "places", stats)
    con.close()
    return stats
