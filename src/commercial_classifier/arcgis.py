"""Download any ArcGIS FeatureServer or MapServer layer to a CSV.

Most counties publish their parcels as a public ArcGIS layer, so this is the
"I have no data" path: find the county's parcel layer URL, probe it, pull it.

  cc probe-layer <layer url>              fields, aliases, row count, paging
  cc fetch-parcels <layer url> --out parcels.csv

Pages with resultOffset when the server supports it, and falls back to
fetching object ids in chunks when it does not. Coordinates are requested in
latitude/longitude (outSR=4326). Polygons are written as WKT, plus a centroid,
so the evidence pass can test business points against the real parcel shape.
Resumable: a stopped pull continues from its progress file. Field aliases go
to <out>.fields.json, which ingest uses as extra header names.
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import geo, profiles, schema

UA = {"User-Agent": "siftstack-commercial-classifier/1.0"}


class LayerError(RuntimeError):
    pass


def layer_url(url):
    u = url.strip().split("?")[0].rstrip("/")
    if u.lower().endswith("/query"):
        u = u[:-6]
    if not re.search(r"/(FeatureServer|MapServer)/\d+$", u, re.I):
        raise LayerError("expected a layer URL ending in /FeatureServer/<n> or /MapServer/<n>, got %s" % url)
    return u


def _call(url, params, timeout=120, tries=4):
    data = urllib.parse.urlencode(params)
    last = None
    for attempt in range(tries):
        try:
            if len(data) > 1800:
                req = urllib.request.Request(url, data=data.encode(), headers=dict(UA, **{
                    "Content-Type": "application/x-www-form-urlencoded"}))
            else:
                req = urllib.request.Request(url + "?" + data, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            if isinstance(body, dict) and body.get("error"):
                err = body["error"]
                raise LayerError("server error %s: %s" % (err.get("code"), err.get("message") or err.get("details")))
            return body
        except LayerError:
            raise
        except (urllib.error.URLError, TimeoutError, ValueError, ConnectionError) as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise LayerError("request failed after %d tries: %s" % (tries, last))


def probe(url):
    base = layer_url(url)
    meta = _call(base, {"f": "json"})
    fields = [(f["name"], f.get("alias") or f["name"], f.get("type", "")) for f in meta.get("fields") or []]
    try:
        count = _call(base + "/query", {"where": "1=1", "returnCountOnly": "true", "f": "json"}).get("count")
    except LayerError:
        count = None
    names = [n for n, _, _ in fields]
    aliases = {a: n for n, a, _ in fields if a != n}
    prof = "arcgis"
    mapping = profiles.build_mapping(prof, names, None, aliases)
    return {"url": base, "name": meta.get("name"), "geometry_type": meta.get("geometryType"),
            "max_record_count": meta.get("maxRecordCount") or 1000,
            "supports_pagination": bool((meta.get("advancedQueryCapabilities") or {}).get("supportsPagination")),
            "object_id_field": meta.get("objectIdField") or next(
                (n for n, _, t in fields if t == "esriFieldTypeOID"), "OBJECTID"),
            "count": count, "fields": fields,
            "suggested_mapping": {k: profiles.header_of(v) for k, v in mapping.items()},
            "date_fields": [n for n, _, t in fields if t == "esriFieldTypeDate"]}


def _geom_cols(feat_geom, mode):
    if not feat_geom:
        return None, None, ""
    if "x" in feat_geom and "y" in feat_geom:
        return feat_geom["y"], feat_geom["x"], ""
    rings = feat_geom.get("rings")
    if not rings:
        return None, None, ""
    polys = geo._esri_rings_to_polys(rings)
    if not polys:
        return None, None, ""
    cx, cy = geo.centroid(polys)
    return round(cy, 7), round(cx, 7), (geo.to_wkt(polys) if mode == "polygon" else "")


def fetch(url, out, where="1=1", fields="*", geometry="polygon", page=None, limit=None, log=print):
    """Pull a layer into a CSV. Returns stats. geometry: polygon | centroid | none."""
    info = probe(url)
    base = info["url"]
    skip = ("esriFieldTypeGeometry", "esriFieldTypeBlob", "esriFieldTypeRaster")
    names = ([n for n, _, t in info["fields"] if t not in skip and "(" not in n and not n.lower().startswith("shape")]
             if fields == "*" else [f.strip() for f in fields.split(",")])
    dates = set(info["date_fields"])
    page = int(page or min(info["max_record_count"], 2000))
    want_geom = geometry != "none" and info["geometry_type"] in ("esriGeometryPolygon", "esriGeometryPoint")
    header = list(names) + (["centroid_lat", "centroid_lon"] if want_geom else []) + \
        (["geometry_wkt"] if want_geom and geometry == "polygon" and info["geometry_type"] == "esriGeometryPolygon" else [])
    prog_path = out + ".progress.json"
    prog = {}
    if os.path.exists(prog_path) and os.path.exists(out):
        with open(prog_path, encoding="utf-8") as f:
            prog = json.load(f)
        if prog.get("url") != base or prog.get("header") != header or prog.get("where") != where:
            prog = {}
    mode = "a" if prog else "w"
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out + ".fields.json", "w", encoding="utf-8") as f:
        json.dump({"url": base, "name": info["name"], "fields": {n: a for n, a, _ in info["fields"]}}, f, indent=1)
    params = {"where": where, "outFields": ",".join(names), "f": "json", "outSR": "4326",
              "returnGeometry": "true" if want_geom else "false", "geometryPrecision": "7"}
    total = prog.get("rows", 0)
    with open(out, mode, newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if mode == "w":
            w.writerow(header)

        def write(feats):
            n = 0
            for ft in feats:
                a = ft.get("attributes") or {}
                row = []
                for nm in names:
                    v = a.get(nm)
                    if nm in dates and isinstance(v, (int, float)):
                        v = schema.parse_date(v)
                    row.append("" if v is None else v)
                if want_geom:
                    lat, lon, wkt = _geom_cols(ft.get("geometry"), geometry)
                    row += ["" if lat is None else lat, "" if lon is None else lon]
                    if len(header) > len(names) + 2:
                        row.append(wkt)
                w.writerow(row)
                n += 1
            return n

        def save(state):
            state.update({"url": base, "header": header, "where": where, "rows": total})
            with open(prog_path, "w", encoding="utf-8") as f:
                json.dump(state, f)

        if info["supports_pagination"]:
            offset = prog.get("offset", 0)
            while True:
                q = dict(params, resultOffset=offset, resultRecordCount=page, orderByFields=info["object_id_field"])
                body = _call(base + "/query", q)
                feats = body.get("features") or []
                total += write(feats)
                offset += len(feats)
                fh.flush()
                save({"offset": offset})
                log("  %d rows" % total)
                if not feats or (len(feats) < page and not body.get("exceededTransferLimit")):
                    break
                if limit and total >= limit:
                    break
                time.sleep(0.3)
        else:
            ids = prog.get("ids")
            if ids is None:
                ids = sorted(_call(base + "/query", {"where": where, "returnIdsOnly": "true", "f": "json"})
                             .get("objectIds") or [])
            i = prog.get("i", 0)
            while i < len(ids):
                chunk = ids[i:i + page]
                q = dict(params, objectIds=",".join(str(x) for x in chunk))
                q.pop("where", None)
                body = _call(base + "/query", q)
                total += write(body.get("features") or [])
                i += len(chunk)
                fh.flush()
                save({"ids": ids, "i": i})
                log("  %d of %d rows" % (total, len(ids)))
                if limit and total >= limit:
                    break
                time.sleep(0.3)
    if os.path.exists(prog_path):
        os.remove(prog_path)
    return {"rows": total, "out": out, "layer": info["name"], "count_reported": info["count"],
            "geometry": info["geometry_type"] if want_geom else "none", "fields": len(names)}
